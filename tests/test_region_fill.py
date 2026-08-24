"""Background top-up of a region's 7-day volumes.

Covers the two properties that matter for correctness rather than speed:
nothing silently reports a wrong volume (stale days must not decay to "0
traded"), and the top-up never competes with a load the user is waiting for.
"""
import asyncio
import json
import sqlite3
import time

import pytest

from app.market import prices as P


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE market_hist_etag (region_id INTEGER, type_id INTEGER,"
              " etag TEXT, days_json TEXT, cached_at REAL, expires_at REAL,"
              " PRIMARY KEY (region_id, type_id))")
    c.execute("CREATE TABLE sde_types (type_id INTEGER PRIMARY KEY, type_name TEXT,"
              " market_group_id INTEGER, published INTEGER)")
    # Both always exist in the app (ensure_price_table); coverage consults them
    # so that a region loaded into its dedicated columns is not re-fetched.
    c.execute("CREATE TABLE IF NOT EXISTS market_price_cache"
              " (type_id INTEGER PRIMARY KEY, volume INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS hub_price_cache"
              " (region_id INTEGER, type_id INTEGER, volume INTEGER,"
              "  PRIMARY KEY (region_id, type_id))")
    return c


def _days(*vols: int) -> str:
    import datetime
    today = datetime.date.today()
    return json.dumps({
        (today - datetime.timedelta(days=i)).isoformat(): v
        for i, v in enumerate(vols)
    })


def _store(conn, region, tid, days, age_s=0.0, ttl=0.0):
    now = time.time()
    conn.execute("INSERT OR REPLACE INTO market_hist_etag VALUES (?,?,?,?,?,?)",
                 (region, tid, "e", days, now - age_s, now + ttl))
    conn.execute("INSERT OR REPLACE INTO sde_types VALUES (?,?,?,?)",
                 (tid, f"T{tid}", 4, 1))


def test_any_swept_region_is_reusable():
    """A custom-station region has no dedicated column, but its stored days count."""
    conn = _conn()
    _store(conn, 10000048, 34, _days(100, 200, 300))
    vols = P._region_volume_from_etags(conn, 10000048)
    assert vols[34] == 600


def test_stale_days_do_not_count_as_coverage():
    """Left long enough the stored days fall out of the window; a missing number
    is honest, a decayed 0 is not."""
    conn = _conn()
    _store(conn, 10000048, 34, _days(100), age_s=P._REGION_VOLUME_MAX_AGE + 60)
    assert P._region_volume_from_etags(conn, 10000048) == {}


def test_fresh_within_expiry_counts_even_if_old():
    conn = _conn()
    _store(conn, 10000048, 34, _days(5, 5), age_s=10 * 24 * 3600, ttl=3600)
    assert P._region_volume_from_etags(conn, 10000048)[34] == 10


def test_coverage_ignores_unanswerable_types():
    """Types the history endpoint answers 400/404 for are not counted against
    progress - otherwise it could never reach 100 %."""
    conn = _conn()
    _store(conn, 10000048, 34, _days(7))
    conn.execute("INSERT INTO sde_types VALUES (?,?,?,?)", (99, "Unpub", None, 0))
    have, total = P.region_volume_coverage(conn, 10000048, [34, 99])
    assert (have, total) == (1, 1)


def test_coverage_counts_missing_types():
    conn = _conn()
    _store(conn, 10000048, 34, _days(7))
    conn.execute("INSERT INTO sde_types VALUES (?,?,?,?)", (35, "Other", 4, 1))
    assert P.region_volume_coverage(conn, 10000048, [34, 35]) == (1, 2)


def test_dedicated_column_wins_over_etag_derived():
    """A hub station must agree with the number its own column shows."""
    conn = _conn()
    conn.execute("INSERT INTO market_price_cache VALUES (34, 999)")
    _store(conn, P.JITA_REGION, 34, _days(1, 1))
    _store(conn, P.JITA_REGION, 35, _days(4, 4))
    vols = P._cached_region_volume(conn, P.JITA_REGION)
    assert vols[34] == 999      # column, not the 2 from stored days
    assert vols[35] == 8        # filled in from the ETag cache


def test_fill_skips_types_already_covered():
    conn = _conn()
    _store(conn, 10000048, 34, _days(3))
    res = asyncio.run(P.fill_region_volumes(conn, 10000048, [34]))
    assert res == {"fetched": 0, "remaining": 0, "reason": "complete"}


def test_fill_waits_for_foreground(monkeypatch):
    """The top-up must stand aside while a user-initiated load is running: a 429
    parks the whole rate-limit group, so a background burst would make the user
    wait out its penalty."""
    conn = _conn()
    conn.execute("INSERT INTO sde_types VALUES (?,?,?,?)", (34, "Trit", 4, 1))
    order = []

    async def fake_fetch(client, region_id, type_id):
        order.append("background")

    monkeypatch.setattr(P, "_fetch_region_volume", fake_fetch)
    monkeypatch.setattr(P, "esi_client", _null_client)

    async def scenario():
        with P.foreground_prices():
            task = asyncio.create_task(
                P.fill_region_volumes(conn, 10000048, [34]))
            await asyncio.sleep(0.05)
            order.append("foreground-done")     # still nothing fetched
        await task

    asyncio.run(scenario())
    assert order == ["foreground-done", "background"]


def test_foreground_counter_survives_an_error():
    """An exception inside a foreground op must not leave the top-up blocked
    forever."""
    with pytest.raises(RuntimeError):
        with P.foreground_prices():
            raise RuntimeError("boom")
    assert not P.foreground_prices_active()


def test_foreground_nests():
    with P.foreground_prices():
        with P.foreground_prices():
            assert P.foreground_prices_active()
        assert P.foreground_prices_active()
    assert not P.foreground_prices_active()


class _null_client:
    def __init__(self, *a, **kw): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *e): return False


def test_coverage_counts_dedicated_columns():
    """A fully-loaded hub region must not read as 0 % covered - that would send
    the top-up off to re-fetch volumes we already have."""
    conn = _conn()
    conn.execute("INSERT INTO market_price_cache VALUES (34, 500)")
    conn.execute("INSERT INTO sde_types VALUES (?,?,?,?)", (34, "Trit", 4, 1))
    have, total = P.region_volume_coverage(conn, P.JITA_REGION, [34])
    assert (have, total) == (1, 1)


def test_fill_skips_a_region_already_covered_by_columns():
    conn = _conn()
    conn.execute("INSERT INTO market_price_cache VALUES (34, 500)")
    conn.execute("INSERT INTO sde_types VALUES (?,?,?,?)", (34, "Trit", 4, 1))
    res = asyncio.run(P.fill_region_volumes(conn, P.JITA_REGION, [34]))
    assert res == {"fetched": 0, "remaining": 0, "reason": "complete"}


def test_fill_waits_while_the_budget_is_low(monkeypatch):
    """The top-up must not spend the last of the token bucket: a cold region needs
    more requests than the bucket holds, so without a reserve it drains it and the
    next user-initiated load pays a 60 s penalty this job earned."""
    conn = _conn()
    for t in (34, 35):
        conn.execute("INSERT INTO sde_types VALUES (?,?,?,?)", (t, f"T{t}", 4, 1))
    fetched = []

    async def fake_fetch(client, region_id, type_id):
        fetched.append(type_id)

    shares = [0.05, 0.05, 0.80]        # low, low, then refilled
    monkeypatch.setattr(P, "_fetch_region_volume", fake_fetch)
    monkeypatch.setattr(P, "esi_client", _null_client)
    monkeypatch.setattr(P, "esi_budget_share", lambda url: shares.pop(0) if shares else 0.80)
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(P.asyncio, "sleep", fake_sleep)
    asyncio.run(P.fill_region_volumes(conn, 10000048, [34, 35]))
    assert fetched == [34, 35]          # ran once the bucket refilled
    assert 10.0 in sleeps               # and waited first


def test_fill_gives_up_if_the_budget_never_recovers(monkeypatch):
    """Rather than looping forever it returns, so a later pass can resume - the
    ETags already written are not lost."""
    conn = _conn()
    conn.execute("INSERT INTO sde_types VALUES (?,?,?,?)", (34, "T34", 4, 1))
    fetched = []

    async def fake_fetch(client, region_id, type_id):
        fetched.append(type_id)

    monkeypatch.setattr(P, "_fetch_region_volume", fake_fetch)
    monkeypatch.setattr(P, "esi_client", _null_client)
    monkeypatch.setattr(P, "esi_budget_share", lambda url: 0.01)

    async def fake_sleep(s):
        pass

    monkeypatch.setattr(P.asyncio, "sleep", fake_sleep)
    res = asyncio.run(P.fill_region_volumes(conn, 10000048, [34]))
    assert fetched == []
    assert res["remaining"] == 1


def test_budget_stop_is_reported_as_such():
    """The caller needs to tell "no budget right now" from "finished" - otherwise
    the loop abandons a region half-filled after the first squeeze."""
    conn = _conn()
    conn.execute("INSERT INTO sde_types VALUES (?,?,?,?)", (34, "T34", 4, 1))
    import unittest.mock as mock
    with mock.patch.object(P, "esi_budget_share", lambda url: 0.01), \
         mock.patch.object(P, "esi_client", _null_client), \
         mock.patch.object(P.asyncio, "sleep", _noop):
        res = asyncio.run(P.fill_region_volumes(conn, 10000048, [34]))
    assert res["reason"] == "budget" and res["remaining"] == 1


async def _noop(*a, **kw):
    return None


def test_fill_stops_when_a_penalty_is_already_in_force():
    """Waiting out a 429 and carrying straight on is how a depleted bucket turns
    into a spiral; the next thing to pay for it is a user clicking Load."""
    conn = _conn()
    conn.execute("INSERT INTO sde_types VALUES (?,?,?,?)", (34, "T34", 4, 1))
    fetched = []

    async def fake_fetch(client, region_id, type_id):
        fetched.append(type_id)

    import unittest.mock as mock
    with mock.patch.object(P, "_fetch_region_volume", fake_fetch), \
         mock.patch.object(P, "esi_client", _null_client), \
         mock.patch.object(P, "esi_throttle_status",
                           lambda url=None: {"paused": True, "seconds": 60}):
        res = asyncio.run(P.fill_region_volumes(conn, 10000048, [34]))
    assert fetched == []
    assert res["reason"] == "throttled" and res["remaining"] == 1
