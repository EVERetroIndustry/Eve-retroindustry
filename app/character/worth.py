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


# ===========================================================================
# Estimating the past: what the pile CONTAINED, not just what it is worth now
# ===========================================================================
#
# Net worth before the recording started can be estimated, and the accuracy
# comes from two measured pieces rather than a guess:
#
#   1. What was held. Start from today's holdings and undo every movement ESI
#      records: market trades in kind (transactions), manufacturing and
#      reactions (a product becomes the materials it came from), and mining (ore
#      that came out of the ground was not in the pile before it).
#   2. What it was worth. Value that reconstructed basket at each day's own
#      market price, from ESI's daily history - which reaches 13 months, far
#      past anything else here.
#
# What ESI does not record, and therefore neither do we: loot and salvage, PI
# extraction, contracts, reprocessing, and ships lost. Those stay invisible, so
# the window says so rather than implying the line is exact.
#
# The estimate stops where the wallet journal stops. Net worth is wallet plus
# assets and the wallet half cannot be walked back any further, so a longer
# asset curve would be half a number.

import json
import math
from collections import Counter

JITA_REGION = 10000002

# Types to fetch daily prices for, biggest holding first. Measured on a real
# account: the top 200 types carry 87.9% of the value and the top 500 carry
# 97.5%, so this is far into diminishing returns already. Anything left out is
# carried at today's price, which the anchoring below makes harmless.
_INDEX_TYPES = 600

_INDEX_KIND = "worth_index"
_INDEX_TTL = 12 * 3600.0


def current_basket(conn: sqlite3.Connection) -> dict[int, int]:
    """Everything owned right now, by type. Blueprints are excluded for the same
    reason the dashboard excludes them from net worth: a BPO and its copy share
    a type_id and pricing them together is how they used to be valued wrongly."""
    qty: Counter = Counter()
    for table in ("char_assets_cache", "corp_assets_cache"):
        try:
            rows = conn.execute(f"SELECT data_json FROM {table}").fetchall()
        except sqlite3.OperationalError:
            continue
        for (blob,) in rows:
            try:
                for a in json.loads(blob):
                    if a.get("type_id"):
                        qty[int(a["type_id"])] += int(a.get("quantity") or 1)
            except (ValueError, TypeError):
                continue
    bp_groups = {r[0] for r in conn.execute(
        "SELECT group_id FROM sde_groups WHERE name LIKE '%Blueprint%'")}
    if bp_groups:
        ph = ",".join("?" * len(bp_groups))
        bp_types = {r[0] for r in conn.execute(
            f"SELECT type_id FROM sde_types WHERE group_id IN ({ph})", list(bp_groups))}
        for t in list(qty):
            if t in bp_types:
                del qty[t]
    return dict(qty)


def _job_material_qty(base_qty: int, runs: int, me: float) -> int:
    """One job's material need, per the EVE formula, rounded once for the job.

    Structure and rig bonuses are deliberately not applied: which structure a
    job ran in months ago is not something we can know now, and inventing one
    would make the number look more precise than it is. The effect is small and
    in a known direction - materials come out slightly high, so the pile before
    the job is estimated slightly large.
    """
    return max(runs, math.ceil(round(base_qty * runs * (1 - me / 100.0), 2)))


def _blueprint_me(conn: sqlite3.Connection) -> tuple[dict[int, float], dict[int, float]]:
    """ME by blueprint item id, and the best ME seen per blueprint type, for jobs
    whose copy has since been consumed."""
    by_item: dict[int, float] = {}
    by_type: dict[int, float] = {}
    try:
        rows = conn.execute("SELECT data_json FROM char_blueprints_cache").fetchall()
    except sqlite3.OperationalError:
        return by_item, by_type
    for (blob,) in rows:
        try:
            for b in json.loads(blob):
                me = float(b.get("material_efficiency") or 0)
                if b.get("item_id"):
                    by_item[int(b["item_id"])] = me
                if b.get("type_id"):
                    t = int(b["type_id"])
                    by_type[t] = max(by_type.get(t, 0.0), me)
        except (ValueError, TypeError):
            continue
    return by_item, by_type


def basket_events(conn: sqlite3.Connection, since: float) -> tuple[list[tuple], dict]:
    """Net quantity per type that ENTERED the pile between `since` and now.

    Subtracting it from today's holdings gives the basket as it was. Four
    sources, each of them a record rather than an assumption:

      bought  entered        sold      left
      mined   entered        produced  entered (and its materials left)
    """
    from app.character.history_store import ensure_history_tables
    ensure_history_tables(conn)
    events: list[tuple] = []
    used = {"transactions": 0, "mining": 0, "jobs": 0}

    for ts, type_id, qty, is_buy in conn.execute(
            "SELECT date_ts, type_id, quantity, is_buy FROM wallet_transactions"
            " WHERE date_ts >= ?", (since,)):
        events.append((ts, type_id, qty if is_buy else -qty))
        used["transactions"] += 1

    day_since = time.strftime("%Y-%m-%d", time.gmtime(since))
    for day, type_id, qty in conn.execute(
            "SELECT day, type_id, SUM(quantity) FROM mining_ledger WHERE day >= ?"
            " GROUP BY day, type_id", (day_since,)):
        # A day's mining is credited at the end of that day: ESI gives no finer
        # timestamp, and putting it at the start would move ore into a basket it
        # was not in yet.
        ts = time.mktime(time.strptime(day, "%Y-%m-%d")) + 86399
        events.append((ts, type_id, qty))
        used["mining"] += 1

    by_item, by_type = _blueprint_me(conn)
    jobs = conn.execute(
        "SELECT activity_id, blueprint_id, blueprint_type_id, product_type_id,"
        " runs, successful_runs, end_ts FROM industry_jobs_done"
        " WHERE status = 'delivered' AND end_ts >= ?", (since,)).fetchall()
    for activity, bp_item, bp_type, product, runs, ok_runs, end_ts in jobs:
        runs = int(ok_runs or runs or 0)
        if not runs or not bp_type:
            continue
        used["jobs"] += 1
        # The product arrived...
        prod = conn.execute(
            "SELECT product_type_id, quantity FROM sde_blueprint_products"
            " WHERE blueprint_type_id = ? AND activity = ?", (bp_type, activity)).fetchone()
        if prod:
            events.append((end_ts, prod[0], prod[1] * runs))
        elif product:
            events.append((end_ts, product, runs))
        # ...and the materials it was made of left.
        me = by_item.get(bp_item, by_type.get(bp_type, 0.0))
        for mat, base in conn.execute(
                "SELECT material_type_id, quantity FROM sde_blueprint_materials"
                " WHERE blueprint_type_id = ? AND activity = ?", (bp_type, activity)):
            events.append((end_ts, mat, -_job_material_qty(base, runs, me)))

    events.sort(key=lambda e: e[0], reverse=True)
    return events, used


def _cached_histories(conn: sqlite3.Connection, type_ids: list[int]
                      ) -> dict[int, dict[str, float]]:
    """Daily average price per type, from the history cache only."""
    out: dict[int, dict[str, float]] = {}
    if not type_ids:
        return out
    for i in range(0, len(type_ids), 400):
        chunk = type_ids[i:i + 400]
        ph = ",".join("?" * len(chunk))
        for tid, blob in conn.execute(
                f"SELECT type_id, data_json FROM price_history_cache"
                f" WHERE region_id = ? AND type_id IN ({ph})", [JITA_REGION, *chunk]):
            try:
                days = json.loads(blob)
            except (ValueError, TypeError):
                continue
            out[tid] = {d["date"]: d["average"] for d in days
                        if d.get("date") and d.get("average") is not None}
    return out


def index_type_ids(conn: sqlite3.Connection, basket: dict[int, int],
                   events: list[tuple]) -> list[int]:
    """Which types are worth having daily prices for: the biggest holdings, plus
    everything that moved (a thing sold last month is not in today's pile but was
    in the past one)."""
    px = {r[0]: r[1] for r in conn.execute(
        "SELECT type_id, sell_price FROM market_price_cache WHERE sell_price IS NOT NULL")}
    ranked = sorted(basket.items(), key=lambda kv: -kv[1] * px.get(kv[0], 0.0))
    want = [t for t, _q in ranked[:_INDEX_TYPES]]
    seen = set(want)
    for _ts, tid, _q in events:
        if tid not in seen:
            seen.add(tid)
            want.append(tid)
    return want


def index_coverage(conn: sqlite3.Connection, basket: dict[int, int],
                   have: dict[int, dict]) -> float:
    """Share of today's value whose types already have daily prices cached."""
    px = {r[0]: r[1] for r in conn.execute(
        "SELECT type_id, sell_price FROM market_price_cache WHERE sell_price IS NOT NULL")}
    total = sum(q * px.get(t, 0.0) for t, q in basket.items())
    if total <= 0:
        return 1.0
    got = sum(q * px.get(t, 0.0) for t, q in basket.items() if t in have)
    return got / total


def _market_isk_flows(conn: sqlite3.Connection, since: float, until: float
                      ) -> tuple[float, float]:
    """Market money moved in a window, from the journal - the fallback for a
    stretch the transactions no longer reach. Sells are what left the pile,
    escrow is what was committed to buying into it."""
    sells = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM wallet_journal"
        " WHERE ref_type='market_transaction' AND date_ts >= ? AND date_ts < ?",
        (since, until)).fetchone()[0]
    escrow = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM wallet_journal"
        " WHERE ref_type='market_escrow' AND date_ts >= ? AND date_ts < ?",
        (since, until)).fetchone()[0]
    return float(sells or 0.0), float(-(escrow or 0.0))


def estimate_series(conn: sqlite3.Connection, days: float, histories: dict) -> dict:
    """The dashed part of the chart: net worth before the recording started.

    Anchored on the oldest real sample rather than on "now", so the dashed line
    meets the solid one instead of stepping away from it, and scaled so that the
    basket priced from daily averages agrees with the figure the dashboard shows
    (measured: the same basket is 124.95B at Jita sell and 121.68B at average
    trade price - a 2.7% basis difference that anchoring removes entirely).
    """
    ensure_worth_tables(conn)
    row = conn.execute(
        "SELECT MIN(ts) FROM net_worth_history").fetchone()
    anchor_ts = row[0]
    if anchor_ts:
        assets_at_anchor = conn.execute(
            "SELECT COALESCE(SUM(asset_value), 0) FROM net_worth_history WHERE ts = ?",
            (anchor_ts,)).fetchone()[0]
    else:
        anchor_ts = time.time()
        assets_at_anchor = None

    basket = current_basket(conn)
    if not basket:
        return {"points": [], "reason": "no assets stored yet"}

    px_now = {r[0]: r[1] for r in conn.execute(
        "SELECT type_id, sell_price FROM market_price_cache WHERE sell_price IS NOT NULL")}
    if assets_at_anchor is None or assets_at_anchor <= 0:
        assets_at_anchor = sum(q * px_now.get(t, 0.0) for t, q in basket.items())
    if assets_at_anchor <= 0:
        return {"points": [], "reason": "no prices stored yet"}

    until = time.time()
    since = until - days * 86400.0
    # The journal is the floor: net worth is wallet plus assets, and the wallet
    # half cannot be walked back past it.
    j_oldest = conn.execute("SELECT MIN(date_ts) FROM wallet_journal").fetchone()[0]
    if j_oldest:
        since = max(since, j_oldest)
    if since >= anchor_ts:
        return {"points": [], "reason": "the recording already covers this window"}

    tx_oldest = conn.execute(
        "SELECT MIN(date_ts) FROM wallet_transactions").fetchone()[0] or anchor_ts

    def value_of(bk: dict[int, int], day: str) -> float:
        total = 0.0
        for t, q in bk.items():
            if q <= 0:
                continue
            h = histories.get(t)
            p = h.get(day) if h else None
            if p is None:
                p = px_now.get(t)          # no history: held at today's price
            if p:
                total += q * p
        return total

    anchor_day = time.strftime("%Y-%m-%d", time.gmtime(anchor_ts))
    base_value = value_of(basket, anchor_day)
    if base_value <= 0:
        return {"points": [], "reason": "no daily prices available yet"}
    scale = assets_at_anchor / base_value

    events, used = basket_events(conn, since)
    grid = [t for t in _grid(since, anchor_ts) if t <= anchor_ts]
    past = dict(basket)
    ev = 0
    points = []
    # Newest first: undo every movement down to the grid point, then price it.
    for t in reversed(grid):
        while ev < len(events) and events[ev][0] > t:
            _ts, tid, q = events[ev]
            past[tid] = past.get(tid, 0) - q
            ev += 1
        day = time.strftime("%Y-%m-%d", time.gmtime(t))
        central = value_of(past, day) * scale
        if t < tx_oldest:
            sells, buys = _market_isk_flows(conn, t, tx_oldest)
            spread = max(0.0, sells - buys)
        else:
            spread = 0.0
        points.append({"t": int(t), "assets_lo": central,
                       "assets_hi": central + spread})
    points.reverse()
    return {
        "points": points,
        "anchor_ts": int(anchor_ts),
        "transactions_from": int(tx_oldest) if tx_oldest else None,
        "sources": used,
    }


async def fetch_index_histories(conn: sqlite3.Connection, type_ids: list[int],
                                concurrency: int = 20) -> int:
    """Daily prices for the types the estimate needs, fetched once and cached.

    `/markets/{region}/history/` is public and carries no token-bucket limit, and
    the cache is the one the price chart already uses - so a second opening of
    the window costs nothing. Returns how many were actually fetched.
    """
    import asyncio as _asyncio

    from app.esi.client import esi_client
    from app.market.prices import fetch_region_history
    from app.web.prices_helper import HISTORY_TTL, ensure_price_table

    ensure_price_table(conn)
    fresh = {r[0] for r in conn.execute(
        "SELECT type_id FROM price_history_cache WHERE region_id = ? AND cached_at > ?",
        (JITA_REGION, time.time() - HISTORY_TTL))}
    todo = [t for t in type_ids if t not in fresh]
    if not todo:
        return 0

    sem = _asyncio.Semaphore(concurrency)
    got: list[tuple] = []

    async def one(client, tid):
        async with sem:
            try:
                series = await fetch_region_history(client, JITA_REGION, tid)
            except Exception:
                return
            if series is not None:
                got.append((JITA_REGION, tid, json.dumps(series), time.time()))

    async with esi_client() as client:
        await _asyncio.gather(*[one(client, t) for t in todo])

    if got:
        conn.executemany(
            "INSERT INTO price_history_cache (region_id, type_id, data_json, cached_at)"
            " VALUES (?,?,?,?) ON CONFLICT(region_id, type_id) DO UPDATE SET"
            " data_json = excluded.data_json, cached_at = excluded.cached_at", got)
        conn.commit()
    return len(got)
