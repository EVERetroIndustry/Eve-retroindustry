"""Applying a downloaded update without behaving like a dropper.

The old Windows path wrote an `update.bat` into the install directory and ran it
through `cmd.exe`. That script killed processes by image name, copied over the
program folder, wrote the uninstall registry key with `reg.exe`, started the
freshly written binary and then deleted itself. Each step is defensible on its
own; the SEQUENCE is the textbook shape of a dropper - script dropped on disk,
processes terminated, executables replaced, registry touched, self-deleted - and
an unsigned PyInstaller build has no reputation to argue the point with. A tester
running AVG had it flagged mid-update, and code signing is not on the table, so
the behaviour has to go rather than be explained away.

What replaces it: the copy that was just downloaded applies the update itself.
It WAITS for the old process to exit (it never kills anything), copies the tree,
and starts the installed copy. No script on disk, no shell, no taskkill, no
reg.exe, and nothing deletes itself - the next start of the app cleans the
staging directory up, which is an ordinary tidy-up rather than a cover-up.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

EXE_NAME = "EVE_Retroindustry.exe" if sys.platform == "win32" else "EVE_Retroindustry"
STAGING_DIR = "update_staging"


def wait_for_exit(pid: int, timeout: float = 90.0) -> bool:
    """Block until `pid` is gone. True if it exited, False on timeout.

    Waiting, not killing. On Windows that means OpenProcess(SYNCHRONIZE) +
    WaitForSingleObject - note `os.kill(pid, 0)` is NOT a liveness probe there,
    it calls TerminateProcess and would kill the very process we are waiting for.
    """
    if pid <= 0:
        return True
    deadline = time.monotonic() + timeout
    if sys.platform == "win32":
        import ctypes

        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not handle:
            return True                      # already gone (or not ours to see)
        try:
            ms = max(0, int(timeout * 1000))
            return k32.WaitForSingleObject(handle, ms) != WAIT_TIMEOUT
        finally:
            k32.CloseHandle(handle)
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return True
        time.sleep(0.25)
    return False


def copy_tree(src: Path, dst: Path, attempts: int = 20, delay: float = 0.5,
              sleep=time.sleep) -> list[str]:
    """Copy `src` over `dst`, keeping whatever else lives in `dst`.

    Never deletes anything that is not being replaced: user data (eve_cache.db,
    the webview profile) sits in the data directory, but a portable install keeps
    it right here, and an update must not take it with it.

    Files can still be locked for a moment after the app exits - a lingering
    QtWebEngineProcess holds bundled DLLs - so each file is retried instead of
    the whole update failing (or a process being killed to force it).
    """
    failures: list[str] = []
    for root, _dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        target_dir = dst / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            source = Path(root) / name
            target = target_dir / name
            for attempt in range(attempts):
                try:
                    shutil.copy2(source, target)
                    break
                except OSError:
                    if attempt == attempts - 1:
                        failures.append(str(target))
                    else:
                        sleep(delay)
    return failures


def set_installed_version(version: str) -> bool:
    """Keep the Windows uninstall entry honest, but only if the installer made it.

    Written with winreg from this process rather than by shelling out to reg.exe,
    and ONLY when the key already exists: creating it would give portable users a
    bogus entry in Apps & features.
    """
    if sys.platform != "win32" or not version:
        return False
    try:
        import winreg

        path = (r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
                r"\{7F3A9C42-5D18-4B6E-9E2A-1C8B0F4D7A63}_is1")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, version)
        return True
    except OSError:
        return False                          # not an installed copy, or no access


def relaunch(exe: Path) -> bool:
    """Start the updated copy and return whether it went out the door."""
    if not exe.exists():
        return False
    try:
        if sys.platform != "win32":
            exe.chmod(exe.stat().st_mode | 0o111)
        kwargs: dict = {"cwd": str(exe.parent), "close_fds": True}
        if sys.platform == "win32":
            kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                       | subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([str(exe)], **kwargs)
        return True
    except OSError:
        return False


def clean_staging(install_dir: Path) -> None:
    """Remove the staging tree left by an update. Called on a normal start."""
    try:
        staging = Path(install_dir) / STAGING_DIR
        if staging.is_dir():
            shutil.rmtree(staging, ignore_errors=True)
    except OSError:
        pass


def _arg(argv: list[str], name: str) -> str | None:
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def main(argv: list[str]) -> int:
    src = _arg(argv, "--src")
    dst = _arg(argv, "--dst")
    if not src or not dst:
        print("[update] --src and --dst are required", flush=True)
        return 2
    src_p, dst_p = Path(src), Path(dst)
    if not src_p.is_dir():
        print(f"[update] source is missing: {src_p}", flush=True)
        return 3

    pid = int(_arg(argv, "--wait-pid") or 0)
    if not wait_for_exit(pid):
        # It never exited, so its files are still locked. Better to leave the
        # install untouched than to half-replace it (or start killing things).
        print(f"[update] pid {pid} did not exit - nothing was changed", flush=True)
        return 4

    failures = copy_tree(src_p, dst_p)
    version = _arg(argv, "--version")
    if version:
        set_installed_version(version)
    started = relaunch(dst_p / EXE_NAME)
    if failures:
        print(f"[update] {len(failures)} file(s) could not be replaced: "
              f"{failures[:3]}", flush=True)
    print(f"[update] done, relaunched={started}", flush=True)
    return 0 if started and not failures else (5 if failures else 6)
