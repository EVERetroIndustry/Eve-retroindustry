"""Custom stations: any number of them, added and removed on the Prices page,
priced from one read of each region's order book.

Before, there was one custom station, remembered only by the browser, loaded by
hand with its own sweep of the region - even when the Jita refresh had just read
the very same order book.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest

from app.market import prices as P
from app.web import background as bg
from app.web import prices_helper as PH

JITA_44 = 60003760            # Jita IV - Moon 4 - Caldari Navy Assembly Plant (The Forge)
JITA_BUREAU = 60003466        # Jita IV - Moon 4 - Caldari Business Tribunal Bureau Offices
AMARR = 60008494              # Amarr VIII (Oris) - Emperor Family Academy (Domain, a hub region)
DOMAIN = 10000043
STRUCTURE = 1_035_466_617_946


def _order(tid, price, loc, qty=5, buy=False):
    return {"type_id": tid, "price": price, "is_buy_order": buy, "volume_remain": qty,
            "location_id": loc}


# ── One pass over the book ───────────────────────────────────────────────────

def test_one_pass_prices_the_region_and_every_station_in_it():
    pages = [[_order(34, 5.0, JITA_44, 10), _order(34, 4.5, JITA_BUREAU, 3),
              _order(34, 6.0, JITA_44, 2), _order(35, 9.0, 1234),
              _order(34, 7.0, JITA_44, 50, buy=True)]]
    agg, per = P._aggregate_book(pages, JITA_44, {JITA_44, JITA_BUREAU})
    assert agg[34] == {"sell": 4.5, "buy": 7.0, "available": 12}
    assert per[JITA_44] == {34: (12, 5.0)}           # buy orders are not stock
    assert per[JITA_BUREAU] == {34: (3, 4.5)}
    assert 35 not in per[JITA_44]


def _db():
    conn = sqlite3.connect(":memory:")
    P.ensure_price_table(conn)
    return conn


def _client(pages, counter):
    def handler(req):
        page = int(req.url.params.get("page", 1))
        counter.append(page)
        body = pages.get(page, 503)
        if isinstance(body, int):
            return httpx.Response(body, json={"error": "x"})
        return httpx.Response(200, json=body, headers={"x-pages": str(len(pages))})
    return lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def wired(monkeypatch):
    real_sleep = asyncio.sleep

    async def _sleep(_s, *a, **k):
        await real_sleep(0)
    monkeypatch.setattr(P.asyncio, "sleep", _sleep)

    async def _no_discovery(conn, seen):
        return []
    monkeypatch.setattr(PH, "discover_market_types", _no_discovery)

    async def _no_volumes(*a, **k):
        return 0
    monkeypatch.setattr(PH, "_fill_volumes", _no_volumes)
    monkeypatch.setattr(PH, "_cached_region_volume", lambda conn, rid: {34: 777, 35: 888})

    def wire(pages):
        calls: list[int] = []
        client = _client(pages, calls)
        monkeypatch.setattr(P, "esi_client", client)
        monkeypatch.setattr(PH, "esi_client", client)
        return calls
    return wire


def _station(conn, loc):
    return {r[0]: r[1:] for r in conn.execute(
        "SELECT type_id, volume, best_sell, traded_volume FROM station_volume_cache"
        " WHERE location_id=?", (loc,))}


def test_a_station_in_the_forge_costs_no_sweep_of_its_own(wired):
    calls = wired({1: [_order(34, 5.0, JITA_44, 10)], 2: [_order(35, 9.0, 999)]})
    conn = _db()
    res = asyncio.run(PH.refresh_jita(conn, [34, 35], stations=[JITA_44]))
    assert sorted(calls) == [1, 2], "the Forge book is read once, for Jita and the station"
    assert res["stations"] == 1
    assert conn.execute("SELECT sell_price FROM market_price_cache WHERE type_id=34"
                        ).fetchone()[0] == 5.0
    st = _station(conn, JITA_44)
    # On sale here: price, units and the region's 7-day volume. Not on sale here:
    # an answer (0, nothing) - and no regional volume pinned to it.
    assert st[34] == (10, 5.0, 777)
    assert st[35] == (0, None, None)


def test_a_sweep_that_fails_leaves_the_stations_as_they_were(wired):
    wired({1: [_order(34, 5.0, JITA_44)], 2: 503})
    conn = _db()
    conn.execute("INSERT INTO station_volume_cache VALUES (?,34,8,4.0,600,0)", (JITA_44,))
    conn.commit()
    with pytest.raises(P.IncompleteOrderBook):
        asyncio.run(PH.refresh_jita(conn, [34], stations=[JITA_44]))
    assert _station(conn, JITA_44) == {34: (8, 4.0, 600)}


def test_stations_sharing_a_region_share_one_sweep(wired, monkeypatch):
    calls = wired({1: [_order(34, 5.0, AMARR, 4), _order(34, 4.0, 60008495, 1)],
                   2: [_order(35, 9.0, AMARR, 2)]})
    asked = []

    async def fake_volumes(conn, region_id, type_ids, progress_cb=None):
        asked.append(sorted(type_ids))
        return {t: 100 + t for t in type_ids}
    monkeypatch.setattr(PH, "region_volumes_for", fake_volumes)
    conn = _db()
    asyncio.run(PH.refresh_region_stations(conn, DOMAIN, [AMARR, 60008495], [34, 35, 36]))
    assert sorted(calls) == [1, 2]
    assert asked == [[34, 35]], "only what the stations sell needs a 7-day volume"
    assert _station(conn, AMARR) == {34: (4, 5.0, 134), 35: (2, 9.0, 135), 36: (0, None, None)}
    assert _station(conn, 60008495)[34] == (1, 4.0, 134)


# ── The list, through the app ────────────────────────────────────────────────

@pytest.fixture
def m(app_module):
    m = app_module
    conn = m.get_conn()
    m._ensure_tracked_stations(conn)
    conn.execute("DELETE FROM tracked_stations")
    conn.execute("DELETE FROM hub_price_cache")
    conn.commit()
    conn.close()
    m._sync_station_jobs()
    yield m
    conn = m.get_conn()
    conn.execute("DELETE FROM tracked_stations")
    conn.execute("DELETE FROM hub_price_cache")
    conn.commit()
    conn.close()
    m._sync_station_jobs()


def _add(client, loc, name, refresh=False):
    return client.post("/api/prices/stations",
                       json={"location_id": loc, "name": name, "refresh": refresh})


def test_stations_are_added_listed_and_removed(client, m):
    assert _add(client, AMARR, "Amarr VIII (Oris) - Emperor Family Academy").json()["ok"]
    assert _add(client, JITA_44, "Jita IV - Moon 4 - Caldari Navy Assembly Plant").json()["ok"]
    listed = client.get("/api/prices/stations").json()["stations"]
    assert [s["location_id"] for s in listed] == [AMARR, JITA_44]
    assert {s["location_id"]: s["job"] for s in listed} == {
        AMARR: f"stations:{DOMAIN}", JITA_44: "jita"}
    assert client.delete(f"/api/prices/stations/{AMARR}").json()["ok"]
    assert [s["location_id"] for s in client.get("/api/prices/stations").json()["stations"]] \
        == [JITA_44]


def test_a_station_needs_a_location(client, m):
    assert client.post("/api/prices/stations", json={"name": "x"}).status_code == 400


def test_each_region_that_needs_its_own_sweep_gets_one_job(client, m):
    _add(client, AMARR, "Amarr VIII (Oris) - Emperor Family Academy")
    _add(client, 60008495, "Amarr VIII (Oris) - Another")
    assert bg.keys("stations:") == [f"stations:{DOMAIN}"]
    titles = {j["key"]: j["title"] for j in client.get("/api/activity").json()["jobs"]}
    assert "Amarr VIII (Oris)" in titles[f"stations:{DOMAIN}"]
    client.delete(f"/api/prices/stations/{AMARR}")
    client.delete("/api/prices/stations/60008495")
    assert bg.keys("stations:") == [], "nothing left to sweep that region for"


def test_a_station_in_the_forge_rides_on_the_jita_refresh(client, m):
    _add(client, JITA_44, "Jita IV - Moon 4 - Caldari Navy Assembly Plant")
    assert bg.keys("stations:") == []
    titles = {j["key"]: j["title"] for j in client.get("/api/activity").json()["jobs"]}
    assert titles["jita"] == "Jita prices + 1 station"


def test_once_its_hub_is_downloaded_the_hub_prices_the_station(client, m):
    _add(client, AMARR, "Amarr VIII (Oris) - Emperor Family Academy")
    assert bg.keys("stations:") == [f"stations:{DOMAIN}"]
    conn = m.get_conn()
    conn.execute("INSERT INTO hub_price_cache (region_id, type_id, sell_price, cached_at)"
                 " VALUES (?, 34, 5.0, 1)", (DOMAIN,))
    conn.commit()
    conn.close()
    m._sync_station_jobs()
    assert bg.keys("stations:") == []
    assert client.get("/api/prices/stations").json()["stations"][0]["job"] == f"hub:{DOMAIN}"


def test_a_structure_has_a_job_of_its_own(client, m):
    conn = m.get_conn()
    m.ensure_location_name_table(conn)
    conn.execute("INSERT OR REPLACE INTO location_name_cache (location_id, name, region_id)"
                 " VALUES (?, 'C-N4OD - Fortizar', 10000058)", (STRUCTURE,))
    conn.commit()
    conn.close()
    assert _add(client, STRUCTURE, "C-N4OD - Fortizar").json()["job"] == f"structure:{STRUCTURE}"
    assert bg._JOBS[f"structure:{STRUCTURE}"].group == "structure-market"


def test_removing_a_station_keeps_its_prices_so_adding_it_back_is_instant(client, m):
    _add(client, AMARR, "Amarr VIII (Oris) - Emperor Family Academy")
    conn = m.get_conn()
    conn.execute("INSERT OR REPLACE INTO station_volume_cache VALUES (?,34,7,3.25,90,1)", (AMARR,))
    conn.commit()
    conn.close()
    client.delete(f"/api/prices/stations/{AMARR}")
    _add(client, AMARR, "Amarr VIII (Oris) - Emperor Family Academy")
    html = client.get("/prices").text
    assert f'ph-s{AMARR}" data-v="3.25"' in html


# ── The page ─────────────────────────────────────────────────────────────────

def test_every_station_gets_columns_a_chip_and_a_row_in_the_column_picker(client, m):
    _add(client, AMARR, "Amarr VIII (Oris) - Emperor Family Academy")
    _add(client, JITA_44, "Jita IV - Moon 4 - Caldari Navy Assembly Plant")
    conn = m.get_conn()
    conn.execute("INSERT OR REPLACE INTO station_volume_cache VALUES (?,34,7,3.25,90,1)", (JITA_44,))
    conn.commit()
    conn.close()
    html = client.get("/prices").text
    for loc in (AMARR, JITA_44):
        assert html.count(f'data-hub="s{loc}"') == 4          # sell, vol, isk, avail
        assert f'class="stn-chip" data-loc="{loc}"' in html
        for metric in ("sell", "vol", "isk", "avail"):
            assert f'pm-{metric} ph-s{loc}" data-col=' in html
    # The seeded row carries the station's numbers, priced like the hub cells.
    assert f'pm-sell ph-s{JITA_44}" data-v="3.25"' in html
    assert f'pm-avail ph-s{JITA_44}" data-v="7"' in html
    assert f'pm-vol ph-s{JITA_44}" data-v="90"' in html
    # No stored daily averages for the region: volume at the station's sell, flagged.
    assert f'pm-isk ph-s{JITA_44}" data-v="292.5" data-est="1"' in html


def test_two_stations_in_one_system_are_told_apart(client, m):
    _add(client, JITA_44, "Jita IV - Moon 4 - Caldari Navy Assembly Plant")
    _add(client, JITA_BUREAU, "Jita IV - Moon 4 - Caldari Business Tribunal Bureau Offices")
    labels = [s["label"] for s in client.get("/api/prices/stations").json()["stations"]]
    assert labels == ["Jita IV - Moon 4 - Caldari Navy Assembly Plant",
                      "Jita IV - Moon 4 - Caldari Business Tribunal Bureau Offices"]
    client.delete(f"/api/prices/stations/{JITA_BUREAU}")
    assert client.get("/api/prices/stations").json()["stations"][0]["label"] == "Jita IV"


def test_search_results_carry_station_prices(client, m):
    _add(client, JITA_44, "Jita IV - Moon 4 - Caldari Navy Assembly Plant")
    conn = m.get_conn()
    conn.execute("INSERT OR REPLACE INTO station_volume_cache VALUES (?,34,7,3.25,90,1)", (JITA_44,))
    conn.commit()
    conn.close()
    items = client.get("/api/prices/search?q=tritanium").json()["items"]
    trit = next(i for i in items if i["type_id"] == 34)
    assert trit["stations"] == {str(JITA_44): {"sell": 3.25, "volume": 90, "available": 7,
                                               "isk": 292.5, "isk_est": True}}


def test_the_one_browser_station_of_older_versions_is_taken_over():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app/web/templates/prices.html").read_text()
    body = src[src.index("function adoptBrowserStation"):]
    body = body[:body.index("})();")]
    assert "localStorage.getItem('eveCustomStation')" in body
    assert "addStation(saved.location_id" in body and "false)" in body, \
        "taken over without a refresh: its prices are already stored"
    assert "removeItem('eveCustomStation')" in body
