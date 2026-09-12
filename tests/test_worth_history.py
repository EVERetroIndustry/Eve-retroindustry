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


# ── estimating the past ──────────────────────────────────────────────────────
#
# The wallet half of net worth can be walked back exactly; the asset half has to
# be rebuilt from what ESI records about items moving. Three sources, measured on
# a real account before any of this was written: transactions reach ~30 days
# (461 rows), industry jobs ~90 (457), the mining ledger ~90 (184). Everything
# else that moves an item - loot, PI, contracts, reprocessing, ships lost - is
# not recorded anywhere, which is why the result carries a band.

from app.character import history_store as H


def _hdb():
    conn = _db()
    H.ensure_history_tables(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS char_assets_cache"
                 " (character_id INTEGER, data_json TEXT, cached_at REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS sde_groups (group_id INTEGER, name TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS sde_types (type_id INTEGER, group_id INTEGER)")
    conn.execute("CREATE TABLE IF NOT EXISTS market_price_cache"
                 " (type_id INTEGER PRIMARY KEY, sell_price REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS sde_blueprint_products"
                 " (blueprint_type_id INTEGER, activity INTEGER, product_type_id INTEGER,"
                 "  quantity INTEGER, probability REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS sde_blueprint_materials"
                 " (blueprint_type_id INTEGER, activity INTEGER, material_type_id INTEGER,"
                 "  quantity INTEGER)")
    conn.commit()
    return conn


def _assets(conn, cid, items):
    import json
    conn.execute("INSERT INTO char_assets_cache VALUES (?,?,?)",
                 (cid, json.dumps([{"type_id": t, "quantity": q} for t, q in items]),
                  time.time()))
    conn.commit()


def test_a_sale_puts_the_goods_back_into_the_past_basket():
    """Sold last week means it was in the pile last week. Without this the
    estimate reads a sale as wealth appearing out of nowhere."""
    conn = _hdb()
    _assets(conn, 1, [(34, 100)])
    now = time.time()
    conn.execute("INSERT INTO wallet_transactions VALUES (1, 5, ?, 34, 40, 5.0, 0)",
                 (now - 86400,))
    conn.commit()
    events, used = worth.basket_events(conn, now - 7 * 86400)
    assert used["transactions"] == 1
    assert events == [(now - 86400, 34, -40)], "a sale is a negative arrival"


def test_a_purchase_was_not_there_before_and_mining_did_not_exist():
    conn = _hdb()
    now = time.time()
    conn.execute("INSERT INTO wallet_transactions VALUES (1, 6, ?, 34, 25, 5.0, 1)",
                 (now - 3600,))
    day = time.strftime("%Y-%m-%d", time.gmtime(now - 86400))
    conn.execute("INSERT INTO mining_ledger VALUES (1, ?, 30000142, 1230, 9000)", (day,))
    conn.commit()
    events, used = worth.basket_events(conn, now - 7 * 86400)
    kinds = {tid: q for _ts, tid, q in events}
    assert kinds[34] == 25, "bought goods entered the pile"
    assert kinds[1230] == 9000, "mined ore entered the pile"
    assert used["mining"] == 1


def test_a_delivered_job_becomes_its_materials_again():
    """The manufacturer's case. Going back past a delivered job, the product has
    to turn back into what it was built from, or the estimate reads the whole
    build as value from nowhere."""
    conn = _hdb()
    now = time.time()
    conn.execute("INSERT INTO sde_blueprint_products VALUES (1000, 1, 500, 2, 1.0)")
    conn.executemany("INSERT INTO sde_blueprint_materials VALUES (1000, 1, ?, ?)",
                     [(34, 100), (35, 50)])
    conn.execute("INSERT INTO industry_jobs_done VALUES"
                 " (1, 77, 1, 900, 1000, 500, 3, 3, ?, 'delivered')", (now - 7200,))
    conn.commit()
    events, used = worth.basket_events(conn, now - 7 * 86400)
    by = {tid: q for _ts, tid, q in events}
    assert used["jobs"] == 1
    assert by[500] == 6, "3 runs of a blueprint that makes 2"
    assert by[34] == -300 and by[35] == -150, "materials go back into the past pile"


def test_an_undelivered_job_is_not_unwound():
    """A job still running has consumed its materials but produced nothing, and
    it is not what the delivered record describes."""
    conn = _hdb()
    now = time.time()
    conn.execute("INSERT INTO sde_blueprint_products VALUES (1000, 1, 500, 2, 1.0)")
    conn.execute("INSERT INTO industry_jobs_done VALUES"
                 " (1, 78, 1, 900, 1000, 500, 3, 0, ?, 'active')", (now - 7200,))
    conn.commit()
    events, used = worth.basket_events(conn, now - 7 * 86400)
    assert used["jobs"] == 0 and events == []


def test_material_efficiency_follows_the_eve_formula():
    """One job, rounded once - the same shape as the planner, and the reason a
    10% ME blueprint does not simply need 10% less."""
    assert worth._job_material_qty(100, 1, 0) == 100
    assert worth._job_material_qty(100, 10, 10) == 900
    assert worth._job_material_qty(1, 5, 10) == 5, "never below one per run"


def test_blueprints_are_left_out_of_the_basket():
    """A BPO and its copy share a type_id, so pricing them together is how they
    were once valued wrongly. Net worth excludes them and so does this."""
    conn = _hdb()
    conn.execute("INSERT INTO sde_groups VALUES (2, 'Ship Blueprint')")
    conn.execute("INSERT INTO sde_types VALUES (999, 2)")
    conn.execute("INSERT INTO sde_types VALUES (34, 18)")
    conn.commit()
    _assets(conn, 1, [(34, 100), (999, 1)])
    basket = worth.current_basket(conn)
    assert basket == {34: 100}


def test_the_estimate_meets_the_recorded_line_and_stops_at_the_journal():
    """Two properties that make the dashed line honest: it joins the solid one
    instead of stepping away from it, and it does not reach back past the wallet
    journal, because net worth without a wallet is half a number."""
    conn = _hdb()
    _char(conn, 1, "A", 1000.0)
    _assets(conn, 1, [(34, 100)])
    conn.execute("INSERT INTO sde_types VALUES (34, 18)")
    conn.execute("INSERT INTO market_price_cache VALUES (34, 10.0)")
    now = time.time()
    # a journal reaching 10 days back, and one recorded sample now
    for i in range(1, 11):
        _entry(conn, 1, i, now - i * 86400, 10.0)
    worth.record_worth(conn, [(1, 1000.0, 1000.0)])
    conn.commit()

    # Daily prices deliberately disagree with today's: average trade price is not
    # the sell price the dashboard uses (measured on a real basket: 121.68B
    # against 124.95B, a 2.7% basis gap). The estimate has to be scaled onto the
    # recorded figure, not shown 2.7% below it.
    day = lambda k: time.strftime("%Y-%m-%d", time.gmtime(now - k * 86400))
    histories = {34: {day(k): 8.0 for k in range(0, 40)}}
    d = worth.estimate_series(conn, 90, histories)
    assert d["points"], d.get("reason")
    oldest = d["points"][0]["t"]
    assert oldest >= now - 11 * 86400, "must not reach past the journal"
    assert d["points"][-1]["assets_lo"] == pytest.approx(1000.0, rel=0.02), \
        "the newest estimated point must meet the recorded sample, not the raw index"


def test_the_band_opens_only_where_the_trade_record_stops():
    """Inside the transactions window every trade is known in kind, so there is
    nothing to be uncertain about and the band must be flat. Before it, only the
    ISK totals survive - and that is exactly where the chart should widen."""
    conn = _hdb()
    _char(conn, 1, "A", 1000.0)
    _assets(conn, 1, [(34, 100)])
    conn.execute("INSERT INTO sde_types VALUES (34, 18)")
    conn.execute("INSERT INTO market_price_cache VALUES (34, 10.0)")
    now = time.time()
    for i in range(1, 21):
        _entry(conn, 1, i, now - i * 86400, 10.0)
    # a sale eight days ago that only the journal remembers
    conn.execute("INSERT INTO wallet_journal (character_id, journal_id, date_ts, ref_type,"
                 " amount, description) VALUES (1, 500, ?, 'market_transaction', 5000, '')",
                 (now - 8 * 86400,))
    # transactions only reach five days back
    conn.execute("INSERT INTO wallet_transactions VALUES (1, 9, ?, 34, 1, 10.0, 0)",
                 (now - 5 * 86400,))
    worth.record_worth(conn, [(1, 1000.0, 1000.0)])
    conn.commit()

    # A sale INSIDE the transactions window too: it is known in kind there, so it
    # must not widen anything - that is what separates the two halves.
    conn.execute("INSERT INTO wallet_journal (character_id, journal_id, date_ts, ref_type,"
                 " amount, description) VALUES (1, 501, ?, 'market_transaction', 4000, '')",
                 (now - 2 * 86400,))
    conn.commit()

    d = worth.estimate_series(conn, 90, {34: {}})
    inside = [p for p in d["points"] if p["t"] >= now - 5 * 86400]
    outside = [p for p in d["points"] if p["t"] < now - 8 * 86400]
    assert inside and all(p["assets_hi"] == p["assets_lo"] for p in inside), \
        "no band where every trade is known in kind"
    assert outside and any(p["assets_hi"] > p["assets_lo"] for p in outside), \
        "a band where only the ISK total is left"


def test_the_store_is_idempotent_and_a_growing_day_is_updated():
    conn = _hdb()
    H.store_transactions(conn, 1, [{"transaction_id": 1, "date": "2026-09-01T10:00:00Z",
                                    "type_id": 34, "quantity": 5, "unit_price": 1.0,
                                    "is_buy": False}])
    H.store_transactions(conn, 1, [{"transaction_id": 1, "date": "2026-09-01T10:00:00Z",
                                    "type_id": 34, "quantity": 5, "unit_price": 1.0,
                                    "is_buy": False}])
    assert conn.execute("SELECT COUNT(*) FROM wallet_transactions").fetchone()[0] == 1

    H.store_mining(conn, 1, [{"date": "2026-09-01", "solar_system_id": 1, "type_id": 9,
                              "quantity": 100}])
    H.store_mining(conn, 1, [{"date": "2026-09-01", "solar_system_id": 1, "type_id": 9,
                              "quantity": 250}])
    rows = conn.execute("SELECT quantity FROM mining_ledger").fetchall()
    assert rows == [(250,)], "a day still being mined is replaced, not doubled"


def test_the_endpoint_reports_the_estimate(client):
    d = client.get("/api/worth/history?days=30").json()
    assert "estimate" in d and "status" in d["estimate"]
    assert d["estimate"]["status"] in ("ready", "building", "unavailable")
    assert "reach" in d
