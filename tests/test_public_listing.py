"""A region's public contract listing is stored whole or not at all.

Storing a listing REPLACES the region and deletes the contents of contracts no
longer in it. So a listing with a page missing used to drop every contract on
that page out of search and pricing, and threw their fetched contents away.
Plan's "Fetch contract" index was worse: a failed first page wiped the region,
and it re-read the contents of every contract in it on every call.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest

from app.web import contracts_helper as CH

REGION = 10000002


def _db():
    conn = sqlite3.connect(":memory:")
    CH.ensure_public_contract_tables(conn)
    for cid in (1, 2):
        conn.execute("INSERT INTO public_contracts (contract_id, type, price, volume, region_id)"
                     " VALUES (?, 'item_exchange', 5.0, 1.0, ?)", (cid, REGION))
        conn.execute("INSERT INTO public_contract_items (contract_id, type_id, quantity,"
                     " is_included) VALUES (?, 34, 1, 1)", (cid,))
    conn.commit()
    return conn


def _contract(cid):
    return {"contract_id": cid, "type": "item_exchange", "price": 9.0, "volume": 1.0,
            "start_location_id": 60003760, "issuer_id": 1}


@pytest.fixture
def esi(monkeypatch):
    """pages: listing page -> list or status; items: contract -> list."""
    calls = {"items": []}

    def wire(pages, items=None):
        def handler(req):
            if "/contracts/public/items/" in req.url.path:
                cid = int(req.url.path.rstrip("/").split("/")[-1])
                calls["items"].append(cid)
                return httpx.Response(200, json=(items or {}).get(
                    cid, [{"type_id": 35, "quantity": 1, "is_included": True}]))
            page = int(req.url.params.get("page", 1))
            body = pages.get(page, 503)
            if isinstance(body, int):
                return httpx.Response(body, json={"error": "x"})
            return httpx.Response(200, json=body, headers={"x-pages": str(len(pages))})
        client = lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(CH, "esi_client", client)
        return calls

    async def no_sov(conn, force=False):
        return 0

    async def systems(conn, stations, token=None):
        return {}
    monkeypatch.setattr(CH, "refresh_sov_map", no_sov)
    monkeypatch.setattr(CH, "resolve_station_systems", systems)
    return wire


def _ids(conn):
    return sorted(r[0] for r in conn.execute(
        "SELECT contract_id FROM public_contracts WHERE region_id=?", (REGION,)))


def test_a_listing_with_a_page_missing_keeps_the_region_as_it_was(esi):
    esi({1: [_contract(3)], 2: 503})
    conn = _db()
    res = asyncio.run(CH.refresh_public_listings(conn, [REGION]))
    assert res["regions"] == 0
    assert _ids(conn) == [1, 2]
    assert conn.execute("SELECT COUNT(*) FROM public_contract_items").fetchone()[0] == 2


def test_a_whole_listing_still_replaces_the_region(esi):
    esi({1: [_contract(2)], 2: [_contract(3)]})
    conn = _db()
    asyncio.run(CH.refresh_public_listings(conn, [REGION]))
    assert _ids(conn) == [2, 3]


def _stream(conn):
    async def go():
        out = []
        async for chunk in CH.stream_public_index(conn, REGION):
            out.append(json.loads(chunk[6:]))
        return out
    return asyncio.run(go())


def test_plans_index_reads_only_contents_nobody_has_read(esi):
    calls = esi({1: [_contract(1), _contract(2), _contract(3)]})
    conn = _db()
    events = _stream(conn)
    assert events[-1]["done"] and "error" not in events[-1]
    assert calls["items"] == [3], "1 and 2 were read before; contents never change"
    assert _ids(conn) == [1, 2, 3]


def test_plans_index_keeps_the_region_when_esi_cannot_list_it(esi):
    calls = esi({1: 503})
    conn = _db()
    events = _stream(conn)
    assert events[-1]["done"] and "kept" in events[-1]["error"]
    assert _ids(conn) == [1, 2]
    assert calls["items"] == []
