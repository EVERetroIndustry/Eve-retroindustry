"""Contracts asking less than their contents are worth.

Every test here is a way the naive version claimed a bargain that was not there,
found on a real index before the feature was written. The arithmetic is trivial;
refusing to lie about it is the whole job.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time

import pytest

from app.web import deals


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE public_contracts (contract_id INTEGER PRIMARY KEY, region_id INTEGER,
            type TEXT, price REAL, reward REAL, collateral REAL, buyout REAL, volume REAL,
            date_expired TEXT, title TEXT, start_location_id INTEGER, end_location_id INTEGER,
            issuer_id INTEGER, system_id INTEGER);
        CREATE TABLE public_contract_items (contract_id INTEGER, type_id INTEGER,
            quantity INTEGER, is_included INTEGER, is_bpc INTEGER, runs INTEGER,
            me INTEGER, te INTEGER);
        CREATE TABLE alliance_contracts (contract_id INTEGER PRIMARY KEY, alliance_id INTEGER,
            source_corp_id INTEGER, type TEXT, status TEXT, price REAL, reward REAL,
            collateral REAL, buyout REAL, volume REAL, date_issued TEXT, date_expired TEXT,
            days_to_complete INTEGER, title TEXT, start_location_id INTEGER,
            end_location_id INTEGER, start_name TEXT, end_name TEXT, issuer_id INTEGER,
            issuer_corp_id INTEGER, issuer_name TEXT, issuer_corp_name TEXT, for_corp INTEGER);
        CREATE TABLE alliance_contract_items (contract_id INTEGER, type_id INTEGER,
            quantity INTEGER, is_included INTEGER, is_bpc INTEGER);
        CREATE TABLE market_price_cache (type_id INTEGER PRIMARY KEY, sell_price REAL,
            buy_price REAL, cached_at REAL, volume REAL, jita_available REAL);
        CREATE TABLE price_history_cache (region_id INTEGER, type_id INTEGER,
            data_json TEXT, cached_at REAL, PRIMARY KEY (region_id, type_id));
        CREATE TABLE sde_blueprints (blueprint_type_id INTEGER PRIMARY KEY,
            max_production_limit INTEGER, manufacturing_time INTEGER, reaction_time INTEGER);
        CREATE TABLE sov_map_cache (system_id INTEGER PRIMARY KEY, alliance_id INTEGER,
            faction_id INTEGER, cached_at REAL);
        CREATE TABLE sde_types (type_id INTEGER PRIMARY KEY, name TEXT, group_id INTEGER);
    """)
    conn.commit()
    deals.drop_cache()
    return conn


ORE, HULL, BPRINT = 34, 22544, 999


def _price(conn, type_id, sell):
    conn.execute("INSERT OR REPLACE INTO market_price_cache (type_id, sell_price)"
                 " VALUES (?,?)", (type_id, sell))
    conn.commit()


def _traded(conn, type_id, days_ago=1, vol=100):
    """Give a type a trade that many days back, in the daily history."""
    day = (dt.date.today() - dt.timedelta(days=days_ago)).isoformat()
    conn.execute("INSERT OR REPLACE INTO price_history_cache VALUES (?,?,?,?)",
                 (deals.JITA_REGION, type_id,
                  json.dumps([{"d": day, "avg": 1.0, "vol": vol}]), time.time()))
    conn.commit()


def _public(conn, cid, price, items, system=30000142, title=""):
    conn.execute("INSERT INTO public_contracts (contract_id, region_id, type, price,"
                 " volume, title, start_location_id, issuer_id, system_id)"
                 " VALUES (?,?,?,?,?,?,?,?,?)",
                 (cid, 10000002, "item_exchange", price, 1.0, title, 60003760, 90001, system))
    for tid, qty, *rest in items:
        inc = rest[0] if rest else 1
        bpc = rest[1] if len(rest) > 1 else 0
        conn.execute("INSERT INTO public_contract_items (contract_id, type_id, quantity,"
                     " is_included, is_bpc) VALUES (?,?,?,?,?)", (cid, tid, qty, inc, bpc))
    conn.commit()


def _alliance(conn, cid, price, items, status="outstanding"):
    conn.execute("INSERT INTO alliance_contracts (contract_id, alliance_id, type, status,"
                 " price, volume, issuer_id) VALUES (?,?,?,?,?,?,?)",
                 (cid, 99000001, "item_exchange", status, price, 1.0, 90002))
    for tid, qty, *rest in items:
        bpc = rest[0] if rest else 0
        conn.execute("INSERT INTO alliance_contract_items (contract_id, type_id, quantity,"
                     " is_included, is_bpc) VALUES (?,?,?,1,?)", (cid, tid, qty, bpc))
    conn.commit()


# ── the basic claim ──────────────────────────────────────────────────────────

def test_a_contract_under_its_contents_is_found_and_one_over_is_not():
    conn = _db()
    _price(conn, ORE, 100.0)
    _traded(conn, ORE)
    _public(conn, 1, 8_000.0, [(ORE, 100)])      # worth 10 000
    _public(conn, 2, 12_000.0, [(ORE, 100)])     # worth 10 000, no deal
    rows, meta = deals.find_deals(conn, min_discount=0.05)
    assert [r["contract_id"] for r in rows] == [1]
    assert rows[0]["value"] == pytest.approx(10_000.0)
    assert rows[0]["saving"] == pytest.approx(2_000.0)
    assert rows[0]["discount"] == pytest.approx(0.2)


def test_the_discount_filter_is_a_floor():
    conn = _db()
    # Two different items on purpose: two contracts holding the SAME item become
    # each other's reference, which is the subject of the next test.
    _price(conn, ORE, 100.0)
    _price(conn, HULL, 100.0)
    _traded(conn, ORE)
    _traded(conn, HULL)
    _public(conn, 1, 9_500.0, [(ORE, 100)])      # 5% under
    _public(conn, 2, 8_000.0, [(HULL, 100)])     # 20% under
    assert len(deals.find_deals(conn, min_discount=0.05)[0]) == 2
    assert [r["contract_id"] for r in deals.find_deals(conn, min_discount=0.20)[0]] == [2]


def test_a_contract_is_judged_against_the_NEXT_cheapest_not_itself():
    """The cheapest offer of anything would otherwise score exactly zero against
    itself, and the list could never contain the contracts it exists to find.
    Here the market says 100 a unit and two contracts undercut it: the cheaper one
    is a deal against the dearer, and the dearer is no deal at all."""
    conn = _db()
    _price(conn, ORE, 100.0)
    _traded(conn, ORE)
    _public(conn, 1, 9_500.0, [(ORE, 100)])      # 95 a unit
    _public(conn, 2, 8_000.0, [(ORE, 100)])      # 80 a unit, the cheapest
    rows, _ = deals.find_deals(conn, min_discount=0.05)
    assert [r["contract_id"] for r in rows] == [2]
    assert rows[0]["value"] == pytest.approx(9_500.0), "measured against contract 1"


def test_a_contract_wanting_goods_back_is_not_called_a_bargain():
    """Reported from the screen: "a Mackinaw is not 8m". A contract offered one
    for 10 ISK and read as 100 % under - and it also demanded two Ladar-FTL
    Interlink Communicators worth 194M in return. The net was a real 8.6M, but
    "asking price" and "percent under" cannot describe a barter: the ISK figure
    is a token, and the true cost is goods the buyer may not own.

    134 contracts in the whole index are like this, 70 of them priced at exactly
    10 ISK.
    """
    conn = _db()
    _price(conn, HULL, 202_600_000.0)
    _price(conn, ORE, 97_000_000.0)
    _traded(conn, HULL)
    _traded(conn, ORE)
    # get a hull worth 202.6M, hand over two items worth 194M, pay 10 ISK
    _public(conn, 1, 10.0, [(HULL, 1, 1), (ORE, 2, 0)])
    rows, meta = deals.find_deals(conn, min_discount=0.05)
    assert rows == [], "a barter must not be presented as a 100 % discount"
    assert meta["skipped"]["barter"] == 1, "and it must be counted, not silently dropped"


# ── the three ways it used to lie ────────────────────────────────────────────

def test_a_blueprint_copy_is_never_valued_at_the_original(app_module):
    """The 350m Hel Blueprint against a 23b original. A copy shares its type id
    with the original and cannot be on the market at all, so no market price is a
    statement about it."""
    conn = _db()
    conn.execute("INSERT INTO sde_blueprints VALUES (?,0,0,0)", (BPRINT,))
    _price(conn, BPRINT, 23_000_000_000.0)
    _traded(conn, BPRINT)
    _public(conn, 1, 350_000_000.0, [(BPRINT, 1, 1, 1)])       # is_bpc = 1
    rows, meta = deals.find_deals(conn, min_discount=0.05)
    assert rows == []
    assert meta["skipped"]["copies"] == 1


def test_a_blueprint_whose_copy_flag_was_never_read_is_not_valued_either():
    conn = _db()
    conn.execute("INSERT INTO sde_blueprints VALUES (?,0,0,0)", (BPRINT,))
    _price(conn, BPRINT, 23_000_000_000.0)
    _traded(conn, BPRINT)
    conn.execute("INSERT INTO public_contracts (contract_id, region_id, type, price,"
                 " volume, issuer_id, system_id) VALUES (1,10000002,'item_exchange',"
                 " 350000000, 1.0, 9, 30000142)")
    conn.execute("INSERT INTO public_contract_items (contract_id, type_id, quantity,"
                 " is_included, is_bpc) VALUES (1,?,1,1,NULL)", (BPRINT,))
    # ...and something in the same index IS flagged, so the column is informative
    _public(conn, 2, 1.0, [(BPRINT, 1, 1, 1)])
    conn.commit()
    rows, _ = deals.find_deals(conn, min_discount=0.05)
    assert rows == []


def test_an_index_that_never_flags_a_copy_has_its_blueprints_left_out():
    """Measured: all 17 910 alliance item rows said "original", including a Hel
    Blueprint at 350m. A column that is uniformly zero is not evidence of no
    copies, it is evidence nobody read it."""
    conn = _db()
    conn.execute("INSERT INTO sde_blueprints VALUES (?,0,0,0)", (BPRINT,))
    _price(conn, BPRINT, 23_000_000_000.0)
    _price(conn, ORE, 100.0)
    _traded(conn, BPRINT)
    _traded(conn, ORE)
    _alliance(conn, 1, 350_000_000.0, [(BPRINT, 1, 0)])   # "original", says the flag
    _alliance(conn, 2, 8_000.0, [(ORE, 100, 0)])          # not a blueprint: fine
    rows, meta = deals.find_deals(conn, min_discount=0.05)
    assert [r["contract_id"] for r in rows] == [2], "the blueprint one must not appear"
    assert meta["blueprint_flag"]["alliance"] is False

    # Once ANY copy is recorded there, the flag starts meaning something again.
    conn.execute("UPDATE alliance_contract_items SET is_bpc=1 WHERE contract_id=1")
    conn.commit()
    deals.drop_cache()
    rows, meta = deals.find_deals(conn, min_discount=0.05)
    assert meta["blueprint_flag"]["alliance"] is True


def test_something_that_has_not_traded_is_not_priced():
    """A Rorqual SKIN quoted at 10b had not traded once in thirty days, and an
    abyssal module has no meaningful type price at all."""
    conn = _db()
    _price(conn, HULL, 10_000_000_000.0)
    _traded(conn, HULL, days_ago=deals.LIQUIDITY_DAYS + 5)     # too long ago
    _public(conn, 1, 1_000_000_000.0, [(HULL, 1)])
    rows, meta = deals.find_deals(conn, min_discount=0.05)
    assert rows == []
    assert meta["skipped"]["illiquid"] == 1

    _traded(conn, HULL, days_ago=3)
    deals.drop_cache()
    assert len(deals.find_deals(conn, min_discount=0.05)[0]) == 1


def test_liquidity_is_a_calendar_window_not_the_last_few_records():
    """ESI omits days with no trade, so "the last twelve records" for a Keepstar
    Blueprint span January to June. Summing those would read as a busy market."""
    conn = _db()
    old = (dt.date.today() - dt.timedelta(days=200)).isoformat()
    older = (dt.date.today() - dt.timedelta(days=300)).isoformat()
    conn.execute("INSERT INTO price_history_cache VALUES (?,?,?,?)",
                 (deals.JITA_REGION, HULL,
                  json.dumps([{"d": older, "vol": 1}, {"d": old, "vol": 1}]), time.time()))
    conn.commit()
    assert HULL not in deals.liquid_types(conn)


def test_the_reference_is_the_cheapest_offer_not_the_market():
    """Jita sell said a Salvation was worth 13.40b while nine contracts offered
    one at 7b. Quoting the market made all nine look like a bargain."""
    conn = _db()
    _price(conn, HULL, 13_400_000_000.0)
    _traded(conn, HULL)
    _public(conn, 1, 7_000_000_000.0, [(HULL, 1)])       # the cheap offer itself
    _public(conn, 2, 7_200_000_000.0, [(HULL, 1)])       # not a deal against 7b
    market, offers = deals.reference_prices(conn)
    # Contract 2 is judged against contract 1's 7b, not against the 13.4b market.
    assert deals._line_reference(HULL, market, offers, 2) == pytest.approx(7e9)
    assert deals.find_deals(conn, min_discount=0.05)[0] == [] or \
        [r["contract_id"] for r in deals.find_deals(conn, min_discount=0.05)[0]] == [1]


def test_a_bait_offer_does_not_reprice_everything():
    """One silly listing at 1 % of market must not make every honest contract
    look overpriced."""
    conn = _db()
    _price(conn, HULL, 10_000_000_000.0)
    _traded(conn, HULL)
    _public(conn, 1, 50_000_000.0, [(HULL, 1)])          # 0.5 % of market: bait
    market, offers = deals.reference_prices(conn)
    assert deals._line_reference(HULL, market, offers, 99) == pytest.approx(1e10), \
        "the market still rules"


def test_a_contract_in_player_sov_is_not_used_as_a_reference():
    """Same rule the appraisal already applies: in sov space the price says what
    one group charges its own, not what the thing is worth."""
    conn = _db()
    _price(conn, HULL, 10_000_000_000.0)
    _traded(conn, HULL)
    conn.execute("INSERT INTO sov_map_cache VALUES (30004759, 99000009, NULL, ?)", (time.time(),))
    conn.commit()
    _public(conn, 1, 2_000_000_000.0, [(HULL, 1)], system=30004759)
    market, offers = deals.reference_prices(conn)
    assert deals._line_reference(HULL, market, offers, 99) == pytest.approx(1e10)


# ── plumbing ─────────────────────────────────────────────────────────────────

def test_a_contract_whose_contents_were_never_read_is_not_judged():
    conn = _db()
    _price(conn, ORE, 100.0)
    _traded(conn, ORE)
    conn.execute("INSERT INTO public_contracts (contract_id, region_id, type, price,"
                 " volume, issuer_id, system_id) VALUES (9,10000002,'item_exchange',1,1,9,1)")
    conn.commit()
    assert deals.find_deals(conn, min_discount=0.05)[0] == []


def test_the_headline_is_the_most_valuable_line_not_the_biggest_stack():
    """A Rorqual with a fit and fuel was headlined "Oxygen Isotopes"."""
    conn = _db()
    _price(conn, HULL, 8_000_000_000.0)
    _price(conn, ORE, 10.0)
    _traded(conn, HULL)
    _traded(conn, ORE)
    _public(conn, 1, 5_000_000_000.0, [(HULL, 1), (ORE, 1_000_000)])
    rows, _ = deals.find_deals(conn, min_discount=0.05)
    assert rows[0]["top_type_id"] == HULL


def test_the_scan_is_cached_and_a_rescan_is_explicit(monkeypatch):
    conn = _db()
    _price(conn, ORE, 100.0)
    _traded(conn, ORE)
    _public(conn, 1, 8_000.0, [(ORE, 100)])
    scans = []
    real = deals._scan
    monkeypatch.setattr(deals, "_scan", lambda c: (scans.append(1), real(c))[1])
    deals.find_deals(conn, min_discount=0.05)
    deals.find_deals(conn, min_discount=0.10)
    assert len(scans) == 1, "one scan answers every filter"
    deals.find_deals(conn, min_discount=0.05, force=True)
    assert len(scans) == 2


def test_the_page_renders(client):
    r = client.get("/contracts/deals")
    assert r.status_code == 200
    assert "Deals" in r.text


def test_every_contracts_page_offers_all_five_tabs(client):
    for url in ("/contracts", "/contracts/alliance", "/contracts/public",
                "/contracts/deals"):
        html = client.get(url).text
        for tab in ("Personal", "Corporation", "Alliance", "Public", "Deals"):
            assert f">{tab}</a>" in html, (url, tab)


@pytest.fixture
def seeded_deal(app_module):
    """One real deal in the app's own database, so the Deals table actually
    renders rows. Without it the sort test skipped the page it was written for
    and passed against both faults it was meant to catch."""
    from app.web import contracts_helper as ch
    conn = app_module.get_conn()
    try:
        ch.ensure_public_contract_tables(conn)
        conn.execute("DELETE FROM public_contracts")
        conn.execute("DELETE FROM public_contract_items")
        conn.execute("INSERT INTO public_contracts (contract_id, region_id, type, price,"
                     " volume, title, start_location_id, issuer_id, system_id)"
                     " VALUES (7001, 10000002, 'item_exchange', 8000, 1, 'probe',"
                     " 60003760, 90000001, 30000142)")
        conn.execute("INSERT INTO public_contract_items (contract_id, type_id, quantity,"
                     " is_included, is_bpc) VALUES (7001, 34, 100, 1, 0)")
        conn.execute("INSERT OR REPLACE INTO market_price_cache (type_id, sell_price)"
                     " VALUES (34, 100)")
        day = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        conn.execute("INSERT OR REPLACE INTO price_history_cache VALUES (?,?,?,?)",
                     (deals.JITA_REGION, 34,
                      json.dumps([{"d": day, "avg": 100.0, "vol": 5000}]), time.time()))
        conn.commit()
    finally:
        conn.close()
    deals.drop_cache()
    yield
    conn = app_module.get_conn()
    try:
        conn.execute("DELETE FROM public_contracts WHERE contract_id = 7001")
        conn.execute("DELETE FROM public_contract_items WHERE contract_id = 7001")
        conn.commit()
    finally:
        conn.close()
    deals.drop_cache()


def test_every_contract_table_can_actually_be_sorted(client, seeded_deal):
    """Reported: the Deals columns did not sort. Two faults at once - the shared
    sorter was never included, and its rows lack the attribute it selects on
    (`tr[data-search]`), so including it alone would still have done nothing.

    That contract between template and script is invisible to the eye and to a
    page that merely renders, so it is pinned here for every contract table.
    """
    import re
    checked = 0
    for url in ("/contracts", "/contracts/alliance", "/contracts/public",
                "/contracts/deals"):
        html = client.get(url).text
        if 'id="contract-table"' not in html:
            continue                      # nothing indexed in this fixture
        checked += 1
        assert "th[data-sort]" in html, f"{url}: the sorter script is missing"
        heads = re.findall(r'<th[^>]*data-sort="([^:]+):', html)
        assert heads, f"{url}: no sortable columns"
        body = html[html.index("<tbody"):]
        assert "<tr data-search=" in body, \
            f"{url}: rows lack data-search, which is what the sorter selects on"
        for key in heads:
            attr = "data-" + re.sub(r"(?<!^)(?=[A-Z])", "-", key).lower()
            assert attr in body, f"{url}: column {key} sorts on a missing {attr}"
    assert checked, "no contract table was rendered, so nothing was checked"


def test_the_deals_table_renders_rows_to_sort(client, seeded_deal):
    r = client.get("/contracts/deals?discount=5")
    assert r.status_code == 200
    assert "<tr data-search=" in r.text


def test_the_items_toggle_collapses_by_hiding_not_by_emptying(client, seeded_deal):
    """Reported: expand, collapse, and the third click showed nothing. The first
    version collapsed by setting innerHTML to an empty string, so there was
    nothing left to put back - the row stayed blank for good.

    Verified in a browser through four clicks (shown/hidden/shown/hidden, content
    intact, computed display block then none). This pins the rule that made it
    work, because the failure only appears on the THIRD click and no rendering
    test would reach it.
    """
    import re
    html = client.get("/contracts/deals?discount=5").text
    fn = re.search(r"function dealItems\(.*?\n\}", html, re.S)
    assert fn, "the items toggle is missing"
    # Strip the comments first: the one explaining the bug quotes the very code
    # this is looking for.
    body = re.sub(r"//.*", "", fn.group(0))
    assert "hidden" in body, "collapsing must hide the box, not destroy it"
    assert not re.search(r"innerHTML\s*=\s*['\"]{2}", body), \
        "emptying innerHTML to collapse is the bug being pinned"


# ── can I actually get in? ───────────────────────────────────────────────────

def test_dockable_counts_npc_stations_and_structures_we_can_name():
    """A structure's name can only have come from /universe/structures/{id}/,
    and ESI answers that one with "Forbidden" unless you are on the ACL - so a
    name we hold is a structure a character is admitted to, and a name we lack is
    one nobody is."""
    conn = _db()
    conn.execute("CREATE TABLE sde_stations (station_id INTEGER PRIMARY KEY, name TEXT,"
                 " system_id INTEGER, region_id INTEGER)")
    conn.execute("CREATE TABLE location_name_cache (location_id INTEGER PRIMARY KEY,"
                 " name TEXT, solar_system_id INTEGER, region_id INTEGER, has_market INTEGER)")
    conn.execute("INSERT INTO sde_stations VALUES (60003760, 'Jita 4-4', 30000142, 10000002)")
    conn.execute("INSERT INTO location_name_cache VALUES (1022000000001, 'Our Fortizar',"
                 " 30000142, 10000002, 0)")
    conn.commit()
    got = deals.dockable_locations(conn)
    assert 60003760 in got, "an NPC station is open to everyone"
    assert 1022000000001 in got, "a structure we could name is one we are admitted to"
    assert 1022000000002 not in got, "one we have never been able to name is not"


def test_the_dockable_filter_hides_what_cannot_be_collected():
    conn = _db()
    conn.execute("CREATE TABLE sde_stations (station_id INTEGER PRIMARY KEY, name TEXT,"
                 " system_id INTEGER, region_id INTEGER)")
    conn.execute("CREATE TABLE location_name_cache (location_id INTEGER PRIMARY KEY,"
                 " name TEXT, solar_system_id INTEGER, region_id INTEGER, has_market INTEGER)")
    conn.execute("INSERT INTO sde_stations VALUES (60003760, 'Jita 4-4', 30000142, 10000002)")
    conn.commit()
    # Two different items: the same item twice would make the two contracts each
    # other's reference and neither would be a deal.
    _price(conn, ORE, 100.0)
    _price(conn, HULL, 100.0)
    _traded(conn, ORE)
    _traded(conn, HULL)
    _public(conn, 1, 8_000.0, [(ORE, 100)])                       # Jita, reachable
    conn.execute("UPDATE public_contracts SET start_location_id = 60003760 WHERE contract_id = 1")
    _public(conn, 2, 8_000.0, [(HULL, 100)])                      # a structure nobody can see
    conn.execute("UPDATE public_contracts SET start_location_id = 1099999999999"
                 " WHERE contract_id = 2")
    conn.commit()
    deals.drop_cache()

    everything, meta = deals.find_deals(conn, min_discount=0.05)
    assert len(everything) == 2
    assert meta["unreachable"] == 1, "and it says how many are out of reach"

    reachable, _ = deals.find_deals(conn, min_discount=0.05, dockable_only=True)
    assert [r["contract_id"] for r in reachable] == [1]
    assert reachable[0]["dockable"] is True


def test_the_checkbox_is_on_the_page(client, seeded_deal):
    html = client.get("/contracts/deals").text
    assert 'name="dockable"' in html
    assert client.get("/contracts/deals?dockable=1").status_code == 200
