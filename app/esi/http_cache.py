"""ESI's own HTTP caching, used the way CCP asks: Expires and ETag.

Every ESI answer says until when it will not change (Expires) and carries a
fingerprint of its body (ETag). Two things follow, and neither loses any data:

* Before Expires, ESI hands back the very same body from its own cache, so
  asking again only spends rate-limit tokens. The stored copy is used instead,
  with no request at all.
* After Expires, the request carries If-None-Match. When nothing changed ESI
  answers 304 with no body - 1 token instead of 2, and no download - and the
  stored copy is handed to the caller as the 200 it would have got.

A third thing is a refusal. A 403 for a missing role or scope (corporation
wallets without Accountant, planets without the PI scope, ...) costs 5 tokens
and one of the 100 errors a minute ESI allows the whole IP; asked every
15 minutes by a background job it is the commonest way to that limit. A
refusal is remembered for REFUSAL_TTL and forgotten at once when a character
logs in again or the user runs a Sync, the two moments roles and scopes change.

Callers see an ordinary httpx.Response either way, with the headers ESI sent
(X-Pages, Expires, Last-Modified), so nothing above the transport changes.

Kept in its own SQLite file next to the main one: it is a cache that can be
deleted at any time, and its writes happen on the event loop, so they must not
wait on the main database's locks.
"""
from __future__ import annotations

import base64
import email.utils
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import zlib
from typing import Optional

import httpx

REFUSAL_TTL = 60 * 60
STRUCTURE_REFUSAL_TTL = 6 * 60 * 60     # docking access changes rarely
KEEP_SECONDS = 7 * 24 * 3600            # entries unused this long are dropped

# Routes whose answers are kept. The signature has ids collapsed to {} and the
# version prefix removed. Left out on purpose:
#   * /markets/{}/history/ - has its own ETag store with only the days it needs
#     (19k types a region; whole bodies would be ~150 MB). A caller that wants
#     the whole series opts in per request (extensions={"esi_cache": True}).
#   * /contracts/public/items/{}/ - immutable, stored for good elsewhere.
#   * /universe/systems|stations|... - static, in the SDE or stored elsewhere.
#   * POST routes (names, ids) - not cacheable.
_CACHED = re.compile(
    r"^/(characters/\{\}/(?!search/)"
    r"|corporations/\{\}/"
    r"|alliances/\{\}/$"
    r"|markets/\{\}/orders/$"
    r"|markets/structures/\{\}/$"
    r"|markets/prices/$"
    r"|industry/"
    r"|sovereignty/"
    r"|contracts/public/\{\}/$"
    r"|universe/structures/"
    r"|universe/system_(jumps|kills)/$"
    r"|universe/(types|groups)/\{\}/$"
    r"|route/\{\}/\{\}/$"
    r"|incursions/$)")

# Routes where "not found" stays true: a type or group that is not there, a
# dissolved alliance, two systems with no route. Asked again a day later
# (new content arrives with a patch), not on every page load as before.
_NOT_FOUND = re.compile(r"^/(universe/(types|groups)/\{\}/$|alliances/\{\}/$|route/)")
NOT_FOUND_TTL = 24 * 3600

# Routes kept only for their "not found": a found route's jump count has its
# own store (route_jump_cache in main.py), which drops counts when the gate
# network changes. A body kept here as well could hand a count from before the
# change back to that store, which would then keep it for good.
_ONLY_NOT_FOUND = re.compile(r"^/route/")
_VERSION = re.compile(r"^/(latest|legacy|dev|v\d+)(?=/)")

# Headers that describe the transfer, not the content: the stored body is
# already decoded, so passing these on would make httpx decode it again.
_DROP = {"content-encoding", "content-length", "transfer-encoding", "connection",
         "keep-alive", "date", "x-esi-request-id"}


def signature(url: httpx.URL) -> str:
    path = _VERSION.sub("", url.path)
    return "/".join("{}" if seg.isdigit() else seg for seg in path.split("/"))


def wanted(request: httpx.Request) -> bool:
    if request.method != "GET":
        return False
    choice = request.extensions.get("esi_cache")
    if choice is not None:
        return bool(choice)
    sig = signature(request.url)
    if sig == "/markets/{}/orders/" and "type_id" not in request.url.params:
        # A whole region's book changes on every 5-minute snapshot (measured on
        # The Forge: five pages spread over 408, all five different after
        # 13 minutes), so a stored copy would never answer a 304 - it would only
        # take ~100 MB of disk. One type's orders often do stay the same.
        return False
    return bool(_CACHED.match(sig))


def _owner(request: httpx.Request) -> str:
    """Whose answer this is. A corporation route answers differently per
    character (roles), so the key includes who asked. The token is a JWT whose
    subject is the character; it is only read here, never stored."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return ""
    token = auth[7:].strip()
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        sub = json.loads(base64.urlsafe_b64decode(payload)).get("sub", "")
        return str(sub).rsplit(":", 1)[-1] or "?"
    except Exception:
        return "t" + hashlib.sha1(token.encode()).hexdigest()[:12]


def key_of(request: httpx.Request) -> str:
    return f"{request.url}|{_owner(request)}"


def _seconds_to_expiry(response: httpx.Response) -> Optional[float]:
    """How long the answer stays the same, by ESI's own clock (Expires minus
    Date), so a skewed local clock cannot stretch or shrink it."""
    try:
        exp = email.utils.parsedate_to_datetime(response.headers["expires"]).timestamp()
    except Exception:
        return None
    try:
        now = email.utils.parsedate_to_datetime(response.headers["date"]).timestamp()
    except Exception:
        now = time.time()
    return max(0.0, exp - now)


def _is_refusal(response: httpx.Response, body: bytes) -> bool:
    """A 403 that will come back the same until roles or scopes change. An
    expired or bad token also answers 403 on some routes; that one is not a
    refusal (the next token fixes it) and is never remembered."""
    if response.status_code != 403:
        return False
    text = body[:500].decode("utf-8", "replace").lower()
    return not any(w in text for w in ("expired", "invalid token", "token is invalid",
                                       "authorization not provided"))


class HttpCache:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self.stats = {"fresh": 0, "revalidated": 0, "changed": 0, "refusal_hit": 0}

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            # Wallet journals and assets end up in here: readable by the user
            # only, like the main database (SQLite gives -wal/-shm the same mode).
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            c = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("CREATE TABLE IF NOT EXISTS esi_http (key TEXT PRIMARY KEY,"
                      " status INTEGER, etag TEXT, headers TEXT, body BLOB,"
                      " fresh_until REAL, used_at REAL, owner TEXT)")
            c.execute("DELETE FROM esi_http WHERE used_at < ?", (time.time() - KEEP_SECONDS,))
            c.commit()
            self._conn = c
        return self._conn

    def get(self, key: str) -> Optional[dict]:
        with self._lock:
            row = self._db().execute(
                "SELECT status, etag, headers, body, fresh_until FROM esi_http WHERE key=?",
                (key,)).fetchone()
        if not row:
            return None
        try:
            body = zlib.decompress(row[3]) if row[3] else b""
            headers = json.loads(row[2] or "{}")
        except Exception:
            return None             # a damaged entry is a miss, never an error
        return {"status": row[0], "etag": row[1], "headers": headers, "body": body,
                "fresh_until": row[4] or 0.0}

    def put(self, key: str, owner: str, status: int, etag: Optional[str],
            headers: dict, body: bytes, fresh_for: float) -> None:
        now = time.time()
        with self._lock:
            c = self._db()
            c.execute("INSERT OR REPLACE INTO esi_http VALUES (?,?,?,?,?,?,?,?)",
                      (key, status, etag, json.dumps(headers), zlib.compress(body, 6),
                       now + fresh_for, now, owner))
            c.commit()

    def touch(self, key: str, headers: dict, fresh_for: float) -> None:
        now = time.time()
        with self._lock:
            c = self._db()
            c.execute("UPDATE esi_http SET headers=?, fresh_until=?, used_at=? WHERE key=?",
                      (json.dumps(headers), now + fresh_for, now, key))
            c.commit()

    def forget_refusals(self, character_id: Optional[int] = None) -> None:
        with self._lock:
            c = self._db()
            if character_id is None:
                c.execute("DELETE FROM esi_http WHERE status=403")
            else:
                c.execute("DELETE FROM esi_http WHERE status=403 AND owner=?",
                          (str(character_id),))
            c.commit()


_CACHE: Optional[HttpCache] = None
_CACHE_DIR: Optional[str] = None


def cache() -> Optional[HttpCache]:
    """The store under EVE_APP_DIR, or None when there is none (a bare test
    process or a script): then every request simply goes to ESI as before."""
    global _CACHE, _CACHE_DIR
    d = os.environ.get("EVE_APP_DIR")
    if not d or os.environ.get("EVE_NO_HTTP_CACHE"):
        return None
    if _CACHE is None or _CACHE_DIR != d:
        _CACHE = HttpCache(os.path.join(d, "esi_http_cache.db"))
        _CACHE_DIR = d
    return _CACHE


def forget_refusals(character_id: Optional[int] = None) -> None:
    c = cache()
    if c is not None:
        try:
            c.forget_refusals(character_id)
        except Exception:
            pass


def kept_headers(response: httpx.Response) -> dict:
    return {k: v for k, v in response.headers.items() if k.lower() not in _DROP}


def response_from(entry: dict, request: httpx.Request, how: str,
                  headers: Optional[dict] = None) -> httpx.Response:
    h = dict(headers if headers is not None else entry["headers"])
    h["x-app-cache"] = how
    return httpx.Response(entry["status"], headers=h, content=entry["body"], request=request)


async def answer(transport_send, request: httpx.Request) -> httpx.Response:
    """Send request through transport_send, using and filling the store."""
    store = cache()
    if store is None or not wanted(request):
        return await transport_send(request)
    key = key_of(request)
    owner = _owner(request)      # before the transport may add a market token
    sig = signature(request.url)
    only_not_found = bool(_ONLY_NOT_FOUND.match(sig))
    try:
        entry = store.get(key)
    except Exception:
        entry = None
    if only_not_found and entry is not None and entry["status"] != 404:
        entry = None             # kept by v0.11.43/44; no longer used
    now = time.time()
    if entry is not None and now < entry["fresh_until"]:
        store.stats["fresh" if entry["status"] == 200 else "refusal_hit"] += 1
        return response_from(entry, request, "fresh")

    conditional = (entry is not None and entry["status"] == 200 and entry["etag"]
                   and "if-none-match" not in request.headers)
    if conditional:
        request.headers["If-None-Match"] = entry["etag"]
    response = await transport_send(request)
    if conditional:
        del request.headers["If-None-Match"]

    status = response.status_code
    if status == 304 and conditional:
        await response.aread()
        await response.aclose()
        headers = dict(entry["headers"])
        headers.update(kept_headers(response))
        fresh = _seconds_to_expiry(response) or 0.0
        try:
            store.touch(key, headers, fresh)
        except Exception:
            pass
        store.stats["revalidated"] += 1
        return response_from(entry, request, "revalidated", headers)

    if status not in (200, 403, 404):
        return response
    body = await response.aread()
    await response.aclose()
    headers = kept_headers(response)
    fresh = _seconds_to_expiry(response)
    try:
        if status == 200 and not only_not_found and (response.headers.get("etag") or fresh):
            store.put(key, owner, 200, response.headers.get("etag"),
                      headers, body, fresh or 0.0)
            store.stats["changed"] += 1
        elif status == 404 and _NOT_FOUND.match(sig):
            store.put(key, owner, 404, None, headers, body, NOT_FOUND_TTL)
        elif status == 403 and _is_refusal(response, body):
            ttl = (STRUCTURE_REFUSAL_TTL if "/universe/structures/" in request.url.path
                   else REFUSAL_TTL)
            store.put(key, owner, 403, None, headers, body, ttl)
    except Exception:
        pass
    return httpx.Response(status, headers=headers, content=body, request=request)
