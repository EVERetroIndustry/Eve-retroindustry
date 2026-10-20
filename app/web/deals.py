"""Contracts asking less than their contents are worth, across the whole index.

The app already reads every public contract in New Eden and every alliance
contract it can see, item by item - 49 155 public contracts with contents and
355 000 item rows on a real account. Valuing all of them is one local SQL pass,
measured at 0.49 s, so this needs nothing from ESI that the index has not
already fetched.

The hard part is not the arithmetic, it is refusing to claim a bargain that is
not there. Three ways a naive version lies, all three found on real data before
this was written:

  A price nobody would pay. Jita sell said a Salvation was worth 13.40b while
  NINE independent contracts offered one at 7-8b. Quoting the market there made
  every one of those look like a 90 % bargain. So the reference is the cheapest
  real offer we can see - `min(Jita sell, cheapest single-item contract)` - the
  same rule `appraise_items` already uses on the contract detail.

  A price for a different object. "Orbital Skyhook Blueprint, 11 offers at 22m
  against 5.5b on the market" was eleven blueprint COPIES read as originals. A
  copy cannot be on the market at all, so no market price describes it, and a
  blueprint whose copy flag was never read is left unvalued rather than guessed.
  A whole index whose flag is uniformly zero is treated the same way: on this
  account all 17 910 alliance item rows say "original", including a Hel Blueprint
  at 350m against an original worth 23b, so that column carries no information
  there and every blueprint from it is unvaluable until a re-index fills it in.

  A trade that is not a purchase. A contract offering a Mackinaw for 10 ISK
  looked like a 100 % discount, and it also demanded two Ladar-FTL Interlink
  Communicators worth 194M in return. The net was a genuine 8.6M, but the table
  speaks in asking price and percent under, and neither word means anything when
  the ISK figure is a token 10 and the real cost is goods you may not own. Those
  134 contracts across the whole index - 70 of them priced at exactly 10 ISK -
  are barters, and they are left out rather than described wrongly.

  A price from a market that is not there. A Rorqual SKIN quoted at 10b had not
  traded once in thirty days; an Abyssal module has no meaningful type price at
  all because every one is a different object. So every item in a contract must
  have actually traded recently, or the contract is not shown.

What remains is small and believable: measured on a real index, 451 public and
31 alliance contracts at 15 % or better, headed by a Rorqual at 7.95b against a
10.25b reference, compressed ore, moon drills and faction modules.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import time

from app.web import contracts_helper

JITA_REGION = 10000002

# How far back an item must have traded for its price to be evidence. Seven days
# was the first try and it threw away the things a deal matters most for:
# measured, widening to thirty recovers 389 types, among them deadspace modules
# and capital blueprints worth tens of billions that simply do not trade weekly.
# Thirty still excludes the genuinely untraded - a Keepstar Blueprint has sold
# twice in a year, so no recent price describes it.
LIQUIDITY_DAYS = 30

# Discounts the buttons offer. A contract has to be at least this much under the
# reference to be worth a row.
DISCOUNT_STEPS = (0.05, 0.10, 0.20)

_CACHE_TTL = 15 * 60.0
_CACHE: dict = {"at": 0.0, "rows": None, "meta": None}


def dockable_locations(conn: sqlite3.Connection) -> set[int]:
    """Locations at least one added character can actually get into.

    Two kinds, and both are things the app already knows rather than a new ESI
    question:

      NPC stations, from the SDE. Anyone can dock at one - the exception is
      standing low enough for a faction to refuse you, which ESI does not expose
      and which almost never bites.

      Structures whose name we hold. That name can only have come from
      `/universe/structures/{id}/`, and ESI's own words for that endpoint are
      "returns information on requested structure IF YOU ARE ON THE ACL,
      otherwise Forbidden" - so a name we have is a structure one of the
      characters is admitted to, and a name we lack is one nobody is.

    The caveat worth stating rather than hiding: being on the ACL is not exactly
    the same permission as docking, and ESI exposes no finer detail. It is the
    same signal the contract appraisal already trusts for deciding which
    structures may set a price.
    """
    out: set[int] = set()
    for table, col in (("sde_stations", "station_id"),
                       ("location_name_cache", "location_id")):
        try:
            out.update(r[0] for r in conn.execute(f"SELECT {col} FROM {table}"))
        except sqlite3.OperationalError:
            continue
    return out


def _cut_day(days: int) -> str:
    return (_dt.date.today() - _dt.timedelta(days=days)).isoformat()


def liquid_types(conn: sqlite3.Connection, days: int = LIQUIDITY_DAYS) -> set[int]:
    """Types that actually traded inside the window, from the daily history the
    price chart already caches.

    A CALENDAR window, not "the last N records": ESI omits days with no trade, so
    the last twelve records for a Keepstar Blueprint span January to June and
    summing them would read as a lively market. That trap has bitten this project
    before, in the 7-day volume column.
    """
    cut = _cut_day(days)
    out: set[int] = set()
    try:
        rows = conn.execute(
            "SELECT type_id, data_json FROM price_history_cache WHERE region_id = ?",
            (JITA_REGION,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for tid, blob in rows:
        try:
            for d in json.loads(blob):
                if (d.get("d") or "") >= cut and (d.get("vol") or 0) > 0:
                    out.add(int(tid))
                    break
        except (ValueError, TypeError):
            continue
    out |= _contract_traded_types(conn, days)
    return out


def _contract_traded_types(conn: sqlite3.Connection, days: int) -> set[int]:
    """Types that changed hands on a COMPLETED contract inside the window.

    The market cannot answer for everything. A supercarrier or a titan may not
    be put on the market at all, so it has no history and never will - and those
    are exactly the items this list was built for, since a capital is the
    biggest contract in the game. Judging them by market history meant every
    contract holding one was thrown away as illiquid. Measured on a real index:
    a Wyvern worth 42.95b sat in a contract 9.2 % under the cheapest offer
    around and never appeared.

    COMPLETED sales, deliberately, not outstanding offers. An offer is an asking
    price, which is the very thing the liquidity test exists to distrust; a
    finished contract is somebody actually paying.
    """
    try:
        return {r[0] for r in conn.execute(
            "SELECT DISTINCT i.type_id FROM contract_sale_items i"
            "  JOIN contract_sales s ON s.contract_id = i.contract_id"
            " WHERE s.completed_ts >= ?", (time.time() - days * 86400,))}
    except sqlite3.OperationalError:
        return set()               # alliance sales have never been recorded


# ESI rebuilds market history once a day, so a day is how old a series may get.
HISTORY_MAX_AGE = 86400.0


def types_needing_history(conn: sqlite3.Connection,
                          max_age: float = HISTORY_MAX_AGE) -> list[int]:
    """Contracted types whose daily history is missing OR too old to answer.

    Measured: 6659 missing ones on a real index, fetched in 12.2 s at 544
    types/s. The history endpoint is public and carries no token-bucket limit,
    and the cache is the one the price chart already uses.

    Two things this used to get wrong, both of which quietly shrank the Deals
    list over time:

      * it asked only for types with NO row at all, so a cached history was
        never refreshed. Once the window slid past the day it was fetched, the
        item read as "did not trade" however briskly it was trading. Measured a
        month after a fetch: Pyerite and Warp Scrambler II both counted as
        illiquid;
      * it looked only at public contract items, so a type that appears solely
        in alliance contracts never had its history fetched even once.

    And a third, found in the audit of 2026-10-20: "too old" meant older than
    the 30-day liquidity window itself, so a series fetched 20 days ago passed as
    current while covering only the first 10 days of the window it was judged
    by. On a real index 1 831 contracted types were 7-14 days old and 34 were
    14-30 days old. A day is the limit now - ESI rebuilds the history daily.
    """
    stale_before = time.time() - max_age
    try:
        return [r[0] for r in conn.execute("""
            SELECT DISTINCT i.type_id FROM (
                     SELECT type_id FROM public_contract_items
                     UNION SELECT type_id FROM alliance_contract_items) i
            JOIN market_price_cache p ON p.type_id = i.type_id
            WHERE p.sell_price IS NOT NULL
              AND i.type_id NOT IN (
                  SELECT type_id FROM price_history_cache
                   WHERE region_id = ? AND cached_at >= ?)
            """, (JITA_REGION, stale_before))]
    except sqlite3.OperationalError:
        return []


def reference_prices(conn: sqlite3.Connection) -> tuple[dict, dict]:
    """(market, offers) - everything needed to price a line against the alternatives.

    `market` is the Jita sell price. `offers` is the two cheapest single-item
    contracts per type, so a contract that is itself the cheapest offer can be
    compared with the next one instead of with itself.

    Straight out of `appraise_items`' rule otherwise, because the Deals list and
    the contract you open from it must agree: the lower of a Jita sell order and
    the cheapest contract, with an offer under a tenth of the market ignored as
    bait so one silly listing cannot reprice everything.
    """
    market = {r[0]: r[1] for r in conn.execute(
        "SELECT type_id, sell_price FROM market_price_cache WHERE sell_price IS NOT NULL")}
    return market, contracts_helper.contract_unit_prices_all(conn)


def _line_reference(type_id: int, market: dict, offers: dict,
                    exclude_contract: int) -> float | None:
    """What this item costs somewhere OTHER than the contract being judged."""
    mk = market.get(type_id)
    best = None
    for unit, _source, cid in offers.get(type_id, ()):
        if cid == exclude_contract:
            continue                      # comparing a contract with itself proves nothing
        if mk is not None and unit < mk * contracts_helper._BAIT_FLOOR:
            continue                      # bait
        best = unit
        break
    if mk is None:
        return best
    if best is None:
        return mk
    return min(mk, best)


def _flag_is_informative(conn: sqlite3.Connection, table: str) -> bool:
    """Does this index actually distinguish copies from originals?

    A table where every row says "original" is not telling us there are no copies,
    it is telling us nobody read the flag - and a blueprint valued off that is the
    23b-for-350m mistake. Rechecked on every scan, so a re-index repairs it by
    itself.
    """
    try:
        return conn.execute(
            f"SELECT EXISTS(SELECT 1 FROM {table} WHERE is_bpc = 1)").fetchone()[0] == 1
    except sqlite3.OperationalError:
        return False


def _scan(conn: sqlite3.Connection) -> tuple[list[dict], dict]:
    """Value every indexed contract once. Everything after this is filtering."""
    t0 = time.time()
    market, offers = reference_prices(conn)
    liquid = liquid_types(conn)
    bps = contracts_helper._all_blueprint_types(conn)

    rows: list[dict] = []
    # Not "copy": Jinja resolves `meta.skipped.copy` to dict.copy, the method,
    # and renders a TypeError rather than the number.
    skipped = {"unpriced": 0, "copies": 0, "illiquid": 0, "barter": 0}

    sources = (
        ("public", """
            SELECT c.contract_id, c.price, c.title, c.volume, c.date_expired,
                   c.start_location_id, c.region_id, c.issuer_id, NULL, c.system_id
            FROM public_contracts c
            WHERE c.type = 'item_exchange' AND c.price > 0""",
         "SELECT contract_id, type_id, quantity, is_included, is_bpc"
         " FROM public_contract_items"),
        ("alliance", """
            SELECT c.contract_id, c.price, c.title, c.volume, c.date_expired,
                   c.start_location_id, NULL, c.issuer_id, c.issuer_name, NULL
            FROM alliance_contracts c
            WHERE c.type = 'item_exchange' AND c.status = 'outstanding' AND c.price > 0""",
         "SELECT contract_id, type_id, quantity, is_included, is_bpc"
         " FROM alliance_contract_items"),
    )
    for scope, csql, isql in sources:
        trust_flag = _flag_is_informative(conn, f"{scope}_contract_items")
        try:
            items: dict[int, list] = {}
            for cid, tid, qty, inc, bpc in conn.execute(isql):
                items.setdefault(cid, []).append((tid, qty, inc, bpc))
            contracts = conn.execute(csql).fetchall()
        except sqlite3.OperationalError:
            continue                       # that index has never been built

        for (cid, price, title, volume, expires, loc, region, issuer,
             issuer_name, system) in contracts:
            lines = items.get(cid)
            if not lines:
                continue                   # contents never read: nothing to claim
            if any(not inc for _t, _q, inc, _b in lines):
                # Wants goods handed back, so the ISK figure is not the price and
                # a percentage off it means nothing.
                skipped["barter"] += 1
                continue
            value = 0.0
            bad = None
            top_tid, top_worth = None, 0.0
            for tid, qty, inc, bpc in lines:   # every line is included by now
                if tid in bps and (bpc == 1 or bpc is None or not trust_flag):
                    bad = "copies"         # a copy is not what the price describes
                    break
                ref = _line_reference(tid, market, offers, cid)
                if ref is None:
                    bad = "unpriced"
                    break
                if tid not in liquid:
                    bad = "illiquid"
                    break
                worth = qty * ref
                if inc and worth > top_worth:
                    top_tid, top_worth = tid, worth
                value += (qty if inc else -qty) * ref
            if bad:
                skipped[bad] += 1
                continue
            if value <= 0 or price >= value:
                continue
            rows.append({
                "scope": scope, "contract_id": cid, "price": price, "value": value,
                "saving": value - price, "discount": 1.0 - price / value,
                "title": title or "", "volume": volume, "expires": expires,
                "location_id": loc, "region_id": region, "system_id": system,
                "issuer_id": issuer, "issuer_name": issuer_name,
                "lines": len(lines), "top_type_id": top_tid,
            })
    rows.sort(key=lambda r: -r["saving"])
    meta = {"scanned_at": time.time(), "took": time.time() - t0,
            "skipped": skipped, "priced_types": len(market),
            "liquid_types": len(liquid), "found": len(rows),
            "blueprint_flag": {s: _flag_is_informative(conn, f"{s}_contract_items")
                               for s in ("public", "alliance")}}
    return rows, meta


def find_deals(conn: sqlite3.Connection, min_discount: float = 0.05,
               scope: str = "", region_id: int | None = None,
               max_price: float | None = None, dockable_only: bool = False,
               limit: int = 200, force: bool = False) -> tuple[list[dict], dict]:
    """The Deals list. The scan is cached for a quarter of an hour: it is the same
    answer for every filter, and re-running it per click would be work for nothing.
    """
    if force or _CACHE["rows"] is None or (time.time() - _CACHE["at"]) > _CACHE_TTL:
        rows, meta = _scan(conn)
        _CACHE.update({"at": time.time(), "rows": rows, "meta": meta})
    rows, meta = _CACHE["rows"], dict(_CACHE["meta"])
    # A hair of tolerance: an exact 20 % comes out of the division as
    # 0.19999999999999996, and a button labelled 20% has to include it.
    floor = min_discount - 1e-9
    out = [r for r in rows if r["discount"] >= floor]
    if scope in ("public", "alliance"):
        out = [r for r in out if r["scope"] == scope]
    if region_id:
        out = [r for r in out if r["region_id"] == region_id]
    if max_price is not None:
        out = [r for r in out if r["price"] <= max_price]
    dockable = dockable_locations(conn)
    meta["unreachable"] = sum(1 for r in out if r["location_id"] not in dockable)
    if dockable_only:
        out = [r for r in out if r["location_id"] in dockable]
    for r in out:
        r["dockable"] = r["location_id"] in dockable
    meta["matched"] = len(out)
    meta["age"] = time.time() - _CACHE["at"]
    return out[:limit], meta


def drop_cache() -> None:
    _CACHE.update({"at": 0.0, "rows": None, "meta": None})
