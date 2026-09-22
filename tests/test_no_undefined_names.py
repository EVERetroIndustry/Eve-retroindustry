"""A name that does not exist is a bug that only fires on the path nobody runs.

Two of them were sitting in main.py at once, both swallowed by a bare `except`:

  * `_esi_client` in `_bg_fetch_prices` - so a type the user searched for but
    that had never been priced NEVER got its first price. New items from a game
    patch reach the Prices table through exactly that call.
  * `owned_ids` in `_resolve_corp_container_names` - so corp containers lost
    their custom names from v0.9.21 onward.

Neither showed up in the UI as an error; both just quietly did nothing. pyflakes
finds the whole class in about a second, so it runs as a test.
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ["app", "launcher.py", "scripts", "tests"]


def test_no_undefined_names():
    pyflakes = pytest.importorskip("pyflakes")       # dev-only dependency
    proc = subprocess.run(
        [sys.executable, "-m", "pyflakes", *TARGETS],
        cwd=ROOT, capture_output=True, text=True,
    )
    # pyflakes reports plenty of style noise (unused imports, shadowing); only
    # undefined names are wrong by construction.
    bad = [ln for ln in proc.stdout.splitlines() if "undefined name" in ln]
    assert not bad, "undefined name(s):\n" + "\n".join(bad)
