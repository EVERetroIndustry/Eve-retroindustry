"""The bundled SDE has to be readable from read-only media.

Reported as "the new crate still is not in the listing" while running the very
release that shipped it. The app was v0.11.28, its AppImage carried SDE build
3532181, and the user's database was still on 3482594.

Cause: an AppImage runs off a SquashFS mount, and `sde_base.db` was built in WAL
mode. Opening a WAL database needs a `-shm` file created beside it, which
read-only media refuses - so `sqlite3.connect()` raised, the caller's `except`
swallowed it, and the message went to a stdout a windowed app does not have.
Every AppImage install was therefore frozen on whatever SDE it first copied,
while fresh installs (a plain file copy, no SQLite) were fine.

Both halves are fixed: the reader passes `immutable=1`, and the bundle is now
built in DELETE journal mode. Each alone is enough; the tests keep both honest.
"""
import os
import sqlite3
import stat
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLED = os.path.join(ROOT, "sde_base.db")


def _header_journal_mode(path) -> str:
    """Journal mode straight out of the SQLite file header (bytes 18 and 19).

    2 = WAL, 1 = rollback journal. PRAGMA journal_mode cannot answer this on an
    immutable connection - it says "delete" whatever the file actually is.
    """
    with open(path, "rb") as f:
        header = f.read(20)
    return "wal" if header[18] == 2 else "rollback"



@pytest.mark.skipif(sys.platform.startswith("win"),
                    reason="POSIX permission bits do not make a directory read-only on Windows")
@pytest.mark.skipif(os.geteuid() == 0 if hasattr(os, "geteuid") else False,
                    reason="root ignores the read-only bit")
def test_refresh_reads_a_wal_bundle_from_read_only_media(app_module, tmp_path):
    """The exact shape of the bug: a WAL bundle on media that cannot be written."""
    m = app_module

    # A bundle in WAL mode, the way every release before this one shipped it.
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    bundle = bundle_dir / "sde_base.db"
    src = sqlite3.connect(BUNDLED)
    dst = sqlite3.connect(str(bundle))
    src.backup(dst)
    dst.execute("PRAGMA journal_mode=WAL")
    dst.commit()
    dst.close()
    src.close()
    assert _header_journal_mode(bundle) == "wal"

    # Real read-only media carries no -wal/-shm sidecars. Leaving the ones
    # SQLite just made would let a plain connect succeed and the test would
    # pass against the very bug it is for - it did, the first time.
    for sidecar in ("-wal", "-shm", "-journal"):
        side = bundle.with_name(bundle.name + sidecar)
        if side.exists():
            side.unlink()

    os.chmod(bundle, stat.S_IRUSR)
    os.chmod(bundle_dir, stat.S_IRUSR | stat.S_IXUSR)   # read-only "media"
    try:
        conn = m._open_bundled_sde(str(bundle))
        assert conn.execute("SELECT COUNT(*) FROM sde_types").fetchone()[0] > 0
        conn.close()
    finally:
        os.chmod(bundle_dir, stat.S_IRWXU)
        os.chmod(bundle, stat.S_IRUSR | stat.S_IWUSR)


def test_shipped_bundle_is_not_in_wal_mode():
    """A WAL database is the wrong thing to ship as read-only payload, whatever
    the reader does about it.

    Read from the file header, not from PRAGMA journal_mode: an immutable
    connection reports "delete" for ANY file, so asking it was worthless - the
    first version of this test passed with a WAL bundle sitting right there.
    """
    assert _header_journal_mode(BUNDLED) != "wal", \
        f"sde_base.db ships in {_header_journal_mode(BUNDLED)} mode"
