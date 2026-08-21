"""Loading-screen progress reporting.

The screen used to cycle three canned messages off the poll counter and then sit
on "Almost done..." indefinitely, so a tester could not tell a slow sync from a
hung one. These cover the real signal that replaced it.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def sync_state(app_module):
    """Snapshot and restore the module-global sync state around each test."""
    original = dict(app_module._sync_state)
    yield app_module._sync_state
    app_module._sync_state.clear()
    app_module._sync_state.update(original)


def test_pct_rises_monotonically_through_the_whole_sync(app_module, sync_state):
    steps = app_module._SYNC_STEPS
    seen = []
    for index in range(1, 13):
        for step in steps:
            sync_state.update({"running": True, "done": False, "total": 12,
                               "index": index, "step": step, "phase": "characters"})
            seen.append(app_module._sync_pct())
    assert seen == sorted(seen), "progress must never go backwards"
    assert seen[0] < seen[-1]
    # The character loop stops short of 100 - the trailing steps own the rest.
    assert max(seen) <= 95
    # And it actually moves per step, not once per character.
    assert len(set(seen)) > 12


def test_pct_boundaries(app_module, sync_state):
    # Nothing known yet: a sliver, so the bar is visibly present but not lying.
    sync_state.update({"running": True, "done": False, "total": 0, "index": 0,
                       "step": "", "phase": ""})
    assert app_module._sync_pct() == 2
    # Station-name resolution is the tail, past every character.
    sync_state.update({"total": 12, "index": 12, "step": "", "phase": "locations"})
    assert app_module._sync_pct() == 96
    sync_state.update({"done": True})
    assert app_module._sync_pct() == 100


def test_pct_survives_an_unknown_step(app_module, sync_state):
    """A future step name must not raise ValueError from .index()."""
    sync_state.update({"running": True, "done": False, "total": 4, "index": 2,
                       "step": "something new", "phase": "characters"})
    pct = app_module._sync_pct()
    assert 2 <= pct <= 95


def test_status_endpoint_reports_what_the_server_is_waiting_on(client, app_module, sync_state):
    sync_state.update({"running": True, "done": False, "total": 12, "index": 3,
                       "char": "Retrovisor", "step": "assets", "phase": "characters",
                       "failed": 1, "started_at": app_module._time.time() - 42})
    d = client.get("/api/sync-status").json()
    assert d["running"] is True and d["done"] is False
    assert (d["index"], d["total"], d["char"], d["step"]) == (3, 12, "Retrovisor", "assets")
    assert d["failed"] == 1
    assert 40 <= d["elapsed"] <= 45          # a real clock, not a poll counter
    assert 2 < d["pct"] < 95


def test_status_endpoint_when_idle(client, app_module, sync_state):
    sync_state.update({"running": False, "done": True, "started_at": 0.0})
    d = client.get("/api/sync-status").json()
    assert d["done"] is True and d["pct"] == 100
    assert d["elapsed"] == 0                  # never started → no fake clock


def test_reset_clears_stale_progress(app_module, sync_state):
    """A second sync must not inherit the first one's counters."""
    sync_state.update({"running": False, "done": True, "total": 12, "index": 12,
                       "char": "Retrovisor", "step": "corp assets",
                       "phase": "locations", "failed": 3})
    app_module._sync_reset()
    assert sync_state["running"] is True and sync_state["done"] is False
    assert (sync_state["total"], sync_state["index"], sync_state["failed"]) == (0, 0, 0)
    assert sync_state["char"] == "" and sync_state["step"] == "" and sync_state["phase"] == ""
    assert sync_state["started_at"] > 0


def test_loading_page_has_a_real_bar_and_no_canned_messages(client):
    html = client.get("/auth/sync").text
    assert 'id="sync-bar"' in html            # a real bar, like the price phase has
    assert "sync-elapsed" in html
    # The canned rotation is gone. Matching the literal "Almost done" would hit the
    # comment explaining why it went, so assert on the mechanism instead: no message
    # list, no poll counter, and a status line fed by the server's own fields.
    assert "const msgs" not in html
    assert "syncAttempts" not in html
    assert "d.step" in html and "d.pct" in html


def test_dashboard_shows_the_ship_next_to_the_undocked_badge(client):
    """Reported: undocked shows the system, but not which hull is out there."""
    html = client.get("/").text
    assert "c.ship_label" in html
    # Escaped like every other injected value on that card.
    assert "esc(c.ship_label)" in html
    # Only while undocked, and left of the badge (the badge loses ms-auto to it).
    assert "st === 'undocked'" in html
    assert "c.ship_label ? 'ms-2' : 'ms-auto'" in html
