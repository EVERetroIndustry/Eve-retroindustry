"""Reused region volumes must never stand in for volumes we have not fetched.

The regression this pins down: reuse of a region's cached 7-day volumes was
treated as all-or-nothing. That was correct while Jita and the four hubs were
the only reusable regions, because a non-empty map really did mean full
coverage. Once any previously swept region became reusable, a region holding a
handful of cached types took the same branch and every other type was written as
a blank - on C-N4OD in Fountain, whose region had 2 cached types, that produced
4 697 items with a price and exactly 1 with a volume.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import sqlite3
import time
import unittest.mock as mock

from app.market import prices as P

STATION, REGION = 60000061, 10000038
SOLD_HERE = [34, 35, 36]          # have an order at the station
CACHED = 34                       # ...and this one is already in the ETag cache


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    P.ensure_price_table(conn)
    P.ensure_hist_etag_table(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS sde_types (type_id INTEGER PRIMARY KEY,"
                 " name TEXT, market_group_id INTEGER, published INTEGER)")
    for t in SOLD_HERE:
        conn.execute("INSERT OR REPLACE INTO sde_types VALUES (?,?,?,?)",
                     (t, f"T{t}", 4, 1))
    conn.commit()
    return conn


def _seed_one_cached_type(conn: sqlite3.Connection) -> None:
    """One type in this region already has stored daily volumes."""
    today = datetime.date.today()
    days = {(today - datetime.timedelta(days=i)).isoformat(): 100 for i in range(3)}
    conn.execute(
        "INSERT OR REPLACE INTO market_hist_etag"
        " (region_id, type_id, etag, days_json, cached_at, expires_at)"
        " VALUES (?,?,?,?,?,?)",
        (REGION, CACHED, "etag", json.dumps(days), time.time(), time.time() + 3600))
    conn.commit()


class _Client:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _run(conn):
    """Load the station, mocking only the two ESI calls."""
    asked: list[int] = []

    async def orders(client, region_id, location_id, tid):
        return (10, 1_000.0)              # every type has a sell order here

    async def volume(client, region_id, type_id):
        asked.append(type_id)
        return 777

    with mock.patch.object(P, "esi_client", lambda **kw: _Client()), \
         mock.patch.object(P, "_fetch_orders_for_type", orders), \
         mock.patch.object(P, "_fetch_region_volume", volume):
        res = asyncio.run(P.fetch_station_volumes(conn, STATION, REGION, SOLD_HERE))
    return res, asked


def test_partial_cache_does_not_blank_the_rest():
    """The bug: with one type cached, the other two came back as None."""
    conn = _conn()
    _seed_one_cached_type(conn)
    res, asked = _run(conn)

    assert sorted(asked) == [35, 36], "the uncached types must still be fetched"
    assert res[CACHED][2] == 300, "the cached type keeps its stored window"
    assert res[35][2] == 777 and res[36][2] == 777


def test_partial_cache_is_persisted_for_every_type():
    conn = _conn()
    _seed_one_cached_type(conn)
    _run(conn)
    rows = dict(conn.execute(
        "SELECT type_id, traded_volume FROM station_volume_cache WHERE location_id=?",
        (STATION,)).fetchall())
    assert rows == {34: 300, 35: 777, 36: 777}
    assert None not in rows.values()


def test_an_empty_cache_still_fetches_everything():
    conn = _conn()
    res, asked = _run(conn)
    assert sorted(asked) == SOLD_HERE
    assert all(res[t][2] == 777 for t in SOLD_HERE)


def test_a_fully_cached_region_asks_nothing():
    """The saving that made reuse worth having in the first place."""
    conn = _conn()
    today = datetime.date.today()
    days = {(today - datetime.timedelta(days=i)).isoformat(): 5 for i in range(2)}
    for t in SOLD_HERE:
        conn.execute(
            "INSERT OR REPLACE INTO market_hist_etag"
            " (region_id, type_id, etag, days_json, cached_at, expires_at)"
            " VALUES (?,?,?,?,?,?)",
            (REGION, t, "e", json.dumps(days), time.time(), time.time() + 3600))
    conn.commit()
    res, asked = _run(conn)
    assert asked == []
    assert all(res[t][2] == 10 for t in SOLD_HERE)


def test_stale_cached_days_are_refetched_not_reused():
    """Stored days age out of the moving window, so past the freshness limit they
    have to be asked about again rather than summed into a confident wrong 0."""
    conn = _conn()
    old = datetime.date.today() - datetime.timedelta(days=20)
    days = {(old - datetime.timedelta(days=i)).isoformat(): 100 for i in range(3)}
    conn.execute(
        "INSERT OR REPLACE INTO market_hist_etag"
        " (region_id, type_id, etag, days_json, cached_at, expires_at)"
        " VALUES (?,?,?,?,?,?)",
        (REGION, CACHED, "etag", json.dumps(days),
         time.time() - P._REGION_VOLUME_MAX_AGE - 60, 0.0))
    conn.commit()
    res, asked = _run(conn)
    assert CACHED in asked
    assert res[CACHED][2] == 777
