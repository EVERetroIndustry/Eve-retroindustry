"""A price for the things the market never carries.

The Prices table showed a dash for every supercarrier, titan and faction hull -
while two tabs away the same app appraised those very hulls off contracts, to the
ISK. A dash there does not mean "worthless" or "unknown", it means "Jita has no
sell order", and the app knew the difference everywhere except here.

The chain is deliberately `appraise_items`': cheapest single-item contract, then
CCP's adjusted price named as the index value it is, then nothing. Two numbers for
the same hull in two tabs would be worse than the dash.
"""
from __future__ import annotations

import pytest

from app.web import contracts_helper as ch
from app.web.prices_helper import (attach_off_market_prices,
                                  fill_missing_sell_prices, off_market_prices)

NPC_STATION = 60003760
JITA_SYSTEM = 30000142
FORGE = 10000002
ALLIANCE = 99000001
WYVERN = 23917                 # a supercarrier: never on a market order, ever
TRIT = 34


@pytest.fixture
def conn(app_module):
    c = app_module.get_conn()
    ch.ensure_public_contract_tables(c)
    ch.ensure_alliance_contract_tables(c)
    for t in ("public_contracts", "public_contract_items",
              "alliance_contracts", "alliance_contract_items"):
        c.execute(f"DELETE FROM {t}")
    c.execute("DELETE FROM market_price_cache WHERE type_id=?", (WYVERN,))
    c.execute("DELETE FROM adjusted_price_cache WHERE type_id=?", (WYVERN,))
    c.commit()
    yield c
    for t in ("public_contracts", "public_contract_items",
              "alliance_contracts", "alliance_contract_items"):
        c.execute(f"DELETE FROM {t}")
    c.execute("DELETE FROM market_price_cache WHERE type_id=?", (WYVERN,))
    c.execute("DELETE FROM adjusted_price_cache WHERE type_id=?", (WYVERN,))
    c.commit()
    c.close()


def _price(conn, type_id: int, sell: float | None, buy: float | None = None):
    conn.execute("INSERT OR REPLACE INTO market_price_cache (type_id, sell_price,"
                 " buy_price, cached_at) VALUES (?,?,?,0)", (type_id, sell, buy))
    conn.commit()


def _adjusted(conn, type_id: int, value: float):
    conn.execute("INSERT OR REPLACE INTO adjusted_price_cache (type_id, adjusted,"
                 " cached_at) VALUES (?,?,0)", (type_id, value))
    conn.commit()


def _pub(conn, cid: int, price: float, items: list[dict]):
    conn.execute(
        "INSERT OR REPLACE INTO public_contracts (contract_id, region_id, type, price,"
        " reward, collateral, buyout, volume, date_expired, title, start_location_id,"
        " end_location_id, issuer_id, system_id)"
        " VALUES (?,?,'item_exchange',?,0,0,0,1.0,'2026-12-31T00:00:00Z','t',?,0,1,?)",
        (cid, FORGE, price, NPC_STATION, JITA_SYSTEM))
    conn.commit()
    ch.store_public_items(conn, {cid: items})


def _ally(conn, cid: int, price: float, items: list[tuple[int, int]]):
    conn.execute(
        "INSERT OR REPLACE INTO alliance_contracts (contract_id, alliance_id, type,"
        " status, price, start_location_id, date_expired)"
        " VALUES (?,?,'item_exchange','outstanding',?,?,'2026-12-31T00:00:00Z')",
        (cid, ALLIANCE, price, NPC_STATION))
    conn.executemany(
        "INSERT INTO alliance_contract_items (contract_id, type_id, quantity,"
        " is_included, is_bpc) VALUES (?,?,?,1,0)",
        [(cid, t, q) for t, q in items])
    conn.commit()


def _item(type_id: int, qty: int = 1) -> dict:
    return {"type_id": type_id, "quantity": qty, "is_included": True,
            "is_blueprint_copy": False}


# ── what gets a price ─────────────────────────────────────────────────────────

def test_a_hull_the_market_cannot_hold_is_priced_off_its_contract(conn):
    """The reported case: a Wyvern on an alliance contract, a dash in Prices."""
    _price(conn, WYVERN, None, 1_234_000_000.0)      # a lowball buy order, no sell
    _ally(conn, 7001, 42_950_000_000.0, [(WYVERN, 1)])

    got = off_market_prices(conn, [WYVERN])[WYVERN]
    assert got == {"price": 42_950_000_000.0, "source": "contract",
                   "where": "alliance"}


def test_the_cheapest_offer_is_the_one_quoted(conn):
    _price(conn, WYVERN, None)
    _ally(conn, 7002, 42_950_000_000.0, [(WYVERN, 1)])
    _pub(conn, 7003, 39_000_000_000.0, [_item(WYVERN)])

    got = off_market_prices(conn, [WYVERN])[WYVERN]
    assert got["price"] == 39_000_000_000.0 and got["where"] == "public"


def test_a_bundle_says_nothing_about_what_one_hull_costs(conn):
    """40b for a Wyvern plus three rigs is not a Wyvern price, and 40b/4 is not
    one either. With nothing else to go on the dash is the honest answer."""
    _price(conn, WYVERN, None)
    _price(conn, TRIT, 5.0)
    _ally(conn, 7004, 40_000_000_000.0, [(WYVERN, 1), (TRIT, 3)])

    assert WYVERN not in off_market_prices(conn, [WYVERN])


def test_ccps_index_value_is_the_last_resort_and_says_so(conn):
    _price(conn, WYVERN, None)
    _adjusted(conn, WYVERN, 26_999_749_591.0)

    got = off_market_prices(conn, [WYVERN])[WYVERN]
    assert got == {"price": 26_999_749_591.0, "source": "estimate", "where": None}


def test_a_contract_beats_the_index_value(conn):
    """Even a dearer one: the contract is an offer somebody can accept, the
    adjusted price is a number CCP computes."""
    _price(conn, WYVERN, None)
    _adjusted(conn, WYVERN, 26_999_749_591.0)
    _ally(conn, 7005, 42_950_000_000.0, [(WYVERN, 1)])

    got = off_market_prices(conn, [WYVERN])[WYVERN]
    assert got["source"] == "contract" and got["price"] == 42_950_000_000.0


def test_an_adjusted_price_of_zero_is_not_a_price(conn):
    _price(conn, WYVERN, None)
    _adjusted(conn, WYVERN, 0.0)

    assert WYVERN not in off_market_prices(conn, [WYVERN])


def test_nothing_known_stays_nothing(conn):
    _price(conn, WYVERN, None)
    assert off_market_prices(conn, [WYVERN]) == {}


# ── where it is applied ───────────────────────────────────────────────────────

def test_the_market_keeps_the_last_word_when_it_has_one(conn):
    """This is a fallback, not a comparison. A contract undercutting Jita is the
    appraisal's business, where it is read next to an asking price; quoting it in
    a column headed "Jita sell" would be a different claim."""
    _price(conn, WYVERN, 50_000_000_000.0)
    _ally(conn, 7006, 42_950_000_000.0, [(WYVERN, 1)])

    items = [{"type_id": WYVERN, "sell_price": 50_000_000_000.0}]
    attach_off_market_prices(conn, items)
    assert "alt_price" not in items[0]


def test_rows_the_market_does_cover_cost_nothing_to_render(conn):
    """No missing price, no query at all - the page renders ~2 000 rows."""
    conn.execute("DROP TABLE IF EXISTS alliance_contract_items")   # would raise
    items = [{"type_id": TRIT, "sell_price": 5.0}]
    attach_off_market_prices(conn, items)
    assert "alt_price" not in items[0]
    ch.ensure_alliance_contract_tables(conn)


def test_more_types_than_sqlite_takes_parameters(conn):
    """The ids go into the lookup in chunks, because a statement may only carry so
    many host parameters - and how many is a SQLite build option, 999 in older
    builds and far more in newer ones. The limit is pinned to the old value here
    rather than trusted: this machine's SQLite allows 250 000, Android's and the
    AppImage's are somebody else's build, and 2 764 types with no sell price is
    the real cache's number already."""
    import sqlite3

    _price(conn, WYVERN, None)
    _ally(conn, 7007, 42_950_000_000.0, [(WYVERN, 1)])

    was = conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    try:
        got = off_market_prices(conn, list(range(2_000_000, 2_003_000)) + [WYVERN])
    finally:
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, was)
    assert got[WYVERN]["price"] == 42_950_000_000.0


# ── the page and the search behind it ─────────────────────────────────────────

def test_the_prices_page_carries_the_number_into_the_cell(conn, client):
    """The cell is painted from data-alt, never from data-v: data-v is the Jita
    price, and both the column sort and the history chart's reference line read
    it. A contract price in there would draw a "current sell" line that no market
    ever quoted."""
    _price(conn, WYVERN, None, 1_234_000_000.0)
    _ally(conn, 7008, 42_950_000_000.0, [(WYVERN, 1)])
    # custom_price_override is what puts a type on the default page without
    # pretending the test character owns a supercarrier.
    conn.execute("INSERT OR REPLACE INTO custom_price_override (type_id, price,"
                 " updated_at) VALUES (?,1,0)", (WYVERN,))
    conn.commit()
    try:
        html = client.get("/prices").text
    finally:
        conn.execute("DELETE FROM custom_price_override WHERE type_id=?", (WYVERN,))
        conn.commit()
    cell = [ln for ln in html.splitlines() if f'data-id="{WYVERN}"' in ln]
    assert cell, "the Wyvern row is not on the page"
    assert 'data-alt-src="contract"' in cell[0]
    assert 'data-alt-where="alliance"' in cell[0]
    # The Jita attribute stays empty and the number sits beside it, in one string
    # so the two can never drift apart.
    assert 'pm-sell ph-jita text-muted" data-v="" data-alt="42950000000.0"' in cell[0]


def test_the_search_answers_with_it_too(conn, client):
    """Most of the table arrives through /api/prices/search - a fix that only
    reached the server-rendered rows would miss everything the user looks up."""
    _price(conn, WYVERN, None, 1_234_000_000.0)
    _ally(conn, 7009, 42_950_000_000.0, [(WYVERN, 1)])

    d = client.get("/api/prices/search", params={"q": "wyvern"}).json()
    row = next(i for i in d["items"] if i["type_id"] == WYVERN)
    assert row["sell_price"] is None
    assert row["alt_price"] == 42_950_000_000.0
    assert row["alt_source"] == "contract" and row["alt_where"] == "alliance"


# ── assets: the same pile, the same number ────────────────────────────────────

def test_a_hangar_is_not_worth_less_because_the_market_is_silent(conn):
    """The reported case: a fitted Wyvern read as 1.11b - the three rigs - because
    the hull itself, 42.95b of it, had no price to count."""
    prices = {WYVERN: (None, 1_234_000_000.0), TRIT: (5.0, 4.0)}
    _ally(conn, 7010, 42_950_000_000.0, [(WYVERN, 1)])

    sources = fill_missing_sell_prices(conn, prices)
    assert prices[WYVERN] == (42_950_000_000.0, 1_234_000_000.0)   # buy side kept
    assert sources == {WYVERN: "contract"}
    assert prices[TRIT] == (5.0, 4.0)                              # untouched


def test_nothing_known_leaves_the_row_without_a_price(conn):
    """Not zero. A row with no price is read as "we do not know", and a zero
    would be read as "worthless" - which is how a supercarrier came to weigh
    nothing at all in a hangar's total."""
    prices = {WYVERN: (None, None)}
    assert fill_missing_sell_prices(conn, prices) == {}
    assert prices[WYVERN] == (None, None)


def test_the_assets_page_shows_the_number_and_says_where_it_is_from(conn, client):
    """End to end, because the valuation and the marker have to arrive together:
    a contract price in the Value column with nothing saying so would be the app
    quoting a market price it does not have.

    The buy price stays on the row on purpose - that is the real shape of the
    reported case (a Wyvern with a 1.23b lowball buy order and no sell order) and
    it is what makes the row count as cached at all. Strip it and the type reads
    as never fetched, which sends get_prices_for_ids to ESI for an average price,
    and the test would then depend on the network.
    """
    seeded = conn.execute("SELECT sell_price, buy_price FROM market_price_cache"
                          " WHERE type_id=?", (TRIT,)).fetchone()
    conn.execute("UPDATE market_price_cache SET sell_price=NULL, buy_price=4.0"
                 " WHERE type_id=?", (TRIT,))
    conn.commit()
    _ally(conn, 7011, 7.0, [(TRIT, 1)])
    try:
        html = client.get("/assets").text
    finally:
        conn.execute("UPDATE market_price_cache SET sell_price=?, buy_price=?"
                     " WHERE type_id=?", (seeded[0], seeded[1], TRIT))
        conn.commit()
    assert "Tritanium" in html, "the seeded Tritanium is not on the page"
    assert "Not on a Jita sell order" in html and ">c</span>" in html
    # 10 000 units at the contract's 7.00, where the row used to carry nothing.
    assert "70 000" in html


def test_the_folded_hull_keeps_the_marker(app_module):
    """A ship with a fit is drawn as a container and its hull row is built from
    scratch, not taken from the hangar bucket. That is the one row where a
    contract price is most likely - a fitted capital is exactly what sits on
    contracts - so losing the marker there would show the number as a market
    price on the only line that is usually not one."""
    owner, ship_cid = 900000001, 555002
    hangar_row = {"type_id": WYVERN, "name": "Wyvern", "quantity": 1,
                  "is_blueprint_copy": False, "character_id": owner,
                  "character_name": "Pilot", "unit_price": 42_950_000_000.0,
                  "total_value": 42_950_000_000.0, "price_source": "contract"}
    node = {"hangar": {(WYVERN, owner, False, "", False): hangar_row},
            "containers": {ship_cid: {}}}
    app_module._fold_ship_hulls(node, {ship_cid: WYVERN}, {ship_cid: (owner, "Pilot")})

    hull = node["containers"][ship_cid][("_hull", ship_cid)]
    assert hull["unit_price"] == 42_950_000_000.0
    assert hull["price_source"] == "contract"
