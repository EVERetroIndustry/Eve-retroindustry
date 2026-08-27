"""Income across characters over an arbitrary rolling window.

Two things here are easy to get quietly wrong, and both were:

  - ISK moved between the account's own characters. One transfer is two journal
    entries sharing an id, minus for the sender and plus for the receiver, so
    counting incoming amounts reads as income when nothing entered the account.
    Measured on a real account: 3.08 billion of `player_donation`.
  - "no characters ticked" against "every character". With a plain string
    parameter the two are indistinguishable, and unticking everything showed the
    total for all of them.
"""
from __future__ import annotations

import sqlite3
import time

from app.character import income as I

DAY = 86400.0


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    I.ensure_income_tables(conn)
    return conn


def _entry(conn, char_id, journal_id, hours_ago, ref_type, amount):
    conn.execute(
        "INSERT OR IGNORE INTO wallet_journal"
        " (character_id, journal_id, date_ts, ref_type, amount, description)"
        " VALUES (?,?,?,?,?,?)",
        (char_id, journal_id, time.time() - hours_ago * 3600, ref_type, amount, ""))
    conn.commit()


def test_the_window_is_rolling_and_exact():
    """18 hours has to mean 18 hours - the reason for a rolling window is that
    the question is asked with a different number every time."""
    conn = _conn()
    _entry(conn, 1, 100, 2, "bounty_prizes", 1_000_000)
    _entry(conn, 1, 101, 17, "bounty_prizes", 2_000_000)
    _entry(conn, 1, 102, 19, "bounty_prizes", 4_000_000)

    assert I.income_summary(conn, 6)["total"] == 1_000_000
    assert I.income_summary(conn, 18)["total"] == 3_000_000
    assert I.income_summary(conn, 24)["total"] == 7_000_000


def test_only_incoming_amounts_count():
    conn = _conn()
    _entry(conn, 1, 200, 1, "bounty_prizes", 5_000_000)
    _entry(conn, 1, 201, 1, "market_transaction", -9_000_000)   # bought something
    s = I.income_summary(conn, 24)
    assert s["total"] == 5_000_000, "spending must not net off income"


def test_transfers_between_own_characters_are_excluded():
    """The reported shape: one id, two of our characters, plus on one side."""
    conn = _conn()
    _entry(conn, 1, 300, 1, "player_donation", -1_000_000_000)   # sender
    _entry(conn, 2, 300, 1, "player_donation", 1_000_000_000)    # receiver
    _entry(conn, 1, 301, 1, "bounty_prizes", 7_000_000)

    s = I.income_summary(conn, 24)
    assert s["total"] == 7_000_000
    assert s["internal_excluded"] == 1_000_000_000
    assert s["internal_entries"] == 1

    # ...and it is still counted when asked for explicitly
    raw = I.income_summary(conn, 24, exclude_internal=False)
    assert raw["total"] == 1_007_000_000


def test_a_donation_from_an_outsider_is_income():
    """Detected from the data, not from ref_type: only one side is ours, so the
    same ref_type has to count."""
    conn = _conn()
    _entry(conn, 1, 400, 1, "player_donation", 500_000_000)
    assert I.income_summary(conn, 24)["total"] == 500_000_000


def test_bounty_and_ess_are_reported_whatever_the_filter():
    conn = _conn()
    _entry(conn, 1, 500, 1, "bounty_prizes", 3_000_000)
    _entry(conn, 1, 501, 1, "ess_escrow_transfer", 4_000_000)
    _entry(conn, 1, 502, 1, "market_transaction", 90_000_000)

    s = I.income_summary(conn, 24, ref_types=["market_transaction"])
    assert s["total"] == 90_000_000
    assert s["bounty_ess"] == 7_000_000, "the tile's headline must not follow the filter"


def test_per_character_and_per_type_breakdowns():
    conn = _conn()
    _entry(conn, 1, 600, 1, "bounty_prizes", 3_000_000)
    _entry(conn, 2, 601, 1, "bounty_prizes", 5_000_000)
    _entry(conn, 2, 602, 1, "ess_escrow_transfer", 1_000_000)

    s = I.income_summary(conn, 24)
    assert [(r["character_id"], r["total"]) for r in s["by_character"]] == \
        [(2, 6_000_000), (1, 3_000_000)]
    assert [(r["ref_type"], r["total"]) for r in s["by_ref_type"]] == \
        [("bounty_prizes", 8_000_000), ("ess_escrow_transfer", 1_000_000)]


def test_an_empty_character_list_means_nothing_not_everything(client, app_module):
    """Through the route, because that is where the distinction lives: an absent
    parameter is "all", a present but empty one is "none"."""
    conn = app_module.get_conn()
    try:
        I.ensure_income_tables(conn)
        conn.execute("DELETE FROM wallet_journal")
        cid = app_module.list_characters(conn)[0][0]
        _entry(conn, cid, 700, 1, "bounty_prizes", 8_000_000)
    finally:
        conn.close()

    all_chars = client.get("/api/income?hours=24&refresh=0").json()
    assert all_chars["total"] == 8_000_000

    none = client.get("/api/income?hours=24&refresh=0&chars=").json()
    assert none["total"] == 0, "unticking every character must not show them all"
    # ...but the list you tick from still shows what each one earned
    assert any(r["total"] == 8_000_000 for r in none["by_character"])


def test_storing_the_same_journal_twice_adds_nothing():
    """journal_id is stable, so re-reading ESI's 30 days cannot double-count."""
    conn = _conn()
    entries = [{"id": 800, "date": "2026-08-27T09:21:31Z",
                "ref_type": "bounty_prizes", "amount": 1_500_000},
               {"id": 801, "date": "2026-08-27T09:25:00Z",
                "ref_type": "ess_escrow_transfer", "amount": 2_500_000}]
    assert I.store_journal(conn, 1, entries) == 2
    assert I.store_journal(conn, 1, entries) == 0
    assert conn.execute("SELECT COUNT(*) FROM wallet_journal").fetchone()[0] == 2


def test_a_stored_journal_is_not_refetched_inside_esis_cache_window():
    conn = _conn()
    assert I.journal_is_stale(conn, 1) is True
    I.store_journal(conn, 1, [{"id": 900, "date": "2026-08-27T09:00:00Z",
                               "ref_type": "bounty_prizes", "amount": 1}])
    assert I.journal_is_stale(conn, 1) is False
    conn.execute("UPDATE wallet_journal_meta SET fetched_at = ?",
                 (time.time() - I.JOURNAL_MAX_AGE - 60,))
    conn.commit()
    assert I.journal_is_stale(conn, 1) is True


def test_a_malformed_entry_is_skipped_not_stored_as_zero():
    conn = _conn()
    stored = I.store_journal(conn, 1, [
        {"id": None, "date": "2026-08-27T09:00:00Z", "ref_type": "x", "amount": 5},
        {"id": 1000, "date": "not a date", "ref_type": "x", "amount": 5},
        {"id": 1001, "date": "2026-08-27T09:00:00Z", "ref_type": "bounty_prizes",
         "amount": 42},
    ])
    assert stored == 1
    assert conn.execute("SELECT journal_id FROM wallet_journal").fetchall() == [(1001,)]
