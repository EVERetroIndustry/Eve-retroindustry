"""ESI calls saved without losing any data.

* The character sync asked /universe/structures/ about every container and
  ship that holds items (their item id is the items' location) - 264 failing
  calls per sync on a real account, past the 100-errors-a-minute limit.
* The volume phase threw away the whole market history body that the price
  chart and Deals keep, so Deals downloaded it again the same day.
* A structure's region came from three ESI calls although the SDE has it.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx

from app.character.assets import CharAsset
from app.market import prices as P

STATION = 60003760
STRUCTURE = 1_035_000_000_001
CONTAINER = 1_050_000_000_002


def _asset(item_id, location_id):
    return CharAsset(item_id=item_id, type_id=34, location_id=location_id,
                     location_flag="Hangar", quantity=1, is_singleton=True,
                     is_blueprint_copy=False)


def test_sync_does_not_ask_for_containers_as_structures(app_module, monkeypatch):
    m = app_module
    asked = []

    async def token(_cid):
        return "tok"

    async def nothing(*_a, **_k):
        return []

    async def assets(client, char_id, token, conn):
        return [_asset(CONTAINER, STRUCTURE), _asset(7, CONTAINER), _asset(8, STATION)]

    async def corp(client, char_id, token, conn):
        return None, []

    async def resolve(ids, token=None, conn=None):
        asked.extend(ids)
        return {}

    monkeypatch.setattr(m, "_valid_token_async", token)
    monkeypatch.setattr(m, "fetch_blueprints", nothing)
    monkeypatch.setattr(m, "fetch_assets", assets)
    monkeypatch.setattr(m, "fetch_skills", nothing)
    monkeypatch.setattr(m, "fetch_corp_assets", corp)
    monkeypatch.setattr(m, "resolve_station_names_bulk", resolve)
    asyncio.run(m._bg_initial_sync())
    assert sorted(set(asked)) == [STATION, STRUCTURE]


def test_the_volume_phase_refreshes_the_history_deals_keeps(monkeypatch):
    conn = sqlite3.connect(":memory:")
    P.ensure_price_table(conn)
    P.ensure_hist_etag_table(conn)
    conn.execute("INSERT INTO price_history_cache VALUES (10000002, 34, '[]', 0)")
    conn.commit()
    body = [{"date": "2026-11-02", "average": 5.0, "lowest": 4.0, "highest": 6.0,
             "volume": 100}]

    def handler(req):
        return httpx.Response(200, json=body, headers={"etag": '"e"'})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await P._fetch_region_volume(c, 10000002, 34)     # kept by Deals
            await P._fetch_region_volume(c, 10000002, 35)     # not kept: not added
    P.load_hist_etags(conn, 10000002)
    asyncio.run(run())
    P.flush_hist_etags(conn)
    rows = conn.execute("SELECT type_id, data_json, cached_at FROM price_history_cache").fetchall()
    assert [r[0] for r in rows] == [34]
    assert json.loads(rows[0][1]) == [{"d": "2026-11-02", "avg": 5.0, "low": 4.0,
                                       "high": 6.0, "vol": 100}]
    assert rows[0][2] > 0


def test_a_structures_region_comes_from_the_sde(app_module, monkeypatch):
    conn = app_module.get_conn()
    try:
        conn.execute("INSERT OR REPLACE INTO location_name_cache (location_id, name,"
                     " solar_system_id) VALUES (?,?,?)", (STRUCTURE, "Some Fortizar", 30000142))
        conn.commit()
        P._region_cache.pop(STRUCTURE, None)

        def no_esi(**_k):
            raise AssertionError("asked ESI")
        monkeypatch.setattr(P, "esi_client", no_esi)
        assert asyncio.run(P.get_region_for_structure(STRUCTURE, conn)) == 10000002
    finally:
        conn.close()


def test_a_small_regions_whole_book_is_read_instead_of_type_by_type(monkeypatch):
    asked = []

    def handler(req):
        asked.append(dict(req.url.params))
        return httpx.Response(200, json=[], headers={"x-pages": "2"})
    monkeypatch.setattr(P, "esi_client",
                        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    P._book_pages.clear()
    conn = sqlite3.connect(":memory:")
    asyncio.run(P.fetch_station_volumes(conn, STATION, 10000033, [34, 35, 36]))
    assert not any("type_id" in a for a in asked)        # 3 types > 2 pages
