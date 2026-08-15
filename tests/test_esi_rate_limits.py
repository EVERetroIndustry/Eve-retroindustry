"""ESI rate-limit governors.

ESI runs two limiters side by side: the old error limit (client-wide, HTTP 420)
and a token bucket (per rate-limit group, HTTP 429 + Retry-After). Client code
cannot choose between them — the server picks per route — so both have to be
survivable, and a drained bucket in one group must not stall the others.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.esi import client as esi


@pytest.fixture(autouse=True)
def clean_governors():
    """Governor state is process-global; keep tests from leaking into each other."""
    esi._ERROR_LIMIT._pause_until = 0.0
    esi._TOKEN_LIMIT._pause_until = {}
    esi._TOKEN_LIMIT._group_of = {}
    esi._market_token_provider = None
    esi._market_token_disabled_until = 0.0
    yield
    esi._ERROR_LIMIT._pause_until = 0.0
    esi._TOKEN_LIMIT._pause_until = {}
    esi._TOKEN_LIMIT._group_of = {}
    esi._market_token_provider = None
    esi._market_token_disabled_until = 0.0


def _resp(status: int, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, content=b"{}")


class _Transport(esi._GovernedTransport):
    """Governed transport with the network replaced by a scripted response list."""

    def __init__(self, responses):
        super().__init__()
        self._responses = list(responses)
        # Header SNAPSHOTS, not the request object: retries reuse the same
        # httpx.Request and mutate its headers in place, so keeping references
        # would show every attempt with the final header set.
        self.sent: list[dict] = []

    async def handle_async_request(self, request):  # type: ignore[override]
        return await super().handle_async_request(request)

    async def _send(self, request):
        self.sent.append({k.lower(): v for k, v in request.headers.items()})
        return self._responses.pop(0)


# httpx's real transport would open a socket; route the parent call to _send.
@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    async def fake(self, request):
        return await self._send(request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake)


def _get(transport, url="https://esi.evetech.net/latest/markets/10000002/orders/"):
    async def run():
        async with httpx.AsyncClient(transport=transport) as c:
            return await c.get(url)
    return asyncio.run(run())


# ── path signature ────────────────────────────────────────────────────────────

def test_signature_collapses_ids_so_all_regions_share_a_bucket_key():
    sig = esi._TokenBucketGovernor.signature
    a = sig(httpx.URL("https://esi.evetech.net/latest/markets/10000002/orders/"))
    b = sig(httpx.URL("https://esi.evetech.net/latest/markets/10000043/orders/"))
    assert a == b == "/latest/markets/{}/orders/"
    # Different routes must not collide.
    assert sig(httpx.URL("https://esi.evetech.net/latest/markets/1/history/")) != a


def test_limit_header_is_tokens_per_window():
    # "12000/15m" — the window is not a token count.
    assert esi._limit_header_total("12000/15m") == 12000
    assert esi._limit_header_total("150/15m") == 150
    assert esi._limit_header_total(None) is None
    assert esi._limit_header_total("nonsense") is None


# ── 429 ───────────────────────────────────────────────────────────────────────

def test_429_is_retried_after_the_server_says_so():
    t = _Transport([
        _resp(429, {"Retry-After": "1", "X-Ratelimit-Group": "market-order"}),
        _resp(200, {"X-Ratelimit-Group": "market-order",
                    "X-Ratelimit-Limit": "12000/15m", "X-Ratelimit-Remaining": "11000"}),
    ])
    started = time.monotonic()
    r = _get(t)
    assert r.status_code == 200
    assert len(t.sent) == 2
    assert time.monotonic() - started >= 1.0      # actually waited


def test_429_without_headers_is_still_survived():
    """CCP's docs warn that some 429s come from deep in the game servers with no
    rate-limit headers at all."""
    t = _Transport([_resp(429), _resp(200)])
    r = _get(t)
    assert r.status_code == 200 and len(t.sent) == 2


def test_429_pauses_only_its_own_group():
    """A drained market bucket must not stall wallet or asset calls — that is the
    whole reason this governor is per group rather than client-wide."""
    esi._TOKEN_LIMIT._group_of["/latest/markets/{}/orders/"] = "market-order"
    esi._TOKEN_LIMIT._pause_until["market-order"] = time.monotonic() + 30

    async def run():
        # An unrelated group is not paused, so this returns immediately.
        await asyncio.wait_for(
            esi._TOKEN_LIMIT.wait("/latest/characters/{}/wallet/"), timeout=0.5)
        # The market group is paused, so waiting on it would block.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                esi._TOKEN_LIMIT.wait("/latest/markets/{}/orders/"), timeout=0.3)
    asyncio.run(run())


def test_retry_after_is_clamped():
    """Never trust an unbounded delay, and never busy-loop on a zero."""
    g = esi._TokenBucketGovernor()
    assert g.blocked("x", _resp(429, {"Retry-After": "99999"})) == 300.0
    assert g.blocked("y", _resp(429, {"Retry-After": "0"})) == 1.0
    assert g.blocked("z", _resp(429, {"Retry-After": "junk"})) == 60.0


def test_exhausted_retries_hand_the_429_back():
    """The caller's raise_for_status must still fire rather than a hang or a lie."""
    t = _Transport([_resp(429, {"Retry-After": "0"}) for _ in range(4)])
    r = _get(t)
    assert r.status_code == 429


# ── proactive brake ───────────────────────────────────────────────────────────

def test_low_remaining_brakes_that_group():
    t = _Transport([_resp(200, {"X-Ratelimit-Group": "market-order",
                                "X-Ratelimit-Limit": "12000/15m",
                                "X-Ratelimit-Remaining": "500"})])   # ~4 %
    _get(t)
    assert esi._TOKEN_LIMIT._pause_until["market-order"] > time.monotonic()


def test_healthy_remaining_does_not_brake():
    t = _Transport([_resp(200, {"X-Ratelimit-Group": "market-order",
                                "X-Ratelimit-Limit": "12000/15m",
                                "X-Ratelimit-Remaining": "11998"})])
    _get(t)
    assert esi._TOKEN_LIMIT._pause_until.get("market-order", 0) <= time.monotonic()


# ── market bucket isolation ───────────────────────────────────────────────────

def test_token_is_attached_to_market_calls_only():
    esi.set_market_token_provider(lambda: "abc123")
    t = _Transport([_resp(200), _resp(200)])
    _get(t, "https://esi.evetech.net/latest/markets/10000002/orders/")
    _get(t, "https://esi.evetech.net/latest/universe/types/34/")
    assert t.sent[0].get("authorization") == "Bearer abc123"
    assert "authorization" not in t.sent[1]


def test_no_provider_means_unauthenticated_exactly_as_before():
    t = _Transport([_resp(200)])
    _get(t)
    assert "authorization" not in t.sent[0]


def test_existing_authorization_is_never_overwritten():
    esi.set_market_token_provider(lambda: "ours")
    t = _Transport([_resp(200)])

    async def run():
        async with httpx.AsyncClient(transport=t) as c:
            return await c.get("https://esi.evetech.net/latest/markets/1/orders/",
                               headers={"Authorization": "Bearer caller"})
    asyncio.run(run())
    assert t.sent[0]["authorization"] == "Bearer caller"


def test_a_rejected_token_falls_back_instead_of_breaking_the_call():
    """A stale token must never break a market call that works unauthenticated."""
    esi.set_market_token_provider(lambda: "expired")
    t = _Transport([_resp(403), _resp(200)])
    r = _get(t)
    assert r.status_code == 200
    assert t.sent[0].get("authorization") == "Bearer expired"
    assert "authorization" not in t.sent[1]      # retried anonymously
    assert esi._market_token_disabled_until > time.monotonic()   # and stops trying


def test_a_raising_provider_is_ignored():
    def boom(): raise RuntimeError("no db")
    esi.set_market_token_provider(boom)
    t = _Transport([_resp(200)])
    assert _get(t).status_code == 200
    assert "authorization" not in t.sent[0]


# ── the old error limit still works ───────────────────────────────────────────

def test_420_still_pauses_everything():
    """The error limit is client-wide by design; that behaviour must not regress."""
    t = _Transport([_resp(420, {"X-Esi-Error-Limit-Reset": "1"}), _resp(200)])
    r = _get(t)
    assert r.status_code == 200 and len(t.sent) == 2


def test_error_message_covers_both_limiters(app_module):
    for code in (420, 429):
        exc = httpx.HTTPStatusError("x", request=None, response=_resp(code))
        assert "rate-limiting" in (esi.esi_error_message(exc) or "")


# ── the market-token provider must never block the event loop ─────────────────

def test_market_token_provider_never_triggers_an_oauth_refresh(app_module, monkeypatch):
    """It runs inside the async transport, so a blocking call freezes the server.

    get_valid_token() does a synchronous httpx.post to the SSO endpoint with a
    15 s timeout, behind a per-character lock, whenever the access token has
    expired — and EVE access tokens last about 20 minutes. Calling it from the
    transport stalled every request in the process, the dashboard included, for
    the length of that round trip. The provider therefore reads a stored, still
    valid token and nothing else.
    """
    import app.auth.token_store as ts

    def _boom(*a, **k):
        raise AssertionError("provider must not call get_valid_token")

    monkeypatch.setattr(ts, "get_valid_token", _boom)
    monkeypatch.setattr(app_module, "_get_valid_token_for", _boom)
    monkeypatch.setattr(ts.httpx, "post", _boom)      # nor any HTTP of its own

    app_module._market_token_cache.update({"token": None, "until": 0.0})
    app_module._market_bucket_token()                  # must not raise


def test_market_token_provider_ignores_expired_tokens(app_module):
    """An expired access token is worse than none: ESI would 401 the market call."""
    import time as _t

    conn = app_module.get_conn()
    saved = conn.execute("SELECT character_id, access_token, token_expires_at"
                         " FROM characters").fetchall()
    try:
        conn.execute("UPDATE characters SET access_token='stale', token_expires_at=?",
                     (_t.time() - 60,))
        conn.commit(); conn.close()
        app_module._market_token_cache.update({"token": None, "until": 0.0})
        assert app_module._market_bucket_token() is None

        conn = app_module.get_conn()
        conn.execute("UPDATE characters SET access_token='fresh', token_expires_at=?",
                     (_t.time() + 3600,))
        conn.commit(); conn.close()
        app_module._market_token_cache.update({"token": None, "until": 0.0})
        assert app_module._market_bucket_token() == "fresh"
    finally:
        conn = app_module.get_conn()
        for cid, tok, exp in saved:
            conn.execute("UPDATE characters SET access_token=?, token_expires_at=?"
                         " WHERE character_id=?", (tok, exp, cid))
        conn.commit(); conn.close()
        app_module._market_token_cache.update({"token": None, "until": 0.0})


def test_market_token_provider_is_cached(app_module):
    """A burst of market pages must cost one query, not one per request."""
    calls = []
    real_get_conn = app_module.get_conn

    def counting():
        calls.append(1)
        return real_get_conn()

    app_module._market_token_cache.update({"token": None, "until": 0.0})
    app_module.get_conn = counting
    try:
        for _ in range(50):
            app_module._market_bucket_token()
    finally:
        app_module.get_conn = real_get_conn
        app_module._market_token_cache.update({"token": None, "until": 0.0})
    assert len(calls) == 1, calls
