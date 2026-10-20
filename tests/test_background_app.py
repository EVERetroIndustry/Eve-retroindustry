"""The scheduler wired into the app: what opens it, what the switch stops, and
that a price refresh no longer belongs to the page that started it."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.market.prices import IncompleteOrderBook
from app.web import background as bg


@pytest.fixture
def m(app_module, monkeypatch, tmp_path):
    # token_store is imported while tests are collected, before the app's data
    # directory is set, so its config path points at the repository. The switch
    # must be stored somewhere throwaway.
    from app.auth import token_store
    monkeypatch.setattr(token_store, "CONFIG_PATH", str(tmp_path / "config.json"))
    yield app_module


def _events(resp) -> list[dict]:
    return [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]


def _settle_job(key: str) -> None:
    """Leave no run behind for the next test."""
    st = bg.job_state(key)
    assert not st.running, f"{key} still running after the test"
    st.error = st.result = None
    st.retry_at = 0.0


# ── The gate ─────────────────────────────────────────────────────────────────

def test_the_dashboard_answering_opens_the_gate(client, m):
    bg.GATE._delivered = bg.GATE._opened = False
    assert not bg.GATE.is_open()
    assert client.get("/api/dashboard/live").status_code == 200
    assert bg.GATE.is_open()


def test_a_dashboard_that_fails_still_opens_the_gate(client, m, monkeypatch):
    # Delivered, not successful: an ESI outage must not keep background work
    # from ever starting.
    bg.GATE._delivered = bg.GATE._opened = False

    async def boom(request, force):
        raise RuntimeError("ESI is down")
    monkeypatch.setattr(m, "_dashboard_live", boom)
    with pytest.raises(RuntimeError):
        client.get("/api/dashboard/live")
    assert bg.GATE.is_open()


# ── The panel ────────────────────────────────────────────────────────────────

def test_the_panel_lists_what_applies_here(client, m):
    snap = client.get("/api/activity").json()
    keys = [j["key"] for j in snap["jobs"]]
    assert "characters" in keys and "jita" in keys
    # The test database has no hub prices, so no hub is refreshed or shown.
    assert not any(k.startswith("hub:") for k in keys)
    assert snap["auto"] is True


def test_an_unknown_job_cannot_be_started(client, m):
    assert client.post("/api/activity/run", json={"key": "nope"}).status_code == 404


def test_switched_off_a_contract_tab_no_longer_starts_its_filler(client, m):
    m._cancel(m._PUBLIC_WORKER)
    m._PUBLIC_WORKER.clear()
    assert client.post("/api/activity/auto", json={"enabled": False}).json()["auto"] is False
    assert client.get("/contracts/public").status_code == 200
    assert m._PUBLIC_WORKER.get("task") is None
    assert client.get("/api/activity").json()["auto"] is False


# ── A price refresh belongs to the scheduler, not to the page ────────────────

def test_leaving_the_page_does_not_throw_the_refresh_away(m, monkeypatch):
    # The refresh used to live inside the streaming request: closing the page
    # during the order phase discarded every page fetched while the fetch went
    # on. Now the stream only watches.
    release = asyncio.Event()

    async def fake_refresh(conn, type_ids, report=None):
        report(phase="orders", current=1, total=2, pct=40)
        await release.wait()
        return {"refreshed": 7, "total": 9, "volume_updated": 0}
    monkeypatch.setattr(m, "refresh_jita", fake_refresh)

    async def go():
        stream = m._follow_job("jita")
        first = json.loads((await stream.__anext__())[6:])
        await stream.aclose()                 # the user navigates away
        release.set()
        st = bg.job_state("jita")
        await st.task
        return first, st
    first, st = asyncio.run(go())
    assert first.get("note") or first.get("phase") == "orders"
    assert st.result == {"refreshed": 7, "total": 9, "volume_updated": 0}
    conn = m.get_conn()
    try:
        assert conn.execute("SELECT last_ok FROM background_runs WHERE job='jita'").fetchone()
    finally:
        conn.close()
    _settle_job("jita")


def test_the_prices_page_stream_reports_the_run_to_its_end(client, m, monkeypatch):
    async def fake_refresh(conn, type_ids, report=None):
        report(phase="orders", current=2, total=2, pct=80)
        return {"refreshed": 3, "total": 5, "volume_updated": 1}
    monkeypatch.setattr(m, "refresh_jita", fake_refresh)
    events = _events(client.get("/prices/refresh/stream"))
    assert events[-1] == {"pct": 100, "done": True, "refreshed": 3, "total": 5,
                          "volume_updated": 1}
    _settle_job("jita")


def test_the_prices_page_stream_says_the_prices_were_kept(client, m, monkeypatch):
    async def fake_refresh(conn, type_ids, report=None):
        raise IncompleteOrderBook("ESI did not return page 1 of the order book (HTTP 503).")
    monkeypatch.setattr(m, "refresh_jita", fake_refresh)
    events = _events(client.get("/prices/refresh/stream"))
    assert "left as they were" in events[-1]["error"]
    assert not any(e.get("done") for e in events)
    _settle_job("jita")


def test_an_error_after_prices_were_written_does_not_claim_they_were_kept():
    # Only an unreadable order book is known to have written nothing.
    from app.web.prices_helper import sweep_error_message
    assert "left as they were" not in sweep_error_message(RuntimeError("volumes failed"))
    assert "left as they were" in sweep_error_message(IncompleteOrderBook("page 3"))


def test_a_filler_not_started_yet_does_not_read_as_due(client, m):
    bg.GATE._delivered = bg.GATE._opened = False
    m._FILLER_ROWS.clear()             # rows are cached for a few seconds
    try:
        rows = {r["key"]: r for r in client.get("/api/activity").json()["jobs"]}
        assert rows["public"]["state"] == "waiting"
        assert "dashboard" in rows["public"]["reason"]
    finally:
        bg.GATE.delivered()


def test_sync_all_is_the_panel_on_every_page(client, m):
    html = client.get("/").text
    assert html.count('id="act-btn"') == 1 and 'id="act-menu"' in html
    assert "syncAll(" not in html, "the old blind-reload Sync All is gone"
    # The poll must not light the busy bar on every page every two seconds.
    assert "rawFetch('/api/activity')" in html
