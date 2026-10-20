"""What the 2026-10-20 audit of data loading found, each pinned down.

Every case here was a piece of data that either nothing refreshed by itself, or
that a refresh could not tell apart from "not loaded".
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import httpx
import pytest

from app.market import prices as P
from app.web import background as bg
from app.web import industry_helper as IH


def _client(status, body):
    def handler(req):
        return httpx.Response(status, json=body)
    return lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ih_db():
    conn = sqlite3.connect(":memory:")
    IH.ensure_industry_tables(conn)
    return conn


# ── Adjusted prices ──────────────────────────────────────────────────────────

def test_a_type_ccp_stopped_pricing_does_not_keep_its_old_price(monkeypatch):
    # 149 days old on a real cache, still shown as an "est" value.
    conn = _ih_db()
    conn.execute("INSERT INTO adjusted_price_cache VALUES (99999, 5.0, ?)",
                 (time.time() - 149 * 86400,))
    conn.commit()
    monkeypatch.setattr(IH, "esi_client",
                        _client(200, [{"type_id": 34, "adjusted_price": 4.2}]))
    asyncio.run(IH.refresh_adjusted_prices(conn))
    assert conn.execute("SELECT type_id, adjusted FROM adjusted_price_cache").fetchall() \
        == [(34, 4.2)]


def test_the_adjusted_cache_can_be_fresh_at_all(monkeypatch):
    # The freshness test reads the OLDEST row; with leftovers it was never
    # fresh, so every Plan calculation fetched the whole list again.
    conn = _ih_db()
    conn.execute("INSERT INTO adjusted_price_cache VALUES (99999, 5.0, ?)",
                 (time.time() - 149 * 86400,))
    conn.commit()
    monkeypatch.setattr(IH, "esi_client",
                        _client(200, [{"type_id": 34, "adjusted_price": 4.2}]))
    asyncio.run(IH.refresh_adjusted_prices(conn))
    assert IH._adj_is_fresh(conn)


def test_a_failed_adjusted_refresh_keeps_the_stored_list(monkeypatch):
    conn = _ih_db()
    conn.execute("INSERT INTO adjusted_price_cache VALUES (34, 5.0, 1)")
    conn.commit()
    monkeypatch.setattr(IH, "esi_client", _client(503, {"error": "down"}))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(IH.refresh_adjusted_prices(conn))
    assert conn.execute("SELECT adjusted FROM adjusted_price_cache").fetchall() == [(5.0,)]


# ── System cost indices ──────────────────────────────────────────────────────

def test_a_system_without_industry_does_not_fetch_every_index_again(monkeypatch):
    # No row for it, so it read as stale on every call - one /industry/systems/
    # download (all 16 000 systems) per lookup.
    conn = _ih_db()
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json=[{"solar_system_id": 30000142, "cost_indices": [
            {"activity": "manufacturing", "cost_index": 0.05}]}])
    monkeypatch.setattr(IH, "esi_client",
                        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    asyncio.run(IH.get_sci_for_system(conn, 31000001, "manufacturing"))
    asyncio.run(IH.get_sci_for_system(conn, 31000001, "manufacturing"))
    asyncio.run(IH.get_sci_for_system(conn, 30000142, "manufacturing"))
    assert len(calls) == 1


# ── 7-day volume of a type ESI keeps no history for ─────────────────────────

@pytest.mark.parametrize("status", [400, 404])
def test_no_history_is_zero_traded_and_remembered(tmp_path, status):
    # A SKIN and an event crate with sell orders showed a dash in the 7-day
    # column - "not loaded" - and were asked again on every refresh.
    conn = sqlite3.connect(tmp_path / "c.db")
    P.ensure_price_table(conn)
    P._hist_etags.clear()
    P._hist_etags_dirty.clear()
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(status, json={"error": "Type not found!"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            first = await P._fetch_region_volume(c, 10000002, 90014)
            second = await P._fetch_region_volume(c, 10000002, 90014)
            return first, second
    assert asyncio.run(go()) == (0, 0)
    assert len(calls) == 1, "remembered for a day, not asked again"
    P.flush_hist_etags(conn)
    # ... and the region top-up counts it as covered.
    assert P._region_volume_from_etags(conn, 10000002, fresh_only=True) == {90014: 0}


def test_a_failure_that_may_pass_is_still_unknown():
    def handler(req):
        return httpx.Response(503, json={"error": "down"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await P._fetch_region_volume(c, 10000002, 34)
    P._hist_etags.clear()
    assert asyncio.run(go()) is None


# ── The new jobs are scheduled ───────────────────────────────────────────────

def test_histories_and_indices_have_jobs_of_their_own(client, app_module):
    keys = {j["key"] for j in client.get("/api/activity").json()["jobs"]}
    assert {"char-history", "industry"} <= keys
    assert "deals-history" in bg.keys()          # shown once contracts are indexed


def test_the_wallet_history_job_reads_every_character(app_module, monkeypatch):
    m = app_module
    seen = []

    async def journal(client, conn, cid, token, max_age=3600.0):
        seen.append(("journal", cid))
        return 0

    async def rest(client, conn, cid, token):
        seen.append(("rest", cid))
        return {}

    async def token(cid):
        return "tok"
    monkeypatch.setattr(m.income_api, "refresh_journal", journal)
    monkeypatch.setattr(m.hist_api, "refresh_all", rest)
    monkeypatch.setattr(m, "_valid_token_async", token)
    res = asyncio.run(m._job_char_history(lambda **kw: None))
    assert res == {"characters": 2}
    assert sorted(seen) == [("journal", 900000001), ("journal", 900000002),
                            ("rest", 900000001), ("rest", 900000002)]


def test_the_wallet_history_job_fails_loudly_when_no_login_works(app_module, monkeypatch):
    m = app_module

    async def no_token(cid):
        return None
    monkeypatch.setattr(m, "_valid_token_async", no_token)
    with pytest.raises(RuntimeError):
        asyncio.run(m._job_char_history(lambda **kw: None))


# ── Journal and the Market popup: whole, or say so ──────────────────────────

def _paged(responses):
    def handler(req):
        page = int(req.url.params.get("page", 1))
        status, body = responses.get(page, (503, {}))
        return httpx.Response(status, json=body, headers={"x-pages": str(len(responses))})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("corp", [False, True])
def test_a_journal_cut_short_is_not_a_quiet_wallet(corp):
    from app.character import wallet as W
    from app.esi.client import IncompleteAnswer
    entry = {"id": 1, "date": "2026-10-20T10:00:00Z", "amount": 5.0, "ref_type": "bounty_prizes"}

    async def go():
        async with _paged({1: (200, [entry]), 2: (502, {})}) as c:
            if corp:
                return await W.fetch_corp_journal(c, 98000001, 1, "tok")
            return await W.fetch_journal(c, 900000001, "tok")
    with pytest.raises(IncompleteAnswer):
        asyncio.run(go())


def test_the_market_popup_says_when_the_book_is_not_whole(client, app_module, monkeypatch):
    m = app_module
    monkeypatch.setattr(m, "esi_client", lambda **kw: _paged(
        {1: (200, [{"type_id": 34, "price": 5.0, "volume_remain": 1, "is_buy_order": False,
                    "location_id": 60003760}]), 2: (503, {})}))
    d = client.get("/api/prices/orders?type_id=34&region_id=10000002").json()
    assert d["ok"] is False and "whole order book" in d["error"]


# ── A history read that fails is retried, then reported ────────────────────

def _history_world(monkeypatch, fail_first: set):
    """ESI where the types in `fail_first` fail their first read only."""
    import app.esi.client as esi
    import app.market.prices as P
    seen: dict[int, int] = {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def history(client, region, tid):
        seen[tid] = seen.get(tid, 0) + 1
        if tid in fail_first and seen[tid] == 1:
            return None                          # ESI shedding load
        return [{"d": "2026-10-19", "avg": 5.0, "vol": 1}]

    real_sleep = asyncio.sleep

    async def no_wait(_s, *a, **k):              # the 5 s pause before the retry
        await real_sleep(0)
    monkeypatch.setattr(esi, "esi_client", lambda **kw: _Client())
    monkeypatch.setattr(P, "fetch_region_history", history)
    monkeypatch.setattr(asyncio, "sleep", no_wait)
    return seen


def test_a_failed_history_read_is_retried_once(monkeypatch):
    from app.character import worth
    seen = _history_world(monkeypatch, fail_first={35})
    conn = sqlite3.connect(":memory:")
    progress = []
    got = asyncio.run(worth.fetch_index_histories(
        conn, [34, 35], progress_cb=lambda d, t: progress.append((d, t))))
    assert got == 2 and seen == {34: 1, 35: 2}
    assert max(progress) == (2, 2), "a retry does not count past the total"


def test_the_deals_job_does_not_call_a_run_with_holes_a_success(app_module, monkeypatch):
    m = app_module
    calls = {"n": 0}

    async def fetch(conn, todo, progress_cb=None):
        return 0                                  # nothing could be read

    monkeypatch.setattr(m.worth_api, "fetch_index_histories", fetch)
    monkeypatch.setattr(m.deals_api, "types_needing_history", lambda conn: [34, 35])
    with pytest.raises(RuntimeError) as err:
        asyncio.run(m._job_deals_history(lambda **kw: None))
    assert "2 of 2 types" in str(err.value)
