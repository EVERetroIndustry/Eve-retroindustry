"""Webview profile directory handling in launcher.py.

A Windows user could not start the app at all: pywebview validates its
storage_path by stat-ing it and then calling os.makedirs() unguarded, and turns
any OSError into "Storage path ... is not writable" — discarding the real error
(there, FileExistsError / WinError 183). These tests cover both halves of the
fix: pick a directory that passes pywebview's own checks, and keep the app
starting even if the directory goes bad right afterwards.
"""
from __future__ import annotations

import importlib.util
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def launcher(tmp_path_factory):
    # EVE_APP_DIR is set by the app_module fixture; launcher only uses setdefault,
    # so importing it cannot disturb the other tests.
    spec = importlib.util.spec_from_file_location("_launcher_under_test",
                                                  os.path.join(REPO, "launcher.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _accepted_by_pywebview(path: str) -> bool:
    """Exactly what pywebview.start() will do with the path we hand it."""
    import webview
    try:
        getattr(webview, "_" + "_set_storage_path")(path)
        return True
    except Exception:
        return False


def test_clean_dir(launcher, tmp_path):
    got = launcher._prepare_webview_storage(str(tmp_path))
    assert got == str(tmp_path / "webview_data")
    assert _accepted_by_pywebview(got)


def test_existing_profile_is_reused(launcher, tmp_path):
    (tmp_path / "webview_data").mkdir()
    (tmp_path / "webview_data" / "keep.txt").write_text("user data")
    assert launcher._prepare_webview_storage(str(tmp_path)) == str(tmp_path / "webview_data")
    assert (tmp_path / "webview_data" / "keep.txt").exists()   # never wiped


def test_leftover_file_is_moved_aside_not_deleted(launcher, tmp_path):
    (tmp_path / "webview_data").write_text("not a directory")
    got = launcher._prepare_webview_storage(str(tmp_path))
    assert got == str(tmp_path / "webview_data")
    assert os.path.isdir(got)
    assert (tmp_path / "webview_data.broken").read_text() == "not a directory"
    assert _accepted_by_pywebview(got)


def test_broken_symlink_is_recovered(launcher, tmp_path):
    """The exact signature from the Windows report: the name exists, stat says it
    does not. os.makedirs(exist_ok=True) raises here, so the old code silently
    dropped persistence; pywebview would have raised its misleading error."""
    os.symlink(str(tmp_path / "nonexistent"), tmp_path / "webview_data")
    assert not os.path.exists(tmp_path / "webview_data")      # stat lies
    assert os.path.lexists(tmp_path / "webview_data")         # the name is taken
    got = launcher._prepare_webview_storage(str(tmp_path))
    assert os.path.isdir(got)
    assert _accepted_by_pywebview(got)


def test_unwritable_profile_falls_through_to_an_alternate(launcher, tmp_path):
    d = tmp_path / "webview_data"
    d.mkdir()
    d.chmod(0o500)
    try:
        got = launcher._prepare_webview_storage(str(tmp_path))
        assert got == str(tmp_path / "webview_data-2")
        assert _accepted_by_pywebview(got)
    finally:
        d.chmod(0o700)


def test_unwritable_app_dir_falls_back_to_temp(launcher, tmp_path):
    tmp_path.chmod(0o500)
    try:
        got = launcher._prepare_webview_storage(str(tmp_path))
        assert got is not None and not got.startswith(str(tmp_path))
        assert _accepted_by_pywebview(got)
    finally:
        tmp_path.chmod(0o700)


# ── the retry: a profile that goes bad between our check and pywebview's ──────

class _FakeWebview:
    def __init__(self, fail_times: int, exc: BaseException):
        self.calls: list[dict] = []
        self._left, self._exc = fail_times, exc

    def start(self, **kw):
        self.calls.append(kw)
        if self._left > 0:
            self._left -= 1
            raise self._exc


def test_storage_failure_retries_without_a_profile(launcher):
    import webview
    path = "/some/profile/dir"
    wv = _FakeWebview(1, webview.errors.WebViewException(
        f"Storage path {path} is not writable"))
    launcher._start_webview(wv, path)
    assert len(wv.calls) == 2
    assert wv.calls[0]["storage_path"] == path
    assert "storage_path" not in wv.calls[1]          # retried with the default
    assert wv.calls[1]["private_mode"] is False       # still not private mode


def test_unrelated_gui_failure_is_not_swallowed(launcher):
    wv = _FakeWebview(1, RuntimeError("Qt platform plugin could not be initialized"))
    with pytest.raises(RuntimeError):
        launcher._start_webview(wv, "/some/profile/dir")
    assert len(wv.calls) == 1                          # no blind retry


def test_no_retry_loop_if_the_retry_also_fails(launcher):
    import webview
    path = "/some/profile/dir"
    wv = _FakeWebview(2, webview.errors.WebViewException(
        f"Storage path {path} is not writable"))
    with pytest.raises(webview.errors.WebViewException):
        launcher._start_webview(wv, path)
    assert len(wv.calls) == 2                          # retried exactly once
