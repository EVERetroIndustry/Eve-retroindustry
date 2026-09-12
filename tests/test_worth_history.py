"""Net worth and ISK over time.

Two series that look alike and are made completely differently, which is the
whole reason the window has two tabs:

  ISK   is RECONSTRUCTED backwards out of the stored wallet journal. Between two
        entries nothing moves, so the balance before an entry is the balance
        after the previous one, walked back from the balance we hold now.
  Worth is SAMPLED hourly from here on, because nothing has ever recorded what
        was owned in the past - `char_assets_cache` keeps the current snapshot
        and is overwritten on every sync.

Verified against ESI on a real account before this was written: 725 journal
entries, every one of them carrying a balance, and the walk reproduced all 725
exactly - but only after journal_id was added as the tie-breaker. See the
regression test below.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from app.character import worth
from app.character.income import ensure_income_tables


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE characters (character_id INTEGER PRIMARY KEY,"
                 " character_name TEXT)")
    conn.execute("CREATE TABLE char_wallet_cache (character_id INTEGER PRIMARY KEY,"
                 " balance REAL, cached_at REAL)")
    ensure_income_tables(conn)
    worth.ensure_worth_tables(conn)
    return conn


def _char(conn, cid, name, balance):
    conn.execute("INSERT INTO characters VALUES (?,?)", (cid, name))
    conn.execute("INSERT INTO char_wallet_cache VALUES (?,?,?)",
                 (cid, balance, time.time()))
    conn.commit()


def _entry(conn, cid, jid, ts, amount, balance=None):
    conn.execute("INSERT INTO wallet_journal (character_id, journal_id, date_ts,"
                 " ref_type, amount, description, balance) VALUES (?,?,?,?,?,?,?)",
                 (cid, jid, ts, "bounty_prizes", amount, "", balance))
    conn.commit()


# ── the walk back ────────────────────────────────────────────────────────────

def test_the_balance_is_walked_back_through_the_amounts():
    conn = _db()
    _char(conn, 1, "A", 1000.0)
    now = time.time()
    _entry(conn, 1, 3, now - 100, 300.0)     # +300 -> 1000
    _entry(conn, 1, 2, now - 200, 200.0)     # +200 ->  700
    _entry(conn, 1, 1, now - 300, 500.0)     # +500 ->  500
    pts, before = worth._character_balance_points(conn, 1, 1000.0, now - 3600)
    got = {int(t): v for t, v in pts}
    assert got[int(now - 100)] == 1000.0
    assert got[int(now - 200)] == 700.0
    assert got[int(now - 300)] == 500.0
    assert before == 0.0, "the balance before the oldest entry we hold"


def test_entries_sharing_a_second_are_ordered_by_journal_id():
    """The bug this found on real data. 275 of one character's timestamps carried
    more than one entry - a market fill writes several in the same second - and
    ordering by time alone put the walk in the wrong order: checked against ESI's
    own balance field, 458 of 725 entries came out wrong. With journal_id as the
    tie-breaker, 0.
    """
    conn = _db()
    _char(conn, 1, "A", 1000.0)
    now = time.time()
    t = now - 100
    # Same second, applied oldest first: 400 (+100) -> 500 (+500) -> 1000
    _entry(conn, 1, 10, t, 100.0)
    _entry(conn, 1, 11, t, 500.0)
    pts, before = worth._character_balance_points(conn, 1, 1000.0, now - 3600)
    # Newest first, so the newer of the two tied entries is reported first.
    assert [v for _t, v in pts] == [1000.0, 1000.0, 500.0]
    assert before == 400.0, "500 - 100, i.e. before the older of the two"


def test_a_stored_esi_balance_wins_over_the_walk():
    """ESI sends the balance after each entry. Where we have it, a gap earlier in
    the journal cannot go on skewing everything older than it."""
    conn = _db()
    _char(conn, 1, "A", 1000.0)
    now = time.time()
    _entry(conn, 1, 2, now - 100, 300.0)
    _entry(conn, 1, 1, now - 200, 50.0, balance=9999.0)   # ESI says otherwise
    pts, _before = worth._character_balance_points(conn, 1, 1000.0, now - 3600)
    got = {int(t): v for t, v in pts}
    assert got[int(now - 200)] == 9999.0, "the anchor, not 700 from the walk"


def test_a_character_with_no_journal_is_held_flat_and_named():
    conn = _db()
    _char(conn, 1, "Has journal", 1000.0)
    _char(conn, 2, "No journal", 250.0)
    _entry(conn, 1, 1, time.time() - 3600, 100.0)
    d = worth.isk_series(conn, 7)
    assert d["chars_without_journal"] == ["No journal"]
    assert d["points"], "a series is still produced"
    assert d["points"][-1]["isk"] == pytest.approx(1250.0), "both balances counted"


def test_the_series_ends_on_the_balance_we_actually_hold():
    """Measured on a real account: 2694 entries over 46 days, and the newest point
    landed on the stored balance to the ISK."""
    conn = _db()
    _char(conn, 1, "A", 67_523_010_751.0)
    now = time.time()
    for i in range(1, 40):
        _entry(conn, 1, i, now - i * 3600, 1_000_000.0)
    d = worth.isk_series(conn, 30)
    assert d["points"][-1]["isk"] == pytest.approx(67_523_010_751.0)
    assert d["entries"] == 39


# ── the hourly sample ────────────────────────────────────────────────────────

def test_samples_are_bucketed_to_the_hour_and_the_later_one_wins():
    conn = _db()
    _char(conn, 1, "A", 100.0)
    worth.record_worth(conn, [(1, 100.0, 900.0)])
    worth.record_worth(conn, [(1, 150.0, 950.0)])
    rows = conn.execute("SELECT ts, wallet, asset_value FROM net_worth_history").fetchall()
    assert len(rows) == 1, "one row an hour, not one a visit"
    assert rows[0][1] == 150.0 and rows[0][2] == 950.0


def test_worth_sums_characters_and_carries_an_unseen_one_forward():
    """Every character is sampled together, so a gap means "not seen this hour",
    not "worth nothing"."""
    conn = _db()
    _char(conn, 1, "A", 0.0)
    _char(conn, 2, "B", 0.0)
    h = (time.time() // 3600) * 3600
    conn.executemany("INSERT INTO net_worth_history VALUES (?,?,?,?)", [
        (h - 7200, 1, 100.0, 1000.0), (h - 7200, 2, 50.0, 500.0),
        (h - 3600, 1, 120.0, 1100.0),                      # B missing this hour
    ])
    conn.commit()
    pts = worth.worth_series(conn, 7)["points"]
    assert len(pts) == 2
    assert pts[0]["net"] == pytest.approx(1650.0)
    assert pts[1]["net"] == pytest.approx(1220.0 + 550.0), "B carried forward"


def test_a_stale_character_stops_being_carried_forward():
    """A reading from three weeks ago is not evidence about today."""
    conn = _db()
    _char(conn, 1, "A", 0.0)
    _char(conn, 2, "B", 0.0)
    h = (time.time() // 3600) * 3600
    conn.executemany("INSERT INTO net_worth_history VALUES (?,?,?,?)", [
        (h - worth._CARRY_MAX - 7200, 2, 50.0, 500.0),     # long gone
        (h - 3600, 1, 120.0, 1100.0),
    ])
    conn.commit()
    pts = worth.worth_series(conn, 60)["points"]
    assert pts[-1]["net"] == pytest.approx(1220.0)
    assert pts[-1]["chars"] == 1


def test_nothing_recorded_yet_is_an_empty_series_not_a_guess():
    conn = _db()
    _char(conn, 1, "A", 100.0)
    d = worth.worth_series(conn, 30)
    assert d["points"] == []
    assert d["recording_since"] is None


# ── the endpoint ─────────────────────────────────────────────────────────────

def test_the_endpoint_serves_both_tabs(client, app_module):
    d = client.get("/api/worth/history?days=30").json()
    assert d["logged_in"] is True
    assert "points" in d["worth"] and "points" in d["isk"]
    assert "recording_since" in d["worth"]
    assert "oldest_entry" in d["isk"]


def test_opening_the_dashboard_records_a_sample(client, app_module):
    """The only way net worth ever gets a history is being written down while it
    is on screen, so the dashboard's live build is where it happens."""
    conn = app_module.get_conn()
    try:
        worth.ensure_worth_tables(conn)
        conn.execute("DELETE FROM net_worth_history")
        conn.commit()
    finally:
        conn.close()

    client.get("/api/dashboard/live?force=1")

    conn = app_module.get_conn()
    try:
        n = conn.execute("SELECT COUNT(*) FROM net_worth_history").fetchone()[0]
    finally:
        conn.close()
    assert n > 0, "a dashboard load must leave a sample behind"
