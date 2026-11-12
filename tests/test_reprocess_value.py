"""A sell order is an asking price, and ore is worth its minerals.

Found on a real index: 210 606 Carnotite and 52 512 Replete Carnotite (plus some
PI) for 750M sat at the top of Deals as worth 210.63b. Carnotite's cheapest Jita
sell was 1 000 000 ISK a unit while it had traded at 2 526-2 720, and Replete
Carnotite's was 251 ISK for the 50 units on offer. What the pile is worth to
anyone who buys it is what it reprocesses into: about 1.5b.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time

import pytest

from app.web import contracts_helper as ch
from app.web import deals

CARNOTITE, REPLETE = 45502, 46304
ATMO, COBALT, TECH = 16634, 16640, 16649
BASE_METALS = 2267
HULL = 22544


def _history(conn, type_id, days):
    """days: [(days_ago, avg, vol)]"""
    rows = [{"d": (dt.date.today() - dt.timedelta(days=ago)).isoformat(),
             "avg": avg, "low": avg, "high": avg, "vol": vol} for ago, avg, vol in days]
    conn.execute("INSERT OR REPLACE INTO price_history_cache (region_id, type_id,"
                 " data_json, cached_at) VALUES (?,?,?,?)",
                 (ch.JITA_REGION, type_id, json.dumps(rows), time.time()))
    conn.commit()


def _price(conn, type_id, sell, buy=None):
    conn.execute("INSERT OR REPLACE INTO market_price_cache (type_id, sell_price,"
                 " buy_price, cached_at) VALUES (?,?,?,0)", (type_id, sell, buy))
    conn.commit()


# ── the yield ─────────────────────────────────────────────────────────────────

def test_the_yield_is_the_best_reachable_one():
    # T2 rig 53 % x nullsec 1.12 x Tatara 5.5 % x Reprocessing V x Efficiency V
    # x ore skill V x RX-804. EVE University quotes 90.6 % for the same setup.
    assert ch.REPROCESS_YIELD == pytest.approx(0.9063, abs=5e-5)


# ── the sell cap on its own ──────────────────────────────────────────────────

def test_a_sell_order_far_above_the_trades_counts_at_the_traded_price():
    assert ch.credible_sell(1_000_000.0, 2_720.0) == (2_720.0, True)


def test_a_sell_order_near_the_trades_is_left_alone():
    assert ch.credible_sell(8_000.0, 2_720.0) == (8_000.0, False)
    assert ch.credible_sell(8_000.0, None) == (8_000.0, False)   # never traded
    assert ch.credible_sell(None, 2_720.0) == (None, False)      # nothing on sale


def test_one_silly_trade_does_not_move_the_traded_price():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE price_history_cache (region_id INTEGER, type_id INTEGER,"
                 " data_json TEXT, cached_at REAL, PRIMARY KEY (region_id, type_id))")
    # Carnotite's real month: two quiet days and one somebody paid 50 000 on.
    _history(conn, CARNOTITE, [(30, 2_526.0, 46), (20, 2_720.0, 26), (16, 13_099.32, 41),
                               (45, 900_000.0, 1)])        # outside the window
    assert ch.jita_trade_summary(conn)[CARNOTITE] == pytest.approx(2_720.0)


def test_a_day_without_an_average_still_counts_as_traded():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE price_history_cache (region_id INTEGER, type_id INTEGER,"
                 " data_json TEXT, cached_at REAL, PRIMARY KEY (region_id, type_id))")
    day = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    conn.execute("INSERT INTO price_history_cache VALUES (?,?,?,?)",
                 (ch.JITA_REGION, HULL, json.dumps([{"d": day, "vol": 3}]), time.time()))
    assert ch.jita_trade_summary(conn) == {HULL: None}


# ── the Deals list ───────────────────────────────────────────────────────────

def _deals_db() -> sqlite3.Connection:
    from tests.test_deals import _db
    conn = _db()
    conn.execute("CREATE TABLE sde_ore_reprocess (type_id INTEGER, material_type_id INTEGER,"
                 " quantity INTEGER, portion_size INTEGER)")
    conn.executemany("INSERT INTO sde_ore_reprocess VALUES (?,?,?,100)",
                     [(CARNOTITE, ATMO, 15), (CARNOTITE, COBALT, 10), (CARNOTITE, TECH, 50),
                      (REPLETE, ATMO, 17), (REPLETE, COBALT, 12), (REPLETE, TECH, 58)])
    for tid, sell in ((ATMO, 130.0), (COBALT, 694.9), (TECH, 12_760.0),
                      (CARNOTITE, 1_000_000.0), (REPLETE, 251.2), (BASE_METALS, 2.5)):
        _price(conn, tid, sell)
    for tid, avg in ((ATMO, 135.0), (COBALT, 690.0), (TECH, 12_500.0),
                     (CARNOTITE, 2_720.0), (REPLETE, 251.1), (BASE_METALS, 2.6)):
        _history(conn, tid, [(2, avg, 1000)])
    return conn


def _contract(conn, cid, price, items):
    conn.execute("INSERT INTO public_contracts (contract_id, region_id, type, price,"
                 " volume, start_location_id, issuer_id, system_id)"
                 " VALUES (?,?,?,?,?,?,?,?)",
                 (cid, 10000016, "item_exchange", price, 1.0, 60003760, 90001, 30001363))
    conn.executemany("INSERT INTO public_contract_items (contract_id, type_id, quantity,"
                     " is_included, is_bpc) VALUES (?,?,?,1,0)",
                     [(cid, t, q) for t, q in items])
    conn.commit()


def _per_unit(mats):
    return ch.REPROCESS_YIELD * sum(q * p for q, p in mats) / 100


def test_the_carnotite_contract_is_worth_its_minerals_not_210b():
    conn = _deals_db()
    _contract(conn, 1, 750e6, [(CARNOTITE, 210_606), (REPLETE, 52_512),
                               (BASE_METALS, 1_702_433)])
    rows, _meta = deals.find_deals(conn, min_discount=0.05)
    assert [r["contract_id"] for r in rows] == [1]
    carn = _per_unit([(15, 130.0), (10, 694.9), (50, 12_760.0)])
    repl = _per_unit([(17, 130.0), (12, 694.9), (58, 12_760.0)])
    expect = 210_606 * carn + 52_512 * repl + 1_702_433 * 2.5
    assert rows[0]["value"] == pytest.approx(expect)
    assert 1.4e9 < rows[0]["value"] < 1.7e9          # not 210b, not 0.6b either


def test_ore_whose_own_market_never_traded_is_still_valued():
    """Ore is worth its minerals whether or not the ore itself traded."""
    conn = _deals_db()
    conn.execute("DELETE FROM price_history_cache WHERE type_id = ?", (CARNOTITE,))
    conn.commit()
    _contract(conn, 1, 100e6, [(CARNOTITE, 100_000)])
    rows, meta = deals.find_deals(conn, min_discount=0.05)
    assert [r["contract_id"] for r in rows] == [1]
    assert meta["skipped"]["illiquid"] == 0


def test_ore_with_an_unpriced_material_falls_back_to_the_market_rule():
    conn = _deals_db()
    conn.execute("DELETE FROM market_price_cache WHERE type_id = ?", (TECH,))
    conn.commit()
    _contract(conn, 1, 100e6, [(CARNOTITE, 100_000)])
    rows, _meta = deals.find_deals(conn, min_discount=0.0)
    # 100 000 x 2 720 (the traded price, not the 1M sell) = 272M
    assert rows[0]["value"] == pytest.approx(100_000 * 2_720.0)


def test_a_thin_market_item_is_valued_at_what_it_trades_for():
    conn = _deals_db()
    _price(conn, HULL, 134e6)
    _history(conn, HULL, [(3, 2.3e6, 1), (9, 2.2e6, 2)])
    _contract(conn, 1, 0.8e6, [(HULL, 1)])
    rows, _meta = deals.find_deals(conn, min_discount=0.05)
    assert rows[0]["value"] == pytest.approx(2.25e6)


def test_a_cheap_honest_contract_is_no_longer_called_bait():
    """With the market at 1M, an offer at 3 000 read as bait (under a tenth). At
    the traded price it is an ordinary offer and becomes the reference."""
    conn = _deals_db()
    _price(conn, HULL, 1_000_000.0)
    _history(conn, HULL, [(3, 2_720.0, 10)])
    _contract(conn, 1, 3_000.0, [(HULL, 1)])     # the cheapest single-item offer
    market, offers = deals.reference_prices(conn)
    assert deals._line_reference(HULL, market, offers, 99) == pytest.approx(2_720.0)


def test_the_minerals_of_contracted_ore_get_a_history_fetched():
    conn = _deals_db()
    conn.execute("DELETE FROM price_history_cache")
    conn.commit()
    _contract(conn, 1, 1e6, [(CARNOTITE, 10)])
    assert {ATMO, COBALT, TECH, CARNOTITE} <= set(deals.types_needing_history(conn))


# ── the contract you open from it ────────────────────────────────────────────

@pytest.fixture
def conn(app_module):
    c = app_module.get_conn()
    from app.market.prices import ensure_price_table
    ensure_price_table(c)
    ch.ensure_public_contract_tables(c)
    ch.ensure_alliance_contract_tables(c)
    mine = (ATMO, COBALT, TECH, CARNOTITE, HULL)
    ph = ",".join("?" * len(mine))

    def _clear():
        for t in ("market_price_cache", "price_history_cache"):
            c.execute(f"DELETE FROM {t} WHERE type_id IN ({ph})", mine)
        c.commit()
    _clear()
    yield c
    _clear()                     # the session's seeded prices stay as they were
    c.close()


def test_the_bundled_sde_knows_what_carnotite_reprocesses_into(conn):
    portion, mats = ch.ore_materials(conn, [CARNOTITE])[CARNOTITE]
    assert portion == 100
    assert sorted(mats) == [(ATMO, 15), (COBALT, 10), (TECH, 50)]
    # Ice and compressed ore are in the same table; a module is not.
    assert conn.execute("SELECT COUNT(DISTINCT type_id) FROM sde_ore_reprocess").fetchone()[0] > 400
    assert not ch.ore_materials(conn, [HULL])


def test_the_appraisal_agrees_with_the_deals_list(conn):
    for tid, sell in ((ATMO, 130.0), (COBALT, 694.9), (TECH, 12_760.0),
                      (CARNOTITE, 1_000_000.0), (HULL, 134e6)):
        _price(conn, tid, sell)
    _history(conn, HULL, [(3, 2.3e6, 1)])
    items = [{"type_id": CARNOTITE, "quantity": 1000, "name": "Carnotite"},
             {"type_id": HULL, "quantity": 1, "name": "Hull"}]
    a = ch.appraise_items(conn, items)
    carn, hull = items
    assert carn["price_source"] == "reprocess"
    assert carn["unit"] == pytest.approx(_per_unit([(15, 130.0), (10, 694.9), (50, 12_760.0)]))
    assert carn["jita_sell"] == 1_000_000.0          # still shown, just not believed
    assert hull["price_source"] == "traded"
    assert hull["unit"] == pytest.approx(2.3e6) and hull["traded"] == pytest.approx(2.3e6)
    assert a["counts"]["reprocess"] == 1 and a["counts"]["traded"] == 1
    assert a["reprocess_yield"] == ch.REPROCESS_YIELD
