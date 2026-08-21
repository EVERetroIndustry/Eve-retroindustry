"""
EVE Retroindustry - launcher with embedded webview window.

Entry point for both development mode and PyInstaller frozen bundle.

Architecture:
- FastAPI/uvicorn runs in a background thread on 127.0.0.1:8000
- pywebview opens a native window that points at that server
- Closing the window stops the server and exits

Usage (dev):   python launcher.py
Usage (build): pyinstaller eve_retroindustry.spec
"""
from __future__ import annotations

import multiprocessing
import os
import socket
import sys
import threading

# Windows: suppress harmless ConnectionResetError noise from ProactorEventLoop
if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _user_data_dir() -> str:
    """Per-user data directory OUTSIDE the install folder, so an app update
    (which replaces the install dir) can never touch eve_cache.db / config."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "EVE Retroindustry")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/EVE Retroindustry")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "eve-retroindustry")


def _migrate_legacy_data(old_dir: str, new_dir: str) -> None:
    """One-time copy of user data from the old in-install location into the
    stable data dir - only when the data dir has no eve_cache.db yet."""
    import shutil
    if not old_dir or os.path.abspath(old_dir) == os.path.abspath(new_dir):
        return
    if os.path.exists(os.path.join(new_dir, "eve_cache.db")):
        return
    for fn in ("eve_cache.db", ".eve_config.json"):
        src = os.path.join(old_dir, fn)
        if os.path.exists(src):
            try:
                shutil.copy2(src, os.path.join(new_dir, fn))
            except Exception:
                pass
    src_wv, dst_wv = os.path.join(old_dir, "webview_data"), os.path.join(new_dir, "webview_data")
    if os.path.isdir(src_wv) and not os.path.exists(dst_wv):
        try:
            shutil.copytree(src_wv, dst_wv)
        except Exception:
            pass


if getattr(sys, "frozen", False):
    _BUNDLE_DIR: str = sys._MEIPASS          # type: ignore[attr-defined]
    # $APPIMAGE points to the .appimage file itself when running as one.
    _appimage = os.environ.get("APPIMAGE")
    # Install dir = where the exe / AppImage lives = the update target.
    _INSTALL_DIR: str = os.path.dirname(_appimage) if _appimage else os.path.dirname(sys.executable)
    # User data lives in a stable per-user dir, migrated from the old
    # in-install location once, so updates can't wipe characters / prices.
    _APP_DIR = _user_data_dir()
    try:
        os.makedirs(_APP_DIR, exist_ok=True)
        _migrate_legacy_data(_INSTALL_DIR, _APP_DIR)
    except Exception:
        _APP_DIR = _INSTALL_DIR   # fall back to old behavior if data dir is unusable
    sys.path.insert(0, _BUNDLE_DIR)
else:
    _BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    _INSTALL_DIR = _BUNDLE_DIR
    _APP_DIR = _BUNDLE_DIR

os.environ.setdefault("EVE_APP_DIR", _APP_DIR)
os.environ.setdefault("EVE_INSTALL_DIR", _INSTALL_DIR)
os.environ.setdefault("EVE_BUNDLE_DIR", _BUNDLE_DIR)

# console=False in PyInstaller sets sys.stdout/stderr to None. Redirect to a
# rotating log file next to the .exe so tracebacks survive (uvicorn's log
# formatter also calls stream.isatty() which would crash on None).
if getattr(sys, "frozen", False) and sys.stdout is None:
    _log_path = os.path.join(_APP_DIR, "eve_retroindustry.log")
    try:
        _log_file = open(_log_path, "a", buffering=1, encoding="utf-8")
    except Exception:
        _log_file = open(os.devnull, "w")
    sys.stdout = _log_file
    sys.stderr = _log_file
    setattr(_log_file, "isatty", lambda: False)


# ---------------------------------------------------------------------------
# Uvicorn server thread
# ---------------------------------------------------------------------------

class _ServerThread(threading.Thread):
    def __init__(self, port: int) -> None:
        super().__init__(daemon=True)
        from app.web.main import app as _app
        import uvicorn
        self._server = uvicorn.Server(
            uvicorn.Config(_app, host="127.0.0.1", port=port, log_level="warning")
        )

    def run(self) -> None:
        import asyncio
        asyncio.run(self._server.serve())

    def stop(self) -> None:
        self._server.should_exit = True


def _wait_for_server(port: int, timeout: float = 15.0) -> bool:
    """Poll TCP until the server accepts connections (or timeout)."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            import time as _t
            _t.sleep(0.1)
    return False


def _patch_qt_clipboard() -> None:
    """Fix the clipboard in the pywebview Qt backend (pywebview 6.x + PyQt6 6.11).

    Two problems that made "Copy" in the Shopping List crash the app on Linux:
      1. JS access to the clipboard is disabled by default → execCommand('copy') fails.
      2. onFeaturePermissionRequested calls setFeaturePermission(url, feature, int),
         but PyQt6 6.11 requires the PermissionPolicy enum → TypeError crashes the app
         the moment the page calls navigator.clipboard.writeText() on a secure
         origin (our http://127.0.0.1). That is exactly what broke copying the list.

    We patch the bundled pywebview (running in the AppImage) before webview.start().
    Everything is in try/except - a failed patch must not crash app startup.
    """
    try:
        from webview.platforms.qt import BrowserView
        from qtpy.QtWebEngineWidgets import QWebEnginePage, QWebEngineSettings
    except Exception as exc:  # pragma: no cover
        print(f"[clipboard-patch] skipped: {exc!r}", file=sys.stderr)
        return

    WebPage = getattr(BrowserView, "WebPage", None)
    if WebPage is None:
        return

    attr = QWebEngineSettings.WebAttribute
    _orig_init = WebPage.__init__

    def _init(self, parent=None, profile=None):
        _orig_init(self, parent, profile)
        try:
            self.settings().setAttribute(attr.JavascriptCanAccessClipboard, True)
            self.settings().setAttribute(attr.JavascriptCanPaste, True)
        except Exception:
            pass

    def _on_perm(self, url, feature):
        try:
            policy = QWebEnginePage.PermissionPolicy
            media = (
                QWebEnginePage.Feature.MediaAudioCapture,
                QWebEnginePage.Feature.MediaVideoCapture,
                QWebEnginePage.Feature.MediaAudioVideoCapture,
            )
            granted = policy.PermissionGrantedByUser if feature in media else policy.PermissionDeniedByUser
            self.setFeaturePermission(url, feature, granted)
        except Exception:
            pass

    WebPage.__init__ = _init
    if hasattr(WebPage, "onFeaturePermissionRequested"):
        WebPage.onFeaturePermissionRequested = _on_perm


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _smoke_test() -> int:
    """Headless self-test of the *packaged* binary (no GUI window).

    Boots the bundled server, confirms a page renders, and imports the Qt /
    pywebview backend - catching PyInstaller packaging failures (missing hidden
    imports, data files, DLLs) that source-level tests can't see. Returns a
    process exit code. Triggered by ``--smoke`` or ``EVE_SMOKE=1``.
    """
    import urllib.request

    port = 8000
    srv = _ServerThread(port)
    srv.start()
    if not _wait_for_server(port):
        print("SMOKE FAIL: server did not start within 15 s", file=sys.stderr)
        return 1
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/about", timeout=10) as r:
            code = r.getcode()
        if code != 200:
            print(f"SMOKE FAIL: /about returned {code}", file=sys.stderr)
            return 1
    except Exception as exc:
        print(f"SMOKE FAIL: request failed: {exc!r}", file=sys.stderr)
        return 1

    # Import the GUI backend the way main() does - surfaces bundling gaps
    # without needing a display (import only, no widgets created). Skippable via
    # EVE_SMOKE_NO_GUI, because importing QtWebEngine on a headless Linux CI
    # runner pulls system Qt libs that aren't present there.
    if not os.environ.get("EVE_SMOKE_NO_GUI"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            import PyQt6.QtCore  # noqa: F401
            import PyQt6.QtWebEngineWidgets  # noqa: F401
            import webview  # noqa: F401
            import webview.platforms.qt  # noqa: F401
        except Exception as exc:
            print(f"SMOKE FAIL: Qt/pywebview backend import failed: {exc!r}", file=sys.stderr)
            return 1
    else:
        print("SMOKE: skipping GUI-backend import (EVE_SMOKE_NO_GUI)", flush=True)

    print("SMOKE OK", flush=True)
    return 0


# ---------------------------------------------------------------------------
# Webview browser profile
# ---------------------------------------------------------------------------

def _describe_path(path: str | None) -> str:
    """Facts about a path, for the log. Enough to tell the failure modes apart:
    a leftover file, a broken junction, a stat that is denied, or nothing there."""
    if not path:
        return "path=None"
    try:
        os.lstat(path)
    except OSError as exc:
        return f"lexists=0 lstat={exc!r}"
    try:
        return (f"lexists=1 isdir={os.path.isdir(path)} islink={os.path.islink(path)}"
                f" exists={os.path.exists(path)} w_ok={os.access(path, os.W_OK)}")
    except OSError as exc:
        return f"lexists=1 stat_failed={exc!r}"


def _is_storage_path_error(exc: BaseException, storage_dir: str | None) -> bool:
    """True for pywebview's storage-path rejection, and nothing else.

    It raises WebViewException("Storage path <path> is not writable") - matching
    on both the wording and our own path keeps the retry from swallowing an
    unrelated GUI failure.
    """
    if not storage_dir:
        return False
    msg = str(exc)
    return "storage path" in msg.lower() and storage_dir in msg


def _prepare_webview_storage(app_dir: str) -> str | None:
    """Return a directory pywebview will accept for its browser profile, or None.

    pywebview re-validates storage_path itself: it stats the path, calls
    os.makedirs() UNGUARDED when the stat fails, and rewrites any resulting
    OSError into "Storage path ... is not writable" - throwing the real error
    away. A Windows user hit exactly that and the app died before its first
    window: os.path.exists() reported the directory missing while
    CreateDirectory reported it already there (WinError 183). Since our own
    makedirs(exist_ok=True) had just succeeded, that pair means the directory
    was in a transient state (a delete pending from a killed
    QtWebEngineProcess, an antivirus holding the name, a second instance
    starting) rather than plainly broken.

    So: run pywebview's own two checks here first, prove writability with a real
    write, move a poisoned name aside instead of deleting anything, and fall
    back to a private profile rather than failing to start.
    """
    import tempfile

    candidates = [
        os.path.join(app_dir, "webview_data"),
        os.path.join(app_dir, "webview_data-2"),
        os.path.join(tempfile.gettempdir(), "eve-retroindustry-webview"),
    ]
    for path in candidates:
        try:
            # A name that exists but is not a directory (leftover file, broken
            # junction) can never be made usable - rename it aside, never delete:
            # a real profile directory must survive even if we misjudge it.
            if os.path.lexists(path) and not os.path.isdir(path):
                aside = f"{path}.broken"
                for n in range(1, 20):
                    if not os.path.lexists(aside):
                        break
                    aside = f"{path}.broken{n}"
                print(f"[webview] {path} is not a directory "
                      f"({_describe_path(path)}) - moving it to {aside}")
                os.rename(path, aside)

            os.makedirs(path, exist_ok=True)
            # os.access(W_OK) is unreliable for directories on Windows, so
            # actually write something.
            probe = os.path.join(path, ".write_probe")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(probe)

            # The exact two conditions pywebview will check in start().
            if os.path.exists(path) and os.access(path, os.W_OK):
                return path
            print(f"[webview] {path} would fail pywebview's own check: "
                  f"{_describe_path(path)}")
        except Exception as exc:
            print(f"[webview] storage path unusable: {path}: {exc!r}"
                  f" - {_describe_path(path)}")
    print("[webview] no usable profile directory - running with the default "
          "profile (saved form state will not persist)")
    return None


def _default_window_size(webview) -> tuple[int, int]:
    """Startup window size: wide enough to keep the navbar labels.

    The navbar drops to icon-only by measuring real overflow, not a fixed
    breakpoint, so the default has to clear that flip with headroom - the
    measurement is font-dependent and Windows does not lay text out identically
    to Linux. Measured with the current labels (2026-08-22, after "Plan" became
    "Manufacturing"): the bar needs 1845 px of content, so labels survive from
    about 1840 px of window width and are gone at 1820. The previous threshold
    was 1800 with the old label. 1920 keeps roughly the same reserve above the
    flip as the old 1880 did. The height is unchanged.

    Clamped to the screen, because a window wider than the display is worse than
    a few missing labels - it opens with its edges (and possibly the close
    button) off-screen. A laptop at 1366 px simply gets the largest window that
    fits and the icon-only navbar it would have had anyway.
    """
    want_w, want_h = 1920, 1000
    try:
        screens = list(getattr(webview, "screens", None) or [])
        if screens:
            # Widest screen: the user may run a portrait panel alongside a
            # landscape one, and the window opens on the primary/largest.
            sw = max(int(s.width) for s in screens)
            sh = max(int(s.height) for s in screens if int(s.width) == sw)
            want_w = max(900, min(want_w, sw - 40))
            want_h = max(600, min(want_h, sh - 120))
    except Exception as exc:
        print(f"[window] screen size unavailable ({exc!r}) - using {want_w}x{want_h}")
    return want_w, want_h


def _start_webview(webview, storage_dir: str | None) -> None:
    """Run the GUI, surviving a profile directory that goes bad under us.

    _prepare_webview_storage already ran pywebview's checks, but they can still
    fail a moment later - that is exactly what the Windows report showed, where
    our makedirs had just succeeded and pywebview's stat then said the directory
    was missing. pywebview validates storage_path before it touches the GUI or
    the window list, so retrying without a profile is safe here; the only cost is
    that localStorage stops persisting.
    """
    try:
        webview.start(gui="qt", private_mode=False, storage_path=storage_dir)
    except Exception as exc:
        if not _is_storage_path_error(exc, storage_dir):
            raise
        print(f"[webview] {exc!r}")
        print(f"[webview] storage path state: {_describe_path(storage_dir)}")
        print("[webview] starting with the default profile instead - saved form "
              "state will not persist this session")
        webview.start(gui="qt", private_mode=False)


def main() -> None:
    port = 8000
    srv = _ServerThread(port)
    srv.start()

    if not _wait_for_server(port):
        print("ERROR: server did not start within 15 s", file=sys.stderr)
        os._exit(1)

    # Pre-import the Qt backend so any missing-Qt issue surfaces here with a
    # readable traceback instead of cascading through pywebview's silent
    # backend-fallback logic.
    try:
        import PyQt6.QtCore  # noqa: F401
        import PyQt6.QtWebEngineWidgets  # noqa: F401
        import webview.platforms.qt  # noqa: F401
    except Exception as exc:  # pragma: no cover - surfaces at startup only
        print(f"ERROR: Qt backend failed to load: {exc!r}", file=sys.stderr)
        raise

    _patch_qt_clipboard()

    import webview

    url = f"http://127.0.0.1:{port}"
    win_w, win_h = _default_window_size(webview)
    # Not maximized: keep it a normal, movable/resizable window.
    window = webview.create_window(
        title="EVE Retroindustry",
        url=url,
        width=win_w,
        height=win_h,
        min_size=(900, 600),
    )

    def on_closed() -> None:
        srv.stop()

    window.events.closed += on_closed

    # Use PyQt6 + QtWebEngine on both Linux and Windows - self-contained
    # bundled Chromium, no runtime dependency on system webkit2gtk or
    # Edge WebView2 / pythonnet. The default Windows backend tries to
    # load Python.Runtime.dll through pythonnet which silently corrupts
    # under PyInstaller on some user machines ("Failed to resolve
    # Python.Runtime.Loader.Initialize").
    #
    # private_mode=False + storage_path: pywebview defaults to an
    # off-the-record browser profile, which wipes localStorage on every
    # exit - the plan page's "recently used stations/blueprints" and saved
    # form state silently vanished between sessions. Persist the profile
    # next to eve_cache.db (writable app dir).
    _start_webview(webview, _prepare_webview_storage(_APP_DIR))

    srv.join(timeout=3)
    os._exit(0)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    if os.environ.get("EVE_SMOKE") or "--smoke" in sys.argv:
        os._exit(_smoke_test())
    main()
