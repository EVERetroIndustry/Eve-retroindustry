"""An order-book sweep is all or nothing.

A Jita refresh run while ESI answered 503 - which is what it does every day
during downtime - used to read every unanswered page as an empty one, and then
store "no price" for every type in the cache with a fresh timestamp, while the
progress bar said Done. The Jita cache never expires and Assets, Plan, net
worth and the dashboard all read it, so one click at the wrong minute blanked
the whole app. Hubs and custom stations had the same shape.

Only the network is fake here: the real sweep, the real persistence and the
real streams run against an in-memory database.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from app.market import prices as P
from app.web import prices_helper as PH

STORED = [(34, 5.0, 4.9), (35, 9.0, 8.8), (36, 60.0, 55.0)]
REGION = P.JITA_REGION


@pytest.fixture(autouse=True)
def _instant_retries(monkeypatch):
    """The sweep waits 3-5 s between retries; nothing here needs real time."""
    real = asyncio.sleep

    async def _sleep(_s, *a, **k):
        await real(0)
    monkeypatch.setattr(P.asyncio, "sleep", _sleep)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    P.ensure_price_table(conn)
    conn.executemany(
        "INSERT INTO market_price_cache (type_id, sell_price, buy_price, cached_at)"
        " VALUES (?,?,?,0)", STORED)
    conn.executemany(
        "INSERT INTO hub_price_cache (region_id, type_id, sell_price, buy_price, cached_at)"
        " VALUES (10000043,?,?,?,0)", STORED)
    conn.commit()
    return conn


def _orders(*type_prices):
    return [{"type_id": t, "price": p, "is_buy_order": False, "volume_remain": 3,
             "location_id": P.JITA_STATION} for t, p in type_prices]


def _client(pages: dict[int, object]):
    """esi_client() stand-in. pages[n] is a list of orders, or an HTTP status."""
    def handler(req: httpx.Request) -> httpx.Response:
        page = int(req.url.params.get("page", 1))
        body = pages.get(page, 503)
        if isinstance(body, int):
            return httpx.Response(body, json={"error": "unavailable"})
        return httpx.Response(200, json=body, headers={"x-pages": str(len(pages))})
    return lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _wire(monkeypatch, pages):
    client = _client(pages)
    monkeypatch.setattr(P, "esi_client", client)
    monkeypatch.setattr(PH, "esi_client", client)

    async def _no_discovery(conn, seen):
        return []
    monkeypatch.setattr(PH, "discover_market_types", _no_discovery)

    async def _no_volumes(*a, **k):
        return 0
    monkeypatch.setattr(PH, "_fill_volumes", _no_volumes)
    monkeypatch.setattr(PH, "_fill_hub_volumes", _no_volumes)


def _run_stream(gen) -> list[dict]:
    async def _collect():
        out = []
        async for chunk in gen:
            out.append(json.loads(chunk.removeprefix("data: ").strip()))
        return out
    return asyncio.run(_collect())


def _jita(conn):
    return conn.execute("SELECT type_id, sell_price, buy_price FROM market_price_cache"
                        " ORDER BY type_id").fetchall()


# ── Jita ──────────────────────────────────────────────────────────────────────

def test_a_refresh_during_downtime_keeps_every_stored_price(monkeypatch):
    _wire(monkeypatch, {1: 503})
    conn = _db()
    events = _run_stream(PH.stream_jita_refresh(conn, [34, 35, 36]))
    assert _jita(conn) == STORED
    assert not any(e.get("done") for e in events), "a failed sweep must not say Done"
    assert "left as they were" in events[-1]["error"]


def test_one_unreadable_page_keeps_every_stored_price(monkeypatch):
    # Type 36's only order sits on the page that fails: written from the rest,
    # it would read "no price" and the others would be priced off a partial book.
    _wire(monkeypatch, {1: _orders((34, 4.0)), 2: _orders((35, 7.0)), 3: 503})
    conn = _db()
    events = _run_stream(PH.stream_jita_refresh(conn, [34, 35, 36]))
    assert _jita(conn) == STORED
    assert "1 of 3 pages" in events[-1]["error"]


def test_a_complete_sweep_still_writes(monkeypatch):
    # The guard must not stop the case it exists to protect: every page read,
    # and a type with no order anywhere really is "no price".
    _wire(monkeypatch, {1: _orders((34, 4.0)), 2: _orders((35, 7.0))})
    conn = _db()
    events = _run_stream(PH.stream_jita_refresh(conn, [34, 35, 36]))
    assert events[-1].get("done")
    assert _jita(conn) == [(34, 4.0, None), (35, 7.0, None), (36, None, None)]


def test_an_empty_order_book_is_an_answer_not_a_failure():
    # 200 with no orders and one page is a region that sells nothing - it must
    # not be mistaken for the unreadable page the rest of this file is about.
    async def go():
        async with _client({1: []})() as c:
            return await P._fetch_all_region_orders(c, REGION)
    assert asyncio.run(go()) == [[]]


# ── Trade hubs ────────────────────────────────────────────────────────────────

def test_a_hub_refresh_during_downtime_keeps_its_stored_prices(monkeypatch):
    _wire(monkeypatch, {1: 503})
    conn = _db()
    events = _run_stream(PH.stream_hub_refresh(conn, [34, 35, 36], 10000043))
    rows = conn.execute("SELECT type_id, sell_price, buy_price FROM hub_price_cache"
                        " ORDER BY type_id").fetchall()
    assert rows == STORED
    assert "left as they were" in events[-1]["error"]


# ── Custom stations ───────────────────────────────────────────────────────────

STATION = 60003760
STRUCTURE = 1_035_466_617_946


def _station_rows(conn, loc):
    return conn.execute("SELECT type_id, volume, best_sell FROM station_volume_cache"
                        " WHERE location_id=? ORDER BY type_id", (loc,)).fetchall()


def _seed_station(conn, loc):
    conn.executemany(
        "INSERT INTO station_volume_cache (location_id, type_id, volume, best_sell,"
        " traded_volume, cached_at) VALUES (?,?,?,?,NULL,0)",
        [(loc, t, 10, s) for t, s, _b in STORED])
    conn.commit()


def test_a_station_sweep_that_fails_keeps_the_stored_station(monkeypatch):
    _wire(monkeypatch, {1: _orders((34, 4.0)), 2: 503})
    monkeypatch.setattr(P, "_BULK_ORDERS_THRESHOLD", 1)
    conn = _db()
    _seed_station(conn, STATION)
    with pytest.raises(P.IncompleteOrderBook):
        asyncio.run(P.fetch_station_volumes(conn, STATION, REGION, [34, 35, 36]))
    assert _station_rows(conn, STATION) == [(t, 10, s) for t, s, _b in STORED]


def test_a_single_type_lookup_that_fails_keeps_its_stored_price(monkeypatch):
    # The per-type path is what Plan's "Fetch price" uses. A failed request
    # came back as (None, None) and was stored over the price it already had.
    _wire(monkeypatch, {1: 503})
    conn = _db()
    _seed_station(conn, STATION)
    with pytest.raises(P.IncompleteOrderBook):
        asyncio.run(P.fetch_station_volumes(conn, STATION, REGION, [35]))
    assert _station_rows(conn, STATION) == [(t, 10, s) for t, s, _b in STORED]


def test_a_structure_market_page_that_fails_keeps_the_stored_structure(monkeypatch):
    # The first page failing was already caught; a later one was not, and the
    # pages before it were written as if they were the whole market.
    pages = {1: _orders((34, 4.0)), 2: _orders((35, 7.0)), 3: 503}
    monkeypatch.setattr(P, "esi_client", _client(pages))
    conn = _db()
    _seed_station(conn, STRUCTURE)
    with pytest.raises(P.IncompleteOrderBook):
        asyncio.run(P.fetch_structure_market(conn, STRUCTURE, "tok", {34, 35, 36},
                                             region_id=REGION))
    assert _station_rows(conn, STRUCTURE) == [(t, 10, s) for t, s, _b in STORED]


# ── The page has to say it ────────────────────────────────────────────────────

def _handler_source(marker: str) -> str:
    src = (Path(__file__).resolve().parents[1]
           / "app/web/templates/prices.html").read_text(encoding="utf-8")
    start = src.index(marker)
    return src[start:src.index("es.onerror", start)]


@pytest.mark.parametrize("marker", [
    "new EventSource('/prices/refresh/stream')",
    "new EventSource('/prices/refresh/hub/'",
])
def test_the_prices_page_shows_the_error_instead_of_waiting(marker):
    # Without a branch for it, the error event fell through to the progress
    # branch and the bar sat on "Orders: undefined / undefined pages".
    body = _handler_source(marker)
    assert "if (d.error)" in body, "no branch handles the error event"
    assert body.index("if (d.error)") < body.index("if (d.done)")
