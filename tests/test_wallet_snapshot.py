"""Wallet: the Transactions tab is an hourly snapshot, the Journal is not.

Reported with two screenshots: right after a refresh the Journal listed three
market transactions (14:29, 14:33, 14:54) that the Transactions tab did not
have, its newest being 13:02. Both lists came from the same request and both
were exactly what ESI returned - the transactions endpoint answers from a
snapshot, and that one had been taken at 14:29:54. The page said nothing about
it, so it read as data the app had lost.
"""
from __future__ import annotations

TAKEN = "Wed, 14 Oct 2026 14:29:54 GMT"
NEXT = "Wed, 14 Oct 2026 15:29:54 GMT"
META = {"last_modified": TAKEN, "expires": NEXT}


def _txn(tid, date):
    return {"transaction_id": tid, "date": date, "type_id": 34,
            "quantity": 1, "unit_price": 1.0, "client_id": 90000001, "is_buy": False}


def _market_journal(tid, date):
    return {"ref_type": "market_transaction", "context_id": tid, "date": date,
            "amount": 1.0, "balance": 1.0}


def test_the_note_counts_the_sales_the_snapshot_has_not_caught_up_with(app_module):
    txns = [_txn(1, "2026-10-14T13:02:31Z")]
    journal = [_market_journal(3, "2026-10-14T14:54:42Z"),
               _market_journal(2, "2026-10-14T14:33:22Z"),
               _market_journal(1, "2026-10-14T13:02:31Z")]
    note = app_module._txn_snapshot_note(journal, txns, META)
    assert "2 market transactions" in note
    assert "2026-10-14 14:29 UTC" in note       # when this snapshot was taken
    assert "2026-10-14 15:29 UTC" in note       # when the next one is due


def test_nothing_is_said_when_the_snapshot_is_complete(app_module):
    txns = [_txn(1, "2026-10-14T13:02:31Z")]
    journal = [_market_journal(1, "2026-10-14T13:02:31Z")]
    assert app_module._txn_snapshot_note(journal, txns, META) == ""


def test_a_journal_reaching_further_back_does_not_inflate_the_count(app_module):
    """ESI keeps ~2500 of each, and the journal counts fees and taxes too, so it
    can reach back past the oldest transaction. Those are not pending."""
    txns = [_txn(5, "2026-10-10T10:00:00Z")]
    journal = [_market_journal(5, "2026-10-10T10:00:00Z"),
               _market_journal(4, "2026-09-20T10:00:00Z"),   # older than the list
               _market_journal(3, "2026-09-19T10:00:00Z")]
    assert app_module._txn_snapshot_note(journal, txns, META) == ""


def test_a_fetch_that_failed_is_not_reported_as_a_snapshot_lag(app_module):
    """No meta means no answer from ESI. An empty list is then a failure, not a
    wallet that sold nothing, and this note must not guess which."""
    journal = [_market_journal(1, "2026-10-14T14:54:42Z")]
    assert app_module._txn_snapshot_note(journal, [], {}) == ""
    assert app_module._txn_snapshot_note(journal, [], {"expires": NEXT}) == ""


def test_one_pending_sale_reads_as_one(app_module):
    txns = [_txn(1, "2026-10-14T13:02:31Z")]
    journal = [_market_journal(2, "2026-10-14T14:54:42Z")]
    note = app_module._txn_snapshot_note(journal, txns, META)
    assert "1 market transaction already in the Journal is not in it yet" in note


def test_an_unreadable_date_header_does_not_cost_the_note(app_module):
    txns = [_txn(1, "2026-10-14T13:02:31Z")]
    journal = [_market_journal(2, "2026-10-14T14:54:42Z")]
    note = app_module._txn_snapshot_note(journal, txns, {"last_modified": "nonsense"})
    assert "1 market transaction" in note
    assert "taken at" not in note


def test_the_transactions_tab_carries_the_note(client, app_module):
    """End to end, the way it was reported: a refreshed page."""
    journal = [_market_journal(2, "2026-10-14T14:54:42Z"),
               _market_journal(1, "2026-10-14T13:02:31Z")]
    txns = [_txn(1, "2026-10-14T13:02:31Z")]

    async def _bal(*a, **k): return 1_000_000.0
    async def _jr(*a, **k): return journal
    async def _tx(*a, **k): return txns, META
    async def _names(conn, j, t, tok): return {}

    api = app_module.wallet_api
    orig = (api.fetch_balance, api.fetch_journal, api.fetch_transactions,
            app_module._wallet_names)
    api.fetch_balance, api.fetch_journal, api.fetch_transactions = _bal, _jr, _tx
    app_module._wallet_names = _names
    try:
        html = client.get("/wallet?char=900000001&refresh=1").text
    finally:
        (api.fetch_balance, api.fetch_journal, api.fetch_transactions,
         app_module._wallet_names) = orig

    assert "1 market transaction already in the Journal is not in it yet" in html
    assert "2026-10-14 14:29 UTC" in html


def test_a_sale_sharing_the_newest_timestamp_is_still_counted(app_module):
    """Several sales a second apart is routine when an order is eaten in pieces,
    and the snapshot can hold one of them and not the other. Going by date alone
    would miss the one left out, or count the one already there twice."""
    txns = [_txn(1, "2026-10-14T13:02:31Z")]
    journal = [_market_journal(2, "2026-10-14T13:02:31Z"),   # same second, not in it
               _market_journal(1, "2026-10-14T13:02:31Z")]   # the one it has
    note = app_module._txn_snapshot_note(journal, txns, META)
    assert "1 market transaction already in the Journal is not in it yet" in note


# ── not asking for a snapshot ESI has not rebuilt yet ─────────────────────────
# The same hour that puts the Transactions tab behind the Journal also makes
# every repeat call pointless: ESI answers with the rows already in hand and
# charges two tokens of char-wallet, 150 per 15 minutes per character.

import datetime as _dt
import time as _time
from email.utils import format_datetime

import pytest

from app.character import wallet as wallet_api
from app.web.page_cache import drop_cached

CHAR = 900000001


def _http_date(seconds_from_now: float) -> str:
    return format_datetime(_dt.datetime.now(_dt.timezone.utc)
                           + _dt.timedelta(seconds=seconds_from_now), usegmt=True)


class _Wallet:
    """Stand-in wallet endpoints that count what was asked for."""

    def __init__(self, monkeypatch, expires_in=3600.0, journal=None, txns=None):
        self.journal = journal if journal is not None else []
        self.txns = txns if txns is not None else []
        self.expires_in = expires_in
        self.balance_calls = self.journal_calls = self.txn_calls = 0

        async def balance(client, char_id, token):
            self.balance_calls += 1
            return 5_000_000.0

        async def journal_fn(client, char_id, token, limit=2500):
            self.journal_calls += 1
            return list(self.journal)

        async def txns_fn(client, char_id, token):
            self.txn_calls += 1
            return list(self.txns), {"last_modified": _http_date(-60),
                                     "expires": _http_date(self.expires_in)}

        async def names(conn, j, t, tok):
            return {}

        monkeypatch.setattr(wallet_api, "fetch_balance", balance)
        monkeypatch.setattr(wallet_api, "fetch_journal", journal_fn)
        monkeypatch.setattr(wallet_api, "fetch_transactions", txns_fn)
        import app.web.main as m
        monkeypatch.setattr(m, "_wallet_names", names)


@pytest.fixture
def clean_wallet(app_module):
    def _clear():
        conn = app_module.get_conn()
        try:
            drop_cached(conn, "wallet")
        finally:
            conn.close()
    _clear()
    yield
    _clear()


def _refresh(client):
    return client.get(f"/wallet?char={CHAR}&scope=personal&refresh=1")


def test_a_snapshot_still_in_date_is_not_asked_for_again(client, clean_wallet, monkeypatch):
    w = _Wallet(monkeypatch, expires_in=3600,
                txns=[_txn(1, "2026-10-14T13:02:31Z")])
    first = _refresh(client)
    assert w.txn_calls == 1

    second = _refresh(client)
    assert w.txn_calls == 1, "ESI cannot build a new snapshot yet - do not ask"
    assert w.balance_calls == 2, "the balance moves while you play, keep asking"
    assert w.journal_calls == 2, "the journal has its own clock, keep asking"
    # And the rows are still on the page, not dropped with the call.
    assert first.text.count("tbl-txns") == second.text.count("tbl-txns")
    assert "Tritanium" in second.text


def test_a_snapshot_past_its_date_is_fetched_again(client, clean_wallet, monkeypatch):
    w = _Wallet(monkeypatch, expires_in=-1,
                txns=[_txn(1, "2026-10-14T13:02:31Z")])
    _refresh(client)
    _refresh(client)
    assert w.txn_calls == 2


def test_the_pending_count_follows_the_journal_while_the_snapshot_waits(
        client, clean_wallet, monkeypatch):
    """The snapshot is reused, but the journal is not - so the count of what it
    is missing has to be worked out again on every visit."""
    w = _Wallet(monkeypatch, expires_in=3600,
                txns=[_txn(1, "2026-10-14T13:02:31Z")],
                journal=[_market_journal(1, "2026-10-14T13:02:31Z")])
    first = _refresh(client)
    assert "not in it yet" not in first.text

    w.journal = [_market_journal(2, "2026-10-14T14:54:42Z"),
                 _market_journal(1, "2026-10-14T13:02:31Z")]
    second = _refresh(client)
    assert w.txn_calls == 1, "still the same snapshot"
    assert "1 market transaction already in the Journal is not in it yet" in second.text


def test_a_refresh_that_esi_refuses_still_shows_the_stored_copy(
        client, clean_wallet, monkeypatch, app_module):
    """Refresh used to throw the stored copy away before asking. When the ask
    then failed, the page had nothing left to show."""
    w = _Wallet(monkeypatch, expires_in=3600,
                txns=[_txn(1, "2026-10-14T13:02:31Z")])
    assert "Tritanium" in _refresh(client).text

    async def no_balance(client_, char_id, token):
        return None

    monkeypatch.setattr(wallet_api, "fetch_balance", no_balance)
    down = _refresh(client)
    assert "Tritanium" in down.text, "the stored rows must survive a failed Refresh"
    assert "ESI did not answer" in down.text
