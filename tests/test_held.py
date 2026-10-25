"""Net worth counts what ESI takes out of both the assets and the wallet.

Measured on a real account before this existed: 13 sell orders worth 1.09b at
Jita sell, up to 0.85b of buy order escrow and two open item exchange
contracts - none of it in the net worth figure.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app.character import held
from app.web.page_cache import drop_cached


CID = 900000001
STATION = 60003760


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sde_blueprint_products (blueprint_type_id INTEGER,"
                 " activity TEXT, product_type_id INTEGER, quantity INTEGER,"
                 " probability REAL)")
    conn.execute("INSERT INTO sde_blueprint_products VALUES (100, 'manufacturing', 10, 1, 1)")
    conn.execute("INSERT INTO sde_blueprint_products VALUES (200, 'reaction', 20, 200, 1)")
    return conn


@pytest.fixture
def esi(monkeypatch):
    """What ESI answers, per call; None makes that call fail."""
    answers = {"orders": [], "contracts": [], "jobs": [], "items": {}}
    calls = []

    async def orders(client, cid, tok):
        calls.append("orders")
        if answers["orders"] is None:
            raise RuntimeError("down")
        return answers["orders"]

    async def contracts(client, cid, tok):
        calls.append("contracts")
        if answers["contracts"] is None:
            raise RuntimeError("down")
        return answers["contracts"]

    async def jobs(client, cid, tok):
        calls.append("jobs")
        return answers["jobs"]

    async def items(client, cid, contract_id, tok):
        calls.append(("items", contract_id))
        return answers["items"].get(contract_id)

    monkeypatch.setattr(held, "_fetch_orders", orders)
    monkeypatch.setattr(held, "_fetch_contracts", contracts)
    monkeypatch.setattr(held, "_fetch_jobs", jobs)
    monkeypatch.setattr(held, "_fetch_contract_items", items)
    answers["calls"] = calls
    return answers


def _collect(conn, own=frozenset(), live=True):
    return asyncio.run(held.collect(conn, CID, "tok" if live else None, set(own),
                                    object() if live else None))


def test_sell_orders_count_their_remaining_units_and_buy_orders_their_escrow(esi):
    esi["orders"] = [
        {"type_id": 34, "is_buy_order": False, "volume_remain": 1000, "price": 9.0},
        {"type_id": 34, "volume_remain": 500, "price": 9.0},          # flag absent = sell
        {"type_id": 35, "is_buy_order": True, "volume_remain": 70, "escrow": 1234.5},
        {"type_id": 36, "is_corporation": True, "volume_remain": 99},  # the corp's, not ours
    ]
    h = _collect(_db())
    assert h["sell"] == {34: 1500}
    assert h["escrow"] == 1234.5
    # Priced like the assets (Jita sell), not at the asking price.
    v = held.value(h, {34: (5.0, 4.0)})
    assert v["market"] == 7500.0 and v["escrow"] == 1234.5 and v["total"] == 8734.5


def test_open_contracts_count_items_and_isk_paid_in(esi):
    esi["contracts"] = [
        # Own item exchange: the items in it, plus the reward paid in up front.
        {"contract_id": 1, "issuer_id": CID, "status": "outstanding",
         "type": "item_exchange", "price": 3e9, "reward": 100.0},
        # Already finished: nothing tied up any more.
        {"contract_id": 2, "issuer_id": CID, "status": "finished", "type": "item_exchange"},
        # Issued for the corporation: the corporation's, not the character's.
        {"contract_id": 3, "issuer_id": CID, "status": "outstanding",
         "for_corporation": True, "type": "item_exchange"},
        # A courier this character carries: the collateral it paid is held.
        {"contract_id": 4, "issuer_id": 1, "acceptor_id": CID, "status": "in_progress",
         "type": "courier", "collateral": 5000.0},
        # Someone else's contract offered to us: not ours until accepted.
        {"contract_id": 5, "issuer_id": 1, "assignee_id": CID, "status": "outstanding",
         "type": "item_exchange", "reward": 9e9},
    ]
    esi["items"] = {1: [{"type_id": 34, "quantity": 10, "is_included": True},
                        {"type_id": 35, "quantity": 3, "is_included": False}]}
    h = _collect(_db())
    assert h["contract_items"] == {34: 10}           # wanted items are not owned
    assert h["contract_isk"] == 5100.0
    assert [c for c in esi["calls"] if isinstance(c, tuple)] == [("items", 1)]
    assert held.value(h, {34: (5.0,)})["contracts"] == 5150.0


def test_contract_contents_are_fetched_once(esi):
    conn = _db()
    esi["contracts"] = [{"contract_id": 7, "issuer_id": CID, "status": "outstanding",
                         "type": "auction"}]
    esi["items"] = {7: [{"type_id": 34, "quantity": 2}]}
    _collect(conn)
    drop_cached(conn, "nw_contracts")                  # force a new listing
    h = _collect(conn)
    assert esi["calls"].count(("items", 7)) == 1
    assert h["contract_items"] == {34: 2}


def test_contents_that_did_not_arrive_are_not_stored_as_empty(esi):
    conn = _db()
    esi["contracts"] = [{"contract_id": 8, "issuer_id": CID, "status": "outstanding",
                         "type": "item_exchange"}]
    h = _collect(conn)                                  # items: None = ESI failed
    assert h["unknown"] == ["contract items"]
    esi["items"] = {8: [{"type_id": 34, "quantity": 1}]}
    assert _collect(conn)["contract_items"] == {34: 1}


def test_undelivered_personal_jobs_count_their_product(esi):
    esi["jobs"] = [
        # In the station hangar: personal. 5 runs x 1.
        {"activity_id": 1, "status": "active", "blueprint_type_id": 100,
         "product_type_id": 10, "runs": 5, "blueprint_id": 1,
         "blueprint_location_id": STATION, "station_id": STATION, "facility_id": STATION},
        # Reaction, done but not delivered, blueprint in an own container. 3 x 200.
        {"activity_id": 9, "status": "ready", "blueprint_type_id": 200,
         "product_type_id": 20, "runs": 3, "blueprint_id": 2,
         "blueprint_location_id": 555, "station_id": STATION, "facility_id": STATION},
        # Delivered: the product is in the assets already.
        {"activity_id": 1, "status": "delivered", "blueprint_type_id": 100,
         "product_type_id": 10, "runs": 50, "blueprint_location_id": STATION,
         "station_id": STATION, "facility_id": STATION},
        # Blueprint in a corporation office: a corporation job.
        {"activity_id": 1, "status": "active", "blueprint_type_id": 100,
         "product_type_id": 10, "runs": 70, "blueprint_id": 3,
         "blueprint_location_id": 777, "station_id": STATION, "facility_id": STATION},
        # Copying makes a blueprint copy, which net worth leaves out.
        {"activity_id": 5, "status": "active", "blueprint_type_id": 100,
         "product_type_id": 100, "runs": 10, "blueprint_location_id": STATION,
         "station_id": STATION, "facility_id": STATION},
    ]
    h = _collect(_db(), own={555})
    assert h["jobs"] == {10: 5, 20: 600}


def test_an_old_answer_is_used_when_esi_fails(esi):
    conn = _db()
    esi["orders"] = [{"type_id": 34, "volume_remain": 10}]
    _collect(conn)
    conn.execute("UPDATE page_cache SET cached_at = cached_at - 99999")
    esi["orders"] = None
    h = _collect(conn)
    assert h["sell"] == {34: 10} and "orders" not in h["unknown"]


def test_without_esi_only_stored_answers_are_used(esi):
    h = _collect(_db(), live=False)
    assert esi["calls"] == []
    assert set(h["unknown"]) == {"orders", "contracts", "industry jobs"}


def test_blueprints_are_left_out_like_in_the_assets(esi):
    esi["orders"] = [{"type_id": 34, "volume_remain": 1}, {"type_id": 999, "volume_remain": 1}]
    h = _collect(_db())
    assert held.value(h, {34: (5.0,), 999: (1e9,)}, skip_types={999})["market"] == 5.0


def test_dashboard_net_worth_includes_what_is_held(client, app_module):
    m = app_module
    orig = m.held_api._fetch_orders

    async def orders(*_a):
        return [{"type_id": 34, "volume_remain": 1000}]
    m.held_api._fetch_orders = orders
    try:
        conn = m.get_conn()
        try:
            drop_cached(conn, "nw_orders")
        finally:
            conn.close()
        data = client.get("/api/dashboard/live?force=1").json()
    finally:
        m.held_api._fetch_orders = orig
        conn = m.get_conn()
        try:
            drop_cached(conn, "nw_orders")
        finally:
            conn.close()
    c = data["chars"]["900000001"]
    assert c["held_str"] and c["held_parts"]["market"] == c["held_str"]
    as_num = lambda s: int("".join(ch for ch in s if ch.isdigit()))
    assert (as_num(c["net_worth_str"])
            == as_num(c["asset_value_str"]) + as_num(c["wallet_str"]) + as_num(c["held_str"]))
