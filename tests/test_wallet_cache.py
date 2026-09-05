"""The wallet must survive ESI being unavailable.

Reported during an EVE downtime: the Wallet page said "No wallet data". Every
row on it came straight from ESI and nothing was ever kept, so a server that
does not answer emptied the page - even though the same numbers had been on
screen minutes earlier.

It is also the tightest rate-limit group the app touches. Balance, journal (one
call per page, up to the 2500-row cap) and transactions are all `char-wallet`:
150 tokens per 15 minutes per character, and a 2xx costs 2.
"""
from __future__ import annotations

import pytest

from app.character import wallet as wallet_api
from app.web.page_cache import drop_cached


CHAR = 900000001

_JOURNAL = [{"id": 1, "date": "2026-08-30T10:00:00Z", "ref_type": "bounty_prizes",
             "amount": 1000000.0, "balance": 5000000.0, "description": "Bounty"}]
_TXNS = [{"transaction_id": 7, "date": "2026-08-30T09:00:00Z", "type_id": 34,
          "quantity": 100, "unit_price": 5.0, "is_buy": False, "client_id": 0,
          "location_id": 60003760}]


class _Esi:
    """Stand-in wallet endpoints that can be switched to "down"."""

    def __init__(self):
        self.up = True
        self.balance_calls = 0
        self.journal_calls = 0
        self.txn_calls = 0

    def install(self, monkeypatch):
        async def balance(client, char_id, token):
            self.balance_calls += 1
            return 5000000.0 if self.up else None

        async def journal(client, char_id, token, limit=2500):
            self.journal_calls += 1
            return list(_JOURNAL) if self.up else []

        async def txns(client, char_id, token):
            self.txn_calls += 1
            return list(_TXNS) if self.up else []

        monkeypatch.setattr(wallet_api, "fetch_balance", balance)
        monkeypatch.setattr(wallet_api, "fetch_journal", journal)
        monkeypatch.setattr(wallet_api, "fetch_transactions", txns)
        return self

    @property
    def calls(self):
        return self.balance_calls + self.journal_calls + self.txn_calls


@pytest.fixture
def esi(monkeypatch):
    return _Esi().install(monkeypatch)


@pytest.fixture
def clean(app_module):
    def _clear():
        conn = app_module.get_conn()
        try:
            drop_cached(conn, "wallet")
        finally:
            conn.close()
    _clear()
    yield
    _clear()


def _get(client, **kw):
    q = "&".join(f"{k}={v}" for k, v in kw.items())
    return client.get(f"/wallet?char={CHAR}&scope=personal" + (f"&{q}" if q else ""))


def test_a_wallet_that_esi_cannot_serve_falls_back_to_the_stored_copy(client, esi,
                                                                     clean, app_module):
    """The reported case: mid-downtime the page went blank instead of showing
    what it already had."""
    first = _get(client)
    assert first.status_code == 200
    assert "Bounty" in first.text
    assert "No wallet data" not in first.text

    # Age the copy out of the TTL so the page has to try ESI, then take ESI away.
    conn = app_module.get_conn()
    try:
        import time
        conn.execute("UPDATE page_cache SET cached_at=? WHERE kind='wallet'",
                     (time.time() - app_module._WALLET_PAGE_TTL - 60,))
        conn.commit()
    finally:
        conn.close()
    esi.up = False

    down = _get(client)
    assert down.status_code == 200
    assert "Bounty" in down.text, "the stored rows must still be rendered"
    assert "No wallet data" not in down.text
    assert "ESI did not answer" in down.text, "and the page must say they are stored"


def test_a_second_visit_inside_the_ttl_asks_esi_for_nothing(client, esi, clean):
    _get(client)
    after_first = esi.calls
    # Three endpoints per visit - balance, journal, transactions - and the
    # journal is one HTTP call per page on top of that.
    assert after_first == 3, after_first

    second = _get(client)
    assert esi.calls == after_first, "a repeat visit must cost no ESI call"
    assert "Bounty" in second.text


def test_refresh_bypasses_the_stored_copy(client, esi, clean):
    _get(client)
    before = esi.calls
    _get(client, refresh=1)
    assert esi.calls > before, "Refresh must reach ESI"


def test_an_expired_token_still_shows_what_was_stored(client, esi, clean,
                                                      app_module, monkeypatch):
    """Without a token the page used to say only "sign in again" - true, and
    useless when the numbers were sitting in the database."""
    _get(client)
    conn = app_module.get_conn()
    try:
        import time
        conn.execute("UPDATE page_cache SET cached_at=? WHERE kind='wallet'",
                     (time.time() - app_module._WALLET_PAGE_TTL - 60,))
        conn.commit()
    finally:
        conn.close()

    async def no_token(cid):
        return None

    monkeypatch.setattr(app_module, "_valid_token_async", no_token)
    r = _get(client)
    assert "Bounty" in r.text
    assert "ESI could not be reached" in r.text


def test_an_empty_wallet_is_not_mistaken_for_a_failure(client, clean, monkeypatch):
    """A brand-new character has no journal and no transactions, and that is an
    answer: it must be stored and rendered as an empty wallet, not read as ESI
    refusing. This project has taken an empty answer for "ask again" before.

    ESI failing is a different shape, and that is the one the page keys on: no
    balance at all. "No wallet data" - what the downtime screenshot showed - is
    exactly the balance being None.
    """
    async def balance(c, cid, tok):
        return 0.0

    async def empty(*a, **kw):
        return []

    monkeypatch.setattr(wallet_api, "fetch_balance", balance)
    monkeypatch.setattr(wallet_api, "fetch_journal", empty)
    monkeypatch.setattr(wallet_api, "fetch_transactions", empty)

    r = _get(client)
    assert r.status_code == 200
    assert "No wallet data" not in r.text, "a zero balance is still a balance"
    assert "No journal entries" in r.text or "No transactions" in r.text
    assert "ESI did not answer" not in r.text

    # ...and it was stored, so the next visit is free rather than re-asking.
    calls = []
    async def counted(c, cid, tok):
        calls.append(1)
        return 0.0
    monkeypatch.setattr(wallet_api, "fetch_balance", counted)
    _get(client)
    assert calls == [], "the empty answer must be cached like any other"
