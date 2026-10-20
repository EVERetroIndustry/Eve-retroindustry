"""Orders and Contracts are cache-first, and an ESI that cannot answer is no
longer an empty page.

The fetchers used to return [] (or the pages before a failed one) when ESI did
not answer, which reads exactly like a character with nothing on the market.
They raise IncompleteAnswer now, and the pages show their stored copy and say
why, the way the wallet already did.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.character import contracts as contracts_api
from app.character import orders as orders_api
from app.esi.client import IncompleteAnswer

CHAR = 900000001
STATION = 60003760
ORDER = {"order_id": 777001, "type_id": 34, "location_id": STATION, "region_id": 10000002,
         "price": 5.5, "volume_total": 1000, "volume_remain": 400, "is_buy_order": False,
         "issued": "2026-10-19T10:00:00Z", "duration": 90, "range": "station",
         "min_volume": 1}
CONTRACT = {"contract_id": 555001, "type": "item_exchange", "status": "outstanding",
            "title": "Cache test contract", "issuer_id": CHAR, "issuer_corporation_id": 98000001,
            "assignee_id": 0, "acceptor_id": 0, "availability": "public",
            "start_location_id": STATION, "end_location_id": STATION, "price": 1e6,
            "reward": 0, "collateral": 0, "volume": 1.0, "for_corporation": False,
            "date_issued": "2026-10-19T10:00:00Z", "date_expired": "2026-11-19T10:00:00Z",
            "days_to_complete": 0}


# ── The fetchers say when they could not answer ──────────────────────────────

def _client(responses):
    """responses: page number -> (status, body)."""
    def handler(req):
        page = int(req.url.params.get("page", 1))
        status, body = responses.get(page, (503, {"error": "down"}))
        return httpx.Response(status, json=body, headers={"x-pages": str(len(responses))})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(coro):
    return asyncio.run(coro)


def test_active_orders_raise_instead_of_reading_as_none():
    async def go():
        async with _client({}) as c:
            return await orders_api.fetch_orders(c, CHAR, "tok")
    with pytest.raises(IncompleteAnswer):
        _run(go())


def test_a_character_with_no_orders_is_still_an_empty_list():
    async def go():
        async with _client({1: (200, [])}) as c:
            return await orders_api.fetch_orders(c, CHAR, "tok")
    assert _run(go()) == []


def test_order_history_with_a_page_missing_is_not_the_history():
    async def go():
        async with _client({1: (200, [ORDER]), 2: (503, {}), 3: (200, [ORDER])}) as c:
            return await orders_api.fetch_orders_history(c, CHAR, "tok")
    with pytest.raises(IncompleteAnswer):
        _run(go())


def test_corp_orders_with_a_page_missing_are_not_the_orders():
    async def go():
        async with _client({1: (200, [ORDER]), 2: (503, {})}) as c:
            return await orders_api.fetch_corp_orders(c, 98000001, "tok")
    rows, err = _run(go())
    assert rows is None and "page 2" in err


def test_character_contracts_with_a_page_missing_are_not_the_contracts():
    async def go():
        async with _client({1: (200, [CONTRACT]), 2: (502, {})}) as c:
            return await contracts_api.fetch_character_contracts(c, CHAR, "tok")
    with pytest.raises(IncompleteAnswer):
        _run(go())


def test_corp_contracts_with_a_page_missing_are_not_the_contracts():
    # The alliance index lists through this: a partial list used to drop every
    # contract on the pages after the failure until the next listing.
    async def go():
        async with _client({1: (200, [CONTRACT]), 2: (200, [CONTRACT]), 3: (503, {})}) as c:
            return await contracts_api.fetch_corp_contracts(c, 98000001, "tok")
    rows, err = _run(go())
    assert rows is None and "page 3" in err


# ── The pages ────────────────────────────────────────────────────────────────

@pytest.fixture
def pages(app_module, monkeypatch):
    m = app_module
    conn = m.get_conn()
    m.drop_cached(conn, "orders")
    m.drop_cached(conn, "contracts")
    conn.close()

    async def names(ids, token=None, conn=None):
        return {STATION: "Jita IV - Moon 4 - Caldari Navy Assembly Plant"}
    monkeypatch.setattr(m, "resolve_station_names_bulk", names)

    async def parties(ids):
        return {i: f"Party {i}" for i in ids}
    monkeypatch.setattr(m, "_resolve_party_names", parties)

    state = {"calls": 0, "fail": False}

    async def fetch_orders(client, cid, tok):
        state["calls"] += 1
        if state["fail"]:
            raise IncompleteAnswer("ESI returned HTTP 503.")
        return [dict(ORDER)]
    monkeypatch.setattr(m.orders_api, "fetch_orders", fetch_orders)

    async def fetch_contracts(client, cid, tok):
        state["calls"] += 1
        if state["fail"]:
            raise IncompleteAnswer("ESI returned HTTP 503.")
        return [dict(CONTRACT)]
    monkeypatch.setattr(m.contracts_api, "fetch_character_contracts", fetch_contracts)
    yield state
    conn = m.get_conn()
    m.drop_cached(conn, "orders")
    m.drop_cached(conn, "contracts")
    conn.close()


@pytest.mark.parametrize("url, marker", [
    (f"/orders?char={CHAR}", "Tritanium"),
    (f"/contracts?char={CHAR}", "Cache test contract"),
])
def test_switching_back_to_a_page_does_not_ask_esi_again(client, pages, url, marker):
    first = client.get(url).text
    assert marker in first and "updated just now" in first
    assert pages["calls"] == 1
    again = client.get(url).text
    assert marker in again and pages["calls"] == 1, "a fresh stored copy needs no ESI"


@pytest.mark.parametrize("url, marker", [
    (f"/orders?char={CHAR}", "Tritanium"),
    (f"/contracts?char={CHAR}", "Cache test contract"),
])
def test_when_esi_cannot_answer_the_stored_copy_is_shown_and_says_so(client, pages, url, marker):
    client.get(url)
    pages["fail"] = True
    html = client.get(url + "&refresh=1").text
    assert marker in html, "the stored copy, not an empty page"
    assert "ESI returned HTTP 503" in html and "showing the copy stored" in html


@pytest.mark.parametrize("url, marker", [
    (f"/orders?char={CHAR}", "Tritanium"),
    (f"/contracts?char={CHAR}", "Cache test contract"),
])
def test_a_failed_refresh_does_not_replace_the_stored_copy(client, pages, url, marker):
    client.get(url)
    pages["fail"] = True
    client.get(url + "&refresh=1")
    pages["fail"] = False
    pages["calls"] = 0
    html = client.get(url).text
    assert pages["calls"] == 0, "the good copy is still there and still fresh"
    assert marker in html and "ESI returned HTTP 503" not in html, \
        "what is served is the good copy, not the failed answer"


@pytest.mark.parametrize("url", [f"/orders?char={CHAR}", f"/contracts?char={CHAR}"])
def test_with_nothing_stored_a_failure_is_an_error_not_an_empty_list(client, pages, url):
    pages["fail"] = True
    html = client.get(url).text
    assert "ESI returned HTTP 503" in html
    assert "showing the copy stored" not in html
