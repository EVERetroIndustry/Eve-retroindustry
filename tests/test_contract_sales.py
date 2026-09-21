"""What actually sold on alliance contracts, and when.

Prices counts units traded on the market over seven days. ESI has no equivalent
for contracts - but for ALLIANCE contracts the raw material is there: the corp
endpoint returns finished contracts alongside outstanding, and every finished one
carries `date_completed` (measured on a live account: 487 of 487). Public
contracts cannot do this at all, because ESI lists only what is still on offer,
so a sale is a row that vanished and nothing separates it from one the issuer
withdrew.

Measured after wiring this up: 1132 sales over 30 days, 2.33 trillion ISK, with
the contents of 881 of them read - 43 398 units of Nanite Repair Paste across 283
contracts in a week.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from app.web import contract_sales as cs
from app.web import contracts_helper as ch


ALLY, ORE, HULL = 99000001, 34, 645


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    ch.ensure_alliance_contract_tables(conn)
    cs.ensure_sales_tables(conn)
    return conn


def _iso(days_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() - days_ago * 86400))


def _contract(cid, status="finished", done_days_ago=1.0, price=1e9, **kw):
    c = {"contract_id": cid, "type": "item_exchange", "status": status,
         "price": price, "volume": 100.0,
         "date_issued": _iso(10), "date_expired": _iso(-20),
         "issuer_id": 5, "acceptor_id": 7}
    if status == "finished":
        c["date_accepted"] = _iso(done_days_ago)
        c["date_completed"] = _iso(done_days_ago)
    c.update(kw)
    return c


def _items(conn, cid, rows):
    """rows: (type_id, quantity, is_included)"""
    conn.executemany("INSERT INTO alliance_contract_items (contract_id, type_id,"
                     " quantity, is_included, is_bpc) VALUES (?,?,?,?,0)",
                     [(cid, t, q, inc) for t, q, inc in rows])
    conn.commit()


def test_only_finished_contracts_with_a_completion_date_are_sales():
    conn = _db()
    added = cs.record_sales(conn, ALLY, [
        _contract(1, "finished"),
        _contract(2, "outstanding"),
        _contract(3, "deleted"),
        _contract(4, "finished", date_completed=None, date_accepted=None),
    ])
    assert added == 1
    assert [r[0] for r in conn.execute("SELECT contract_id FROM contract_sales")] == [1]


def test_a_sale_is_written_once_and_never_rewritten():
    """The same thirty days are re-read constantly; history must not move."""
    conn = _db()
    cs.record_sales(conn, ALLY, [_contract(1, price=1e9)])
    cs.record_sales(conn, ALLY, [_contract(1, price=5e9)])
    rows = conn.execute("SELECT contract_id, price FROM contract_sales").fetchall()
    assert rows == [(1, 1e9)], "the first reading stands"


def test_the_history_outlives_the_listing_it_came_from():
    """`_store_alliance` REPLACES the contract rows on every re-listing, and ESI's
    own window is 29 days - so a sale kept only there would quietly vanish. That
    is the whole reason for a separate table."""
    conn = _db()
    cs.record_sales(conn, ALLY, [_contract(1)])
    _items(conn, 1, [(ORE, 500, 1)])
    cs.harvest_items(conn)

    conn.execute("DELETE FROM alliance_contracts")
    conn.execute("DELETE FROM alliance_contract_items")
    conn.commit()

    assert cs.sales_summary(conn, 30)["contracts"] == 1
    assert cs.sold_by_type(conn, 30) == [{"type_id": ORE, "quantity": 500, "contracts": 1}]


def test_contents_are_copied_out_and_only_once():
    conn = _db()
    cs.record_sales(conn, ALLY, [_contract(1)])
    _items(conn, 1, [(ORE, 100, 1), (ORE, 50, 1), (HULL, 1, 1)])
    assert cs.harvest_items(conn) == 1
    assert cs.harvest_items(conn) == 0, "already read; not fetched again"
    got = {r["type_id"]: r["quantity"] for r in cs.sold_by_type(conn, 30)}
    assert got == {ORE: 150, HULL: 1}, "stacks of a type add up within a contract"


def test_items_the_contract_asked_for_are_not_sold():
    """A contract can demand goods back. Those moved the other way."""
    conn = _db()
    cs.record_sales(conn, ALLY, [_contract(1)])
    _items(conn, 1, [(ORE, 100, 1), (HULL, 2, 0)])
    cs.harvest_items(conn)
    assert {r["type_id"] for r in cs.sold_by_type(conn, 30)} == {ORE}


def test_a_sale_whose_contents_were_never_read_still_counts_as_a_sale():
    """And the summary says how many, so a quantity is never mistaken for the
    whole picture."""
    conn = _db()
    cs.record_sales(conn, ALLY, [_contract(1), _contract(2)])
    _items(conn, 1, [(ORE, 10, 1)])
    cs.harvest_items(conn)
    s = cs.sales_summary(conn, 30)
    assert s["contracts"] == 2 and s["with_items"] == 1


def test_the_window_is_rolling_and_measured_from_completion():
    conn = _db()
    cs.record_sales(conn, ALLY, [
        _contract(1, done_days_ago=2), _contract(2, done_days_ago=20)])
    _items(conn, 1, [(ORE, 10, 1)])
    _items(conn, 2, [(ORE, 70, 1)])
    cs.harvest_items(conn)
    assert cs.sold_by_type(conn, 7) == [{"type_id": ORE, "quantity": 10, "contracts": 1}]
    assert cs.sold_by_type(conn, 30) == [{"type_id": ORE, "quantity": 80, "contracts": 2}]


def test_sold_quantities_answers_for_the_types_asked_about():
    conn = _db()
    cs.record_sales(conn, ALLY, [_contract(1)])
    _items(conn, 1, [(ORE, 10, 1), (HULL, 1, 1)])
    cs.harvest_items(conn)
    got = cs.sold_quantities(conn, [ORE, 99999], 30)
    assert set(got) == {ORE}
    assert got[ORE]["quantity"] == 10


def test_finished_contracts_are_stored_with_their_completion_fields():
    """ESI sends date_accepted, date_completed and acceptor_id on every finished
    contract and the app used to discard all three."""
    conn = _db()
    ch._store_alliance(conn, ALLY, [_contract(1)], {}, {})
    row = conn.execute("SELECT date_completed, date_accepted, acceptor_id"
                       " FROM alliance_contracts WHERE contract_id=1").fetchone()
    assert row[0] and row[1] and row[2] == 7
    assert cs.sales_summary(conn, 30)["contracts"] == 1, "listing it records the sale"


def test_the_endpoint_answers_for_an_item(client):
    d = client.get("/api/contracts/sold?type_id=34").json()
    assert d["type_id"] == 34
    assert [w["days"] for w in d["windows"]] == [7, 30]
    assert "with_items_30d" in d and "sales_30d" in d


# ── prices actually paid ─────────────────────────────────────────────────────

def test_only_single_item_contracts_give_a_unit_price():
    """A bundle sells for one price covering everything in it, so dividing that
    by one line's quantity would invent a number. Same rule the appraisal uses."""
    conn = _db()
    cs.record_sales(conn, ALLY, [
        _contract(1, price=2e9),                     # one Rorqual
        _contract(2, price=3e9)])                    # a Rorqual AND ore
    _items(conn, 1, [(HULL, 1, 1)])
    _items(conn, 2, [(HULL, 1, 1), (ORE, 5000, 1)])
    cs.harvest_items(conn)
    ser = cs.price_series(conn, HULL, 90)
    assert [p["contract_id"] for p in ser] == [1]
    assert ser[0]["unit"] == pytest.approx(2e9)


def test_the_unit_price_divides_by_the_quantity():
    conn = _db()
    cs.record_sales(conn, ALLY, [_contract(1, price=1e9)])
    _items(conn, 1, [(ORE, 4, 1)])
    cs.harvest_items(conn)
    ser = cs.price_series(conn, ORE, 90)
    assert ser[0]["unit"] == pytest.approx(2.5e8)
    assert ser[0]["price"] == pytest.approx(1e9) and ser[0]["qty"] == 4


def test_the_series_runs_oldest_first_and_respects_the_window():
    conn = _db()
    cs.record_sales(conn, ALLY, [
        _contract(1, done_days_ago=40, price=1e9),
        _contract(2, done_days_ago=5, price=2e9),
        _contract(3, done_days_ago=1, price=3e9)])
    for cid in (1, 2, 3):
        _items(conn, cid, [(HULL, 1, 1)])
    cs.harvest_items(conn)
    assert [p["contract_id"] for p in cs.price_series(conn, HULL, 30)] == [2, 3]
    assert [p["contract_id"] for p in cs.price_series(conn, HULL, 90)] == [1, 2, 3]


def test_a_free_contract_is_not_a_price():
    """price 0 is a handover, not a sale at zero."""
    conn = _db()
    cs.record_sales(conn, ALLY, [_contract(1, price=0)])
    _items(conn, 1, [(HULL, 1, 1)])
    cs.harvest_items(conn)
    assert cs.price_series(conn, HULL, 90) == []


def test_the_endpoint_carries_the_price_series(client):
    d = client.get("/api/contracts/sold?type_id=34").json()
    assert "series" in d and isinstance(d["series"], list)


def test_the_item_popup_keeps_the_three_quantities_apart(client):
    """The first attempt put a contract figure into the market chart's subtitle,
    which made neither readable. One tab, one quantity."""
    html = client.get("/prices").text
    for tab in ("chart", "market", "contracts"):
        assert f'data-tab="{tab}"' in html, tab
    assert 'id="hist-contracts-view"' in html
    # ...and the contract tab must not be wired into the market subtitle again
    assert "hist-contracts\"" not in html.replace('id="hist-contracts-view"', "")
