"""Which types the 7-day volume sweep is allowed to ask about.

Reported symptom: loading prices for a custom station "loads some volumes, looks
stuck, then after a long time loads a bit more". Measured against live ESI
(2026-08-24) on a region with nothing cached:

  * of 60 sampled types with no market group or published = 0, **60 answered 400
    or 404**; of 60 ordinary market types, none did. 379 of the 19 812 types in
    the sweep are such types, so every cold sweep fired ~379 GUARANTEED errors
    against an error budget of 100 per ~60 s window - and our own error-limit
    governor then froze ALL ESI traffic until the window reset, repeatedly.
  * ESI has also started rate-limiting this endpoint: at concurrency 30 (~460
    req/s) it answers 429 after ~6 000 requests; at 10 (~230 req/s) the same
    sweep ran 16 500 requests with zero errors.

Together: 11-14 volumes/s before, ~230/s after.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.market import prices as P


@pytest.fixture
def sde(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("CREATE TABLE sde_types (type_id INTEGER PRIMARY KEY, name TEXT,"
                 " market_group_id INTEGER, published INTEGER)")
    conn.executemany("INSERT INTO sde_types VALUES (?,?,?,?)", [
        (34, "Tritanium", 18, 1),          # ordinary market item
        (638, "Raven", 80, 1),
        (60, "Asset Safety Wrap", None, 0),  # the real 404 case
        (2233, "Unpublished thing", 12, 0),  # published = 0
        (999, "No market group", None, 1),
    ])
    conn.commit()
    return conn


def test_only_types_the_endpoint_can_answer_are_asked(sde):
    kept = P._types_with_market_history(sde, [34, 638, 60, 2233, 999])
    assert kept == [34, 638]


def test_order_is_preserved_and_duplicates_do_not_multiply(sde):
    assert P._types_with_market_history(sde, [638, 34, 638]) == [638, 34, 638]


def test_an_empty_list_is_not_a_query(sde):
    assert P._types_with_market_history(sde, []) == []
    assert P._types_with_market_history(sde, [None, 0]) == []


def test_a_missing_sde_asks_about_everything_rather_than_nothing(tmp_path):
    """Fewer volumes is a worse failure than a slow sweep, so the filter opens up
    rather than closing down when it cannot check."""
    empty = sqlite3.connect(tmp_path / "empty.db")
    assert P._types_with_market_history(empty, [34, 638]) == [34, 638]


def test_more_types_than_sqlite_takes_variables_are_chunked(sde):
    """The real list is ~19 800 ids; SQLite's variable limit is well under that."""
    sde.executemany("INSERT INTO sde_types VALUES (?,?,?,?)",
                    [(10_000 + i, f"t{i}", 5, 1) for i in range(2500)])
    sde.commit()
    ids = [10_000 + i for i in range(2500)] + [60]
    kept = P._types_with_market_history(sde, ids)
    assert len(kept) == 2500 and 60 not in kept


def test_history_concurrency_stays_where_it_was_measured():
    """The limiter goes by request RATE, not by a cumulative count.

    Measured 2026-08-24 on the same 17 307-type list: the refresh path at
    concurrency 30 reaches ~290-380 req/s (it commits every 200 results, which
    paces it) and finishes in 45 s with zero 429s; at 10 it is clean too but takes
    ~105 s. A bare unpaced loop at 30 hits ~460 req/s and does get 429s - which is
    what the custom-station sweep used to be before it stopped asking about types
    the station does not sell. Raise this only with a fresh measurement of BOTH
    paths; the endpoint's behaviour changed under us once already.
    """
    assert P._HIST_SEM._value == 30


# ── fetch_structure_market: scope narrowing + crash-safe persistence ──────────

def _sde_conn(ids, market_group_id=1, published=1):
    conn = sqlite3.connect(":memory:")
    P.ensure_price_table(conn)
    conn.execute("CREATE TABLE sde_types (type_id INTEGER PRIMARY KEY, name TEXT,"
                 " market_group_id INTEGER, published INTEGER)")
    conn.executemany("INSERT INTO sde_types VALUES (?,?,?,?)",
                     [(i, f"t{i}", market_group_id, published) for i in ids])
    conn.commit()
    return conn


class _StructureOrdersClient:
    """Fake esi_client(): one page of sell orders for `listed_ids`, no buy orders."""

    def __init__(self, listed_ids):
        self.listed_ids = listed_ids

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        class _Resp:
            status_code = 200
            headers = {"X-Pages": "1"}
            def json(inner):
                return [{"type_id": t, "is_buy_order": False, "volume_remain": 10,
                         "price": 100.0} for t in self.listed_ids]
        return _Resp()


def test_cold_sweep_only_asks_about_types_actually_listed_here(monkeypatch):
    """Reported bug: the sweep asked about every cached type (~19 800), most of
    which a random remote structure never sells - approved fix: narrow to what
    is actually listed there."""
    import asyncio
    all_ids = list(range(1, 21))
    listed = {1, 2, 3}
    conn = _sde_conn(all_ids)
    monkeypatch.setattr(P, "esi_client", lambda **kw: _StructureOrdersClient(listed))
    monkeypatch.setattr(P, "_cached_region_volume", lambda conn, region: None)

    asked = []
    async def fake_fetch(client, region_id, type_id):
        asked.append(type_id)
        return 99
    monkeypatch.setattr(P, "_fetch_region_volume", fake_fetch)

    result = asyncio.run(P.fetch_structure_market(
        conn, 999_000, "tok", set(all_ids), region_id=10000002))

    assert set(asked) == listed
    for tid in listed:
        assert result[tid] == (10, 100.0, 99)
    for tid in set(all_ids) - listed:
        # Not sold here: no sell order, and no history was ever asked about it -
        # blank (None), never a wrong number.
        assert result[tid] == (0, None, None)


def test_a_warm_region_still_gets_full_coverage_for_free(monkeypatch):
    """When the region's volumes are already cached (Jita/Forge, a hub, or a
    previous sweep), reusing them costs nothing - so there is no reason to
    narrow the scope in that case."""
    import asyncio
    all_ids = list(range(1, 6))
    conn = _sde_conn(all_ids)
    monkeypatch.setattr(P, "esi_client", lambda **kw: _StructureOrdersClient({1}))
    monkeypatch.setattr(P, "_cached_region_volume", lambda conn, region: {i: i * 10 for i in all_ids})

    async def must_not_be_called(client, region_id, type_id):
        raise AssertionError("cold sweep must not run when the region is cached")
    monkeypatch.setattr(P, "_fetch_region_volume", must_not_be_called)

    result = asyncio.run(P.fetch_structure_market(
        conn, 999_000, "tok", set(all_ids), region_id=10000002))
    for tid in all_ids:
        assert result[tid][2] == tid * 10          # every type, not just the listed one


def test_an_interrupted_sweep_keeps_finished_batches_not_just_the_fast_phase(monkeypatch):
    """The whole point of committing per batch rather than once at the end: a
    load cancelled partway (tab closed, navigated away) must not throw away
    history that was already fetched successfully."""
    import asyncio
    listed = set(range(1, 601))                    # 601 listed types -> 3 batches of 300ish? _BATCH=300 -> 2 full + 1
    all_ids = listed | {700, 701}                   # plus a couple never listed here
    conn = _sde_conn(list(all_ids))
    monkeypatch.setattr(P, "esi_client", lambda **kw: _StructureOrdersClient(listed))
    monkeypatch.setattr(P, "_cached_region_volume", lambda conn, region: None)

    gate = asyncio.Event()

    async def fake_fetch(client, region_id, type_id):
        if type_id > 300:                           # second batch onward blocks
            await gate.wait()
        return 7
    monkeypatch.setattr(P, "_fetch_region_volume", fake_fetch)

    async def run():
        task = asyncio.create_task(P.fetch_structure_market(
            conn, 999_000, "tok", set(all_ids), region_id=10000002))
        await asyncio.sleep(0.1)                     # let the first batch land
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(run())

    rows = {r[0]: r[1] for r in conn.execute(
        "SELECT type_id, traded_volume FROM station_volume_cache")}
    # First batch (1-300) completed and was committed before cancellation.
    assert rows[1] == 7 and rows[300] == 7
    # Second batch never got to commit - NULL, not a wrong value, and still
    # retryable on the next load rather than looking like "confirmed no trade".
    assert rows[301] is None
    # The fast phase (sell prices) is there regardless of what happened to history.
    sell = {r[0]: r[1] for r in conn.execute(
        "SELECT type_id, best_sell FROM station_volume_cache")}
    assert sell[1] == 100.0 and sell[500] == 100.0 and sell[700] is None


# ── fetch_station_volumes (NPC stations): same narrowing, same guarantee ──────

def test_npc_station_cold_sweep_also_narrows_to_what_has_an_order_here(monkeypatch):
    import asyncio
    all_ids = list(range(1, 11))
    listed = {2, 4}
    conn = _sde_conn(all_ids)
    monkeypatch.setattr(P, "_cached_region_volume", lambda conn, region: None)

    async def fake_orders(client, region_id, location_id, type_id):
        return (10, 50.0) if type_id in listed else (None, None)
    monkeypatch.setattr(P, "_fetch_orders_for_type", fake_orders)

    asked = []
    async def fake_fetch(client, region_id, type_id):
        asked.append(type_id)
        return 5
    monkeypatch.setattr(P, "_fetch_region_volume", fake_fetch)
    monkeypatch.setattr(P, "esi_client", lambda **kw: _NullClient())

    result = asyncio.run(P.fetch_station_volumes(conn, 60003760, 10000002, all_ids))
    assert set(asked) == listed
    for tid in listed:
        assert result[tid] == (10, 50.0, 5)
    for tid in set(all_ids) - listed:
        assert result[tid] == (None, None, None)


class _NullClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


# ── the HTTP-caching layer: what survives, and what used to be skipped ────────

def test_an_empty_history_is_remembered_instead_of_re_asked(tmp_path, monkeypatch):
    """"This type has never traded here" is an answer. Not storing it meant the
    same types were re-requested on every single load - measured 84 of 537 at one
    station, every time."""
    import asyncio
    conn = sqlite3.connect(tmp_path / "e.db")
    P.ensure_hist_etag_table(conn)
    P._hist_etags.clear(); P._hist_etags_dirty.clear()

    class _Resp:
        status_code = 200
        headers = {"etag": 'W/"abc"', "expires": "Wed, 21 Oct 2099 07:28:00 GMT"}
        def json(self): return []            # traded here: never

    class _Client:
        async def get(self, *a, **k): return _Resp()

    vol = asyncio.run(P._fetch_region_volume(_Client(), 10000002, 34))
    assert vol == 0
    assert (10000002, 34) in P._hist_etags          # remembered...
    assert P.flush_hist_etags(conn) == 1            # ...and persisted
    stored = conn.execute("SELECT etag, days_json FROM market_hist_etag").fetchone()
    assert stored[0] == 'W/"abc"' and stored[1] == "{}"
    P._hist_etags.clear(); P._hist_etags_dirty.clear()


def test_flush_without_clear_keeps_the_map_for_the_rest_of_the_run(tmp_path):
    """A per-batch flush must not drop the in-memory map: the remaining batches
    need it for their If-None-Match headers, or cheap 304s become full bodies."""
    conn = sqlite3.connect(tmp_path / "f.db")
    P.ensure_hist_etag_table(conn)
    P._hist_etags.clear(); P._hist_etags_dirty.clear()
    P._hist_etags[(1, 2)] = ('W/"x"', {"2026-08-20": 5}, 9e9)
    P._hist_etags_dirty.add((1, 2))

    assert P.flush_hist_etags(conn, clear=False) == 1
    assert (1, 2) in P._hist_etags                  # still usable
    assert not P._hist_etags_dirty                  # but no longer pending

    P._hist_etags_dirty.add((1, 2))
    P.flush_hist_etags(conn)                        # final call does clear
    assert not P._hist_etags
    P._hist_etags.clear(); P._hist_etags_dirty.clear()


def test_the_structure_path_uses_the_etag_cache_at_all(monkeypatch):
    """It did not. A citadel load re-fetched every history body every time and
    stored nothing for the next load, so the whole HTTP-caching layer skipped the
    exact case the slow loads were reported for."""
    import asyncio
    all_ids = [1, 2, 3]
    conn = _sde_conn(all_ids)
    calls = []
    monkeypatch.setattr(P, "load_hist_etags", lambda c, r: calls.append(("load", r)))
    monkeypatch.setattr(P, "flush_hist_etags", lambda c, clear=True: calls.append(("flush", clear)))
    monkeypatch.setattr(P, "esi_client", lambda **kw: _StructureOrdersClient({1}))
    monkeypatch.setattr(P, "_cached_region_volume", lambda conn, region: None)

    async def fake_fetch(client, region_id, type_id): return 1
    monkeypatch.setattr(P, "_fetch_region_volume", fake_fetch)

    asyncio.run(P.fetch_structure_market(conn, 999, "tok", set(all_ids), region_id=10000002))
    assert ("load", 10000002) in calls, calls
    assert any(k == "flush" for k, _ in calls), calls
