"""
Public contracts - per-region index into a SQLite cache + local full-text search.

Fetches ALL public contracts of the chosen region (metadata) and their items
(1 call/contract), stores them in the cache, and then anything can be searched
over it (by item, type, price) without further ESI calls. See the discussion: the
only way to search by item, because the metadata listing does not contain items and
the `title` is usually empty.
"""
from __future__ import annotations
import asyncio
import json as _json
import sqlite3
import time

from app.character import contracts as contracts_api
from app.esi.client import esi_client


def ensure_public_contract_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS public_contract_meta (
            region_id      INTEGER PRIMARY KEY,
            indexed_at     REAL,
            contract_count INTEGER
        );
        CREATE TABLE IF NOT EXISTS public_contracts (
            contract_id       INTEGER PRIMARY KEY,
            region_id         INTEGER,
            type              TEXT,
            price             REAL,
            reward            REAL,
            collateral        REAL,
            buyout            REAL,
            volume            REAL,
            date_expired      TEXT,
            title             TEXT,
            start_location_id INTEGER,
            end_location_id   INTEGER,
            issuer_id         INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_pc_region ON public_contracts(region_id);
        CREATE TABLE IF NOT EXISTS public_contract_items (
            contract_id  INTEGER,
            type_id      INTEGER,
            quantity     INTEGER,
            is_included  INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_pci_contract ON public_contract_items(contract_id);
        CREATE INDEX IF NOT EXISTS idx_pci_type ON public_contract_items(type_id);
        CREATE INDEX IF NOT EXISTS idx_pc_volume ON public_contracts(volume DESC);
        -- Contracts whose contents ESI will not hand over (gone since the listing).
        CREATE TABLE IF NOT EXISTS public_contract_items_absent (
            contract_id INTEGER PRIMARY KEY,
            at          REAL
        );
        -- station -> solar system. Static data, so cached forever. 99.5 % of public
        -- contracts sit in NPC stations (measured: 97 distinct stations across all of
        -- The Forge), which is what makes the sovereignty rule below cheap.
        CREATE TABLE IF NOT EXISTS station_system_cache (
            station_id INTEGER PRIMARY KEY,
            system_id  INTEGER,
            cached_at  REAL
        );
        -- /sovereignty/map: alliance_id set = held by a player alliance.
        CREATE TABLE IF NOT EXISTS sov_map_cache (
            system_id   INTEGER PRIMARY KEY,
            alliance_id INTEGER,
            faction_id  INTEGER,
            cached_at   REAL
        );
    """)
    # system_id on the contract itself, so the sovereignty filter is one join.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(public_contracts)").fetchall()}
    if "system_id" not in cols:
        conn.execute("ALTER TABLE public_contracts ADD COLUMN system_id INTEGER")
    conn.commit()


# ── Public contracts, all of known space ──────────────────────────────────────
#
# Measured 2026-08-22, and it reshapes the whole design:
#   * listing every region costs 106 requests and about half a second (70 k-space
#     regions, ~51 000 contracts). There is no global endpoint, but iterating the
#     regions is so cheap that the user never needs to pick one.
#   * CONTENTS are the expensive half: one call per contract. But they are only
#     needed to answer "what is in there", and the things that need a contract price
#     at all - capitals, supercapitals - are the biggest contracts in the game. In
#     The Forge, 84 of 33 243 priced item-exchange contracts are >= 1 000 000 m3.
#     So contents are fetched biggest-first, and the capital band is covered in
#     seconds instead of the three minutes a full index takes.
_PUBLIC_LIST_SEM = asyncio.Semaphore(20)
_PUBLIC_ITEM_SEM = asyncio.Semaphore(30)      # unauthenticated: no ESI token bucket
_SOV_TTL = 6 * 3600
# /sovereignty/map only exists under an older compatibility date; ours (2026-07-17)
# returns 404 for it. Asking for one endpoint with an explicit older date is exactly
# what date-based versioning is for - but it has to be deliberate and visible.
_SOV_COMPAT_DATE = "2020-01-01"


async def refresh_sov_map(conn: sqlite3.Connection, force: bool = False) -> int:
    """Cache which systems a player alliance holds sovereignty in.

    One call, ~375 kB, 8490 systems, ESI caches it for an hour. `alliance_id` set
    means a player alliance holds it; `faction_id` means NPC space (highsec, lowsec
    and NPC null alike). That distinction is the whole point: a contract in someone's
    sov staging is not an open market and would distort a capital's price, while NPC
    null is a perfectly ordinary place to buy one.
    """
    ensure_public_contract_tables(conn)
    if not force:
        row = conn.execute("SELECT MAX(cached_at) FROM sov_map_cache").fetchone()
        if row and row[0] and (time.time() - row[0]) < _SOV_TTL:
            return 0
    try:
        async with esi_client(timeout=30) as client:
            r = await client.get("https://esi.evetech.net/sovereignty/map",
                                 headers={"X-Compatibility-Date": _SOV_COMPAT_DATE})
        if r.status_code != 200:
            return 0
        data = r.json() or []
    except Exception:
        return 0
    now = time.time()
    conn.execute("DELETE FROM sov_map_cache")
    conn.executemany(
        "INSERT OR REPLACE INTO sov_map_cache (system_id, alliance_id, faction_id, cached_at)"
        " VALUES (?,?,?,?)",
        [(e.get("system_id"), e.get("alliance_id"), e.get("faction_id"), now)
         for e in data if e.get("system_id")])
    conn.commit()
    return len(data)


# A structure we cannot read is a structure we probably cannot dock at, so its
# contracts are not a market we could trade in. Worth retrying occasionally though:
# access changes.
_STRUCT_RETRY_TTL = 7 * 24 * 3600


async def resolve_station_systems(conn: sqlite3.Connection, station_ids,
                                  token: str | None = None) -> dict[int, int]:
    """location_id -> system_id, cached (station data is static).

    NPC stations resolve for free and cover 99.5 % of public contracts (measured).
    The remaining 0.5 % are player structures - and they matter out of proportion,
    because capitals are traded in Keepstars: of the single-item contracts in one
    real run, 34 of 41 sat in structures, including a 145b titan. Those need a token
    with esi-universe.read_structures.v1, and about half answer 403 (no docking
    access). A 403 is left unresolved on purpose: somewhere we cannot dock is not a
    price we could act on.
    """
    ensure_public_contract_tables(conn)
    ids = {int(s) for s in station_ids if s}
    if not ids:
        return {}
    out: dict[int, int] = {}
    miss: list[int] = []
    for sid in ids:
        row = conn.execute("SELECT system_id FROM station_system_cache WHERE station_id=?",
                           (sid,)).fetchone()
        if row and row[0]:
            out[sid] = row[0]
        else:
            miss.append(sid)
    # Structures whose last attempt failed are retried only occasionally.
    if miss:
        stale = []
        for sid in list(miss):
            if sid < 1_000_000_000_000:
                continue
            row = conn.execute("SELECT system_id, cached_at FROM station_system_cache"
                               " WHERE station_id=?", (sid,)).fetchone()
            if row and not row[0] and (time.time() - (row[1] or 0)) < _STRUCT_RETRY_TTL:
                miss.remove(sid)          # known-inaccessible, not yet worth retrying
            elif row and not row[0]:
                stale.append(sid)

        async def _one(client, sid):
            try:
                if sid >= 1_000_000_000_000:
                    if not token:
                        return sid, None
                    r = await client.get(
                        f"https://esi.evetech.net/latest/universe/structures/{sid}",
                        headers={"Authorization": f"Bearer {token}"}, timeout=10)
                    if r.status_code == 200:
                        return sid, (r.json() or {}).get("solar_system_id")
                    return sid, None
                r = await client.get(
                    f"https://esi.evetech.net/latest/universe/stations/{sid}", timeout=10)
                if r.status_code == 200:
                    return sid, (r.json() or {}).get("system_id")
            except Exception:
                pass
            return sid, None
        try:
            async with esi_client(timeout=15) as client:
                for sid, sysid in await asyncio.gather(*[_one(client, m) for m in miss]):
                    conn.execute(
                        "INSERT OR REPLACE INTO station_system_cache (station_id, system_id,"
                        " cached_at) VALUES (?,?,?)", (sid, sysid, time.time()))
                    if sysid:
                        out[sid] = sysid
            conn.commit()
        except Exception:
            pass
    return out


def _store_public_listing(conn: sqlite3.Connection, region_id: int, contracts: list[dict],
                          systems: dict[int, int]) -> None:
    """Replace one region's listing. Items are NOT touched: they are immutable and
    keyed by contract_id, so what we already fetched stays valid."""
    ensure_public_contract_tables(conn)
    conn.execute("DELETE FROM public_contracts WHERE region_id=?", (region_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO public_contracts (contract_id, region_id, type, price,"
        " reward, collateral, buyout, volume, date_expired, title, start_location_id,"
        " end_location_id, issuer_id, system_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(c.get("contract_id"), region_id, c.get("type"), c.get("price") or 0,
          c.get("reward") or 0, c.get("collateral") or 0, c.get("buyout") or 0,
          c.get("volume") or 0, c.get("date_expired", ""), c.get("title") or "",
          c.get("start_location_id"), c.get("end_location_id"), c.get("issuer_id"),
          systems.get(int(c.get("start_location_id") or 0)))
         for c in contracts if c.get("contract_id")])
    conn.execute(
        "INSERT OR REPLACE INTO public_contract_meta (region_id, indexed_at, contract_count)"
        " VALUES (?,?,?)", (region_id, time.time(), len(contracts)))
    # Items of contracts that are gone from every region's listing are dead weight.
    conn.execute("DELETE FROM public_contract_items WHERE contract_id NOT IN"
                 " (SELECT contract_id FROM public_contracts)")
    conn.execute("DELETE FROM public_contract_items_absent WHERE contract_id NOT IN"
                 " (SELECT contract_id FROM public_contracts)")
    conn.commit()


async def refresh_public_listings(conn: sqlite3.Connection, region_ids,
                                  progress=None, token: str | None = None) -> dict:
    """Re-list the given regions (all of known space by default) and store them.

    Cheap enough to do wholesale: 106 requests for every region that has contracts.
    """
    ensure_public_contract_tables(conn)
    await refresh_sov_map(conn)
    regions = [int(r) for r in region_ids]
    total = {"regions": 0, "contracts": 0}

    async def _region(client, rid):
        async with _PUBLIC_LIST_SEM:
            try:
                r = await client.get(
                    f"https://esi.evetech.net/latest/contracts/public/{rid}/",
                    params={"page": 1}, timeout=20)
            except Exception:
                return rid, None
            if r.status_code != 200:
                return rid, None
            pages = int(r.headers.get("x-pages", 1))
            out = list(r.json() or [])
        rest = await asyncio.gather(*[
            client.get(f"https://esi.evetech.net/latest/contracts/public/{rid}/",
                       params={"page": p}, timeout=20) for p in range(2, pages + 1)
        ], return_exceptions=True)
        for rr in rest:
            if not isinstance(rr, Exception) and rr.status_code == 200:
                out.extend(rr.json() or [])
        return rid, out

    async with esi_client(timeout=25) as client:
        results = await asyncio.gather(*[_region(client, r) for r in regions],
                                       return_exceptions=True)
    done = 0
    for res in results:
        done += 1
        if isinstance(res, Exception):
            continue
        rid, contracts = res
        if contracts is None:
            continue
        stations = {int(c.get("start_location_id") or 0) for c in contracts}
        systems = await resolve_station_systems(conn, stations, token=token)
        _store_public_listing(conn, rid, contracts, systems)
        total["regions"] += 1
        total["contracts"] += len(contracts)
        if progress:
            progress(done, len(regions), total["contracts"])
    return total


# Measured against the live endpoint: concurrency 30 gives ~450 req/s and MORE
# concurrency is slower (60 -> 228, 120 -> 136 req/s), the same saturation shape as
# the market history endpoint. So the pace comes from processing a lot per pass, not
# from more sockets: 48 000 contents is ~110 s of actual fetching.
_PUBLIC_ITEM_CHUNK = 1500


async def fill_public_items(conn: sqlite3.Connection, budget: int = 12000,
                            progress=None) -> dict:
    """Fetch contents for contracts we do not have yet, BIGGEST VOLUME FIRST.

    That ordering is the whole trick: the items that need a contract price at all are
    capitals, and capitals are the biggest contracts there are. The long tail (BPCs,
    single modules - 98 % of the rows, mostly 0 m3) keeps filling afterwards.
    """
    ensure_public_contract_tables(conn)
    todo = [r[0] for r in conn.execute(
        "SELECT c.contract_id FROM public_contracts c"
        " WHERE c.type IN ('item_exchange','auction') AND c.price > 0"
        "   AND NOT EXISTS (SELECT 1 FROM public_contract_items i"
        "                    WHERE i.contract_id = c.contract_id)"
        "   AND NOT EXISTS (SELECT 1 FROM public_contract_items_absent a"
        "                    WHERE a.contract_id = c.contract_id)"
        " ORDER BY c.volume DESC LIMIT ?", (budget,)).fetchall()]
    if not todo:
        return {"fetched": 0, "gone": 0, "remaining": 0}
    stored = [0]
    done = [0]
    gone: list[int] = []
    batch: dict[int, list[dict]] = {}
    lock = asyncio.Lock()

    async def _one(client, cid):
        async with _PUBLIC_ITEM_SEM:
            try:
                r = await client.get(
                    f"https://esi.evetech.net/latest/contracts/public/items/{cid}/", timeout=15)
            except Exception:
                return
        async with lock:
            done[0] += 1
            if progress and done[0] % 50 == 0:
                progress(done[0], len(todo))
            # 404 = the contract is gone. 403 happens occasionally on this public
            # endpoint too; both are recorded so the tail cannot become infinite,
            # and "Refresh now" clears the record if it was a hiccup.
            if r.status_code in (403, 404):
                gone.append(cid)
                return
            if r.status_code != 200:
                return
            try:
                batch[cid] = r.json() or []
            except Exception:
                return
            if len(batch) >= 300:
                flush = dict(batch)
                batch.clear()
                _store_public_items(conn, flush)
                stored[0] += len(flush)

    async with esi_client(timeout=20) as client:
        # Chunked so the flushes land steadily and the page's progress moves, while
        # one call still gets through thousands of contracts.
        for i in range(0, len(todo), _PUBLIC_ITEM_CHUNK):
            chunk = todo[i:i + _PUBLIC_ITEM_CHUNK]
            await asyncio.gather(*[_one(client, c) for c in chunk], return_exceptions=True)
            if batch:
                flush = dict(batch)
                batch.clear()
                _store_public_items(conn, flush)
                stored[0] += len(flush)
            if gone:
                _mark_public_absent(conn, gone)
                gone.clear()
    if batch:
        _store_public_items(conn, batch)
        stored[0] += len(batch)
    if gone:
        _mark_public_absent(conn, gone)
    remaining = conn.execute(
        "SELECT COUNT(*) FROM public_contracts c"
        " WHERE c.type IN ('item_exchange','auction') AND c.price > 0"
        "   AND NOT EXISTS (SELECT 1 FROM public_contract_items i"
        "                    WHERE i.contract_id = c.contract_id)"
        "   AND NOT EXISTS (SELECT 1 FROM public_contract_items_absent a"
        "                    WHERE a.contract_id = c.contract_id)").fetchone()[0]
    return {"fetched": stored[0], "gone": len(gone), "remaining": remaining}


def _mark_public_absent(conn: sqlite3.Connection, contract_ids) -> None:
    ids = list(contract_ids or [])
    if not ids:
        return
    now = time.time()
    conn.executemany("INSERT OR REPLACE INTO public_contract_items_absent"
                     " (contract_id, at) VALUES (?,?)", [(c, now) for c in ids])
    conn.commit()


def clear_public_absent(conn: sqlite3.Connection) -> int:
    """Forget which contracts refused their contents, so a manual refresh retries
    them. A 403 on this endpoint is sometimes just a hiccup."""
    ensure_public_contract_tables(conn)
    n = conn.execute("SELECT COUNT(*) FROM public_contract_items_absent").fetchone()[0]
    conn.execute("DELETE FROM public_contract_items_absent")
    conn.commit()
    return n


def _store_public_items(conn: sqlite3.Connection, items_by_cid: dict[int, list[dict]]) -> None:
    rows = []
    for cid, items in items_by_cid.items():
        for it in items or []:
            if it.get("type_id"):
                rows.append((cid, it["type_id"], it.get("quantity") or 0,
                             1 if it.get("is_included", True) else 0))
    if not rows:
        return
    cids = list(items_by_cid)
    ph = ",".join("?" * len(cids))
    conn.execute(f"DELETE FROM public_contract_items WHERE contract_id IN ({ph})", cids)
    conn.executemany(
        "INSERT INTO public_contract_items (contract_id, type_id, quantity, is_included)"
        " VALUES (?,?,?,?)", rows)
    conn.commit()


def public_index_status(conn: sqlite3.Connection) -> dict:
    """One global picture instead of per-region bookkeeping."""
    ensure_public_contract_tables(conn)
    row = conn.execute(
        "SELECT COUNT(*), MIN(indexed_at), MAX(indexed_at) FROM public_contract_meta").fetchone()
    total = conn.execute("SELECT COUNT(*) FROM public_contracts").fetchone()[0]
    priced = conn.execute(
        "SELECT COUNT(*) FROM public_contracts WHERE type IN ('item_exchange','auction')"
        " AND price > 0").fetchone()[0]
    with_items = conn.execute(
        "SELECT COUNT(DISTINCT contract_id) FROM public_contract_items").fetchone()[0]
    return {"regions": row[0] or 0, "oldest": row[1], "newest": row[2],
            "contracts": total, "priced": priced, "with_items": with_items}


def get_index_status(conn: sqlite3.Connection, region_id: int) -> dict | None:
    ensure_public_contract_tables(conn)
    row = conn.execute(
        "SELECT indexed_at, contract_count FROM public_contract_meta WHERE region_id=?",
        (region_id,),
    ).fetchone()
    if not row:
        return None
    return {"indexed_at": row[0], "contract_count": row[1]}


def _store(conn: sqlite3.Connection, region_id: int, contracts: list[dict],
           items_by_cid: dict[int, list[dict]]) -> None:
    ensure_public_contract_tables(conn)
    cids = [c["contract_id"] for c in contracts if c.get("contract_id")]
    # delete the region's old index
    conn.execute("DELETE FROM public_contracts WHERE region_id=?", (region_id,))
    if cids:
        ph = ",".join("?" * len(cids))
        conn.execute(f"DELETE FROM public_contract_items WHERE contract_id IN ({ph})", cids)
    conn.executemany(
        "INSERT OR REPLACE INTO public_contracts (contract_id, region_id, type, price, "
        "reward, collateral, buyout, volume, date_expired, title, start_location_id, "
        "end_location_id, issuer_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(c.get("contract_id"), region_id, c.get("type"), c.get("price") or 0,
          c.get("reward") or 0, c.get("collateral") or 0, c.get("buyout") or 0,
          c.get("volume") or 0, c.get("date_expired", ""), c.get("title") or "",
          c.get("start_location_id"), c.get("end_location_id"), c.get("issuer_id"))
         for c in contracts],
    )
    item_rows = []
    for cid, items in items_by_cid.items():
        for it in items:
            if it.get("type_id"):
                item_rows.append((cid, it["type_id"], it.get("quantity", 0),
                                  1 if it.get("is_included", True) else 0))
    if item_rows:
        conn.executemany(
            "INSERT INTO public_contract_items (contract_id, type_id, quantity, is_included) "
            "VALUES (?,?,?,?)", item_rows)
    conn.execute(
        "INSERT OR REPLACE INTO public_contract_meta (region_id, indexed_at, contract_count) "
        "VALUES (?,?,?)", (region_id, time.time(), len(contracts)))
    conn.commit()


async def stream_public_index(conn: sqlite3.Connection, region_id: int):
    """SSE generator: fetch the listing (pages) + items (per contract) and store them."""
    ensure_public_contract_tables(conn)
    total_pages = [0]
    done_pages = [0]
    holder: dict = {}

    def _list_prog(done, total):
        done_pages[0] = done
        total_pages[0] = total

    async def _run_list():
        async with esi_client() as client:
            holder["list"] = await contracts_api.fetch_public_contracts(
                client, region_id, progress_cb=_list_prog)

    task = asyncio.create_task(_run_list())
    while not task.done():
        tp = total_pages[0]
        pct = int(done_pages[0] * 40 / tp) if tp else 0
        yield f"data: {_json.dumps({'phase':'list','done':done_pages[0],'total':tp,'pct':pct})}\n\n"
        await asyncio.sleep(0.4)
    await task
    contracts = holder.get("list", [])

    # Items only for types with contents (courier/loan usually have no items).
    item_contracts = [c for c in contracts if c.get("type") in ("item_exchange", "auction")]
    total_items = len(item_contracts)
    done_items = [0]
    items_by_cid: dict[int, list[dict]] = {}
    lock = asyncio.Lock()

    async def _one(client, c):
        its = await contracts_api.fetch_public_contract_items(client, c["contract_id"])
        async with lock:
            if its:
                items_by_cid[c["contract_id"]] = its
            done_items[0] += 1

    async def _run_items():
        async with esi_client() as client:
            await asyncio.gather(*[_one(client, c) for c in item_contracts],
                                 return_exceptions=True)

    yield f"data: {_json.dumps({'phase':'items','done':0,'total':total_items,'pct':40})}\n\n"
    task2 = asyncio.create_task(_run_items())
    while not task2.done():
        pct = 40 + (int(done_items[0] * 55 / total_items) if total_items else 55)
        yield f"data: {_json.dumps({'phase':'items','done':done_items[0],'total':total_items,'pct':pct})}\n\n"
        await asyncio.sleep(0.4)
    await task2

    _store(conn, region_id, contracts, items_by_cid)
    yield f"data: {_json.dumps({'done':True,'pct':100,'contract_count':len(contracts)})}\n\n"


def search_public_contracts(conn: sqlite3.Connection, region_id: int | None = None, *,
                            item: str = "", exact_item: bool = False, q: str = "",
                            ctype: str = "", min_price: float | None = None,
                            max_price: float | None = None,
                            min_reward: float | None = None,
                            max_collateral: float | None = None,
                            max_volume: float | None = None, location: str = "",
                            issuer: str = "", title: str = "",
                            expires_days: int | None = None, sort: str = "price",
                            limit: int = 300) -> tuple[list[dict], int]:
    """Search the indexed public contracts - the same filters the other contract
    views have, so the four of them read the same way.

    `region_id=None` searches all of known space (it is all indexed, so a region is
    a filter now). Location and issuer are matched against the names we have cached
    locally; nothing is fetched to answer a filter.

    Returns (rows, total_matches).
    """
    ensure_public_contract_tables(conn)
    where: list[str] = []
    params: list = []
    joins = ""
    if region_id:
        where.append("c.region_id = ?")
        params.append(region_id)
    if item.strip():
        joins += (" JOIN public_contract_items i ON i.contract_id = c.contract_id"
                  " JOIN sde_types t ON t.type_id = i.type_id")
        if exact_item:
            where.append("LOWER(t.name) = ?")
            params.append(item.strip().lower())
        else:
            where.append("t.name LIKE ?")
            params.append(f"%{item.strip()}%")
        where.append("i.is_included = 1")
    if location.strip() or q.strip():
        joins += (" LEFT JOIN location_name_cache ls ON ls.location_id = c.start_location_id"
                  " LEFT JOIN location_name_cache le ON le.location_id = c.end_location_id")
    if issuer.strip() or q.strip():
        joins += " LEFT JOIN party_name_cache pn ON pn.party_id = c.issuer_id"
    if q.strip():
        where.append("(c.title LIKE ? OR ls.name LIKE ? OR le.name LIKE ? OR pn.name LIKE ?)")
        params += [f"%{q.strip()}%"] * 4
    if ctype:
        where.append("c.type = ?")
        params.append(ctype)
    if min_price is not None:
        where.append("c.price >= ?")
        params.append(min_price)
    if max_price is not None:
        where.append("c.price <= ?")
        params.append(max_price)
    if min_reward is not None:
        where.append("c.reward >= ?")
        params.append(min_reward)
    if max_collateral is not None:
        where.append("c.collateral <= ?")
        params.append(max_collateral)
    if max_volume is not None:
        where.append("c.volume <= ?")
        params.append(max_volume)
    if location.strip():
        where.append("(ls.name LIKE ? OR le.name LIKE ?)")
        params += [f"%{location.strip()}%"] * 2
    if issuer.strip():
        where.append("pn.name LIKE ?")
        params.append(f"%{issuer.strip()}%")
    if title.strip():
        where.append("c.title LIKE ?")
        params.append(f"%{title.strip()}%")
    if expires_days is not None:
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                               time.gmtime(time.time() + expires_days * 86400))
        where.append("c.date_expired <= ?")
        params.append(cutoff)
    cond = " AND ".join(where) if where else "1=1"
    order = {"price": "c.price ASC", "price_hi": "c.price DESC",
             "expires": "c.date_expired ASC", "reward": "c.reward DESC",
             "issued": "c.date_expired DESC"}.get(sort, "c.price ASC")
    total = conn.execute(
        f"SELECT COUNT(DISTINCT c.contract_id) FROM public_contracts c{joins} WHERE {cond}",
        params).fetchone()[0]
    cols = ["contract_id", "type", "price", "reward", "collateral", "volume",
            "date_expired", "title", "start_location_id", "end_location_id", "issuer_id",
            "region_id"]
    sel = ", ".join(f"c.{c}" for c in cols)
    rows = conn.execute(
        f"SELECT DISTINCT {sel} FROM public_contracts c{joins} WHERE {cond}"
        f" ORDER BY {order} LIMIT ?", params + [limit]).fetchall()
    return [dict(zip(cols, r)) for r in rows], total


def best_contract_price(conn: sqlite3.Connection, region_id: int, type_id: int) -> dict | None:
    """Cheapest price/unit of a product from public item_exchange contracts in the region.
    Prefers single-item contracts (clean price/unit); if there is none, it takes a
    bundle (multiple items) and marks is_bundle=True (the price/unit is then only
    indicative - it also covers the other items in the bundle). Returns None if the product is nowhere."""
    ensure_public_contract_tables(conn)
    rows = conn.execute("""
        SELECT c.contract_id, c.price, pi.quantity,
               (SELECT COUNT(*) FROM public_contract_items x
                 WHERE x.contract_id = c.contract_id AND x.is_included = 1) AS incl
        FROM public_contracts c
        JOIN public_contract_items pi ON pi.contract_id = c.contract_id
        WHERE c.region_id = ? AND c.type = 'item_exchange' AND c.price > 0
          AND pi.type_id = ? AND pi.is_included = 1
    """, (region_id, type_id)).fetchall()
    singles: list[tuple[float, int]] = []
    bundles: list[tuple[float, int]] = []
    for cid, price, qty, incl in rows:
        if not qty or qty <= 0:
            continue
        per_unit = price / qty
        (singles if incl == 1 else bundles).append((per_unit, cid))
    if singles:
        per_unit, cid = min(singles)
        return {"price": per_unit, "is_bundle": False, "contract_id": cid,
                "single_count": len(singles), "bundle_count": len(bundles)}
    if bundles:
        per_unit, cid = min(bundles)
        return {"price": per_unit, "is_bundle": True, "contract_id": cid,
                "single_count": 0, "bundle_count": len(bundles)}
    return None


def get_contract_items(conn: sqlite3.Connection, contract_id: int) -> list[dict]:
    ensure_public_contract_tables(conn)
    rows = conn.execute(
        "SELECT i.type_id, i.quantity, i.is_included, COALESCE(t.name, '#'||i.type_id) "
        "FROM public_contract_items i LEFT JOIN sde_types t ON t.type_id = i.type_id "
        "WHERE i.contract_id=?", (contract_id,)).fetchall()
    return [{"type_id": r[0], "quantity": r[1], "included": bool(r[2]), "name": r[3]}
            for r in rows]


# ── Alliance contracts ────────────────────────────────────────────────────────
#
# /corporations/{id}/contracts/ returns, beyond its own documentation, every
# contract assigned to that corporation's ALLIANCE - issued by any member corp.
# Measured 2026-08-22: two corps of one alliance returned the same 2912 contracts,
# assignee_id = the alliance, issuers = ~40 other corps, and the character endpoint
# returned none of them. `availability` is useless here (ESI says "personal" for all
# of them); assignee_id == alliance_id is the marker.
#
# Same shape as the public browser: index once into SQLite, then filter locally so
# a changed filter costs no ESI request. Issuer and location NAMES are resolved at
# index time and stored, which is what makes filtering by them plain SQL.

_ALLIANCE_ITEM_SEM = asyncio.Semaphore(20)

# /corporations/{id}/contracts/{cid}/items sits in the ESI token bucket group
# `corp-contract`: 600 tokens / 15 min, 2 tokens per 2xx = 300 calls. The bucket is
# per (application, CHARACTER) on authenticated routes, so every capable character is
# a separate 300-call allowance - measured 2026-08-22 on two characters whose
# x-ratelimit-remaining counted down independently. Contents never change, so the
# index resumes: each run spends this much per character and the next one continues.
_ITEM_CALLS_PER_TOKEN = 250
# Contents are written out in batches of this many contracts, so a run that is cut
# short (page closed, app quit) keeps everything it had already fetched.
_ITEM_FLUSH_EVERY = 100
# Consecutive-ish failures that mean "the rate limit is saying no". Stop rather than
# spend the rest of the run on requests that come back empty.
_ITEM_FAIL_LIMIT = 25
# Contents are only fetched for contracts somebody could still accept. Finished and
# deleted ones cannot be taken, so their contents answer no question anyone asks -
# and skipping them is what turns "three runs over 45 minutes" into a single run:
# on a real alliance that is 574 contracts instead of 2875.
_ITEM_STATUSES = ("outstanding",)
# The game server (not ESI) answers 520 ConStopSpamming when it has had enough for
# the moment and says for how long - seconds, measured at ~64. Sleep it off and keep
# going; only an exhausted ESI token bucket deserves waiting out a 15 minute window.
_THROTTLE_RETRIES = 3
_THROTTLE_MAX_WAIT = 90.0

def ensure_alliance_contract_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS alliance_contract_meta (
            alliance_id    INTEGER PRIMARY KEY,
            indexed_at     REAL,
            contract_count INTEGER
        );
        CREATE TABLE IF NOT EXISTS alliance_contracts (
            contract_id       INTEGER PRIMARY KEY,
            alliance_id       INTEGER,
            source_corp_id    INTEGER,
            type              TEXT,
            status            TEXT,
            price             REAL,
            reward            REAL,
            collateral        REAL,
            buyout            REAL,
            volume            REAL,
            date_issued       TEXT,
            date_expired      TEXT,
            days_to_complete  INTEGER,
            title             TEXT,
            start_location_id INTEGER,
            end_location_id   INTEGER,
            start_name        TEXT,
            end_name          TEXT,
            issuer_id         INTEGER,
            issuer_corp_id    INTEGER,
            issuer_name       TEXT,
            issuer_corp_name  TEXT,
            for_corp          INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_ac_alliance ON alliance_contracts(alliance_id);
        CREATE INDEX IF NOT EXISTS idx_ac_status ON alliance_contracts(status);
        CREATE TABLE IF NOT EXISTS alliance_contract_items (
            contract_id  INTEGER,
            type_id      INTEGER,
            quantity     INTEGER,
            is_included  INTEGER,
            is_bpc       INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_aci_contract ON alliance_contract_items(contract_id);
        CREATE INDEX IF NOT EXISTS idx_aci_type ON alliance_contract_items(type_id);
        -- Contracts whose contents ESI will never hand over (accepted, expired or
        -- deleted between the listing and the fetch). Kept out of the retry list so
        -- the filler cannot spin on them forever, and survives a re-listing.
        CREATE TABLE IF NOT EXISTS alliance_contract_items_absent (
            contract_id INTEGER PRIMARY KEY,
            at          REAL
        );
    """)
    conn.commit()


def indexed_alliances(conn: sqlite3.Connection) -> list[int]:
    """Alliances that have been indexed at least once - browsable without a token."""
    ensure_alliance_contract_tables(conn)
    return [r[0] for r in conn.execute(
        "SELECT alliance_id FROM alliance_contract_meta WHERE alliance_id IS NOT NULL"
    ).fetchall()]


def get_alliance_index_status(conn: sqlite3.Connection, alliance_id: int) -> dict | None:
    ensure_alliance_contract_tables(conn)
    row = conn.execute(
        "SELECT indexed_at, contract_count FROM alliance_contract_meta WHERE alliance_id=?",
        (alliance_id,)).fetchone()
    if not row:
        return None
    outstanding = conn.execute(
        "SELECT COUNT(*) FROM alliance_contracts WHERE alliance_id=? AND status='outstanding'",
        (alliance_id,)).fetchone()[0]
    items_for = conn.execute(
        "SELECT COUNT(DISTINCT contract_id) FROM alliance_contract_items WHERE contract_id IN"
        " (SELECT contract_id FROM alliance_contracts WHERE alliance_id=?)",
        (alliance_id,)).fetchone()[0]
    return {"indexed_at": row[0], "contract_count": row[1],
            "outstanding": outstanding, "with_items": items_for}


def alliance_item_coverage(conn: sqlite3.Connection, alliance_id: int) -> tuple[int, int]:
    """(open contracts that can have contents, how many of them have them).

    This is the pair worth showing: it is exactly how far an item search can see.
    """
    ensure_alliance_contract_tables(conn)
    ph = ",".join("?" * len(_ITEM_STATUSES))
    row = conn.execute(
        f"SELECT COUNT(*), SUM(EXISTS(SELECT 1 FROM alliance_contract_items i"
        f"                             WHERE i.contract_id = c.contract_id))"
        f" FROM alliance_contracts c"
        f" WHERE c.alliance_id=? AND c.type IN ('item_exchange','auction')"
        f"   AND c.status IN ({ph})"
        # Contracts that vanished cannot be searched and cannot be bought, so they
        # do not belong in "covers X of Y" - otherwise Y is never reachable.
        f"   AND NOT EXISTS (SELECT 1 FROM alliance_contract_items_absent a"
        f"                    WHERE a.contract_id = c.contract_id)",
        (alliance_id, *_ITEM_STATUSES)).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def contracts_missing_items(conn: sqlite3.Connection, alliance_id: int) -> list[int]:
    """Indexed item_exchange/auction contracts whose items were never fetched.
    Contract contents never change, so a reindex only has to fetch these."""
    ensure_alliance_contract_tables(conn)
    ph = ",".join("?" * len(_ITEM_STATUSES))
    return [r[0] for r in conn.execute(
        f"SELECT c.contract_id FROM alliance_contracts c"
        f" WHERE c.alliance_id=? AND c.type IN ('item_exchange','auction')"
        f"   AND c.status IN ({ph})"
        f"   AND NOT EXISTS (SELECT 1 FROM alliance_contract_items i"
        f"                    WHERE i.contract_id = c.contract_id)"
        f"   AND NOT EXISTS (SELECT 1 FROM alliance_contract_items_absent a"
        f"                    WHERE a.contract_id = c.contract_id)",
        (alliance_id, *_ITEM_STATUSES)).fetchall()]


def _store_alliance(conn: sqlite3.Connection, alliance_id: int, contracts: list[dict],
                    names: dict[int, str], loc_names: dict[int, str]) -> None:
    ensure_alliance_contract_tables(conn)
    keep = [c for c in contracts if c.get("contract_id")]
    conn.execute("DELETE FROM alliance_contracts WHERE alliance_id=?", (alliance_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO alliance_contracts (contract_id, alliance_id, source_corp_id,"
        " type, status, price, reward, collateral, buyout, volume, date_issued, date_expired,"
        " days_to_complete, title, start_location_id, end_location_id, start_name, end_name,"
        " issuer_id, issuer_corp_id, issuer_name, issuer_corp_name, for_corp)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(c.get("contract_id"), alliance_id, c.get("_corp_id") or 0,
          c.get("type") or "", c.get("status") or "",
          c.get("price") or 0.0, c.get("reward") or 0.0, c.get("collateral") or 0.0,
          c.get("buyout") or 0.0, c.get("volume") or 0.0,
          c.get("date_issued") or "", c.get("date_expired") or "",
          c.get("days_to_complete") or 0, c.get("title") or "",
          c.get("start_location_id") or 0, c.get("end_location_id") or 0,
          loc_names.get(c.get("start_location_id"), ""),
          loc_names.get(c.get("end_location_id"), ""),
          c.get("issuer_id") or 0, c.get("issuer_corporation_id") or 0,
          names.get(c.get("issuer_id"), ""),
          names.get(c.get("issuer_corporation_id"), ""),
          1 if c.get("for_corporation") else 0) for c in keep])
    # Drop items of contracts that fell out of the 30-day window, so the item table
    # stays bounded. Everything still listed keeps its items (they are immutable).
    conn.execute("DELETE FROM alliance_contract_items WHERE contract_id NOT IN"
                 " (SELECT contract_id FROM alliance_contracts)")
    conn.execute(
        "INSERT OR REPLACE INTO alliance_contract_meta (alliance_id, indexed_at, contract_count)"
        " VALUES (?,?,?)", (alliance_id, time.time(), len(keep)))
    conn.commit()


def _mark_items_absent(conn: sqlite3.Connection, contract_ids) -> None:
    ids = list(contract_ids or [])
    if not ids:
        return
    ensure_alliance_contract_tables(conn)
    now = time.time()
    conn.executemany(
        "INSERT OR REPLACE INTO alliance_contract_items_absent (contract_id, at)"
        " VALUES (?,?)", [(cid, now) for cid in ids])
    conn.commit()


def _store_alliance_items(conn: sqlite3.Connection, items_by_cid: dict[int, list[dict]]) -> None:
    ensure_alliance_contract_tables(conn)
    rows = []
    for cid, items in items_by_cid.items():
        for it in items:
            if it.get("type_id"):
                # There is no is_blueprint_copy on this endpoint: ESI marks a copy
                # with raw_quantity == -2 (-1 = original / non-stackable singleton).
                rows.append((cid, it["type_id"], it.get("quantity") or 0,
                             1 if it.get("is_included", True) else 0,
                             1 if it.get("raw_quantity") == -2 else 0))
    if not rows:
        return
    cids = list(items_by_cid)
    ph = ",".join("?" * len(cids))
    conn.execute(f"DELETE FROM alliance_contract_items WHERE contract_id IN ({ph})", cids)
    conn.executemany(
        "INSERT INTO alliance_contract_items (contract_id, type_id, quantity, is_included, is_bpc)"
        " VALUES (?,?,?,?,?)", rows)
    conn.commit()


async def list_and_store_alliance(conn: sqlite3.Connection, alliance_id: int,
                                   sources: list[tuple[int, str]],
                                   resolve_parties, resolve_locations) -> int:
    """Fetch the alliance's contract list through every corporation we can read and
    store it (names resolved first). Returns how many contracts are listed.

    Cheap: a few pages per corporation. The expensive half is the contents.
    """
    ensure_alliance_contract_tables(conn)
    found: dict[int, dict] = {}
    per_corp: dict[int, str] = {}
    for corp_id, token in sources:
        per_corp.setdefault(corp_id, token)
    for corp_id, token in per_corp.items():
        async with esi_client() as client:
            lst, _err = await contracts_api.fetch_corp_contracts(client, corp_id, token)
        for c in (lst or []):
            if c.get("assignee_id") != alliance_id:
                continue
            cid = c.get("contract_id")
            if cid and cid not in found:
                c["_corp_id"] = corp_id
                found[cid] = c
    contracts = list(found.values())
    if not contracts:
        return 0
    party_ids = {c[k] for c in contracts for k in ("issuer_id", "issuer_corporation_id")
                 if c.get(k)}
    loc_ids = {c[k] for c in contracts for k in ("start_location_id", "end_location_id")
               if c.get(k)}
    names = await resolve_parties(party_ids) if party_ids else {}
    loc_names = await resolve_locations(loc_ids) if loc_ids else {}
    _store_alliance(conn, alliance_id, contracts, names, loc_names)
    return len(contracts)


async def fill_alliance_items(conn: sqlite3.Connection, alliance_id: int,
                              sources: list[tuple[int, str]], progress=None) -> dict:
    """Fetch the missing contract contents, spread over every capable character.

    Stores in batches, so being interrupted costs nothing. Stops early when ESI
    starts refusing (the corp-contract bucket is 600 tokens / 15 min PER CHARACTER,
    2 per call) and says so, instead of spending the rest of the run on refusals.

    Returns {"fetched", "failed", "attempted", "remaining", "rate_limited"}.
    """
    ensure_alliance_contract_tables(conn)
    todo = contracts_missing_items(conn, alliance_id)
    if not todo:
        return {"fetched": 0, "failed": 0, "attempted": 0, "remaining": 0,
                "rate_limited": False}
    budget = _ITEM_CALLS_PER_TOKEN * max(1, len(sources))
    need, left_over = todo[:budget], max(0, len(todo) - budget)
    stored = [0]
    failed = [0]
    limited = [0]
    done = [0]
    batch: dict[int, list[dict]] = {}
    lock = asyncio.Lock()
    give_up = asyncio.Event()

    gone: list[int] = []

    async def _one(client, contract_id, corp_id, token):
        for attempt in range(_THROTTLE_RETRIES):
            if give_up.is_set():
                return
            async with _ALLIANCE_ITEM_SEM:
                res = await contracts_api.fetch_corp_contract_items(
                    client, corp_id, contract_id, token)
            if res.kind == "throttled":
                # The GAME server's throttle: it names its own wait, in seconds.
                # Sleeping it off and carrying on is the right answer - treating it
                # as an exhausted token bucket (a 15 minute stop) was not.
                await asyncio.sleep(max(1.0, min(_THROTTLE_MAX_WAIT, res.wait)))
                continue
            async with lock:
                if res.kind == "gone":
                    gone.append(contract_id)     # never coming; stop asking
                    done[0] += 1
                elif res.kind == "limited":
                    limited[0] += 1
                    give_up.set()
                elif res.kind == "error":
                    failed[0] += 1
                    if failed[0] >= _ITEM_FAIL_LIMIT:
                        give_up.set()
                else:
                    done[0] += 1
                    if res.items:
                        batch[contract_id] = res.items
                    if len(batch) >= _ITEM_FLUSH_EVERY:
                        flush = dict(batch)
                        batch.clear()
                        _store_alliance_items(conn, flush)
                        stored[0] += len(flush)
                if progress:
                    progress(done[0], len(need), failed[0] + limited[0])
            return
        async with lock:
            failed[0] += 1                       # still throttled after the retries

    async with esi_client() as client:
        jobs = []
        for n, cid in enumerate(need):
            corp_id, token = sources[n % len(sources)]
            jobs.append(_one(client, cid, corp_id, token))
        await asyncio.gather(*jobs, return_exceptions=True)
    if batch:
        _store_alliance_items(conn, batch)
        stored[0] += len(batch)
    if gone:
        _mark_items_absent(conn, gone)
    return {"fetched": stored[0], "failed": failed[0], "attempted": done[0],
            "gone": len(gone),
            "remaining": left_over + max(0, len(need) - done[0]),
            # Only an empty token bucket is worth waiting a window out for.
            "rate_limited": limited[0] > 0}


async def stream_alliance_index(conn: sqlite3.Connection, alliance_id: int,
                                sources: list[tuple[int, str]],
                                resolve_parties, resolve_locations):
    """SSE generator: list the alliance's contracts through every corporation we can
    read, then fetch the items of the ones we do not have yet, resolve names, store.

    `sources` is [(corp_id, token)] - one usable token per corporation. Items must
    come from the same authenticated corp endpoint, so the corporation that listed a
    contract is remembered with it.
    """
    ensure_alliance_contract_tables(conn)
    found: dict[int, dict] = {}
    # One listing call per corporation, even when several of its characters can read it.
    per_corp: dict[int, str] = {}
    for corp_id, token in sources:
        per_corp.setdefault(corp_id, token)
    total_corps = len(per_corp)
    for n, (corp_id, token) in enumerate(per_corp.items(), start=1):
        yield f"data: {_json.dumps({'phase':'list','done':n-1,'total':total_corps,'pct':int((n-1)*25/max(1,total_corps))})}\n\n"
        async with esi_client() as client:
            lst, _err = await contracts_api.fetch_corp_contracts(client, corp_id, token)
        for c in (lst or []):
            if c.get("assignee_id") != alliance_id:
                continue           # the corp's own / personal contracts belong elsewhere
            cid = c.get("contract_id")
            if cid and cid not in found:
                c["_corp_id"] = corp_id
                found[cid] = c
    contracts = list(found.values())
    yield f"data: {_json.dumps({'phase':'list','done':total_corps,'total':total_corps,'pct':20,'contracts':len(contracts)})}\n\n"

    # Resolve names and STORE THE LISTING NOW, before the slow part. Contents take
    # one call per contract and can run for minutes; storing only at the end meant an
    # interrupted run (closed page, given up on) left the database empty and the page
    # still saying "not indexed yet", with every one of those ESI calls wasted.
    yield f"data: {_json.dumps({'phase':'names','pct':22})}\n\n"
    party_ids = {c[k] for c in contracts for k in ("issuer_id", "issuer_corporation_id")
                 if c.get(k)}
    loc_ids = {c[k] for c in contracts for k in ("start_location_id", "end_location_id")
               if c.get(k)}
    names = await resolve_parties(party_ids) if party_ids else {}
    loc_names = await resolve_locations(loc_ids) if loc_ids else {}
    _store_alliance(conn, alliance_id, contracts, names, loc_names)
    yield f"data: {_json.dumps({'phase':'listed','pct':25,'contracts':len(contracts)})}\n\n"

    # Contents: the same routine the background filler uses, so both react to the
    # game server's throttle, to contracts that vanished and to an exhausted token
    # bucket in exactly one way.
    pending = len(contracts_missing_items(conn, alliance_id))
    yield f"data: {_json.dumps({'phase':'items','done':0,'total':pending,'pct':25})}\n\n"
    progress = {"done": 0, "total": pending}

    def _cb(done, total, failed):
        progress.update(done=done, total=total)

    res = {"fetched": 0, "remaining": pending, "rate_limited": False, "gone": 0}
    if pending:
        task = asyncio.create_task(
            fill_alliance_items(conn, alliance_id, sources, progress=_cb))
        while not task.done():
            pct = 25 + int(progress["done"] * 60 / max(1, progress["total"]))
            yield f"data: {_json.dumps({'phase':'items','done':progress['done'],'total':progress['total'],'pct':pct})}\n\n"
            await asyncio.sleep(0.4)
        res = await task

    yield f"data: {_json.dumps({'done':True,'pct':100,'contract_count':len(contracts),'items_fetched':res['fetched'],'items_left':res['remaining'],'rate_limited':res['rate_limited'],'gone':res.get('gone',0)})}\n\n"


_ALLIANCE_SORTS = {
    "expires":  "c.date_expired ASC",
    "issued":   "c.date_issued DESC",
    "price":    "c.price ASC",
    "price_hi": "c.price DESC",
    "reward":   "c.reward DESC",
}


def search_alliance_contracts(conn: sqlite3.Connection, alliance_id: int, *,
                              item: str = "", exact_item: bool = False, q: str = "",
                              ctype: str = "",
                              status: str = "outstanding", min_price: float | None = None,
                              max_price: float | None = None, min_reward: float | None = None,
                              max_collateral: float | None = None,
                              max_volume: float | None = None, location: str = "",
                              issuer: str = "", title: str = "",
                              expires_days: int | None = None, hide_own: bool = False,
                              own_ids: tuple[int, ...] = (), sort: str = "expires",
                              limit: int = 500) -> tuple[list[dict], int]:
    """Filter the indexed alliance contracts. Returns (rows, total_matches)."""
    ensure_alliance_contract_tables(conn)
    where = ["c.alliance_id = ?"]
    params: list = [alliance_id]
    joins = ""
    if item.strip():
        joins = (" JOIN alliance_contract_items i ON i.contract_id = c.contract_id"
                 " JOIN sde_types t ON t.type_id = i.type_id")
        if exact_item:
            where.append("LOWER(t.name) = ?")
            params.append(item.strip().lower())
        else:
            where.append("t.name LIKE ?")
            params.append(f"%{item.strip()}%")
        where.append("i.is_included = 1")
    if q.strip():
        # The same "one box over the obvious columns" as the client-side bar.
        where.append("(c.title LIKE ? OR c.issuer_name LIKE ? OR c.issuer_corp_name LIKE ?"
                     " OR c.start_name LIKE ? OR c.end_name LIKE ?)")
        params += [f"%{q.strip()}%"] * 5
    if ctype:
        where.append("c.type = ?")
        params.append(ctype)
    if status and status != "any":
        where.append("c.status = ?")
        params.append(status)
    if min_price is not None:
        where.append("c.price >= ?")
        params.append(min_price)
    if max_price is not None:
        where.append("c.price <= ?")
        params.append(max_price)
    if min_reward is not None:
        where.append("c.reward >= ?")
        params.append(min_reward)
    if max_collateral is not None:
        where.append("c.collateral <= ?")
        params.append(max_collateral)
    if max_volume is not None:
        where.append("c.volume <= ?")
        params.append(max_volume)
    if location.strip():
        where.append("(c.start_name LIKE ? OR c.end_name LIKE ?)")
        params += [f"%{location.strip()}%"] * 2
    if issuer.strip():
        where.append("(c.issuer_name LIKE ? OR c.issuer_corp_name LIKE ?)")
        params += [f"%{issuer.strip()}%"] * 2
    if title.strip():
        where.append("c.title LIKE ?")
        params.append(f"%{title.strip()}%")
    if expires_days is not None:
        # date_expired is an ISO string, so a lexicographic compare is a date compare.
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                               time.gmtime(time.time() + expires_days * 86400))
        where.append("c.date_expired <= ?")
        params.append(cutoff)
    if hide_own and own_ids:
        ph = ",".join("?" * len(own_ids))
        where.append(f"c.issuer_id NOT IN ({ph}) AND c.issuer_corp_id NOT IN ({ph})")
        params += list(own_ids) + list(own_ids)
    cond = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(DISTINCT c.contract_id) FROM alliance_contracts c{joins} WHERE {cond}",
        params).fetchone()[0]
    order = _ALLIANCE_SORTS.get(sort, _ALLIANCE_SORTS["expires"])
    cols = ["contract_id", "type", "status", "price", "reward", "collateral", "buyout",
            "volume", "date_issued", "date_expired", "days_to_complete", "title",
            "start_name", "end_name", "issuer_name", "issuer_corp_name", "for_corp",
            "source_corp_id"]
    sel = ", ".join(f"c.{c}" for c in cols)
    rows = conn.execute(
        f"SELECT DISTINCT {sel} FROM alliance_contracts c{joins} WHERE {cond}"
        f" ORDER BY {order} LIMIT ?", params + [limit]).fetchall()
    return [dict(zip(cols, r)) for r in rows], total


def contract_unit_prices(conn: sqlite3.Connection, type_ids,
                         exclude_contract_id: int | None = None) -> dict[int, tuple[float, str]]:
    """{type_id: (cheapest price per unit, "alliance"|"public")} from SINGLE-item
    contracts we have indexed.

    This is how capitals and other things the market cannot hold are actually
    priced: a titan is never on a Jita sell order, it is on a contract. Only
    contracts holding exactly one item type are used - in a bundle the price also
    covers everything else, so it says nothing about the unit.

    `exclude_contract_id` keeps the appraisal honest: valuing a contract's contents
    partly from that same contract would make it compare equal to itself.
    """
    ids = sorted({int(t) for t in type_ids if t})
    if not ids:
        return {}
    ensure_alliance_contract_tables(conn)
    ensure_public_contract_tables(conn)
    ph = ",".join("?" * len(ids))
    out: dict[int, tuple[float, str]] = {}
    queries = (
        ("alliance", f"""
            SELECT i.type_id, MIN(c.price * 1.0 / i.quantity)
            FROM alliance_contracts c
            JOIN alliance_contract_items i ON i.contract_id = c.contract_id
            WHERE c.type = 'item_exchange' AND c.status = 'outstanding' AND c.price > 0
              AND i.is_included = 1 AND i.quantity > 0 AND i.type_id IN ({ph})
              AND c.contract_id != ?
              AND (SELECT COUNT(*) FROM alliance_contract_items x
                    WHERE x.contract_id = c.contract_id AND x.is_included = 1) = 1
            GROUP BY i.type_id"""),
        # Public contracts in systems a player alliance holds are left out on
        # purpose: in sov space the market is usually closed to outsiders, so a
        # price there says what one group charges its own, not what the thing is
        # worth. NPC null (faction-held) and unclaimed space are fine - a capital
        # bought in Venal is bought on an open market. A contract whose system we
        # cannot verify (a player structure, 0.5 % of them) is treated the same as
        # sov: not usable as a reference.
        ("public", f"""
            SELECT i.type_id, MIN(c.price * 1.0 / i.quantity)
            FROM public_contracts c
            JOIN public_contract_items i ON i.contract_id = c.contract_id
            LEFT JOIN sov_map_cache s ON s.system_id = c.system_id
            WHERE c.type = 'item_exchange' AND c.price > 0
              AND i.is_included = 1 AND i.quantity > 0 AND i.type_id IN ({ph})
              AND c.contract_id != ?
              AND c.system_id IS NOT NULL AND s.alliance_id IS NULL
              AND (SELECT COUNT(*) FROM public_contract_items x
                    WHERE x.contract_id = c.contract_id AND x.is_included = 1) = 1
            GROUP BY i.type_id"""),
    )
    for source, sql in queries:
        try:
            rows = conn.execute(sql, (*ids, exclude_contract_id or -1)).fetchall()
        except sqlite3.OperationalError:
            continue                      # that index has never been built
        for tid, price in rows:
            if price and (tid not in out or price < out[tid][0]):
                out[int(tid)] = (float(price), source)
    return out


def _adjusted_prices(conn: sqlite3.Connection, type_ids) -> dict[int, float]:
    """CCP's own adjusted price - the last resort for something with no market and
    no contract. It is an index value, not an offer, so it is always labelled as an
    estimate and a zero is treated as "no idea"."""
    ids = sorted({int(t) for t in type_ids if t})
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    try:
        rows = conn.execute(
            f"SELECT type_id, adjusted FROM adjusted_price_cache WHERE type_id IN ({ph})",
            ids).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {int(t): float(v) for t, v in rows if v and v > 0}


def appraise_items(conn: sqlite3.Connection, items: list[dict],
                   contract_id: int | None = None) -> dict:
    """Value a contract's contents from the app's own data, best source first.

    This is the Janice question answered locally - no third-party key, no network -
    and the point of the fallback chain is the things the market cannot hold:

      1. **Jita sell** - a real instant-buy price.
      2. **Contract price** - the cheapest single-item contract we have indexed
         (alliance or public). A titan or a supercarrier is never on a Jita sell
         order; it is on a contract, and that IS its price.
      3. **CCP adjusted price** - an index value, not an offer. Labelled as an
         estimate so nobody mistakes it for a quote. (Measured: a titan has no Jita
         sell, a ~450M lowball buy order, and an adjusted price of ~73b - only the
         last is in the right universe.)
      4. Nothing: counted and named, never silently valued at zero.

    The contract being appraised is excluded from step 2 - valuing its contents from
    itself would make it compare exactly equal to its own price.

    Items the contract ASKS FOR (is_included = 0) are valued separately: accepting
    such a contract means handing those over, so they are a cost, not a gain.
    """
    from app.web.prices_helper import get_cached_jita_prices

    ids = sorted({int(i["type_id"]) for i in items if i.get("type_id")})
    jita = get_cached_jita_prices(conn, ids) if ids else {}
    need_fallback = [t for t in ids if (jita.get(t) or (None, None))[0] is None]
    from_contracts = contract_unit_prices(conn, need_fallback, contract_id) if need_fallback else {}
    still = [t for t in need_fallback if t not in from_contracts]
    estimates = _adjusted_prices(conn, still) if still else {}

    out = {"value": 0.0, "asked": 0.0, "by_source": {"jita": 0.0, "contract": 0.0,
                                                     "estimate": 0.0},
           "counts": {"jita": 0, "contract": 0, "estimate": 0},
           "buy": 0.0, "asked_buy": 0.0,
           "unpriced": 0, "unpriced_names": []}
    for it in items:
        tid = int(it.get("type_id") or 0)
        qty = it.get("quantity") or 0
        sell, buy = jita.get(tid, (None, None))
        it["buy"] = buy
        unit, source = None, None
        if sell is not None:
            unit, source = sell, "jita"
        elif tid in from_contracts:
            unit, source = from_contracts[tid]
            source = "contract"
        elif tid in estimates:
            unit, source = estimates[tid], "estimate"
        it["unit"] = unit
        it["price_source"] = source
        it["value"] = unit * qty if unit is not None else None
        included = it.get("included", True)
        if buy:
            out["buy" if included else "asked_buy"] += buy * qty
        if unit is None:
            out["unpriced"] += 1
            if len(out["unpriced_names"]) < 5:
                out["unpriced_names"].append(it.get("name") or f"#{tid}")
            continue
        out["counts"][source] += 1
        out["by_source"][source] += unit * qty
        out["value" if included else "asked"] += unit * qty
    # What accepting the contract nets you in market terms, before its own price.
    out["net"] = out["value"] - out["asked"]
    out["net_buy"] = out["buy"] - out["asked_buy"]
    out["priced"] = sum(out["counts"].values())
    return out


def best_alliance_contract_price(conn: sqlite3.Connection, alliance_ids,
                                 type_id: int) -> dict | None:
    """Cheapest price/unit of a product from OPEN alliance item-exchange contracts.

    Same shape and same reasoning as best_contract_price for public contracts:
    prefer a contract holding only this product (a clean price per unit), fall back
    to a bundle and say so, because there the price also covers the other items.
    Alliance contracts are not region-scoped here - an alliance offer three jumps
    away is still an offer - so the winning contract's location comes back with it.
    """
    ensure_alliance_contract_tables(conn)
    ids = [int(a) for a in (alliance_ids or []) if a]
    if not ids:
        return None
    ph = ",".join("?" * len(ids))
    rows = conn.execute(f"""
        SELECT c.contract_id, c.price, i.quantity, c.start_name,
               (SELECT COUNT(*) FROM alliance_contract_items x
                 WHERE x.contract_id = c.contract_id AND x.is_included = 1) AS incl
        FROM alliance_contracts c
        JOIN alliance_contract_items i ON i.contract_id = c.contract_id
        WHERE c.alliance_id IN ({ph}) AND c.type = 'item_exchange'
          AND c.status = 'outstanding' AND c.price > 0
          AND i.type_id = ? AND i.is_included = 1
    """, (*ids, type_id)).fetchall()
    singles: list[tuple] = []
    bundles: list[tuple] = []
    for cid, price, qty, where, incl in rows:
        if not qty or qty <= 0:
            continue
        (singles if incl == 1 else bundles).append((price / qty, cid, where))
    pick, is_bundle = (min(singles), False) if singles else (
        (min(bundles), True) if bundles else (None, False))
    if not pick:
        return None
    per_unit, cid, where = pick
    return {"price": per_unit, "is_bundle": is_bundle, "contract_id": cid,
            "location": where or "", "single_count": len(singles),
            "bundle_count": len(bundles)}


def get_alliance_contract_items(conn: sqlite3.Connection, contract_id: int) -> list[dict]:
    """Contents of an indexed contract, grouped like the in-game window.

    ESI returns one row per stack (record_id), so a contract with 49 records of the
    same charge would otherwise print 49 identical lines.
    """
    ensure_alliance_contract_tables(conn)
    rows = conn.execute(
        "SELECT i.type_id, SUM(i.quantity), i.is_included, i.is_bpc,"
        " COALESCE(t.name, '#'||i.type_id) FROM alliance_contract_items i"
        " LEFT JOIN sde_types t ON t.type_id = i.type_id WHERE i.contract_id=?"
        " GROUP BY i.type_id, i.is_included, i.is_bpc"
        " ORDER BY i.is_included DESC, 5", (contract_id,)).fetchall()
    return [{"type_id": r[0], "quantity": r[1], "included": bool(r[2]),
             "is_bpc": bool(r[3]), "name": r[4]} for r in rows]
