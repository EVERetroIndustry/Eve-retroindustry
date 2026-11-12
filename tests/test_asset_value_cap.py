"""Assets and net worth use the same sell cap as Deals and the contract appraisal.

A sell order is only an asking price. Carnotite's cheapest Jita sell was
1 000 000 ISK while it traded at 2 720; a SKIN asked 134M while it traded at
2.3M. Holding either would have inflated the Assets page and net worth by the
same mistake Deals made, so the shared price hook caps it too.
"""
from __future__ import annotations

import datetime as dt
import json
import time

import pytest

from app.web import contracts_helper as ch
from app.web.prices_helper import fill_missing_sell_prices

TRIT = 34
SKIN = 56609


@pytest.fixture
def conn(app_module):
    from app.market.prices import ensure_price_table
    c = app_module.get_conn()
    ensure_price_table(c)
    saved = c.execute("SELECT region_id, type_id, data_json, cached_at FROM"
                      " price_history_cache WHERE type_id IN (?,?)", (TRIT, SKIN)).fetchall()
    c.execute("DELETE FROM price_history_cache WHERE type_id IN (?,?)", (TRIT, SKIN))
    c.commit()
    yield c
    c.execute("DELETE FROM price_history_cache WHERE type_id IN (?,?)", (TRIT, SKIN))
    c.executemany("INSERT INTO price_history_cache VALUES (?,?,?,?)", saved)
    c.commit()
    c.close()


def _history(conn, type_id, avg, days_ago=2, vol=10):
    day = (dt.date.today() - dt.timedelta(days=days_ago)).isoformat()
    conn.execute("INSERT OR REPLACE INTO price_history_cache VALUES (?,?,?,?)",
                 (ch.JITA_REGION, type_id,
                  json.dumps([{"d": day, "avg": avg, "vol": vol}]), time.time()))
    conn.commit()


def test_a_sell_order_nobody_pays_is_replaced_by_the_traded_price(conn):
    _history(conn, SKIN, 2_300_000.0)
    prices = {SKIN: (134_000_000.0, 1_000_000.0)}
    assert fill_missing_sell_prices(conn, prices) == {SKIN: "traded"}
    assert prices[SKIN] == (2_300_000.0, 1_000_000.0)          # buy side kept


def test_a_sell_order_near_the_trades_is_kept(conn):
    _history(conn, SKIN, 50_000_000.0)
    prices = {SKIN: (134_000_000.0, 1_000_000.0)}
    assert fill_missing_sell_prices(conn, prices) == {}
    assert prices[SKIN] == (134_000_000.0, 1_000_000.0)


def test_without_recent_trades_there_is_nothing_to_cap_against(conn):
    _history(conn, SKIN, 2_300_000.0, days_ago=45)            # outside the window
    prices = {SKIN: (134_000_000.0, None)}
    assert fill_missing_sell_prices(conn, prices) == {}
    assert prices[SKIN] == (134_000_000.0, None)


def test_the_assets_page_shows_the_traded_price_and_says_so(conn, client):
    """End to end: the seeded hangar holds 10 000 Tritanium at a sell of 5.00.
    With Tritanium trading at 1.00 the sell is over three times that."""
    _history(conn, TRIT, 1.0)
    html = client.get("/assets").text
    assert "Tritanium" in html, "the seeded Tritanium is not on the page"
    assert ">traded</span>" in html
    assert "median daily price of its Jita trades" in html
