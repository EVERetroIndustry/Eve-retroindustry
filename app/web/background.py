"""Background data refresh: one scheduler, and one place that knows what the app is doing.

Before this, every kind of data had its own way of getting fresh. Alliance and
public contracts started filling when someone opened their tab, the names of
public structures when someone typed into a station search, and Jita, the trade
hubs and the character sync only when someone pressed a button - and the two
price loads lived inside the request that streamed their progress, so leaving
the page threw the work away. Six places kept state, each with its own words
for "busy", and no page could say what the app was doing as a whole.

Here a job is a named piece of work with an interval and a way to tell how old
its data is. One loop runs whatever is due. Three rules shape it:

* **It waits for startup, not for a clock.** Nothing starts until the data the
  first page needs has been delivered: the static data is loaded, the dashboard
  has answered once (from ESI, from its stored copy or with nothing - delivered,
  not necessarily successful, or an ESI outage would hold everything back for
  good), and no post-login sync is running. See StartupGate.
* **One run per rate-limit group at a time.** ESI meters each group in its own
  bucket, so a Jita sweep and a character sync do not compete, but Jita and a
  trade hub do. Work in different groups runs side by side.
* **Age comes from the data, not from this process.** Every job reports when
  its data was last refreshed from what is stored, so the answer survives a
  restart and also counts a refresh someone ran by hand.

Long-running fillers that already manage themselves (the contract indexers) are
not rewritten: they are registered as adapters, started and stopped from here,
and read for the panel.

Automatic updating can be switched off. Off means nothing starts by itself;
anything the user asks for still runs.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

# ── The on/off switch ────────────────────────────────────────────────────────

_AUTO_KEY = "auto_update"


def auto_update_enabled() -> bool:
    """On unless the user switched it off. Stored with the rest of the app config."""
    from app.auth.token_store import _load_json
    return bool(_load_json().get(_AUTO_KEY, True))


def set_auto_update(enabled: bool) -> None:
    from app.auth.token_store import _load_json, _save_json
    data = _load_json()
    data[_AUTO_KEY] = bool(enabled)
    _save_json(data)


# Per source: someone who never looks at Rens should not pay a region sweep for
# it every half hour. A source switched off here refreshes only when asked -
# Run now, or its own button on its page - and Sync All leaves it out too.
_AUTO_OFF_KEY = "auto_off"


def manual_only() -> set[str]:
    from app.auth.token_store import _load_json
    return set(_load_json().get(_AUTO_OFF_KEY) or [])


def set_job_auto(key: str, enabled: bool) -> None:
    from app.auth.token_store import _load_json, _save_json
    data = _load_json()
    off = set(data.get(_AUTO_OFF_KEY) or [])
    (off.discard if enabled else off.add)(key)
    data[_AUTO_OFF_KEY] = sorted(off)
    _save_json(data)


# ── Startup gate ─────────────────────────────────────────────────────────────

class StartupGate:
    """Open once the data the first screen needs is in.

    `sde_ready` and `sync_running` are callables because their state lives in
    main.py; `delivered()` is called by the dashboard's live endpoint on every
    way out of it.
    """

    def __init__(self) -> None:
        self.sde_ready: Callable[[], bool] = lambda: False
        self.sync_running: Callable[[], bool] = lambda: False
        self._delivered = False
        self._opened = False

    def delivered(self) -> None:
        self._delivered = True

    def waiting_for(self) -> Optional[str]:
        """None when open, otherwise what it is still waiting for (for the panel).

        Latches: it is a STARTUP gate. Without that, the hourly character sync -
        itself a background job - would close it again for as long as it runs.
        """
        if self._opened:
            return None
        if not self.sde_ready():
            return "static data"
        if not self._delivered:
            return "the dashboard"
        if self.sync_running():
            return "the character sync"
        self._opened = True
        return None

    def is_open(self) -> bool:
        return self.waiting_for() is None


GATE = StartupGate()


# ── Jobs ─────────────────────────────────────────────────────────────────────

Report = Callable[..., None]


@dataclass
class Job:
    key: str
    title: "str | Callable[[], str]"              # callable when it depends on data
    group: str                                   # ESI rate-limit group it spends
    every: float                                 # seconds between refreshes
    run: Callable[[Report], Awaitable[Optional[dict]]]
    last_ok: Callable[[], Optional[float]]       # age of the stored data, from the data
    # None when the job applies; otherwise why it does not (no characters, a
    # hub nobody downloaded). Such a job is left out of the panel and never
    # scheduled - but a user can still start it, which is how a hub begins.
    applies: Callable[[], Optional[str]] = lambda: None
    # Optional override of "older than `every`" - e.g. a refresh whose own
    # module already knows when it is stale.
    is_due: Optional[Callable[[], bool]] = None
    describe: Callable[[dict], tuple[Optional[int], str]] = (
        lambda p: (p.get("pct"), p.get("detail") or ""))
    throttle_url: Optional[str] = None           # to report an ESI pause while it runs
    in_run_all: bool = True                      # part of "Sync All"
    href: Optional[str] = None                   # page where its data is shown


@dataclass
class _State:
    running: bool = False
    queued: bool = False
    run_id: int = 0
    started: float = 0.0
    progress: dict = field(default_factory=dict)
    result: Optional[dict] = None
    error: Optional[str] = None
    failures: int = 0
    # Earliest the scheduler may start it again: a backoff after a failure, a
    # full interval after a success. The latter matters for a job whose data
    # does not get younger even when it runs fine (a character whose login has
    # died keeps its old sync time) - without it, such a job is due again on
    # the very next tick, every two seconds.
    retry_at: float = 0.0
    task: Optional[asyncio.Task] = None


@dataclass
class Filler:
    """A self-managing worker shown in the panel and started/stopped from here."""
    key: str
    title: str
    start: Callable[[], None]
    stop: Callable[[], None]
    rows: Callable[[], list[dict]]               # panel rows, already shaped


_JOBS: dict[str, Job] = {}
# One-off work the app starts outside the scheduler (a page reading what it needs
# behind the response). Shown while it runs; see track().
_TRACKED: dict[str, dict] = {}
_TRACK_SEQ = [0]
_STATE: dict[str, _State] = {}
_FILLERS: dict[str, Filler] = {}
_LOOP: dict[str, object] = {}
_BACKOFF = (5 * 60, 10 * 60, 20 * 60, 40 * 60, 60 * 60)
# The scheduler's clock. Tests replace it, so no rule here depends on when they run.
_clock: Callable[[], float] = time.time
_TICK = 2.0
# Errors raised by the jobs are turned into text by this; main.py installs one
# that knows ESI's wording.
error_text: Callable[[BaseException], str] = lambda exc: str(exc) or type(exc).__name__
# What decides whether ESI is pausing a group; main.py installs the real one.
throttle_status: Callable[[Optional[str]], dict] = lambda url: {"paused": False, "seconds": 0}
# Whether a user is waiting on price work right now; main.py installs the real one.
foreground_active: Callable[[], bool] = lambda: False


def register(job: Job) -> None:
    _JOBS[job.key] = job
    _STATE.setdefault(job.key, _State())


def track(task: asyncio.Task, title: str, detail: str = "", href: Optional[str] = None,
          key: Optional[str] = None) -> dict:
    """Show a task that runs outside the scheduler in the panel while it runs.

    Without this the panel could only speak for its own jobs: the Deals tab
    reading daily history for a hundred types said so on its own page while the
    panel showed every source as done. Returns a dict the task may update with
    `pct` / `detail` as it goes.
    """
    if key is None:
        _TRACK_SEQ[0] += 1
        key = f"task:{_TRACK_SEQ[0]}"
    entry = {"key": key, "title": title, "detail": detail, "href": href,
             "progress": {}, "started": _clock()}
    _TRACKED[key] = entry

    def _done(_t) -> None:
        if _TRACKED.get(key) is entry:
            _TRACKED.pop(key, None)
    task.add_done_callback(_done)
    return entry["progress"]


def unregister(key: str) -> None:
    """Drop a job that no longer applies (a station that was removed). Its state
    stays, so a stream following a run that is still going can see it end."""
    _JOBS.pop(key, None)


def keys(prefix: str = "") -> list[str]:
    return [k for k in _JOBS if k.startswith(prefix)]


def title_of(job: Job) -> str:
    return job.title() if callable(job.title) else job.title


def register_filler(filler: Filler) -> None:
    _FILLERS[filler.key] = filler


def reset_for_tests() -> None:
    _JOBS.clear()
    _STATE.clear()
    _FILLERS.clear()
    _TRACKED.clear()
    _LOOP.clear()
    GATE._delivered = False
    GATE._opened = False


# ── Scheduling rules ─────────────────────────────────────────────────────────

def in_downtime(now: Optional[float] = None) -> bool:
    """EVE's daily downtime starts at 11:00 UTC and usually ends within ten
    minutes. Asking during it only collects errors, and errors count against the
    budget every other request shares."""
    t = _dt.datetime.fromtimestamp(now if now is not None else _clock(), _dt.timezone.utc)
    minutes = t.hour * 60 + t.minute
    return 10 * 60 + 58 <= minutes < 11 * 60 + 20


def _due(job: Job, st: _State, now: float) -> bool:
    if st.running or now < st.retry_at:
        return False
    if job.is_due is not None:
        return job.is_due()
    last = job.last_ok()
    return last is None or now - last >= job.every


def _group_busy(group: str) -> bool:
    return any(_STATE[k].running for k, j in _JOBS.items() if j.group == group)


def _hold_reason(job: Job, now: float) -> Optional[str]:
    """Why a job that could run right now is being held back, or None."""
    if in_downtime(now):
        return "EVE downtime (11:00 UTC) - waiting it out"
    if job.group == "market" and foreground_active():
        # Not "the one you started": background jobs set the same marker.
        return "waiting for another price load to finish"
    t = throttle_status(job.throttle_url)
    if t.get("paused"):
        return f"ESI rate limit - retrying in {t.get('seconds') or 0}s"
    return None


# ── Running ──────────────────────────────────────────────────────────────────

def _start(key: str) -> int:
    job, st = _JOBS[key], _STATE[key]
    st.running, st.queued = True, False
    st.run_id += 1
    st.started = _clock()
    st.progress, st.result, st.error = {}, None, None

    def report(**payload) -> None:
        st.progress.update(payload)

    async def _go() -> None:
        try:
            st.result = await job.run(report) or {}
            st.failures, st.retry_at = 0, _clock() + job.every
        except asyncio.CancelledError:
            st.error = "stopped"
            raise
        except Exception as exc:                  # noqa: BLE001 - shown to the user
            st.error = error_text(exc)
            st.failures += 1
            st.retry_at = _clock() + _BACKOFF[min(st.failures, len(_BACKOFF)) - 1]
            print(f"[background] {key} failed: {st.error}", flush=True)
        finally:
            st.running = False
            st.task = None

    st.task = asyncio.create_task(_go())
    return st.run_id


def run_now(key: str) -> int:
    """Start a job for the user, or join the run already going.

    Returns the id of the run the caller should wait for. When the job's rate
    limit group is busy with another job, this one is queued and starts as soon
    as the group is free - the id is then the one it will get.
    """
    st = _STATE[key]
    if st.running:
        return st.run_id
    if _group_busy(_JOBS[key].group):
        st.queued = True
        return st.run_id + 1
    return _start(key)


def run_all() -> list[str]:
    started = []
    manual = manual_only()
    for key, job in _JOBS.items():
        if job.in_run_all and job.applies() is None and key not in manual:
            run_now(key)
            started.append(key)
    return started


def tick(now: Optional[float] = None) -> None:
    """One scheduling pass. Separate from the loop so tests can drive it."""
    now = _clock() if now is None else now
    if not GATE.is_open():
        return
    auto = auto_update_enabled()
    manual = manual_only()
    for filler in _FILLERS.values():
        try:
            (filler.start if auto else filler.stop)()
        except Exception as exc:                  # noqa: BLE001
            print(f"[background] filler {filler.key}: {exc}", flush=True)
    for key, job in _JOBS.items():
        st = _STATE[key]
        if st.running or _group_busy(job.group):
            continue
        if st.queued:                             # asked for by the user: no hold rules
            _start(key)
            continue
        if not auto or key in manual or job.applies() is not None or not _due(job, st, now):
            continue
        if _hold_reason(job, now):
            continue
        _start(key)


async def _loop() -> None:
    while True:
        try:
            tick()
        except Exception as exc:                  # noqa: BLE001 - the loop must survive
            print(f"[background] tick failed: {exc}", flush=True)
        await asyncio.sleep(_TICK)


def start_loop() -> None:
    task = _LOOP.get("task")
    if task is not None and not task.done():      # type: ignore[union-attr]
        return
    _LOOP["task"] = asyncio.create_task(_loop())


def stop_fillers() -> None:
    for filler in _FILLERS.values():
        try:
            filler.stop()
        except Exception:                         # noqa: BLE001
            pass


# ── What the panel and the progress streams read ─────────────────────────────

def job_state(key: str) -> _State:
    return _STATE[key]


def finished(key: str, run_id: int) -> bool:
    """Has the run `run_id` (from run_now) come to an end?"""
    st = _STATE[key]
    return st.run_id >= run_id and not st.running and not st.queued


def _row(key: str, now: float, auto: bool, manual: set[str] = frozenset()) -> dict:
    job, st = _JOBS[key], _STATE[key]
    last = job.last_ok()
    row = {"key": key, "title": title_of(job), "kind": "job", "href": job.href,
           "last_ok": last, "pct": None, "detail": "", "reason": None,
           "error": None, "next_in": None, "can_run": True,
           "auto": key not in manual}
    if st.running:
        pct, detail = job.describe(st.progress)
        row.update(state="running", pct=pct, detail=detail)
        t = throttle_status(job.throttle_url)
        if t.get("paused"):
            row["reason"] = f"ESI rate limit - continuing in {t.get('seconds') or 0}s"
        return row
    if st.queued:
        row.update(state="queued", detail="waiting for another refresh in the same ESI group")
        return row
    if st.error and st.error != "stopped":
        row.update(state="error", error=st.error,
                   next_in=max(0, int(st.retry_at - now)) if auto else None)
        return row
    if not auto:
        row.update(state="off")
        return row
    if key in manual:
        row.update(state="manual")
        return row
    if not GATE.is_open():
        row.update(state="waiting", reason=f"starts after {GATE.waiting_for()} has loaded")
        return row
    if job.is_due is not None:
        due = job.is_due()
        next_in = 0 if due else None
    else:
        next_in = 0 if last is None else max(0, int(last + job.every - now))
    if next_in is not None and st.retry_at > now:
        next_in = max(next_in, int(st.retry_at - now))
    hold = _hold_reason(job, now) if next_in == 0 else None
    if hold:
        row.update(state="waiting", reason=hold)
    elif next_in == 0 and _group_busy(job.group):
        row.update(state="waiting", reason="waiting for another refresh in the same ESI group")
    else:
        row.update(state="idle", next_in=next_in)
    return row


def snapshot(now: Optional[float] = None) -> dict:
    now = _clock() if now is None else now
    auto = auto_update_enabled()
    manual = manual_only()
    rows = [_row(k, now, auto, manual) for k, j in _JOBS.items() if j.applies() is None
            or _STATE[k].running or _STATE[k].queued]
    for filler in _FILLERS.values():
        try:
            rows += filler.rows()
        except Exception as exc:                  # noqa: BLE001
            rows.append({"key": filler.key, "title": filler.title, "kind": "filler",
                         "state": "error", "error": str(exc)[:200]})
    for t in list(_TRACKED.values()):
        p = t["progress"]
        rows.append({"key": t["key"], "title": t["title"], "kind": "task", "href": t["href"],
                     "state": "running", "pct": p.get("pct"),
                     "detail": p.get("detail") or t["detail"], "reason": None,
                     "error": None, "next_in": None, "can_run": False, "last_ok": None})
    running = [r for r in rows if r.get("state") == "running"]
    pcts = [r["pct"] for r in running if isinstance(r.get("pct"), (int, float))]
    return {
        "auto": auto,
        "ready": GATE.is_open(),
        "waiting_for": GATE.waiting_for(),
        "running": len(running),
        "pct": int(sum(pcts) / len(pcts)) if pcts else None,
        "errors": sum(1 for r in rows if r.get("state") == "error"),
        "jobs": rows,
    }
