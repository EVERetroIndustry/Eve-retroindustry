"""Names for character, corporation, alliance and other ids: POST /universe/names, made safe.

The route answers for a whole batch or not at all: one id it refuses turns the
call into 404 {"error": "Ensure all IDs are valid before resolving."}. It also
refuses ids that are perfectly valid. Checked on 2026-11-26 against 15 378
issuers of public contracts: 2 of 16 batches failed, on 2113215724 "Andy Bach"
and 2115852767 "NotAlvin". Both are active characters: /characters/{id},
/characters/affiliation and /universe/ids all know them, and /universe/names
resolved the first one on 2026-10-20. Same answer under every compatibility date
and under /latest/. CCP has known about it since 2020 (esi/esi-issues #1181,
#1391, #1433).

Before this module, every caller dropped the whole batch on that 404, so one
such issuer turned every issuer on a Contracts page into a bare number. And the
failure looked exactly like "no name", which hid it.

What this does instead:
  1. names seen in the last week come from party_name_cache, with no request;
  2. the rest goes to ESI in batches of 1000;
  3. a refused batch is split in half until the refused ids are found (each 404
     costs one from the error limit, about 10 for one id among a thousand),
     and splitting stops while that budget is low;
  4. a refused id gets its name from its own route (/characters/{id} and so on),
     and is not sent to /universe/names again for a few hours;
  5. if ESI is down, an older cached name is better than a number.
"""
from __future__ import annotations

import sqlite3
import time

import httpx

ESI = "https://esi.evetech.net"
NAMES_URL = f"{ESI}/universe/names/"

_BATCH = 1000                       # the route's own limit
_FRESH = 7 * 24 * 3600              # names change rarely; a stale one is harmless here
_REFUSED_TTL = 6 * 3600
_KEEP_ERRORS = 30                   # stop splitting while fewer errors than this remain
_STRUCTURE_IDS = 1_000_000_000_000  # player structures: the route cannot name them

# ids /universe/names refused, and when (process memory, like static_data._missing)
_refused: dict[int, float] = {}


def _log(msg: str) -> None:
    print(f"[names] {msg}", flush=True)


def ensure_cache(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS party_name_cache (
        party_id  INTEGER PRIMARY KEY,
        name      TEXT,
        category  TEXT,
        cached_at REAL)""")


def _cached(conn: sqlite3.Connection, ids: list[int], newer_than: float
            ) -> dict[int, tuple[str, str | None]]:
    out: dict[int, tuple[str, str | None]] = {}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        ph = ",".join("?" * len(chunk))
        for pid, name, cat in conn.execute(
                f"SELECT party_id, name, category FROM party_name_cache"
                f" WHERE party_id IN ({ph}) AND name IS NOT NULL AND name != ''"
                f" AND COALESCE(cached_at, 0) >= ?", [*chunk, newer_than]):
            out[pid] = (name, cat)
    return out


def _remember(conn: sqlite3.Connection, found: dict[int, tuple[str, str | None]]) -> None:
    if not found:
        return
    now = time.time()
    conn.executemany(
        "INSERT OR REPLACE INTO party_name_cache (party_id, name, category, cached_at)"
        " VALUES (?,?,?,?)", [(pid, n, c, now) for pid, (n, c) in found.items()])
    conn.commit()


def _errors_left(r: httpx.Response) -> int | None:
    try:
        return int(getattr(r, "headers", {}).get("x-esi-error-limit-remain"))
    except (TypeError, ValueError):
        return None


class _Stop(Exception):
    """ESI failed for a reason other than a refused id: splitting will not help."""


async def _post(client, ids: list[int]) -> httpx.Response:
    try:
        r = await client.post(NAMES_URL, json=ids, headers={"Accept": "application/json"})
    except Exception as exc:                       # noqa: BLE001 - reported by the caller
        raise _Stop(f"{type(exc).__name__}: {exc}") from exc
    if r.status_code not in (200, 404):
        raise _Stop(f"HTTP {r.status_code}")
    return r


async def _resolve_batch(client, ids: list[int], found: dict, refused: list[int]) -> None:
    """Names for `ids` into `found`; the ids the route refuses into `refused`."""
    pending = [ids]
    while pending:
        part = pending.pop()
        r = await _post(client, part)
        if r.status_code == 200:
            for it in r.json():
                if it.get("name"):
                    found[it["id"]] = (it["name"], it.get("category"))
            continue
        if len(part) == 1:
            refused.append(part[0])
            continue
        left = _errors_left(r)
        if left is not None and left < _KEEP_ERRORS:
            _log(f"{len(part)} id(s) left unnamed: the error limit is down to {left}")
            continue
        half = len(part) // 2
        pending += [part[half:], part[:half]]


def _route_for(pid: int, category: str | None) -> tuple[str, str] | None:
    """The route that names one id, from its category or else CCP's id ranges.
    None for anything that is not a character, corporation or alliance:
    stations and systems have no ESI route of their own any more."""
    if category in ("character", "corporation", "alliance"):
        cat = category
    elif category is not None:
        return None
    elif 1_000_000 <= pid < 2_000_000 or 98_000_000 <= pid < 99_000_000:
        cat = "corporation"
    elif 99_000_000 <= pid < 100_000_000:
        cat = "alliance"
    elif 3_000_000 <= pid < 4_000_000 or pid >= 90_000_000:
        cat = "character"
    else:
        return None
    return f"{ESI}/{cat}s/{pid}/", cat


async def _one_by_one(client, pids: list[int], categories: dict[int, str | None]
                      ) -> dict[int, tuple[str, str | None]]:
    out: dict[int, tuple[str, str | None]] = {}
    for pid in pids:
        route = _route_for(pid, categories.get(pid))
        if route is None:
            _log(f"{pid}: refused by /universe/names, and nothing else names it")
            continue
        url, cat = route
        try:
            r = await client.get(url, headers={"Accept": "application/json"})
            name = r.json().get("name") if r.status_code == 200 else None
        except Exception as exc:                   # noqa: BLE001
            _log(f"{pid}: {url} failed: {type(exc).__name__}: {exc}")
            continue
        if name:
            out[pid] = (name, cat)
        else:
            _log(f"{pid}: refused by /universe/names and {url} answered {r.status_code}")
    return out


async def resolve_names(client, ids, conn: sqlite3.Connection | None = None,
                        categories: dict[int, str] | None = None) -> dict[int, str]:
    """id -> name for whatever can be named. `categories`, when given, is filled
    with what each id is ("character", "corporation", "alliance", "solar_system",
    "station", ...)."""
    want = sorted({int(i) for i in ids if i and 0 < int(i) < _STRUCTURE_IDS})
    found: dict[int, tuple[str, str | None]] = {}
    stale: dict[int, tuple[str, str | None]] = {}
    if want and conn is not None:
        try:
            ensure_cache(conn)
            found.update(_cached(conn, want, time.time() - _FRESH))
            stale = _cached(conn, [i for i in want if i not in found], 0)
        except sqlite3.Error as exc:
            _log(f"name cache unreadable: {exc}")

    now = time.monotonic()
    ask = [i for i in want if i not in found
           and now - _refused.get(i, -_REFUSED_TTL) >= _REFUSED_TTL]
    refused = [i for i in want if i not in found and i not in ask]
    fresh: dict[int, tuple[str, str | None]] = {}
    try:
        for b in range(0, len(ask), _BATCH):
            await _resolve_batch(client, ask[b:b + _BATCH], fresh, refused)
    except _Stop as exc:
        _log(f"/universe/names failed ({exc}); {len(want) - len(found) - len(fresh)}"
             f" id(s) fall back to older names")
    for pid in refused:
        _refused.setdefault(pid, now)
    if refused:
        cats = {pid: stale.get(pid, (None, None))[1] for pid in refused}
        fresh.update(await _one_by_one(client, refused, cats))

    if fresh and conn is not None:
        try:
            _remember(conn, fresh)
        except sqlite3.Error as exc:
            _log(f"name cache not written: {exc}")
    found.update(fresh)
    for pid, v in stale.items():
        found.setdefault(pid, v)

    if categories is not None:
        categories.update({pid: c for pid, (_n, c) in found.items() if c})
    return {pid: n for pid, (n, _c) in found.items()}
