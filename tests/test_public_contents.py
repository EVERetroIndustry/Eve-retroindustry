"""A public contract says what is in it, and who issued it the way the game does.

Reported: "most expensive first" showed an Azariel for 465b on the 11th row, but
the row had no title, so nothing on it said what was for sale; the Search box
did not look inside contracts either. The game also names the corporation as
the issuer of a contract made on its behalf (NOD1CE, not the character).
"""
from __future__ import annotations

import sqlite3

from app.web import contracts_helper as CH

AZARIEL, AZARIEL_BP, TRIT = 78576, 79326, 34


def _db():
    conn = sqlite3.connect(":memory:")
    CH.ensure_public_contract_tables(conn)
    conn.execute("CREATE TABLE sde_types (type_id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO sde_types VALUES (?,?)",
                     [(AZARIEL, "Azariel"), (AZARIEL_BP, "Azariel Blueprint"),
                      (TRIT, "Tritanium")])
    conn.execute("CREATE TABLE market_price_cache (type_id INTEGER PRIMARY KEY,"
                 " sell_price REAL)")
    conn.execute("INSERT INTO market_price_cache VALUES (34, 5.0)")
    for cid, price in ((1, 465e9), (2, 345e9), (3, 1e6), (4, 0.0)):
        conn.execute("INSERT INTO public_contracts (contract_id, type, price, region_id,"
                     " title) VALUES (?, 'item_exchange', ?, 10000002, '')", (cid, price))
    conn.executemany(
        "INSERT INTO public_contract_items (contract_id, type_id, quantity, is_included,"
        " is_bpc) VALUES (?,?,?,?,?)",
        [(1, AZARIEL, 1, 1, 0),
         (2, AZARIEL_BP, 1, 1, 1),
         (3, TRIT, 1000, 1, 0), (3, AZARIEL_BP, 1, 1, 1),
         (4, AZARIEL, 1, 0, 0)])
    conn.commit()
    return conn


def test_each_contract_names_what_it_holds():
    s = CH.contents_summary(_db(), [1, 2, 3, 4, 99])
    assert s[1] == {"label": "Azariel", "more": 0, "wants": False}
    assert s[2]["label"] == "Azariel Blueprint (copy)"
    # The most valuable line leads; a copy has no market value of its own.
    assert s[3] == {"label": "Tritanium x 1 000", "more": 1, "wants": False}
    assert s[4] == {"label": "Azariel", "more": 0, "wants": True}
    assert 99 not in s                       # contents not read yet


def test_the_search_box_looks_inside_contracts():
    rows, total = CH.search_public_contracts(_db(), None, q="azariel", sort="price_hi")
    assert [r["contract_id"] for r in rows] == [1, 2, 3]   # 4 only wants one
    assert total == 3


def test_a_corporation_issuer_is_stored():
    conn = _db()
    CH._store_public_listing(conn, 10000030, [
        {"contract_id": 7, "type": "item_exchange", "price": 465e9, "issuer_id": 90504880,
         "issuer_corporation_id": 98788909, "for_corporation": True},
        {"contract_id": 8, "type": "item_exchange", "price": 1.0, "issuer_id": 1,
         "issuer_corporation_id": 2, "for_corporation": False}], {})
    rows, _ = CH.search_public_contracts(conn, 10000030)
    got = {r["contract_id"]: (r["for_corporation"], r["issuer_corporation_id"]) for r in rows}
    assert got == {7: (1, 98788909), 8: (0, 2)}


def test_the_page_shows_contents_and_the_corporation(client, app_module, monkeypatch):
    m = app_module
    conn = m.get_conn()
    try:
        CH.ensure_public_contract_tables(conn)
        conn.execute("INSERT OR REPLACE INTO public_contracts (contract_id, type, price,"
                     " region_id, title, issuer_id, for_corporation, issuer_corporation_id,"
                     " date_expired) VALUES (9123, 'item_exchange', 465e9, 10000030, '',"
                     " 90504880, 1, 98788909, '2099-01-01T00:00:00Z')")
        conn.execute("DELETE FROM public_contract_items WHERE contract_id=9123")
        conn.execute("INSERT INTO public_contract_items (contract_id, type_id, quantity,"
                     " is_included, is_bpc) VALUES (9123, ?, 1, 1, 0)", (AZARIEL,))
        conn.execute("INSERT OR REPLACE INTO public_contract_meta VALUES (10000030, 1e10, 1)")
        conn.commit()
    finally:
        conn.close()

    async def names(ids):
        return {90504880: "Karaff Tuborg", 98788909: "NOD1CE"}
    monkeypatch.setattr(m, "_resolve_party_names", names)
    try:
        html = client.get("/contracts/public?sort=price_hi&q=azariel").text
    finally:
        conn = m.get_conn()
        conn.execute("DELETE FROM public_contracts WHERE contract_id=9123")
        conn.execute("DELETE FROM public_contract_items WHERE contract_id=9123")
        conn.commit()
        conn.close()
    assert "Azariel" in html
    assert ">NOD1CE<" in html and "Issued by Karaff Tuborg for the corporation" in html


def test_header_sorts_ask_the_server_in_server_mode(client, app_module):
    # The page holds only the first rows of the whole index; a header click
    # that sorted those in the browser showed the dearest of the cheapest 300.
    m = app_module
    conn = m.get_conn()
    try:
        CH.ensure_public_contract_tables(conn)
        conn.execute("INSERT OR REPLACE INTO public_contracts (contract_id, type, price,"
                     " region_id, title, date_expired) VALUES (9124, 'item_exchange', 5.0,"
                     " 10000002, 'x', '2099-01-01T00:00:00Z')")
        conn.execute("INSERT OR REPLACE INTO public_contract_meta VALUES (10000002, 1e10, 1)")
        conn.commit()
        html = client.get("/contracts/public?sort=price_hi").text
    finally:
        conn.execute("DELETE FROM public_contracts WHERE contract_id=9124")
        conn.commit()
        conn.close()
    assert "SERVER_SORT" in html and "form.submit()" in html
    assert '<option value="reward_lo"' in html and '<option value="expires_hi"' in html
    assert 'value="price_hi" selected' in html


def test_every_sort_the_headers_use_orders_the_index():
    conn = _db()
    for sort, first in (("price_hi", 1), ("price", 4), ("reward_lo", 1), ("expires_hi", 1)):
        rows, _ = CH.search_public_contracts(conn, None, sort=sort)
        assert rows[0]["contract_id"] == first, sort
