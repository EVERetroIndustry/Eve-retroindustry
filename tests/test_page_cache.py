"""Switching tabs must not wait on ESI.

Measured before this: /planets made 54 ESI calls and /jobs 12 before either page
produced a byte of HTML, every visit. Worse, both took the token through the
BLOCKING refresh helper on the event loop, so twelve characters meant twelve
synchronous SSO round trips one after another - 4.99 s with a 0.4 s stand-in for
SSO, and the whole app frozen for the duration because nothing else on the loop
could run.

The tests here pin all three properties: the second visit costs nothing, a first
visit does not fetch twice, and no page route refreshes a token on the loop.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from app.web.page_cache import (age_label, drop_cached, ensure_page_cache,
                                get_cached, put_cached)


def test_cache_round_trip_and_age():
    conn = sqlite3.connect(":memory:")
    ensure_page_cache(conn)
    assert get_cached(conn, "jobs", 1) is None

    put_cached(conn, "jobs", 1, [{"job_id": 7}])
    payload, age = get_cached(conn, "jobs", 1)
    assert payload == [{"job_id": 7}]
    assert age < 5

    # age comes back as a number so the caller owns the TTL and the page can say it
    conn.execute("UPDATE page_cache SET cached_at = ?", (time.time() - 7200,))
    conn.commit()
    _payload, age = get_cached(conn, "jobs", 1)
    assert 7000 < age < 7400

    drop_cached(conn, "jobs", 1)
    assert get_cached(conn, "jobs", 1) is None


def test_age_label_never_claims_now_when_it_does_not_know():
    assert age_label(None) == ""
    assert age_label(10) == "just now"
    assert age_label(600) == "10 min ago"
    assert age_label(7200) == "2 h ago"
    assert age_label(200000) == "2 d ago"


def test_a_corrupt_payload_reads_as_a_miss():
    """A miss is recoverable - the page fetches. A raise would be a 500."""
    conn = sqlite3.connect(":memory:")
    ensure_page_cache(conn)
    conn.execute("INSERT INTO page_cache VALUES ('jobs','1','not json',?)", (time.time(),))
    conn.commit()
    assert get_cached(conn, "jobs", 1) is None


class _Counter:
    """Counts ESI calls and refuses any token refresh made on the event loop."""

    def __init__(self):
        self.calls = 0
        self.on_loop = 0


@pytest.fixture
def esi(monkeypatch):
    import httpx
    counter = _Counter()
    orig_send = httpx.AsyncClient.send

    async def send(self, request, **kw):
        if "evetech" in request.url.host or "eveonline" in request.url.host:
            counter.calls += 1
        return await orig_send(self, request, **kw)

    def post(*a, **kw):
        # A refresh belongs in a worker thread, where there is no running loop.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            counter.on_loop += 1
        raise RuntimeError("no SSO in tests")

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    monkeypatch.setattr(httpx, "post", post)
    return counter


def _seed_jobs(app_module, char_ids, age_s=0.0):
    conn = app_module.get_conn()
    try:
        ensure_page_cache(conn)
        for cid in char_ids:
            put_cached(conn, "jobs", cid, [])
        if age_s:
            conn.execute("UPDATE page_cache SET cached_at = ? WHERE kind='jobs'",
                         (time.time() - age_s,))
            conn.commit()
    finally:
        conn.close()


def test_a_cached_page_asks_esi_for_nothing(client, app_module, esi):
    """The whole point: a tab switch inside the TTL is a local query."""
    conn = app_module.get_conn()
    try:
        ids = [c for c, _ in app_module.list_characters(conn)]
    finally:
        conn.close()
    _seed_jobs(app_module, ids)

    r = client.get("/jobs")
    assert r.status_code == 200
    assert esi.calls == 0
    assert "just now" in r.text


def test_no_page_route_refreshes_a_token_on_the_event_loop(client, app_module, esi):
    """The regression that made switching slow: twelve synchronous SSO round
    trips, serialised, with the loop blocked for all of them."""
    # The fixture database is session-scoped, so expiring the tokens has to be
    # undone: leaving them expired changed what later tests rendered and broke
    # four of them in the full run while each passed on its own.
    conn = app_module.get_conn()
    try:
        saved = conn.execute(
            "SELECT character_id, token_expires_at FROM characters").fetchall()
        conn.execute("UPDATE characters SET token_expires_at = 0")
        conn.commit()
    finally:
        conn.close()
    try:
        # Every page, not a sample: the one left out of the first pass was
        # /contracts/alliance, and it was still serialising twelve refreshes.
        for page in ("/jobs", "/planets", "/assets", "/wallet", "/orders",
                     "/contracts", "/contracts/alliance", "/blueprints", "/plan",
                     "/prices", "/", "/api/dashboard/live"):
            client.get(page)
        assert esi.on_loop == 0, "a token was refreshed on the event loop"
    finally:
        conn = app_module.get_conn()
        try:
            conn.executemany(
                "UPDATE characters SET token_expires_at=? WHERE character_id=?",
                [(exp, cid) for cid, exp in saved])
            conn.commit()
        finally:
            conn.close()


def test_stale_data_is_served_and_refreshed_afterwards(client, app_module, esi,
                                                       monkeypatch):
    """Stale must render immediately; the refresh belongs after the response."""
    conn = app_module.get_conn()
    try:
        ids = [c for c, _ in app_module.list_characters(conn)]
    finally:
        conn.close()
    _seed_jobs(app_module, ids, age_s=app_module._JOBS_CACHE_TTL + 600)

    scheduled = []
    monkeypatch.setattr(app_module, "_schedule_refresh",
                        lambda keys, worker: scheduled.extend(keys))

    r = client.get("/jobs")
    assert r.status_code == 200
    assert esi.calls == 0, "stale data must be served without waiting for ESI"
    assert sorted(scheduled) == sorted(ids), "and refreshed behind the request"


def test_a_missing_entry_is_fetched_once_not_twice(client, app_module, esi,
                                                  monkeypatch):
    """A cache MISS used to be queued for a background refresh as well, so a
    first visit fetched every character twice - 24 calls where 12 were needed."""
    conn = app_module.get_conn()
    try:
        drop_cached(conn, "jobs")
    finally:
        conn.close()

    scheduled = []
    monkeypatch.setattr(app_module, "_schedule_refresh",
                        lambda keys, worker: scheduled.extend(keys))
    client.get("/jobs")
    assert scheduled == [], "nothing that was just fetched needs refreshing"


def test_any_valid_token_asks_once_per_character(app_module, monkeypatch):
    """The pattern replaced here called the blocking refresh twice per character,
    once to test the token and once to take it."""
    asked = []

    async def fake(cid):
        asked.append(cid)
        return "tok" if cid == 3 else None

    monkeypatch.setattr(app_module, "_valid_token_async", fake)
    got = asyncio.run(app_module._any_valid_token_async([1, 2, 3, 4]))
    assert got == "tok"
    assert asked == [1, 2, 3], "stops at the first usable token, one ask each"
