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
    """)
    conn.commit()


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


def search_public_contracts(conn: sqlite3.Connection, region_id: int, *,
                            item: str = "", ctype: str = "", max_price: float | None = None,
                            limit: int = 300) -> list[dict]:
    ensure_public_contract_tables(conn)
    where = ["c.region_id = ?"]
    params: list = [region_id]
    joins = ""
    if item.strip():
        joins = (" JOIN public_contract_items i ON i.contract_id = c.contract_id"
                 " JOIN sde_types t ON t.type_id = i.type_id")
        where.append("t.name LIKE ?")
        params.append(f"%{item.strip()}%")
    if ctype:
        where.append("c.type = ?")
        params.append(ctype)
    if max_price is not None:
        where.append("c.price <= ?")
        params.append(max_price)
    sql = (f"SELECT DISTINCT c.contract_id, c.type, c.price, c.reward, c.collateral, "
           f"c.volume, c.date_expired, c.title, c.start_location_id, c.end_location_id, "
           f"c.issuer_id FROM public_contracts c{joins} WHERE {' AND '.join(where)} "
           f"ORDER BY c.price LIMIT ?")
    params.append(limit)
    cols = ["contract_id", "type", "price", "reward", "collateral", "volume",
            "date_expired", "title", "start_location_id", "end_location_id", "issuer_id"]
    return [dict(zip(cols, row)) for row in conn.execute(sql, params).fetchall()]


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
    """)
    conn.commit()


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


def contracts_missing_items(conn: sqlite3.Connection, alliance_id: int) -> list[int]:
    """Indexed item_exchange/auction contracts whose items were never fetched.
    Contract contents never change, so a reindex only has to fetch these."""
    ensure_alliance_contract_tables(conn)
    return [r[0] for r in conn.execute(
        "SELECT c.contract_id FROM alliance_contracts c"
        " WHERE c.alliance_id=? AND c.type IN ('item_exchange','auction')"
        "   AND NOT EXISTS (SELECT 1 FROM alliance_contract_items i"
        "                    WHERE i.contract_id = c.contract_id)",
        (alliance_id,)).fetchall()]


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
    yield f"data: {_json.dumps({'phase':'list','done':total_corps,'total':total_corps,'pct':25,'contracts':len(contracts)})}\n\n"

    # Items: only for contracts that can have them, only the ones we are missing, and
    # only as many as the rate limit allows this run. Every alliance corporation can
    # read every alliance contract's items (verified across two corps on a contract
    # issued by a third), so the calls are spread round-robin over all capable
    # characters - each one brings its own bucket.
    have = {r[0] for r in conn.execute(
        "SELECT DISTINCT contract_id FROM alliance_contract_items").fetchall()}
    missing = [c for c in contracts
               if c.get("type") in ("item_exchange", "auction") and c["contract_id"] not in have]
    # Outstanding first: those are the ones somebody may actually want to accept.
    missing.sort(key=lambda c: (c.get("status") != "outstanding", c.get("date_expired") or ""))
    budget = _ITEM_CALLS_PER_TOKEN * max(1, len(sources))
    need = missing[:budget]
    left_over = len(missing) - len(need)
    total_items = len(need)
    done_items = [0]
    items_by_cid: dict[int, list[dict]] = {}
    lock = asyncio.Lock()

    async def _one(client, c, corp_id, token):
        async with _ALLIANCE_ITEM_SEM:
            its = await contracts_api.fetch_corp_contract_items(
                client, corp_id, c["contract_id"], token)
        async with lock:
            if its:
                items_by_cid[c["contract_id"]] = its
            done_items[0] += 1

    async def _run_items():
        async with esi_client() as client:
            jobs = []
            for n, c in enumerate(need):
                corp_id, token = sources[n % len(sources)]
                jobs.append(_one(client, c, corp_id, token))
            await asyncio.gather(*jobs, return_exceptions=True)

    yield f"data: {_json.dumps({'phase':'items','done':0,'total':total_items,'pct':25})}\n\n"
    if total_items:
        task = asyncio.create_task(_run_items())
        while not task.done():
            pct = 25 + int(done_items[0] * 60 / total_items)
            yield f"data: {_json.dumps({'phase':'items','done':done_items[0],'total':total_items,'pct':pct})}\n\n"
            await asyncio.sleep(0.4)
        await task
        _store_alliance_items(conn, items_by_cid)

    # Names once, at index time - that is what lets the filters be plain SQL.
    yield f"data: {_json.dumps({'phase':'names','pct':88})}\n\n"
    party_ids = {c[k] for c in contracts for k in ("issuer_id", "issuer_corporation_id")
                 if c.get(k)}
    loc_ids = {c[k] for c in contracts for k in ("start_location_id", "end_location_id")
               if c.get(k)}
    names = await resolve_parties(party_ids) if party_ids else {}
    loc_names = await resolve_locations(loc_ids) if loc_ids else {}
    _store_alliance(conn, alliance_id, contracts, names, loc_names)
    yield f"data: {_json.dumps({'done':True,'pct':100,'contract_count':len(contracts),'items_fetched':len(items_by_cid),'items_left':left_over})}\n\n"


_ALLIANCE_SORTS = {
    "expires":  "c.date_expired ASC",
    "issued":   "c.date_issued DESC",
    "price":    "c.price ASC",
    "price_hi": "c.price DESC",
    "reward":   "c.reward DESC",
}


def search_alliance_contracts(conn: sqlite3.Connection, alliance_id: int, *,
                              item: str = "", exact_item: bool = False, ctype: str = "",
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
