"""What a character owns that is in neither the assets nor the wallet.

ESI takes things out of both while they are tied up, so net worth built from
assets plus wallet misses them entirely. Measured on a real account: 13 sell
orders worth 1.09b at Jita sell, up to 0.85b in buy order escrow and two item
exchange contracts asking 3.01b, none of it in the figure.

* Sell orders - the remaining units, out of the hangar until sold or cancelled.
* Buy orders - the escrow ESI reports, ISK taken from the wallet.
* Own contracts still open (issued by the character, not for the corporation):
  the items in them, and the ISK reward the issuer paid in up front. A courier
  the character accepted holds its collateral the same way.
* Manufacturing and reaction jobs not yet delivered - the product, which is in
  no hangar until it is delivered (the materials are already gone).

Everything is returned as type quantities and ISK; pricing is the caller's,
so it is the same Jita sell basis as the assets. Each ESI answer is stored and
reused, and an old answer is used when a new one cannot be had, so a slow ESI
leaves the figure as it was rather than dropping a part of it.
"""
from __future__ import annotations

import asyncio
import sqlite3

from app.character import contracts as contracts_api
from app.character import jobs as jobs_api
from app.character import orders as orders_api
from app.web.page_cache import get_cached, put_cached

# ESI's own cache times: orders 1200 s, contracts 300 s, jobs 300 s.
ORDERS_TTL = 20 * 60
CONTRACTS_TTL = 10 * 60
JOBS_TTL = 10 * 60            # the same entry the Industry jobs page keeps

# Statuses in which the issuer's items and ISK are still inside the contract.
_OPEN = ("outstanding", "in_progress")
# Manufacturing and reactions (ESI activity ids) -> SDE activity of the product.
_PRODUCT_ACTIVITY = {1: "manufacturing", 9: "reaction", 11: "reaction"}
_UNDELIVERED = ("active", "paused", "ready")

_ITEMS_SEM = asyncio.Semaphore(4)

# The ESI calls, as names of this module so a test can replace them here
# without touching the Orders, Contracts and Jobs pages.
_fetch_orders = orders_api.fetch_orders
_fetch_contracts = contracts_api.fetch_character_contracts
_fetch_jobs = jobs_api.fetch_industry_jobs


def empty() -> dict:
    return {"sell": {}, "escrow": 0.0, "contract_items": {}, "contract_isk": 0.0,
            "jobs": {}, "unknown": []}


async def _stored(conn, kind: str, key, ttl: float, fetch):
    """The stored answer while it is fresh, else a new one, else the old one.
    fetch is None when no ESI call may be made (no token, cache-only render)."""
    hit = get_cached(conn, kind, key)
    if hit is not None and (hit[1] < ttl or fetch is None):
        return hit[0]
    if fetch is not None:
        try:
            payload = await fetch()
        except Exception:
            payload = None
        if payload is not None:
            put_cached(conn, kind, key, payload)
            return payload
    return hit[0] if hit is not None else None


async def _contract_items(client, char_id: int, contract_id: int, token: str):
    """Items of one contract, or None when ESI did not give them. Separate from
    contracts.fetch_character_contract_items, which returns [] for a failure and
    would have the failure stored as an empty contract for good."""
    async with _ITEMS_SEM:
        try:
            r = await client.get(
                f"{contracts_api.ESI_BASE}/characters/{char_id}/contracts/{contract_id}/items/",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=15)
        except Exception:
            return None
    if r.status_code == 200:
        try:
            return r.json()
        except ValueError:
            return None
    return None


_fetch_contract_items = _contract_items


def _add(bag: dict, type_id, qty) -> None:
    if type_id and qty:
        bag[int(type_id)] = bag.get(int(type_id), 0) + int(qty)


def _own_job(job: dict, own_items: set[int]) -> bool:
    """A job the character runs for themselves, not for the corporation.

    ESI's character job list has no such flag. A personal job's blueprint sits
    in the character's hangar (its location is the station or structure) or in
    one of their containers, or is one of their blueprints; a corporation job's
    sits in a corporation office. Only positive evidence counts, so a job that
    cannot be placed is left out rather than added to the wrong person."""
    loc = job.get("blueprint_location_id")
    if loc and loc in (job.get("station_id"), job.get("facility_id")):
        return True
    return loc in own_items or job.get("blueprint_id") in own_items


def _product_qty(conn, job: dict) -> int:
    act = _PRODUCT_ACTIVITY.get(job.get("activity_id"))
    row = conn.execute(
        "SELECT quantity FROM sde_blueprint_products"
        " WHERE blueprint_type_id=? AND activity=? AND product_type_id=?",
        (job.get("blueprint_type_id"), act, job.get("product_type_id"))).fetchone()
    return (row[0] if row and row[0] else 1) * int(job.get("runs") or 0)


async def collect(conn: sqlite3.Connection, char_id: int, token: str | None,
                  own_items: set[int], client=None) -> dict:
    """The parts of one character's worth held outside assets and wallet.

    own_items: item ids of the character's assets and blueprints, to tell a
    personal industry job from a corporation one. With no client (or no token)
    only stored answers are used. "unknown" names the parts with no answer at
    all, so the page can say the figure lacks them."""
    out = empty()
    live = client is not None and bool(token)

    def fetcher(coro_fn):
        return coro_fn if live else None

    orders = await _stored(conn, "nw_orders", char_id, ORDERS_TTL,
                           fetcher(lambda: _fetch_orders(client, char_id, token)))
    if orders is None:
        out["unknown"].append("orders")
    for o in orders or ():
        if o.get("is_corporation"):
            continue
        if o.get("is_buy_order"):
            out["escrow"] += float(o.get("escrow") or 0.0)
        else:
            _add(out["sell"], o.get("type_id"), o.get("volume_remain"))

    contracts = await _stored(conn, "nw_contracts", char_id, CONTRACTS_TTL,
                              fetcher(lambda: _fetch_contracts(client, char_id, token)))
    if contracts is None:
        out["unknown"].append("contracts")
    issued = []
    for c in contracts or ():
        if c.get("for_corporation") or c.get("status") not in _OPEN:
            continue
        if c.get("issuer_id") == char_id:
            issued.append(c)
            out["contract_isk"] += float(c.get("reward") or 0.0)
        elif (c.get("acceptor_id") == char_id and c.get("type") == "courier"
              and c.get("status") == "in_progress"):
            out["contract_isk"] += float(c.get("collateral") or 0.0)

    async def items_of(c):
        cid = c["contract_id"]
        return await _stored(
            conn, "nw_contract_items", f"{char_id}:{cid}", float("inf"),
            fetcher(lambda: _fetch_contract_items(client, char_id, cid, token)))

    for c, items in zip(issued, await asyncio.gather(*[items_of(c) for c in issued])):
        if items is None:
            if "contract items" not in out["unknown"]:
                out["unknown"].append("contract items")
            continue
        for it in items:
            if it.get("is_included", True):
                _add(out["contract_items"], it.get("type_id"), it.get("quantity"))

    jobs = await _stored(conn, "jobs", char_id, JOBS_TTL,
                         fetcher(lambda: _fetch_jobs(client, char_id, token)))
    if jobs is None:
        out["unknown"].append("industry jobs")
    for j in jobs or ():
        if (j.get("activity_id") in _PRODUCT_ACTIVITY and j.get("status") in _UNDELIVERED
                and j.get("product_type_id") and _own_job(j, own_items)):
            _add(out["jobs"], j["product_type_id"], _product_qty(conn, j))
    return out


def type_ids(held: dict) -> set[int]:
    return set(held["sell"]) | set(held["contract_items"]) | set(held["jobs"])


def value(held: dict, prices: dict, skip_types: set[int] = frozenset()) -> dict:
    """ISK per part. prices: type_id -> (sell, ...) as the dashboard has them.
    skip_types are left out the way the assets leave them (blueprints)."""
    def worth(bag: dict) -> float:
        return sum((prices.get(t, (None,))[0] or 0.0) * q
                   for t, q in bag.items() if t not in skip_types)

    parts = {"market": worth(held["sell"]), "escrow": held["escrow"],
             "contracts": worth(held["contract_items"]) + held["contract_isk"],
             "jobs": worth(held["jobs"])}
    parts["total"] = sum(parts.values())
    return parts
