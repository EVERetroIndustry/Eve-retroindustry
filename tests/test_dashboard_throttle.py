"""The dashboard must not sit and wait when ESI has told us to wait.

Reported: "often after Sync All, opening the dashboard just spins and never
loads". Reproduced with the governor paused - the state a sync leaves behind once
it has spent the error budget - the live endpoint was still running after 25
seconds, and the browser gave up at 45 with "couldn't load live data" and no
reason. Since the dashboard also refetches on focus and every two minutes, that
reads as never loading at all.

The cause was avoidable too: corp assets re-asked for every character lacking the
role on every sync, and each 403 costs five rate-limit tokens plus a slot in the
error budget - which is global, so spending it stops everything.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

import app.esi.client as EC
from app.character.assets import (clear_corp_denied, corp_access_denied,
                                  _mark_corp_denied, _CORP_DENIED_TTL)


def _age_dash_cache(app_module, seconds: float) -> None:
    """Push the stored live payload back in time, so the next request has to
    refresh instead of serving it."""
    conn = app_module.get_conn()
    try:
        conn.execute("UPDATE page_cache SET cached_at = ? WHERE kind = ?",
                     (time.time() - seconds, app_module._DASH_LIVE_KIND))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def no_dash_cache(app_module):
    """The live payload is cached for a minute, so a test that wants a real
    refresh has to start from nothing - and leave nothing behind."""
    from app.web.page_cache import drop_cached

    def _clear():
        conn = app_module.get_conn()
        try:
            drop_cached(conn, app_module._DASH_LIVE_KIND)
        finally:
            conn.close()
        app_module._DASH_FLIGHT[0] = None

    _clear()
    yield
    _clear()


@pytest.fixture
def paused():
    """ESI's global error-limit governor, paused, as a spent budget leaves it."""
    EC._ERROR_LIMIT._pause_until = time.monotonic() + 40
    yield
    EC._ERROR_LIMIT._pause_until = 0.0


def test_the_live_endpoint_answers_at_once_while_esi_is_paused(client, paused,
                                                              no_dash_cache):
    """It used to wait inside the pause; measured still running after 25 s."""
    t0 = time.time()
    r = client.get("/api/dashboard/live")
    assert r.status_code == 200
    assert time.time() - t0 < 5, "the pause must be read, not waited out"

    d = r.json()
    assert d.get("throttled") is True
    assert d.get("wait_s", 0) > 0
    assert d.get("logged_in") is True, "the stored view is still served"


def test_it_says_nothing_is_throttled_when_nothing_is(client, no_dash_cache):
    d = client.get("/api/dashboard/live").json()
    assert d.get("throttled") in (False, None)


def test_a_refused_corporation_is_remembered():
    """The 403s that spent the budget: twelve characters, twelve refusals, on
    every sync."""
    conn = sqlite3.connect(":memory:")
    assert corp_access_denied(conn, 98000001, 900000001) is False

    _mark_corp_denied(conn, 98000001, 900000001)
    assert corp_access_denied(conn, 98000001, 900000001) is True
    # ...and only for that pair
    assert corp_access_denied(conn, 98000001, 900000002) is False
    assert corp_access_denied(conn, 98000002, 900000001) is False


def test_a_refusal_expires_so_a_new_role_is_noticed():
    conn = sqlite3.connect(":memory:")
    _mark_corp_denied(conn, 98000001, 900000001)
    conn.execute("UPDATE corp_assets_denied SET at = ?",
                 (time.time() - _CORP_DENIED_TTL - 60,))
    conn.commit()
    assert corp_access_denied(conn, 98000001, 900000001) is False


def test_a_manual_sync_forgets_every_refusal():
    """A role granted five minutes ago has to work now, not in six hours."""
    conn = sqlite3.connect(":memory:")
    _mark_corp_denied(conn, 98000001, 900000001)
    clear_corp_denied(conn)
    assert corp_access_denied(conn, 98000001, 900000001) is False


def test_a_denied_character_sends_no_request(monkeypatch):
    """The point of remembering: the call is not made at all."""
    import asyncio
    from app.character import assets as A

    conn = sqlite3.connect(":memory:")
    A.ensure_corp_assets_table(conn)
    _mark_corp_denied(conn, 98000001, 900000001)

    asked = []

    class _R:
        status_code = 200
        headers = {"x-pages": "1"}

        def json(self):
            return {"corporation_id": 98000001}

        def raise_for_status(self):
            pass

    class Client:
        async def get(self, url, **kw):
            asked.append(url)
            return _R()

    corp_id, items = asyncio.run(
        A.fetch_corp_assets(Client(), 900000001, "tok", conn))
    assert corp_id == 98000001
    assert items == []
    assert not any("/assets/" in u for u in asked), \
        "the corporation assets endpoint must not be asked again"


def _pause_group(url: str, seconds: float) -> None:
    import httpx
    sig = EC._TokenBucketGovernor.signature(httpx.URL(url))
    EC._TOKEN_LIMIT._group_of[sig] = "grp-" + sig
    EC._TOKEN_LIMIT._pause_until["grp-" + sig] = time.monotonic() + seconds


@pytest.fixture
def clean_governors():
    EC._TOKEN_LIMIT._pause_until.clear()
    EC._ERROR_LIMIT._pause_until = 0.0
    yield
    EC._TOKEN_LIMIT._pause_until.clear()
    EC._ERROR_LIMIT._pause_until = 0.0


def test_a_pause_on_an_unrelated_group_does_not_stop_the_dashboard(client,
                                                                   clean_governors,
                                                                   no_dash_cache):
    """Reported as "the notice is there constantly". It was: asking for the worst
    pause across EVERY rate-limit group meant a 429 on the market group - which a
    price refresh earns routinely - made the dashboard declare itself
    rate-limited. Verified while it was happening: the shared error budget was
    100 of 100, untouched, because a 429 costs nothing."""
    _pause_group("https://esi.evetech.net/latest/markets/10000002/orders/", 120)
    d = client.get("/api/dashboard/live").json()
    assert d.get("throttled") in (False, None)


def test_a_pause_on_a_group_the_dashboard_uses_does_stop_it(client, clean_governors,
                                                           no_dash_cache):
    _pause_group("https://esi.evetech.net/latest/characters/1/wallet/", 90)
    t0 = time.time()
    d = client.get("/api/dashboard/live").json()
    assert d["throttled"] is True
    assert 80 <= d["wait_s"] <= 90
    assert time.time() - t0 < 5, "read the pause, do not wait it out"


def test_the_global_error_limit_still_stops_everything(client, clean_governors,
                                                      no_dash_cache):
    """It is not per-group: a spent error budget pauses every call there is."""
    EC._ERROR_LIMIT._pause_until = time.monotonic() + 45
    d = client.get("/api/dashboard/live").json()
    assert d["throttled"] is True


# --------------------------------------------------------------------------
# What repeated clicking costs, and what happens when the fetch is merely slow
# --------------------------------------------------------------------------
#
# Measured on a copy of a real twelve-character database with the transport
# intercepted (nothing sent): one dashboard load is 38 ESI calls - 12 location,
# 12 ship, 12 skillqueue, universe/names, markets/prices - and before this
# nothing reused any of it. Five clicks meant five independent fetches, 190
# calls. That cannot drain ESI's per-character token buckets on its own (~300
# loads inside 15 minutes would), but the 420 error budget is GLOBAL, 100 errors
# per 60 s, so 38 is the wrong number to multiply once calls start failing.

class _EsiCounter:
    def __init__(self):
        self.calls = 0


@pytest.fixture
def esi_calls(monkeypatch):
    import httpx
    c = _EsiCounter()
    orig = httpx.AsyncClient.send

    async def send(self, request, **kw):
        if "evetech" in (request.url.host or ""):
            c.calls += 1
        return await orig(self, request, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    return c


def test_a_second_click_inside_the_ttl_asks_esi_for_nothing(client, no_dash_cache,
                                                            esi_calls):
    first = client.get("/api/dashboard/live").json()
    assert first["logged_in"] is True
    after_first = esi_calls.calls

    second = client.get("/api/dashboard/live").json()
    assert second.get("cached") is True
    assert esi_calls.calls == after_first, "a repeat click must cost no ESI call"
    assert second["chars"] == first["chars"], "and it must be the same answer"


def test_the_refresh_icon_still_gets_current_numbers(client, no_dash_cache, esi_calls):
    """force=1 is the Refresh icon and the retry link: when someone asks for the
    current numbers, a stored copy is not an answer."""
    client.get("/api/dashboard/live")
    before = esi_calls.calls
    d = client.get("/api/dashboard/live?force=1").json()
    assert d.get("cached") is not True
    assert esi_calls.calls > before, "force must bypass the cache"


def test_clicks_that_arrive_together_share_one_fetch(app_module, no_dash_cache,
                                                    monkeypatch):
    """Concurrent clicks used to start independent full fetches - five clicks,
    five times 38 calls. Measured after this: 38 in total."""
    import types

    builds = []

    async def slow_build(request):
        builds.append(1)
        await asyncio.sleep(0.3)
        return {"logged_in": True, "chars": {"1": {}}, "agg_wallet_str": None,
                "agg_value_str": None}

    monkeypatch.setattr(app_module, "_dash_live_build", slow_build)
    req = types.SimpleNamespace(cookies={}, query_params={})

    async def five():
        return await asyncio.gather(*[
            app_module.api_dashboard_live(req) for _ in range(5)])

    out = asyncio.run(five())
    assert len(builds) == 1, f"one flight, not {len(builds)}"
    assert all(r.get("chars") for r in out), "all five still get an answer"


def test_a_slow_fetch_is_not_called_a_rate_limit(client, app_module, no_dash_cache,
                                                 monkeypatch, clean_governors):
    """The complaint behind this: clicking a few times ended with the dashboard
    empty and an ESI rate-limit notice on it. There was no rate limit - the fetch
    had exceeded its deadline, and the endpoint reported that as `throttled`, so
    the page retried after six seconds and fired another 38 calls."""
    monkeypatch.setattr(app_module, "_DASH_LIVE_DEADLINE", 0.2)

    real = app_module._compute_dashboard

    async def slow(request, conn, *, live):
        if live:
            await asyncio.sleep(5)
        return await real(request, conn, live=False)

    monkeypatch.setattr(app_module, "_compute_dashboard", slow)
    d = client.get("/api/dashboard/live").json()
    assert d.get("slow") is True
    assert d.get("throttled") in (False, None), "slow is not throttled"
    assert d.get("stale") is True


def test_a_pause_serves_the_last_live_values_not_blank_cards(client, app_module,
                                                             no_dash_cache,
                                                             clean_governors):
    """0.11.19 claimed it showed "the last values it stored" while it served the
    cache-only view, which has no wallet, no location and no training at all -
    those are ESI-only fields. Measured then: all three came back None."""
    fresh = client.get("/api/dashboard/live").json()
    cid = next(iter(fresh["chars"]))
    assert fresh["chars"][cid]["wallet_str"] is not None, "fixture should have a wallet"

    # Age it past the TTL: a copy younger than that is served with no notice at
    # all, because nothing needed ESI to answer.
    _age_dash_cache(app_module, app_module._DASH_LIVE_TTL + 60)
    EC._ERROR_LIMIT._pause_until = time.monotonic() + 45
    d = client.get("/api/dashboard/live").json()
    assert d["throttled"] is True
    assert d.get("stale") is True
    assert d["chars"][cid]["wallet_str"] == fresh["chars"][cid]["wallet_str"], \
        "the stored live values must survive the pause"
