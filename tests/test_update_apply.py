"""Applying an update without looking like a dropper.

A tester's AVG objected to the app mid-update, and the reason was not one line of
code but a sequence: a batch file written into the program directory, run through
cmd.exe, killing processes by image name, replacing executables, writing the
uninstall registry key with reg.exe, relaunching and then deleting itself. Code
signing (which would give the binary a reputation to argue with) is not on the
table, so the behaviour had to go instead.

These tests pin down the replacement AND the absence of the old behaviour - the
second half matters, because nothing else would notice it creeping back.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from app import update_apply as ua

REPO = Path(__file__).resolve().parent.parent


# ── what must never come back ─────────────────────────────────────────────────

def test_the_update_path_spawns_no_shell_and_writes_no_script():
    src = (REPO / "app" / "web" / "main.py").read_text()
    # Cut the comments out first: they explain what was removed and would
    # otherwise match every pattern below.
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    for banned in ("update.bat", "update.sh", "taskkill", "robocopy",
                   "reg add", "%~f0", '"cmd"', "cmd /c"):
        assert banned not in code, banned


def test_nothing_deletes_itself():
    """A self-deleting helper is one of the strongest heuristic markers there is."""
    for f in ("app/update_apply.py", "launcher.py"):
        code = (REPO / f).read_text()
        assert "%~f0" not in code
        assert 'rm -- "$0"' not in code


# ── waiting for the old process, not killing it ───────────────────────────────

def test_waiting_on_a_dead_pid_returns_at_once():
    assert ua.wait_for_exit(0) is True
    assert ua.wait_for_exit(-1) is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX liveness probe")
def test_a_live_process_is_waited_for_and_not_killed():
    """os.kill(pid, 0) on Windows TERMINATES - hence the ctypes path there."""
    pid = os.getpid()
    assert ua.wait_for_exit(pid, timeout=0.5) is False
    assert os.getpid() == pid                      # still here, obviously


def test_a_process_that_never_exits_leaves_the_install_alone(tmp_path, monkeypatch):
    src = tmp_path / "src"; src.mkdir()
    (src / "EVE_Retroindustry").write_text("new")
    dst = tmp_path / "install"; dst.mkdir()
    (dst / "EVE_Retroindustry").write_text("old")
    monkeypatch.setattr(ua, "wait_for_exit", lambda *a, **k: False)
    copied = []
    monkeypatch.setattr(ua, "copy_tree", lambda *a, **k: copied.append(1) or [])

    rc = ua.main(["--apply-update", "--src", str(src), "--dst", str(dst),
                  "--wait-pid", "4242"])
    assert rc == 4 and not copied
    assert (dst / "EVE_Retroindustry").read_text() == "old"


# ── copying ───────────────────────────────────────────────────────────────────

def test_copy_replaces_the_tree_and_keeps_everything_else(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "_internal").mkdir(parents=True)
    (src / "EVE_Retroindustry").write_text("v2")
    (src / "_internal" / "qt.dll").write_text("new dll")
    dst.mkdir()
    (dst / "EVE_Retroindustry").write_text("v1")
    # User data a portable install keeps next to the binary must survive.
    (dst / "eve_cache.db").write_text("characters and prices")

    assert ua.copy_tree(src, dst) == []
    assert (dst / "EVE_Retroindustry").read_text() == "v2"
    assert (dst / "_internal" / "qt.dll").read_text() == "new dll"
    assert (dst / "eve_cache.db").read_text() == "characters and prices"


def test_a_locked_file_is_retried_rather_than_forced(tmp_path, monkeypatch):
    """A lingering QtWebEngineProcess holds DLLs for a moment after exit. The old
    path solved that by killing processes by name; this one waits."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir(); dst.mkdir()
    (src / "qt.dll").write_text("new")
    real_copy = ua.shutil.copy2
    attempts = {"n": 0}

    def _flaky(a, b, *rest):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PermissionError("locked")
        return real_copy(a, b, *rest)

    monkeypatch.setattr(ua.shutil, "copy2", _flaky)
    slept = []
    assert ua.copy_tree(src, dst, sleep=slept.append) == []
    assert attempts["n"] == 3 and len(slept) == 2
    assert (dst / "qt.dll").read_text() == "new"


def test_a_file_that_stays_locked_is_reported_not_hidden(tmp_path, monkeypatch):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir(); dst.mkdir()
    (src / "qt.dll").write_text("new")
    monkeypatch.setattr(ua.shutil, "copy2",
                        lambda *a: (_ for _ in ()).throw(PermissionError("locked")))
    failures = ua.copy_tree(src, dst, attempts=3, sleep=lambda _s: None)
    assert len(failures) == 1 and "qt.dll" in failures[0]


# ── the rest of the flow ──────────────────────────────────────────────────────

def test_the_registry_entry_is_left_alone_off_windows():
    """It is only ever updated, never created - a portable copy must not end up
    with an entry in Apps & features."""
    assert ua.set_installed_version("0.11.07") is False or sys.platform == "win32"


def test_missing_arguments_fail_loudly(tmp_path):
    assert ua.main(["--apply-update"]) == 2
    assert ua.main(["--apply-update", "--src", str(tmp_path / "nope"),
                    "--dst", str(tmp_path)]) == 3


def test_staging_cleanup_removes_only_the_staging_tree(tmp_path):
    (tmp_path / ua.STAGING_DIR / "EVE_Retroindustry").mkdir(parents=True)
    (tmp_path / "eve_cache.db").write_text("keep me")
    ua.clean_staging(tmp_path)
    assert not (tmp_path / ua.STAGING_DIR).exists()
    assert (tmp_path / "eve_cache.db").read_text() == "keep me"
    ua.clean_staging(tmp_path)                     # idempotent, no staging left


def test_the_full_run_copies_and_relaunches(tmp_path, monkeypatch):
    src, dst = tmp_path / "src", tmp_path / "install"
    src.mkdir(); dst.mkdir()
    (src / ua.EXE_NAME).write_text("v2")
    (dst / ua.EXE_NAME).write_text("v1")
    started: list[list[str]] = []
    monkeypatch.setattr(ua.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    monkeypatch.setattr(ua, "wait_for_exit", lambda *a, **k: True)

    rc = ua.main(["--apply-update", "--src", str(src), "--dst", str(dst),
                  "--wait-pid", "0", "--version", "0.11.07"])
    assert rc == 0
    assert (dst / ua.EXE_NAME).read_text() == "v2"
    assert started and started[0][0] == str(dst / ua.EXE_NAME)
