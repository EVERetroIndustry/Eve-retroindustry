"""Net worth and ISK over time.

Two questions that look alike and have very different answers, which is why the
window says which is which.

**ISK across every character can be reconstructed backwards.** The wallet
journal is stored locally (it has been since the income tile), every entry
carries the balance after it, and where that balance is missing the curve is
walked back through the amounts from the nearest entry that has one. Measured on
a real account: 2694 entries reaching back 46 days, which is 16 days more than
ESI itself will hand out - it only serves the last 30.

**Net worth cannot be reconstructed at all.** It is the wallet plus the value of
what you own, and nothing has ever recorded what you owned last Tuesday - only
what you own now. `char_assets_cache` holds one row per character and is
overwritten on every sync. Historical prices exist; historical holdings do not.
So net worth is SAMPLED from here on, once an hour, off the figures the dashboard
computes anyway, and the chart says when the recording started rather than
drawing a line it cannot justify.
"""
from __future__ import annotations

import sqlite3
import time

# One sample per character per hour. Twelve characters is 288 rows a day, a few
# MB a year - small enough that thinning it would cost more thought than it saves.
_BUCKET = 3600.0

# A sample is carried forward to later hours the app was not running for, but not
# indefinitely: a balance from three weeks ago is not evidence about today.
_CARRY_MAX = 7 * 86400.0


def ensure_worth_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS net_worth_history (
            ts           REAL    NOT NULL,
            character_id INTEGER NOT NULL,
            wallet       REAL,
            asset_value  REAL,
            PRIMARY KEY (ts, character_id)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_net_worth_ts"
                 " ON net_worth_history(ts)")
    conn.commit()


def record_worth(conn: sqlite3.Connection, samples: list[tuple]) -> int:
    """Store one hourly sample per character. samples: (char_id, wallet, assets).

    REPLACE rather than IGNORE: within an hour the later reading is the better
    one, and the dashboard may compute several.
    """
    ensure_worth_tables(conn)
    bucket = (time.time() // _BUCKET) * _BUCKET
    rows = [(bucket, int(cid), w, a) for cid, w, a in samples
            if w is not None or a is not None]
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO net_worth_history (ts, character_id, wallet, asset_value)"
        " VALUES (?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def _grid(since: float, until: float, target: int = 400) -> list[float]:
    """Regular sampling times, coarse enough that a long window stays small."""
    span = max(until - since, _BUCKET)
    step = max(_BUCKET, span / target)
    step = (step // _BUCKET) * _BUCKET or _BUCKET
    n = int(span // step) + 1
    return [since + i * step for i in range(n + 1) if since + i * step <= until + step]


def worth_series(conn: sqlite3.Connection, days: float) -> dict:
    """Sampled net worth, summed over characters.

    Only hours that were actually sampled become points: inventing an hourly
    reading for a night the app was closed would draw a line nobody measured.
    A character missing from a sampled hour keeps its previous value, because
    the dashboard samples every character together and a gap means "not seen",
    not "worth nothing".
    """
    ensure_worth_tables(conn)
    until = time.time()
    since = until - days * 86400.0
    rows = conn.execute(
        "SELECT ts, character_id, wallet, asset_value FROM net_worth_history"
        " WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    first = conn.execute("SELECT MIN(ts) FROM net_worth_history").fetchone()[0]

    by_ts: dict[float, dict[int, tuple]] = {}
    for ts, cid, w, a in rows:
        by_ts.setdefault(ts, {})[cid] = (w, a)

    last: dict[int, tuple[float, float | None, float | None]] = {}
    points = []
    for ts in sorted(by_ts):
        for cid, (w, a) in by_ts[ts].items():
            last[cid] = (ts, w, a)
        wallet = assets = 0.0
        seen = 0
        for cid, (seen_ts, w, a) in last.items():
            if ts - seen_ts > _CARRY_MAX:
                continue
            seen += 1
            wallet += w or 0.0
            assets += a or 0.0
        if not seen:
            continue
        points.append({"t": int(ts), "wallet": wallet, "assets": assets,
                       "net": wallet + assets, "chars": seen})
    return {"points": points, "recording_since": int(first) if first else None}


def _character_balance_points(conn: sqlite3.Connection, char_id: int,
                              balance_now: float | None, since: float
                              ) -> tuple[list[tuple[float, float]], float | None]:
    """One character's balance over time, newest first, plus the balance in force
    before the oldest entry we hold.

    Walked BACKWARDS from the balance we have now: between two journal entries
    nothing moves, so the balance before an entry is the balance after the
    previous one. A stored `balance` from ESI overrides the walk wherever there
    is one, so a gap in the journal cannot silently skew everything older than
    it - the next anchor puts the curve back on the real number.
    """
    # journal_id breaks the tie, and it is not a detail: on a real account 275 of
    # one character's timestamps carry more than one entry (a market fill writes
    # several in the same second), and ordering by time alone put the walk in the
    # wrong order. Checked against ESI's own balance field for 725 entries:
    # 458 wrong that way, 0 wrong with the id as tie-breaker.
    rows = conn.execute(
        "SELECT date_ts, amount, balance FROM wallet_journal"
        " WHERE character_id = ? ORDER BY date_ts DESC, journal_id DESC",
        (char_id,)).fetchall()
    if balance_now is None:
        # Nothing to anchor to; an ESI balance on the newest entry will do.
        balance_now = next((b for _t, _a, b in rows if b is not None), None)
    if balance_now is None:
        return [], None

    pts: list[tuple[float, float]] = [(time.time(), balance_now)]
    running = balance_now
    for ts, amount, bal in rows:
        after = bal if bal is not None else running
        pts.append((ts, after))
        running = after - (amount or 0.0)
        if ts < since:
            break
    return pts, running


def isk_series(conn: sqlite3.Connection, days: float) -> dict:
    """ISK across every character, reconstructed from the stored journal."""
    # The balance column arrived after the journal table did, and this reads it
    # directly - so make sure the migration has run before selecting it.
    from app.character.income import ensure_income_tables
    ensure_income_tables(conn)
    until = time.time()
    since = until - days * 86400.0

    chars = [(r[0], r[1]) for r in conn.execute(
        "SELECT character_id, character_name FROM characters").fetchall()]
    balances = {r[0]: r[1] for r in conn.execute(
        "SELECT character_id, balance FROM char_wallet_cache").fetchall()}

    curves: dict[int, tuple[list[tuple[float, float]], float | None]] = {}
    no_journal: list[str] = []
    entries = 0
    oldest: float | None = None
    for cid, name in chars:
        n, lo = conn.execute(
            "SELECT COUNT(*), MIN(date_ts) FROM wallet_journal WHERE character_id=?",
            (cid,)).fetchone()
        entries += n or 0
        if lo is not None:
            oldest = lo if oldest is None else min(oldest, lo)
        if not n:
            no_journal.append(name)
        curves[cid] = _character_balance_points(conn, cid, balances.get(cid), since)

    grid = _grid(max(since, oldest or since), until)
    points = []
    for t in grid:
        total = 0.0
        known = 0
        for cid, (pts, before) in curves.items():
            if not pts:
                continue
            known += 1
            # pts is newest-first: the balance at t is the newest point at or
            # before t, and before the oldest entry the pre-entry balance holds.
            val = before
            for ts, bal in pts:
                if ts <= t:
                    val = bal
                    break
            total += val if val is not None else 0.0
        if known:
            points.append({"t": int(t), "isk": total})

    return {
        "points": points,
        "entries": entries,
        "oldest_entry": int(oldest) if oldest else None,
        "chars_without_journal": no_journal,
    }
