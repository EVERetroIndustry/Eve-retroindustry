"""
Loading market prices from ESI.

Two modes:
  adjusted  - global adjusted/average prices, single API call
  jita      - live Jita sell/buy prices, N parallel calls, 30 min cache
"""
import asyncio
import functools
import json
import time
import sqlite3
import httpx
from app.esi.client import esi_client, esi_throttle_status, esi_budget_share

ESI_BASE = "https://esi.evetech.net/latest"
JITA_REGION = 10000002   # The Forge
JITA_STATION = 60003760  # Jita 4-4 CNAP
PRICE_CACHE_TTL = 60 * 60 * 12  # 12 hours

# Secondary trade hubs - region_id → {name, station}. Jita stays the app-wide
# reference (market_price_cache); these are fetched on demand per hub into
# hub_price_cache and shown as comparison columns on the Prices page. Sell/buy are
# region-wide best (as with Jita/The Forge); `station` is the hub's main station,
# used to sum "available" units (sell-order volume) there.
TRADE_HUBS: dict[int, dict] = {
    10000043: {"name": "Amarr",   "station": 60008494},  # Domain / Amarr VIII (Oris)
    10000032: {"name": "Dodixie", "station": 60011866},  # Sinq Laison / Dodixie IX-M20
    10000030: {"name": "Rens",    "station": 60004588},  # Heimatar / Rens VI-M8
    10000042: {"name": "Hek",     "station": 60005686},  # Metropolis / Hek VIII-M12
}
# Used ONLY for the UI freshness indicator (green/red badge on /prices,
# `fresh` flag in the API). For price calculations (`get_prices_for_ids`) the
# cache does NOT expire - the last fetched Jita / The Forge sell value is always
# used, regardless of age. A full refresh via `/markets/{region}/orders/` takes
# ~3 s, and the user usually refreshes once a day.

_JITA_SEM = asyncio.Semaphore(10)
# The 7-day history is fetched per-type (no bulk endpoint), so it is the dominant
# part of a refresh (~19k calls).
# Concurrency for market history. ESI rate-limits this endpoint by REQUEST RATE,
# not by a cumulative count - measured 2026-08-24 on the same 17 307-type list:
#
#   * a bare loop at concurrency 30 reaches ~460 req/s and gets 429s, each of
#     which parks the whole group for a minute;
#   * the refresh path at concurrency 30 reaches only ~290-380 req/s, because it
#     commits every 200 results, and finishes 17 307 types in 45 s with ZERO 429s;
#   * the same path at concurrency 10 is clean too, but takes ~105 s.
#
# So 10 was an over-correction that cost 60 s on every cold price refresh. What
# actually tripped the limiter was the custom-station sweep, which used to fire
# ~19 800 requests in one unpaced burst; it now asks only about types the station
# actually sells (a few hundred to a couple thousand), so that burst is seconds
# long and cannot sustain the rate that trips anything. If a 429 does arrive, the
# governor parks the group and the ETag layer makes the retry nearly free.
_HIST_SEM = asyncio.Semaphore(30)


# Foreground price work in flight. The background region top-up shares ESI's
# rate-limit group with it, and a 429 parks the WHOLE group - so a background
# burst can make a user who just clicked Load wait out a minute they did not
# earn. Measured before this existed: a cold station load that takes 0.8 s alone
# took 42.2 s issued right after the top-up had collected a penalty. The top-up
# now stands aside whenever something the user is waiting for is running.
_foreground_ops = 0


class foreground_prices:
    """Context manager marking a price operation a user is waiting for."""

    def __enter__(self):
        global _foreground_ops
        _foreground_ops += 1
        return self

    def __exit__(self, *exc):
        global _foreground_ops
        _foreground_ops = max(0, _foreground_ops - 1)
        return False


def foreground_prices_active() -> bool:
    return _foreground_ops > 0


def foreground(fn):
    """Mark an async price fetch as work a user is waiting for.

    A decorator rather than a with-block inside each body: the point is to
    reserve ESI's rate-limit group for these calls, and nesting them is fine
    (the counter, not a flag, is what makes that safe).
    """
    @functools.wraps(fn)
    async def wrapper(*a, **kw):
        with foreground_prices():
            return await fn(*a, **kw)
    return wrapper


# ---------------------------------------------------------------------------
# DB schema
# ---------------------------------------------------------------------------

def ensure_price_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_price_cache (
            type_id    INTEGER PRIMARY KEY,
            sell_price REAL,
            buy_price  REAL,
            cached_at  REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS custom_price_override (
            type_id    INTEGER PRIMARY KEY,
            price      REAL NOT NULL,
            updated_at REAL
        )
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(market_price_cache)")}
    if "volume" not in cols:
        conn.execute("ALTER TABLE market_price_cache ADD COLUMN volume INTEGER")
    if "jita_available" not in cols:
        conn.execute("ALTER TABLE market_price_cache ADD COLUMN jita_available INTEGER")
    # Secondary trade-hub prices (Amarr/Dodixie/Rens/Hek …), fetched on demand.
    # Region-wide best sell/buy + 7-day region volume, one row per (region, type).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hub_price_cache (
            region_id  INTEGER NOT NULL,
            type_id    INTEGER NOT NULL,
            sell_price REAL,
            buy_price  REAL,
            volume     INTEGER,
            available  INTEGER,
            cached_at  REAL,
            PRIMARY KEY (region_id, type_id)
        )
    """)
    hub_cols = {r[1] for r in conn.execute("PRAGMA table_info(hub_price_cache)")}
    if "available" not in hub_cols:
        conn.execute("ALTER TABLE hub_price_cache ADD COLUMN available INTEGER")
    # Full daily market history (~1 year) per (region, type) for the price chart.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history_cache (
            region_id INTEGER NOT NULL,
            type_id   INTEGER NOT NULL,
            data_json TEXT NOT NULL,
            cached_at REAL,
            PRIMARY KEY (region_id, type_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS station_volume_cache (
            location_id    INTEGER NOT NULL,
            type_id        INTEGER NOT NULL,
            volume         INTEGER,
            best_sell      REAL,
            traded_volume  INTEGER,
            cached_at      REAL,
            PRIMARY KEY (location_id, type_id)
        )
    """)
    sv_cols = {r[1] for r in conn.execute("PRAGMA table_info(station_volume_cache)")}
    if "traded_volume" not in sv_cols:
        conn.execute("ALTER TABLE station_volume_cache ADD COLUMN traded_volume INTEGER")
    conn.commit()


# ---------------------------------------------------------------------------
# Adjusted prices (global, 1 call)
# ---------------------------------------------------------------------------

async def fetch_adjusted_prices(client: httpx.AsyncClient) -> dict[int, dict]:
    """
    Returns {type_id: {adjusted_price, average_price}} for all types.
    A single API call - suitable for a quick estimate.

    Best-effort: this is only a fallback price estimate. NEVER raises -
    on a 420 (ESI error-limit), timeout, or any other error it returns {}, so
    an ESI failure never takes down the dashboard / plan. The caller handles an empty dict.
    """
    try:
        r = await client.get(
            f"{ESI_BASE}/markets/prices/",
            params={"datasource": "tranquility"},
            timeout=20,
        )
        if r.status_code != 200:
            return {}
        return {d["type_id"]: d for d in r.json()}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Jita live prices (per type, cached)
# ---------------------------------------------------------------------------

def _get_cached_price(conn: sqlite3.Connection, type_id: int) -> tuple[float | None, float | None]:
    row = conn.execute(
        "SELECT sell_price, buy_price, cached_at FROM market_price_cache WHERE type_id=?",
        (type_id,)
    ).fetchone()
    if row and (time.time() - (row[2] or 0)) < PRICE_CACHE_TTL:
        return row[0], row[1]
    return None, None


def _save_cached_price(
    conn: sqlite3.Connection,
    type_id: int,
    sell: float | None,
    buy: float | None,
    volume: int | None = None,
    jita_available: int | None = None,
):
    conn.execute(
        "INSERT OR REPLACE INTO market_price_cache (type_id, sell_price, buy_price, volume, jita_available, cached_at) VALUES (?,?,?,?,?,?)",
        (type_id, sell, buy, volume, jita_available, time.time())
    )
    conn.commit()


async def fetch_region_history(client: httpx.AsyncClient, region_id: int, type_id: int) -> list[dict] | None:
    """Full daily market history (~1 year) for a type in a region. Returns a list
    of {d, avg, low, high, vol} oldest→newest, or None on error. Same ESI endpoint
    the 7-day volume already uses - we just keep the whole series."""
    async with _HIST_SEM:
        try:
            r = await client.get(
                f"{ESI_BASE}/markets/{region_id}/history/",
                params={"type_id": type_id, "datasource": "tranquility"},
                timeout=20,
            )
            if r.status_code != 200:
                return None
            hist = r.json()
            if not isinstance(hist, list):
                return None
            return [
                {"d": e.get("date"), "avg": e.get("average"),
                 "low": e.get("lowest"), "high": e.get("highest"),
                 "vol": e.get("volume", 0)}
                for e in hist
            ]
        except Exception:
            return None


# ── Market-history ETag cache ────────────────────────────────────────────────
# A market-history response is ~42 KB (≈408 daily records) and the volume phase
# asks for ~19k types, i.e. ~800 MB of JSON to download and parse on every
# refresh. ESI serves these with an ETag and honours If-None-Match, answering
# 304 with an EMPTY body when nothing changed (history is rebuilt once a day).
# Measured on a 100-type sample: 3.93 MB -> 0 bytes, 37% less wall-clock.
#
# 304 alone isn't enough to be correct, though: "last 7 CALENDAR days" is a
# moving window, so an unchanged history can still yield a different number
# tomorrow. We therefore keep the last _ETAG_KEEP_DAYS daily volumes next to the
# ETag (a handful of ints) and recompute the window locally on a 304 - exact,
# and still zero bytes over the wire.
_ETAG_KEEP_DAYS = 12          # > 7, so the window can always be recomputed
_HIST_WINDOW_DAYS = 7

# (region_id, type_id) -> (etag, {date: volume}, expires_at epoch)
_hist_etags: dict[tuple[int, int], tuple[str, dict[str, int], float]] = {}
_hist_etags_dirty: set[tuple[int, int]] = set()


def ensure_hist_etag_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_hist_etag (
            region_id INTEGER NOT NULL,
            type_id   INTEGER NOT NULL,
            etag      TEXT,
            days_json TEXT,
            cached_at REAL,
            expires_at REAL,
            PRIMARY KEY (region_id, type_id)
        )
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(market_hist_etag)")}
    if "expires_at" not in cols:                    # migrate older caches
        conn.execute("ALTER TABLE market_hist_etag ADD COLUMN expires_at REAL")
    conn.commit()


def load_hist_etags(conn: sqlite3.Connection, region_id: int) -> int:
    """Load a region's stored ETags into memory before a volume phase."""
    ensure_hist_etag_table(conn)
    n = 0
    for tid, etag, days_json, expires_at in conn.execute(
        "SELECT type_id, etag, days_json, expires_at FROM market_hist_etag WHERE region_id=?",
        (region_id,),
    ):
        if not etag:
            continue
        try:
            days = json.loads(days_json) if days_json else {}
        except Exception:
            days = {}
        _hist_etags[(region_id, tid)] = (etag, days, expires_at or 0.0)
        n += 1
    return n


def flush_hist_etags(conn: sqlite3.Connection, clear: bool = True) -> int:
    """Persist ETags collected during a volume phase (bulk, one transaction).

    `clear=False` writes without dropping the in-memory map, which is what a
    per-batch flush needs: dropping it mid-run would cost the REMAINING batches
    their If-None-Match headers, turning cheap 304s back into full bodies. The
    final call clears, because holding every region's map (~19k types each) in a
    desktop app is tens of MB.
    """
    if not _hist_etags_dirty:
        return 0
    ensure_hist_etag_table(conn)
    now = time.time()
    rows = []
    for key in list(_hist_etags_dirty):
        entry = _hist_etags.get(key)
        if not entry:
            continue
        rows.append((key[0], key[1], entry[0], json.dumps(entry[1]), now, entry[2]))
    conn.executemany(
        "INSERT OR REPLACE INTO market_hist_etag "
        "(region_id, type_id, etag, days_json, cached_at, expires_at) VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    _hist_etags_dirty.clear()
    if clear:
        _hist_etags.clear()
    return len(rows)


def _window_sum(days: dict[str, int]) -> int:
    """Sum the daily volumes that fall inside the current 7-calendar-day window."""
    import datetime
    cutoff = (datetime.date.today() - datetime.timedelta(days=_HIST_WINDOW_DAYS)).isoformat()
    return sum(v for d, v in days.items() if d >= cutoff)


def _recent_days(history: list[dict]) -> dict[str, int]:
    """Keep only the newest _ETAG_KEEP_DAYS entries - enough to recompute the
    window later without storing a year of data per type."""
    tail = history[-_ETAG_KEEP_DAYS:] if len(history) > _ETAG_KEEP_DAYS else history
    return {e["date"]: e.get("volume", 0) for e in tail if e.get("date")}


def _parse_http_date(v: str | None) -> float:
    """RFC-1123 date -> epoch seconds (0 if absent/unparseable)."""
    if not v:
        return 0.0
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(v).timestamp()
    except Exception:
        return 0.0


# Example URL identifying the history endpoint's rate-limit group for
# esi_throttle_status() - only the path shape matters (region/type id values are
# collapsed away), so "1" here is a placeholder, not a real region.
HISTORY_ENDPOINT_URL = f"{ESI_BASE}/markets/1/history/"


def _types_with_market_history(conn, type_ids: list[int]) -> list[int]:
    """Drop type_ids the history endpoint cannot answer for.

    Measured against the live endpoint (Cloud Ring, 2026-08-24): of 60 sampled
    types with no market group or published = 0, **60 answered 400 or 404**; of 60
    ordinary market types, **none did**. A custom-station load sweeps every cached
    type - 379 of the 19 812 here are such types - so a cold sweep fired ~379
    GUARANTEED errors against an error budget of 100 per ~60 s window. Our own
    error-limit governor then froze ALL ESI traffic until the window reset, three
    or four times per load, which is what "it loads a bit, hangs, loads a bit"
    was. Things like "Asset Safety Wrap" have no market presence, so their 7-day
    volume was never going to be anything but blank either way.

    If the SDE is unavailable the list is returned untouched: fewer volumes is a
    worse failure than a slow sweep.
    """
    ids = [int(t) for t in type_ids if t]
    if not ids:
        return []
    keep: set[int] = set()
    try:
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            ph = ",".join("?" * len(chunk))
            keep.update(int(r[0]) for r in conn.execute(
                f"SELECT type_id FROM sde_types WHERE type_id IN ({ph})"
                f" AND market_group_id IS NOT NULL AND published = 1", chunk).fetchall())
    except Exception:
        return ids
    return [t for t in ids if t in keep]


async def _fetch_region_volume(client: httpx.AsyncClient, region_id: int, type_id: int) -> int | None:
    """Total units traded over the last 7 CALENDAR days from ESI history.

    ESI omits days with no trades, so summing the last 7 *entries* over-counts
    for illiquid items (e.g. a SKIN that trades once a week would sum ~2 months
    of days). We sum only entries dated within the last 7 days - 0 if it hasn't
    traded recently, which is the truthful answer.

    Sends If-None-Match when we already have an ETag: a 304 costs no body at all
    and the window is recomputed from the stored daily volumes.
    """
    key = (region_id, type_id)
    cached = _hist_etags.get(key)
    # ESI rebuilds market history once a day and tells us when the current copy
    # stops being authoritative (Expires). While that hasn't passed, a refetch is
    # guaranteed to return the same bytes - so skip the round trip entirely and
    # recompute the moving 7-day window from the stored daily volumes. This is
    # plain HTTP caching (no invented TTL, no staler data), and it turns a repeat
    # refresh on the same day from ~19k requests into zero.
    if cached and cached[2] and time.time() < cached[2]:
        return _window_sum(cached[1])
    req_headers = {"If-None-Match": cached[0]} if cached else {}

    async with _HIST_SEM:
        # Retry on transient failures. Loading a custom station fires this for
        # ~19k types at once; without retries a large fraction hit the ESI error
        # limit (420) or time out and came back None, leaving the "region vol/7d"
        # column blank for ~half the items. A 200 with an empty history list means
        # the type has simply never traded → 0, which is a real answer (not a
        # failure), so we don't retry that.
        for attempt in range(2):   # one quick retry - enough for transient blips
            try:
                r = await client.get(
                    f"{ESI_BASE}/markets/{region_id}/history/",
                    params={"type_id": type_id, "datasource": "tranquility"},
                    headers=req_headers,
                    timeout=12,
                )
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError):
                if attempt == 0:
                    await asyncio.sleep(0.3)
                    continue
                return None
            if r.status_code == 304 and cached:
                exp = _parse_http_date(r.headers.get("expires"))
                if exp:                             # 304s carry a fresh Expires
                    _hist_etags[key] = (cached[0], cached[1], exp)
                    _hist_etags_dirty.add(key)
                return _window_sum(cached[1])       # unchanged upstream → local recompute
            if r.status_code == 200:
                history = r.json()
                if not isinstance(history, list) or not history:
                    # "This type has never traded in this region" is an answer
                    # worth remembering. Without storing it, the same types get
                    # re-requested on every single load - measured 84 of 537 at
                    # one station, every time. Cached the same way as a real
                    # history: the Expires window skips the call outright, and
                    # after it a 304 confirms it is still empty. If it ever
                    # starts trading the ETag changes and a 200 replaces this.
                    etag = r.headers.get("etag")
                    if etag:
                        _hist_etags[key] = (etag, {}, _parse_http_date(r.headers.get("expires")))
                        _hist_etags_dirty.add(key)
                    return 0
                etag = r.headers.get("etag")
                days = _recent_days(history)
                if etag:
                    _hist_etags[key] = (etag, days, _parse_http_date(r.headers.get("expires")))
                    _hist_etags_dirty.add(key)
                return _window_sum(days)
            # 429 reaches here only after the shared token-bucket governor has
            # already retried it internally (see _GovernedTransport) and waited
            # out the group's pause each time - a persistent 429 despite that
            # means real contention, not a fluke, but the type still DOES trade
            # (a 404/400 means it never will). Worth one more try rather than
            # quietly recording "no data" for something we simply haven't asked
            # about successfully yet.
            if (r.status_code in (420, 429) or r.status_code >= 500) and attempt == 0:
                await asyncio.sleep(0.6)
                continue
            return None   # 400/404/… → no usable data
    return None


async def _fetch_jita_volume(client: httpx.AsyncClient, type_id: int) -> int | None:
    return await _fetch_region_volume(client, JITA_REGION, type_id)


async def fetch_jita_price(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    type_id: int,
    force: bool = False,
) -> tuple[float | None, float | None]:
    """
    Returns (best_sell, best_buy) for the given type in Jita.
    Uses the cache - valid for 30 minutes. force=True skips the cache and always fetches fresh data.
    """
    if not force:
        sell_c, buy_c = _get_cached_price(conn, type_id)
        if sell_c is not None or buy_c is not None:
            return sell_c, buy_c

    orders_resp = None
    for attempt in range(3):
        try:
            async with _JITA_SEM:
                orders_resp = await client.get(
                    f"{ESI_BASE}/markets/{JITA_REGION}/orders/",
                    params={"type_id": type_id, "order_type": "all", "datasource": "tranquility"},
                    timeout=15,
                )
        except (httpx.TimeoutException, httpx.ConnectError):
            if attempt < 2:
                await asyncio.sleep(2 ** attempt * 3)
                continue
            return None, None

        if orders_resp.status_code in (420, 429):
            retry_after = int(orders_resp.headers.get("Retry-After", 60))
            await asyncio.sleep(min(retry_after, 120))
            continue
        if orders_resp.status_code == 404:
            _save_cached_price(conn, type_id, None, None, None, None)
            return None, None
        if orders_resp.status_code != 200:
            if attempt < 2:
                await asyncio.sleep(5)
                continue
            return None, None
        break
    else:
        return None, None

    volume = await _fetch_jita_volume(client, type_id)

    orders = orders_resp.json()
    sell_orders = [o for o in orders if not o["is_buy_order"]]
    buy_orders  = [o for o in orders if o["is_buy_order"]]

    best_sell = min((o["price"] for o in sell_orders), default=None)
    best_buy  = max((o["price"] for o in buy_orders),  default=None)

    jita_available = sum(
        o.get("volume_remain", 0) for o in sell_orders
        if o.get("location_id") == JITA_STATION
    )

    _save_cached_price(conn, type_id, best_sell, best_buy, volume, jita_available)
    return best_sell, best_buy


@foreground
async def fetch_jita_prices_bulk(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    type_ids: list[int],
    force: bool = False,
) -> dict[int, tuple[float | None, float | None]]:
    """Fetches Jita prices for a list of types in parallel."""
    tasks = [fetch_jita_price(client, conn, tid, force=force) for tid in type_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return {
        tid: res if isinstance(res, tuple) else (None, None)
        for tid, res in zip(type_ids, results)
    }


# ---------------------------------------------------------------------------
# Bulk Jita orders - fetch all active orders in the region at once (paginated)
# ---------------------------------------------------------------------------

async def _fetch_orders_page(
    client: httpx.AsyncClient,
    region_id: int,
    page: int,
) -> tuple[list[dict], int]:
    """Fetches a single page of orders and returns (orders, x_pages)."""
    async with _JITA_SEM:
        for attempt in range(3):
            try:
                r = await client.get(
                    f"{ESI_BASE}/markets/{region_id}/orders/",
                    params={"order_type": "all", "datasource": "tranquility", "page": page},
                    timeout=30,
                )
            except (httpx.TimeoutException, httpx.ConnectError):
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt * 3)
                    continue
                return [], 0
            if r.status_code in (420, 429):
                retry_after = int(r.headers.get("Retry-After", 60))
                await asyncio.sleep(min(retry_after, 120))
                continue
            if r.status_code != 200:
                if attempt < 2:
                    await asyncio.sleep(5)
                    continue
                return [], 0
            return r.json(), int(r.headers.get("x-pages", 1))
        return [], 0


async def _fetch_all_region_orders(
    client: httpx.AsyncClient,
    region_id: int,
    progress_cb=None,
) -> list[list[dict]]:
    """Fetches ALL pages of the region's orders, paginated (in parallel, _JITA_SEM).
    Returns a list of pages (each = list of orders). progress_cb(done, total) after each
    page. Shared between region-bulk and station-bulk aggregation."""
    first, total_pages = await _fetch_orders_page(client, region_id, 1)
    if not first and total_pages == 0:
        return []

    pages_data: list[list[dict]] = [first]
    if progress_cb:
        await _maybe_call(progress_cb, 1, total_pages)

    remaining = list(range(2, total_pages + 1))
    completed = [1]
    lock = asyncio.Lock()

    async def _one(p: int):
        page_data, _ = await _fetch_orders_page(client, region_id, p)
        async with lock:
            pages_data.append(page_data)
            completed[0] += 1
            if progress_cb:
                await _maybe_call(progress_cb, completed[0], total_pages)

    await asyncio.gather(*[_one(p) for p in remaining], return_exceptions=True)
    return pages_data


async def fetch_station_orders_bulk(
    client: httpx.AsyncClient,
    region_id: int,
    location_id: int,
    progress_cb=None,
) -> dict[int, tuple[int, float]]:
    """Bulk variant for a specific station: fetches regional orders once
    (~pages, not ~19k per-type calls) and returns {type_id: (sell_volume_sum,
    best_sell)} aggregated ONLY for sell orders at the given location_id.

    Orders of magnitude faster than per-type `_fetch_orders_for_type` for large type_ids."""
    pages_data = await _fetch_all_region_orders(client, region_id, progress_cb)
    agg: dict[int, tuple[int, float]] = {}
    for page_orders in pages_data:
        for o in page_orders:
            if o.get("is_buy_order"):
                continue
            if o.get("location_id") != location_id:
                continue
            tid = o.get("type_id")
            price = o.get("price")
            if tid is None or price is None:
                continue
            vol = int(o.get("volume_remain", 0))
            cur = agg.get(tid)
            if cur is None:
                agg[tid] = (vol, price)
            else:
                agg[tid] = (cur[0] + vol, min(cur[1], price))
    return agg


async def fetch_region_orders_bulk(
    client: httpx.AsyncClient,
    region_id: int = JITA_REGION,
    progress_cb=None,
    station_id: int = JITA_STATION,
) -> dict[int, dict]:
    """Fetches ALL active orders for the region, paginated, and aggregates
    per type_id: {type_id: {sell, buy, available}}. `available` = units in sell
    orders at `station_id` (the hub's main station).

    This is orders of magnitude more efficient than a per-type call: ~500 pages vs. 19k calls.
    progress_cb(page, total_pages) is called after each page (if provided).
    """
    pages_data = await _fetch_all_region_orders(client, region_id, progress_cb)
    if not pages_data:
        return {}

    # Aggregate per type_id
    agg: dict[int, dict] = {}
    for page_orders in pages_data:
        for o in page_orders:
            tid = o.get("type_id")
            price = o.get("price")
            if tid is None or price is None:
                continue
            entry = agg.setdefault(tid, {"sell": None, "buy": None, "available": 0})
            if o.get("is_buy_order"):
                if entry["buy"] is None or price > entry["buy"]:
                    entry["buy"] = price
            else:
                if entry["sell"] is None or price < entry["sell"]:
                    entry["sell"] = price
                if o.get("location_id") == station_id:
                    entry["available"] += int(o.get("volume_remain", 0))
    return agg


async def _maybe_call(cb, *args):
    """Helper - the callback can be sync or async."""
    if asyncio.iscoroutinefunction(cb):
        await cb(*args)
    else:
        cb(*args)


# Per-type orders at a custom station (phase A in fetch_station_volumes). Runs
# sequentially before the history phase (_HIST_SEM), so concurrency does not add up - 30 is
# safe under the ESI rate limit (same as _HIST_SEM).
_STATION_SEM = asyncio.Semaphore(30)
STATION_VOLUME_TTL = 60 * 30
# From this many type_ids onward, bulk (a single region download) pays off in
# fetch_station_volumes instead of per-type calls. Below the threshold, per-type is lighter and faster.
_BULK_ORDERS_THRESHOLD = 1000
_region_cache: dict[int, int] = {}  # structure_id → region_id (in-memory)


async def get_region_for_structure(structure_id: int) -> int | None:
    """Resolves the region_id for a structure via ESI (system→constellation→region). Cached in memory."""
    if structure_id in _region_cache:
        return _region_cache[structure_id]
    try:
        async with esi_client() as client:
            # NPC station: /universe/stations/{id}/ → system_id
            if structure_id < 1_000_000_000_000:
                r = await client.get(f"{ESI_BASE}/universe/stations/{structure_id}/",
                                     params={"datasource": "tranquility"}, timeout=8)
                sys_id = r.json().get("system_id") if r.status_code == 200 else None
            else:
                # Player structure - we have no token here, try via DB location_name_cache
                return None

            if not sys_id:
                return None

            sys_r = await client.get(f"{ESI_BASE}/universe/systems/{sys_id}/",
                                     params={"datasource": "tranquility"}, timeout=8)
            if sys_r.status_code != 200:
                return None
            con_id = sys_r.json().get("constellation_id")

            con_r = await client.get(f"{ESI_BASE}/universe/constellations/{con_id}/",
                                     params={"datasource": "tranquility"}, timeout=8)
            if con_r.status_code != 200:
                return None
            region_id = con_r.json().get("region_id")

        if region_id:
            _region_cache[structure_id] = region_id
        return region_id
    except Exception:
        return None


# How stale a stored daily-volume record may be before it stops counting as
# coverage. ESI rebuilds market history once a day, and the stored days are only
# useful while they still overlap the moving 7-day window: left long enough they
# fall out of it one by one and _window_sum quietly decays towards 0 - which
# would read as "nothing traded here" when the truth is "we have not looked
# lately". A missing number is honest; a wrong one is not.
_REGION_VOLUME_MAX_AGE = 36 * 3600

# Pacing for the background top-up. Deliberately far below what the foreground
# sweeps use: the history endpoint limits by request RATE, so firing a batch
# through the shared semaphore of 30 is a short sharp burst that trips 429s even
# though the average works out low - and each 429 parks the whole rate-limit
# group for 60 s, which the next user-initiated load pays for.
#
# Measured on region 10000038, 75 s each from a cooled-down start:
#   conc 30 (shared sem)  24 req/s, 6 penalties
#   conc 6                48 req/s, 3 penalties
#   conc 3                58 req/s, 0 penalties
# A gentle trickle is both faster and penalty-free, because the 60 s waits
# dominate everything else. Those are opening-burst rates though: sustained, the
# bucket becomes the ceiling and the client's own low-water brake takes over.
# Measured over 8 minutes on a cold region: 0 -> 10 534 of 19 432 types, zero
# penalties, the rate tapering from ~3 200/min to a few hundred as the bucket
# drained. A cold region therefore takes a long while to finish - which is fine,
# since every batch persists its ETags and the next pass resumes where it left
# off. What must not happen is a user waiting for it, and that is the guard above.
_FILL_CONC = 3
# Share of the endpoint's token bucket the top-up refuses to dip below, when we
# can see it at all. Usually we cannot: the history responses list the
# x-ratelimit-* headers in access-control-expose-headers but do not actually send
# them, so esi_budget_share() returns None and this check does nothing. It is
# kept as a second line of defence for the responses that do carry them; the
# mechanism that actually holds is stopping the moment a penalty is observed.
_FILL_MIN_BUDGET_SHARE = 0.35
_FILL_BATCH = 300        # how often progress is flushed, not a burst size
_FILL_SLEEP = 0.5
_FILL_SEM = asyncio.Semaphore(_FILL_CONC)


def _region_volume_from_etags(conn: sqlite3.Connection, region_id: int) -> dict[int, int]:
    """7-day volumes recomputed from the stored daily history of ANY region.

    The ETag cache keeps the last few daily volumes per (region, type) precisely
    so the moving window can be recomputed locally - which means every region we
    have ever fetched is already reusable, not just Jita and the hubs that have
    dedicated columns. Only records fresh enough to still overlap the window
    count; see _REGION_VOLUME_MAX_AGE.
    """
    out: dict[int, int] = {}
    try:
        rows = conn.execute(
            "SELECT type_id, days_json FROM market_hist_etag"
            " WHERE region_id=? AND days_json IS NOT NULL"
            "   AND (expires_at > ? OR cached_at > ?)",
            (region_id, time.time(), time.time() - _REGION_VOLUME_MAX_AGE),
        ).fetchall()
    except sqlite3.OperationalError:
        return out
    for tid, days_json in rows:
        try:
            out[int(tid)] = _window_sum(json.loads(days_json))
        except (ValueError, TypeError, AttributeError):
            continue      # one malformed row must not cost the whole region
    return out


def region_volume_coverage(conn: sqlite3.Connection, region_id: int,
                           type_ids: list[int]) -> tuple[int, int]:
    """(types with a fresh 7-day volume, types worth asking about).

    The denominator leaves out types the history endpoint cannot answer for -
    counting those would mean progress never reached 100 %.
    """
    askable = _types_with_market_history(conn, type_ids)
    if not askable:
        return 0, 0
    # _cached_region_volume, not the ETag map alone: Jita and the hubs keep their
    # volumes in dedicated columns, and counting only ETag rows would report a
    # fully-loaded region as 0 % covered - sending the top-up off to re-fetch
    # ~18k histories we already have.
    have = _cached_region_volume(conn, region_id) or {}
    return sum(1 for t in askable if t in have), len(askable)


async def fill_region_volumes(conn: sqlite3.Connection, region_id: int,
                              type_ids: list[int], budget: int = 3000,
                              progress_cb=None) -> dict:
    """Top up the 7-day volumes for a whole region, in the background.

    A custom-station load only asks about what that station actually sells, which
    is what makes it fast; this fills in the rest afterwards so the next load
    there - at any station in the region - is served from cache with full
    coverage. Paced deliberately, and it yields to foreground work: the job is to
    use capacity nobody else wants, not to race a user who just clicked Load.

    Interruptible at any point: every batch persists its own ETags, so a
    cancelled pass is progress rather than waste.
    """
    askable = _types_with_market_history(conn, type_ids)
    if not askable:
        return {"fetched": 0, "remaining": 0, "reason": "complete"}
    load_hist_etags(conn, region_id)
    have = _cached_region_volume(conn, region_id) or {}   # see region_volume_coverage
    todo = [t for t in askable if t not in have]
    if not todo:
        flush_hist_etags(conn)
        return {"fetched": 0, "remaining": 0, "reason": "complete"}

    fetched = 0
    stopped_on_budget = False
    stopped_on_penalty = False
    try:
        async with esi_client() as client:
            for start in range(0, min(len(todo), budget), _FILL_BATCH):
                # Checked before every batch, not once: a load can start at any
                # point during a pass that takes minutes.
                waited = 0.0
                while foreground_prices_active() and waited < 300:
                    await asyncio.sleep(1.0)
                    waited += 1.0
                # Wait out any penalty already in force rather than adding to
                # it - the governor would block us anyway, but asking first
                # keeps this job from being the reason the budget stays low.
                # A penalty is in force. Don't wait it out and carry straight
                # on: that is how a depleted bucket turns into a spiral, and the
                # next thing to pay for it is a user clicking Load. Stop, and let
                # the caller come back much later.
                if esi_throttle_status(HISTORY_ENDPOINT_URL).get("paused"):
                    stopped_on_penalty = True
                    break

                # Second line of defence, when CCP sends the headers for it. On
                # the history endpoint they are usually absent (the response
                # lists them in access-control-expose-headers but does not set
                # them), so this normally does nothing - hence the check above,
                # which relies only on what we can actually observe.
                waited = 0.0
                while waited < 600:
                    share = esi_budget_share(HISTORY_ENDPOINT_URL)
                    if share is None or share >= _FILL_MIN_BUDGET_SHARE:
                        break
                    if progress_cb:
                        try:
                            progress_cb(fetched, len(todo))
                        except Exception:
                            pass
                    await asyncio.sleep(10.0)
                    waited += 10.0
                if waited >= 600:
                    stopped_on_budget = True
                    break        # bucket stayed low - stop and let a later pass resume

                async def _one(t: int):
                    async with _FILL_SEM:
                        return await _fetch_region_volume(client, region_id, t)

                batch = todo[start:start + _FILL_BATCH]
                await asyncio.gather(*[_one(t) for t in batch],
                                     return_exceptions=True)
                fetched += len(batch)
                flush_hist_etags(conn, clear=False)
                if progress_cb:
                    try:
                        progress_cb(fetched, len(todo))
                    except Exception:
                        pass
                await asyncio.sleep(_FILL_SLEEP)
    finally:
        flush_hist_etags(conn)
    # "budget" tells the caller this stopped early with work left rather than
    # finishing - the difference between "wait and try again" and "done".
    reason = "complete"
    if stopped_on_penalty:
        reason = "throttled"
    elif stopped_on_budget:
        reason = "budget"
    return {"fetched": fetched, "remaining": max(0, len(todo) - fetched),
            "reason": reason}


def _cached_region_volume(conn: sqlite3.Connection, region_id: int | None) -> dict[int, int] | None:
    """Reuse an already-fetched 7-day *region* volume map so a custom station in a
    known region needn't re-fetch ~19k histories (the slow part of a station load).
    The Jita refresh stores The Forge volume in market_price_cache; hub refreshes
    store theirs in hub_price_cache. A custom station's "region vol/7d" is exactly
    that region-wide number, so when the region is one we've already loaded we can
    reuse it verbatim - the same data the Jita/hub columns show. Returns
    {type_id: volume} or None if that region isn't cached yet."""
    if not region_id:
        return None
    if region_id == JITA_REGION:
        rows = conn.execute(
            "SELECT type_id, volume FROM market_price_cache WHERE volume IS NOT NULL"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT type_id, volume FROM hub_price_cache WHERE region_id=? AND volume IS NOT NULL",
            (region_id,),
        ).fetchall()
    have = {r[0]: r[1] for r in rows} if rows else {}
    # The dedicated columns win where they exist - that is the data the Jita/hub
    # columns show, so a station in those regions stays consistent with them -
    # and the ETag-derived map fills in everything else.
    from_etags = _region_volume_from_etags(conn, region_id)
    if not have and not from_etags:
        return None
    from_etags.update(have)
    return from_etags


@foreground
async def fetch_structure_market(
    conn: sqlite3.Connection,
    structure_id: int,
    token: str,
    our_type_ids: set[int],
    region_id: int | None = None,
    progress_cb=None,
) -> dict[int, tuple[int | None, float | None, int | None]]:
    """
    Fetches all sell orders from a player structure via the authorized endpoint,
    plus 7-day regional trade volume for whatever the structure is selling.
    Returns {type_id: (volume, best_sell, traded_volume)} for every id in
    our_type_ids (types not listed there get (0, None, traded_volume or None)).
    Requires the esi-markets.structure_markets.v1 scope.
    """
    ensure_price_table(conn)
    aggregated: dict[int, dict] = {}
    page = 1
    got_ok = False   # did we successfully read at least one page?

    async with esi_client() as client:
        while True:
            r = None
            for attempt in range(3):   # retry transient failures - a timed-out
                try:                    # page must NOT silently cache blank prices
                    r = await client.get(
                        f"{ESI_BASE}/markets/structures/{structure_id}/",
                        params={"datasource": "tranquility", "page": page},
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=20,
                    )
                except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError):
                    await asyncio.sleep(0.6 * (attempt + 1)); r = None; continue
                if r.status_code == 403:
                    raise PermissionError("Insufficient permissions to access the structure market (403).")
                if r.status_code == 200:
                    break
                if r.status_code == 420 or r.status_code >= 500:
                    await asyncio.sleep(1.0 * (attempt + 1)); r = None; continue
                r = None; break   # 400/404/… → give up this page

            if r is None or r.status_code != 200:
                # Couldn't read this page after retries. If we never read ANY page,
                # fail loudly so the caller surfaces it and we don't cache blank
                # sell/available over good data (the bug: only 7d vol showed).
                if not got_ok:
                    raise RuntimeError("Could not read the structure market (ESI timeout/error). Try again.")
                break

            got_ok = True
            orders = r.json()
            if not orders:
                break

            for o in orders:
                if o.get("is_buy_order"):
                    continue
                tid = o.get("type_id")
                if tid not in our_type_ids:
                    continue
                if tid not in aggregated:
                    aggregated[tid] = {"volume": 0, "best_sell": None}
                aggregated[tid]["volume"] += o.get("volume_remain", 0)
                price = o.get("price")
                if price and (aggregated[tid]["best_sell"] is None or price < aggregated[tid]["best_sell"]):
                    aggregated[tid]["best_sell"] = price

            total_pages = int(r.headers.get("X-Pages", 1))
            if page >= total_pages:
                break
            page += 1

    # Persist the fast phase (this structure's own listings) BEFORE touching
    # region history - so if the slow phase below gets interrupted (tab closed,
    # navigated away, a long ESI wait), the sell prices we already have are not
    # thrown away with it. traded_volume starts NULL and is filled in per batch
    # further down; a query used to have to wait for BOTH phases to get either.
    now0 = time.time()
    conn.executemany(
        "INSERT OR REPLACE INTO station_volume_cache (location_id, type_id, volume,"
        " best_sell, traded_volume, cached_at) VALUES (?,?,?,?,?,?)",
        [(structure_id, tid, (aggregated.get(tid) or {}).get("volume", 0),
          (aggregated.get(tid) or {}).get("best_sell"), None, now0)
         for tid in our_type_ids],
    )
    conn.commit()

    # The 7-day "volume" is REGIONAL history (ESI does not publish trade history
    # for player structures).
    if region_id is None:
        # Try location_name_cache first (populated by location resolver in web layer)
        try:
            row = conn.execute(
                "SELECT region_id FROM location_name_cache WHERE location_id=?",
                (structure_id,)
            ).fetchone()
            if row and row[0]:
                region_id = row[0]
        except Exception:
            pass
    if region_id is None:
        region_id = await get_region_for_structure(structure_id)

    history_map: dict[int, int | None] = {}
    if region_id and our_type_ids:
        # Reuse stored history ETags, exactly like the NPC-station path does.
        # This was missing entirely: a structure load re-fetched every history
        # body every time and never stored an ETag for the next load, so the
        # whole HTTP-caching layer (a 304 has no body at all, and costs half the
        # rate-limit tokens of a 200) simply did not apply to citadels - which is
        # where the slow custom-station loads were reported.
        load_hist_etags(conn, region_id)
        # Whatever this region already has is a head start, not the answer. It
        # used to be treated as the answer, and that was correct while only Jita
        # and the hubs had reusable volumes: a non-empty map really did mean full
        # coverage. Once ANY swept region became reusable (v0.11.11) a region
        # with a handful of cached types took the same branch, and every other
        # type was written as a blank - measured on C-N4OD in Fountain, whose
        # region had 2 cached types: 4 697 items with a price and exactly 1 with
        # a volume.
        reuse = _cached_region_volume(conn, region_id) or {}
        history_map = {tid: reuse[tid] for tid in our_type_ids if tid in reuse}

        # Ask only about what is still missing AND is actually listed at this
        # structure (`aggregated`), not the whole ~19 800-type catalogue the
        # Prices table can show: asking about the rest spent a region's worth of
        # request budget on rows this one remote structure never carries. The
        # trade-off, made deliberately (reported 2026-08-24): a type traded
        # elsewhere in the region but not listed HERE keeps a blank vol/7d until
        # something warms this region's cache - the background top-up, or a load
        # of another station that lists it. A blank is honest; the old full
        # sweep's minute-long freezes were not a fair price for filling it in.
        tids = [t for t in our_type_ids if t in aggregated and t not in reuse]
        askable = _types_with_market_history(conn, tids)
        skipped = len(our_type_ids) - len(askable) - len(history_map)
        if skipped > 0:
            print(f"[prices] volume sweep: skipping {skipped} type(s) not "
                  f"listed at this structure or with no market history", flush=True)
        if askable:
            total = len(askable)
            done = 0
            _BATCH = 300
            async with esi_client() as client:
                for start in range(0, total, _BATCH):
                    batch = askable[start:start + _BATCH]
                    res = await asyncio.gather(
                        *[_fetch_region_volume(client, region_id, t) for t in batch],
                        return_exceptions=True,
                    )
                    batch_rows = []
                    for tid, r in zip(batch, res):
                        v = r if isinstance(r, int) else None
                        history_map[tid] = v
                        batch_rows.append((v, structure_id, tid))
                    # Committed per batch (~300 types), not once at the end: the
                    # whole point of the fast-phase write above is that partial
                    # work survives an interruption, and this is where the slow
                    # phase actually does its waiting.
                    conn.executemany(
                        "UPDATE station_volume_cache SET traded_volume=?"
                        " WHERE location_id=? AND type_id=?", batch_rows)
                    conn.commit()
                    # ETags per batch as well. Flushing only at the end meant an
                    # interrupted load threw away every ETag it had collected, so
                    # the next attempt re-fetched full bodies for work already
                    # done - measured: a sweep cancelled after 4 of 6 batches
                    # re-requested all 1 817 histories instead of the 565 left.
                    flush_hist_etags(conn, clear=False)
                    done += len(batch)
                    if progress_cb:
                        try:
                            progress_cb(done, total)
                        except Exception:
                            pass
            flush_hist_etags(conn)
        elif progress_cb:
            # Nothing left to fetch - the cache had it all. Report done, or the
            # bar sits at whatever the price phase left behind.
            try:
                progress_cb(len(our_type_ids), len(our_type_ids))
            except Exception:
                pass

    now = time.time()
    result: dict[int, tuple[int | None, float | None, int | None]] = {}
    rows = []
    for tid in our_type_ids:
        entry = aggregated.get(tid)
        vol = entry["volume"] if entry else 0
        sell = entry["best_sell"] if entry else None
        traded = history_map.get(tid)
        result[tid] = (vol, sell, traded)
        rows.append((structure_id, tid, vol, sell, traded, now))

    # Redundant with the incremental writes above for a completed run (both
    # phases already landed row by row) - kept as the single source of truth
    # for what THIS call returns, and it is what actually persists the reuse
    # branch's result (which never wrote to the table on its own).
    conn.executemany(
        "INSERT OR REPLACE INTO station_volume_cache (location_id, type_id, volume, best_sell, traded_volume, cached_at) VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return result


async def _fetch_orders_for_type(
    client: httpx.AsyncClient,
    region_id: int,
    location_id: int,
    type_id: int,
) -> tuple[int | None, float | None]:
    """Returns (volume_sum, best_sell) for the given type at a specific station."""
    async with _STATION_SEM:
        try:
            r = await client.get(
                f"{ESI_BASE}/markets/{region_id}/orders/",
                params={"type_id": type_id, "order_type": "sell", "datasource": "tranquility"},
                timeout=15,
            )
            if r.status_code != 200:
                return None, None
        except Exception:
            return None, None

    orders = [o for o in r.json() if o.get("location_id") == location_id]
    if not orders:
        return 0, None
    volume = sum(o.get("volume_remain", 0) for o in orders)
    best_sell = min(o["price"] for o in orders)
    return volume, best_sell


@foreground
async def fetch_station_volumes(
    conn: sqlite3.Connection,
    location_id: int,
    region_id: int,
    type_ids: list[int],
    progress_cb=None,
) -> dict[int, tuple[int | None, float | None, int | None]]:
    """Fetches and stores volumes+prices+history for all type_ids at the given NPC station."""
    ensure_price_table(conn)
    # Reuse stored history ETags: whatever this region already answered before
    # comes back as a bodyless 304 instead of ~42 KB of JSON per type.
    load_hist_etags(conn, region_id)

    # Phase A (prices): two strategies depending on the number of types.
    #  - few types → per-type calls (light, no 94MB region download); suitable
    #    for plan sell price (1 type).
    #  - many types → bulk regional orders once + station filter (~2 s
    #    instead of ~37 s); crossover ~1000 types (bulk has a fixed ~2 s + 94MB overhead).
    order_map: dict[int, tuple] = {}
    if len(type_ids) >= _BULK_ORDERS_THRESHOLD:
        async with esi_client() as client:
            station_orders = await fetch_station_orders_bulk(client, region_id, location_id)
        for tid in type_ids:
            vs = station_orders.get(tid)
            # type with no sell order at the station → (0, None), consistent with per-type
            order_map[tid] = vs if vs is not None else (0, None)
    else:
        async with esi_client() as client:
            order_tasks = [_fetch_orders_for_type(client, region_id, location_id, tid) for tid in type_ids]
            order_results = await asyncio.gather(*order_tasks, return_exceptions=True)
        for tid, res in zip(type_ids, order_results):
            order_map[tid] = res if isinstance(res, tuple) else (None, None)

    # Persist the fast phase (this station's own orders) before touching region
    # history, same reasoning as fetch_structure_market: a load interrupted
    # partway through the slow phase below must not lose the fast one.
    now0 = time.time()
    conn.executemany(
        "INSERT OR REPLACE INTO station_volume_cache (location_id, type_id, volume,"
        " best_sell, traded_volume, cached_at) VALUES (?,?,?,?,?,?)",
        [(location_id, tid, *order_map.get(tid, (None, None)), None, now0)
         for tid in type_ids],
    )
    conn.commit()

    # The 7-day regional volume.
    history_map: dict[int, int | None] = {}
    if type_ids:
        # Same shape as the structure path, and the same fix: what the region
        # already has is a head start, never a substitute for asking about the
        # rest. Treating a non-empty map as full coverage was safe only while
        # Jita and the hubs were the only reusable regions - see the note in
        # fetch_structure_market for what it cost once that stopped being true.
        reuse = _cached_region_volume(conn, region_id) or {}
        history_map = {tid: reuse[tid] for tid in type_ids if tid in reuse}

        # Only types with an order at THIS station, minus whatever the cache
        # already answered. A type traded elsewhere in the region but not sold
        # here keeps a blank vol/7d until something warms the region.
        tids = [t for t in type_ids
                if order_map.get(t, (None, None))[1] is not None and t not in reuse]
        askable = _types_with_market_history(conn, tids)
        skipped = len(type_ids) - len(askable) - len(history_map)
        if skipped > 0:
            print(f"[prices] station volume sweep: skipping {skipped} type(s) "
                  f"not sold here or with no market history", flush=True)
        if askable:
            total = len(askable)
            done = 0
            _BATCH = 300   # report progress every 300 types (this phase is the slow one)
            async with esi_client() as client:
                for start in range(0, total, _BATCH):
                    batch = askable[start:start + _BATCH]
                    res = await asyncio.gather(
                        *[_fetch_region_volume(client, region_id, t) for t in batch],
                        return_exceptions=True,
                    )
                    batch_rows = []
                    for tid, r in zip(batch, res):
                        v = r if isinstance(r, int) else None
                        history_map[tid] = v
                        batch_rows.append((v, location_id, tid))
                    conn.executemany(
                        "UPDATE station_volume_cache SET traded_volume=?"
                        " WHERE location_id=? AND type_id=?", batch_rows)
                    conn.commit()
                    flush_hist_etags(conn, clear=False)   # see the structure path
                    done += len(batch)
                    if progress_cb:
                        try:
                            progress_cb(done, total)
                        except Exception:
                            pass
        elif progress_cb:
            try:
                progress_cb(len(type_ids), len(type_ids))
            except Exception:
                pass

    now = time.time()
    rows = []
    result_map: dict[int, tuple[int | None, float | None, int | None]] = {}
    for tid in type_ids:
        vol, sell = order_map.get(tid, (None, None))
        traded = history_map.get(tid)
        rows.append((location_id, tid, vol, sell, traded, now))
        result_map[tid] = (vol, sell, traded)

    # Redundant with the incremental writes above for a completed run - kept as
    # the single source of truth for what this call returns, and it is what
    # persists the reuse branch's result (which never wrote on its own).
    conn.executemany(
        "INSERT OR REPLACE INTO station_volume_cache (location_id, type_id, volume, best_sell, traded_volume, cached_at) VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    flush_hist_etags(conn)
    return result_map


def get_cached_station_volumes(
    conn: sqlite3.Connection,
    location_id: int,
) -> dict[int, tuple[int | None, float | None, int | None]] | None:
    """Returns cached data if it is fresh, otherwise None."""
    rows = conn.execute(
        "SELECT type_id, volume, best_sell, traded_volume, cached_at FROM station_volume_cache WHERE location_id=?",
        (location_id,)
    ).fetchall()
    if not rows:
        return None
    now = time.time()
    if any((now - (r[4] or 0)) > STATION_VOLUME_TTL for r in rows):
        return None
    # If there are records with volume>0 but all traded_volume are NULL,
    # the cache is incomplete (the region was unknown at save time) - force a
    # refetch. Note this does NOT catch every incomplete state any more: a
    # completed sweep now legitimately leaves traded_volume NULL for most types
    # (only ones actually listed here get a 7-day volume - see
    # fetch_structure_market), so "some nulls" is normal, not a signal on its
    # own. What this still catches is the narrower, worse case it was written
    # for - nothing was even attempted.
    has_stock = any(r[1] and r[1] > 0 for r in rows)
    all_traded_null = all(r[3] is None for r in rows)
    if has_stock and all_traded_null:
        return None
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def get_station_volumes_any_age(
    conn: sqlite3.Connection,
    location_id: int,
) -> tuple[dict[int, tuple[int | None, float | None, int | None]], float] | None:
    """Cached station volumes regardless of age - for restoring a previously
    loaded custom station on page load (like the never-expiring Jita cache).
    Returns (data, newest_cached_at) or None if nothing is cached."""
    rows = conn.execute(
        "SELECT type_id, volume, best_sell, traded_volume, cached_at FROM station_volume_cache WHERE location_id=?",
        (location_id,)
    ).fetchall()
    if not rows:
        return None
    data = {r[0]: (r[1], r[2], r[3]) for r in rows}
    cached_at = max((r[4] or 0) for r in rows)
    return data, cached_at
