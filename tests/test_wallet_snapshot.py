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
