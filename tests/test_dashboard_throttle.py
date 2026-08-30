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

import sqlite3
import time

import pytest

import app.esi.client as EC
from app.character.assets import (clear_corp_denied, corp_access_denied,
                                  _mark_corp_denied, _CORP_DENIED_TTL)


@pytest.fixture
def paused():
    """ESI's global error-limit governor, paused, as a spent budget leaves it."""
    EC._ERROR_LIMIT._pause_until = time.monotonic() + 40
    yield
    EC._ERROR_LIMIT._pause_until = 0.0


def test_the_live_endpoint_answers_at_once_while_esi_is_paused(client, paused):
    """It used to wait inside the pause; measured still running after 25 s."""
    t0 = time.time()
    r = client.get("/api/dashboard/live")
    assert r.status_code == 200
    assert time.time() - t0 < 5, "the pause must be read, not waited out"

    d = r.json()
    assert d.get("throttled") is True
    assert d.get("wait_s", 0) > 0
    assert d.get("logged_in") is True, "the stored view is still served"


def test_it_says_nothing_is_throttled_when_nothing_is(client):
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
                                                                   clean_governors):
    """Reported as "the notice is there constantly". It was: asking for the worst
    pause across EVERY rate-limit group meant a 429 on the market group - which a
    price refresh earns routinely - made the dashboard declare itself
    rate-limited. Verified while it was happening: the shared error budget was
    100 of 100, untouched, because a 429 costs nothing."""
    _pause_group("https://esi.evetech.net/latest/markets/10000002/orders/", 120)
    d = client.get("/api/dashboard/live").json()
    assert d.get("throttled") in (False, None)


def test_a_pause_on_a_group_the_dashboard_uses_does_stop_it(client, clean_governors):
    _pause_group("https://esi.evetech.net/latest/characters/1/wallet/", 90)
    t0 = time.time()
    d = client.get("/api/dashboard/live").json()
    assert d["throttled"] is True
    assert 80 <= d["wait_s"] <= 90
    assert time.time() - t0 < 5, "read the pause, do not wait it out"


def test_the_global_error_limit_still_stops_everything(client, clean_governors):
    """It is not per-group: a spent error budget pauses every call there is."""
    EC._ERROR_LIMIT._pause_until = time.monotonic() + 45
    d = client.get("/api/dashboard/live").json()
    assert d["throttled"] is True
