"""A small per-key cache for page data, so switching tabs never waits on ESI.

Measured before this existed: /planets made 54 ESI calls and /jobs 12 before
either page returned a single byte of HTML. On this machine that is a fraction of
a second; on a worse connection it is seconds, and it burns a character's
rate-limit budget on every visit - so someone who clicks around ends up throttled,
at which point the governor pauses ESI entirely and the app looks frozen.

The pattern is the one the dashboard already uses: serve what we have, say how
old it is, and refresh in the background. A page therefore has three states
rather than two:

  fresh   - inside the TTL, rendered from SQLite, no ESI at all
  stale   - rendered from SQLite immediately, refreshed behind the request so the
            next visit is current
  missing - nothing stored yet, so the fetch has to be waited for once

Payloads are JSON blobs keyed by (kind, key). Deliberately not one table per
data type: what these pages need is "the last answer for this character", and a
schema per page would be five migrations for the same idea.
"""
from __future__ import annotations

import json
import sqlite3
import time


def ensure_page_cache(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS page_cache (
            kind      TEXT NOT NULL,
            key       TEXT NOT NULL,
            payload   TEXT NOT NULL,
            cached_at REAL NOT NULL,
            PRIMARY KEY (kind, key)
        )""")
    conn.commit()


def get_cached(conn: sqlite3.Connection, kind: str, key) -> tuple[object, float] | None:
    """(payload, age_in_seconds), or None when nothing is stored.

    Age is returned rather than a fresh/stale verdict: the caller owns the TTL,
    and the page wants the number anyway so it can tell the user.
    """
    ensure_page_cache(conn)
    row = conn.execute(
        "SELECT payload, cached_at FROM page_cache WHERE kind=? AND key=?",
        (kind, str(key))).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row[0])
    except (ValueError, TypeError):
        return None
    return payload, max(0.0, time.time() - (row[1] or 0))


def put_cached(conn: sqlite3.Connection, kind: str, key, payload) -> None:
    ensure_page_cache(conn)
    conn.execute(
        "INSERT OR REPLACE INTO page_cache (kind, key, payload, cached_at)"
        " VALUES (?,?,?,?)", (kind, str(key), json.dumps(payload), time.time()))
    conn.commit()


def drop_cached(conn: sqlite3.Connection, kind: str, key=None) -> None:
    """Forget one key, or a whole kind. What an explicit Refresh does."""
    ensure_page_cache(conn)
    if key is None:
        conn.execute("DELETE FROM page_cache WHERE kind=?", (kind,))
    else:
        conn.execute("DELETE FROM page_cache WHERE kind=? AND key=?", (kind, str(key)))
    conn.commit()


def age_label(seconds: float | None) -> str:
    """Short, honest age for a page header. None means "no idea", not "now"."""
    if seconds is None:
        return ""
    if seconds < 45:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"
