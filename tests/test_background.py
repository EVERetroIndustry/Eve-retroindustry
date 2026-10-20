"""The background scheduler: when work starts, what runs side by side, and what
the panel is told.

Driven through tick() with fake jobs and a fake clock, so nothing here reaches
ESI and nothing waits on real time. The scenarios are async because a job is an
asyncio task.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app.auth import token_store
from app.web import background as bg

NOON = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.timezone.utc).timestamp()
DOWNTIME = dt.datetime(2026, 10, 20, 11, 5, tzinfo=dt.timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(token_store, "CONFIG_PATH", str(tmp_path / "config.json"))
    # The app registers its own jobs at import; these tests use a registry of
    # their own and must hand the app's back untouched.
    saved = (dict(bg._JOBS), dict(bg._STATE), dict(bg._FILLERS),
             bg.GATE.sde_ready, bg.GATE.sync_running, bg.GATE._delivered, bg.GATE._opened)
    bg.reset_for_tests()
    bg.GATE.sde_ready = lambda: True
    bg.GATE.sync_running = lambda: False
    bg.GATE.delivered()
    monkeypatch.setattr(bg, "throttle_status", lambda url: {"paused": False, "seconds": 0})
    monkeypatch.setattr(bg, "foreground_active", lambda: False)
    monkeypatch.setattr(bg, "_clock", lambda: NOON)
    yield
    bg.reset_for_tests()
    bg._JOBS.update(saved[0])
    bg._STATE.update(saved[1])
    bg._FILLERS.update(saved[2])
    (bg.GATE.sde_ready, bg.GATE.sync_running,
     bg.GATE._delivered, bg.GATE._opened) = saved[3:]


class Fake:
    """A job body that waits until released, counting how often it was started."""

    def __init__(self, fail: Exception | None = None, result=None):
        self.starts = 0
        self.release = asyncio.Event()
        self.fail = fail
        self.result = result or {"ok": True}

    async def __call__(self, report):
        self.starts += 1
        report(pct=10, detail="working")
        await self.release.wait()
        if self.fail:
            raise self.fail
        return self.result


def _job(key, body, group="market", last_ok=None, every=1800, **kw):
    bg.register(bg.Job(key=key, title=key, group=group, every=every, run=body,
                       last_ok=lambda: last_ok, **kw))


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


def run(coro):
    return asyncio.run(coro)


# ── When work starts ─────────────────────────────────────────────────────────

def test_nothing_starts_before_the_dashboard_has_answered():
    async def go():
        bg.reset_for_tests()
        bg.GATE.sde_ready = lambda: True
        body = Fake()
        _job("jita", body)
        bg.tick(NOON)
        assert body.starts == 0 and bg.GATE.waiting_for() == "the dashboard"
        bg.GATE.delivered()
        bg.tick(NOON)
        await _settle()
        assert body.starts == 1
        body.release.set()
    run(go())


def test_nothing_starts_without_static_data_or_during_the_login_sync():
    async def go():
        bg.reset_for_tests()
        bg.GATE.delivered()
        sde, sync = [False], [True]
        bg.GATE.sde_ready = lambda: sde[0]
        bg.GATE.sync_running = lambda: sync[0]
        body = Fake()
        _job("jita", body)
        bg.tick(NOON)
        assert bg.GATE.waiting_for() == "static data"
        sde[0] = True
        bg.tick(NOON)
        assert bg.GATE.waiting_for() == "the character sync"
        sync[0] = False
        bg.tick(NOON)
        await _settle()
        assert body.starts == 1
        body.release.set()
    run(go())


def test_once_open_the_gate_stays_open_while_a_later_sync_runs():
    # The hourly character sync is itself a background job. If it could close
    # the gate, nothing else would be scheduled for as long as it runs.
    sync = [False]
    bg.GATE.sync_running = lambda: sync[0]
    assert bg.GATE.is_open()
    sync[0] = True
    assert bg.GATE.is_open()


def test_a_job_with_fresh_data_is_not_due():
    async def go():
        body = Fake()
        _job("jita", body, last_ok=NOON - 60)
        bg.tick(NOON)
        await _settle()
        assert body.starts == 0
        bg.tick(NOON + 1800)
        await _settle()
        assert body.starts == 1
        body.release.set()
    run(go())


def test_a_successful_run_waits_a_full_interval_even_if_its_data_did_not_age():
    # A character whose login died keeps its old sync time however often the
    # sync runs; without the wait it would be due again on the next tick.
    async def go():
        body = Fake()
        body.release.set()
        _job("characters", body, group="character", last_ok=None, every=3600)
        bg.tick()
        await _settle()
        bg.tick()
        await _settle()
        assert body.starts == 1
    run(go())


def test_downtime_holds_scheduled_work_but_not_what_the_user_asks_for():
    async def go():
        body = Fake()
        _job("jita", body)
        bg.tick(DOWNTIME)
        await _settle()
        assert body.starts == 0
        assert "downtime" in bg.snapshot(DOWNTIME)["jobs"][0]["reason"]
        bg.run_now("jita")
        await _settle()
        assert body.starts == 1
        body.release.set()
    run(go())


def test_an_esi_pause_on_the_jobs_own_group_holds_it(monkeypatch):
    async def go():
        monkeypatch.setattr(bg, "throttle_status",
                            lambda url: {"paused": url == "orders", "seconds": 40})
        held, free = Fake(), Fake()
        _job("jita", held, throttle_url="orders")
        _job("characters", free, group="character", throttle_url="assets")
        bg.tick(NOON)
        await _settle()
        assert (held.starts, free.starts) == (0, 1)
        row = next(r for r in bg.snapshot(NOON)["jobs"] if r["key"] == "jita")
        assert "rate limit" in row["reason"] and "40s" in row["reason"]
        free.release.set()
    run(go())


def test_market_work_stands_aside_while_the_user_waits_on_a_price_load(monkeypatch):
    async def go():
        monkeypatch.setattr(bg, "foreground_active", lambda: True)
        body = Fake()
        _job("jita", body)
        bg.tick(NOON)
        await _settle()
        assert body.starts == 0
    run(go())


# ── What runs side by side ───────────────────────────────────────────────────

def test_one_run_per_rate_limit_group_and_groups_run_side_by_side():
    async def go():
        jita, amarr, chars = Fake(), Fake(), Fake()
        _job("jita", jita)
        _job("hub:amarr", amarr)
        _job("characters", chars, group="character")
        bg.tick(NOON)
        await _settle()
        assert (jita.starts, amarr.starts, chars.starts) == (1, 0, 1)
        jita.release.set()
        await _settle()
        bg.tick(NOON)
        await _settle()
        assert amarr.starts == 1
        amarr.release.set()
        chars.release.set()
    run(go())


def test_asking_twice_joins_the_run_instead_of_starting_another():
    async def go():
        body = Fake()
        _job("jita", body)
        first = bg.run_now("jita")
        second = bg.run_now("jita")
        await _settle()
        assert first == second and body.starts == 1
        assert not bg.finished("jita", first)
        body.release.set()
        await _settle()
        assert bg.finished("jita", first)
        assert bg.job_state("jita").result == {"ok": True}
    run(go())


def test_a_run_asked_for_while_its_group_is_busy_starts_when_it_frees():
    async def go():
        jita, amarr = Fake(), Fake()
        _job("jita", jita)
        _job("hub:amarr", amarr)
        bg.run_now("jita")
        want = bg.run_now("hub:amarr")
        await _settle()
        assert amarr.starts == 0 and not bg.finished("hub:amarr", want)
        assert next(r for r in bg.snapshot()["jobs"] if r["key"] == "hub:amarr")["state"] == "queued"
        jita.release.set()
        await _settle()
        bg.tick(DOWNTIME)              # a queued run is the user's: no hold rules
        await _settle()
        assert amarr.starts == 1
        amarr.release.set()
        await _settle()
        assert bg.finished("hub:amarr", want)
    run(go())


# ── Failures ─────────────────────────────────────────────────────────────────

def test_a_failed_run_says_why_and_backs_off():
    async def go():
        body = Fake(fail=RuntimeError("ESI said no"))
        body.release.set()
        _job("jita", body)
        bg.tick()
        await _settle()
        row = bg.snapshot()["jobs"][0]
        assert row["state"] == "error" and row["error"] == "ESI said no"
        assert 0 < row["next_in"] <= 5 * 60
        bg.tick()
        await _settle()
        assert body.starts == 1, "a failure must not be retried on the next tick"
    run(go())


# ── The switch ───────────────────────────────────────────────────────────────

def test_switched_off_nothing_starts_by_itself_but_asking_still_works():
    async def go():
        bg.set_auto_update(False)
        started, stopped = [], []
        bg.register_filler(bg.Filler("public", "Public", lambda: started.append(1),
                                     lambda: stopped.append(1), lambda: []))
        body = Fake()
        _job("jita", body)
        bg.tick(NOON)
        await _settle()
        assert body.starts == 0 and not started and stopped
        assert bg.snapshot(NOON)["jobs"][0]["state"] == "off"
        bg.run_now("jita")
        await _settle()
        assert body.starts == 1
        body.release.set()
    run(go())


def test_the_switch_is_on_until_someone_turns_it_off():
    assert bg.auto_update_enabled() is True
    bg.set_auto_update(False)
    assert bg.auto_update_enabled() is False
    bg.set_auto_update(True)
    assert bg.auto_update_enabled() is True


# ── What the panel is told ───────────────────────────────────────────────────

def test_a_job_that_does_not_apply_is_left_out_until_someone_starts_it():
    async def go():
        body = Fake()
        _job("hub:amarr", body, applies=lambda: "not downloaded yet")
        bg.tick(NOON)
        await _settle()
        assert body.starts == 0 and bg.snapshot(NOON)["jobs"] == []
        bg.run_now("hub:amarr")        # the user's first download
        await _settle()
        assert [r["key"] for r in bg.snapshot(NOON)["jobs"]] == ["hub:amarr"]
        body.release.set()
    run(go())


def test_the_panel_reports_progress_and_an_overall_percentage():
    async def go():
        body = Fake()
        _job("jita", body)
        bg.run_now("jita")
        await _settle()
        snap = bg.snapshot()
        assert snap["running"] == 1 and snap["pct"] == 10
        assert snap["jobs"][0]["detail"] == "working"
        body.release.set()
    run(go())


# ── Work started outside the scheduler ───────────────────────────────────────

def test_a_task_started_elsewhere_shows_while_it_runs_and_goes_when_done():
    # The Deals tab read history for 103 types and said so on its own page,
    # while the panel showed every source as done.
    async def go():
        release = asyncio.Event()

        async def work():
            await release.wait()
        task = asyncio.create_task(work())
        progress = bg.track(task, "Market history for Deals", "103 types", "/contracts/deals")
        snap = bg.snapshot()
        row = next(r for r in snap["jobs"] if r["kind"] == "task")
        assert row["title"] == "Market history for Deals" and row["state"] == "running"
        assert row["detail"] == "103 types" and snap["running"] == 1
        progress.update(pct=40, detail="41 / 103 types")
        row = next(r for r in bg.snapshot()["jobs"] if r["kind"] == "task")
        assert (row["pct"], row["detail"]) == (40, "41 / 103 types")
        release.set()
        await task
        await _settle()
        snap = bg.snapshot()
        assert not [r for r in snap["jobs"] if r["kind"] == "task"] and snap["running"] == 0
    run(go())
