"""scripts/update_website_fallback.py - the download page's hardcoded fallback.

The page fetches version/date/sizes live from the GitHub API, but the values
baked into the HTML are what a visitor sees when that fetch does not happen -
no JS, a blocked API, a link-preview crawler. Reported: the site said v0.11.08
after v0.11.09 had already been tagged, because updating it was a manual step
someone had to remember. These tests pin down the script CI now runs
automatically after every release.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "update_website_fallback.py"

SAMPLE_HTML = """<html><body>
<span class="chip" id="chip-ver">v0.9.8</span>
<span class="v gold" id="dl-ver">v0.9.8</span>
<span class="v" id="dl-date">14 Aug 2026</span>
<span class="v" id="dl-size">130.7 MB &middot; installer</span>
<div class="alt"><a href="#">Windows portable</a><span class="sz">189.0 MB</span></div>
<div class="alt"><a href="#">Linux AppImage</a><span class="sz">210.8 MB</span></div>
<div class="alt"><a href="#">Linux portable</a><span class="sz">328.7 MB</span></div>
<div class="alt"><a href="#">Android (experimental)</a><span class="sz">32.7 MB</span></div>
</body></html>"""

RELEASE = {
    "tagName": "v0.11.09",
    "publishedAt": "2026-08-24T09:03:00Z",
    "assets": [
        {"name": "EVE_Retroindustry-v0.11.09-win64-setup.zip", "size": 136_812_994},
        {"name": "EVE_Retroindustry-v0.11.09-win64-portable.zip", "size": 197_517_947},
        {"name": "EVE_Retroindustry-v0.11.09-linux.AppImage", "size": 222_419_448},
        {"name": "EVE_Retroindustry-v0.11.09-linux-portable.zip", "size": 347_181_940},
        {"name": "EveRetroindustry.apk", "size": 34_524_156},
        {"name": "sde_base.db", "size": 6_602_752},
        {"name": "version.json", "size": 161},
    ],
}


def _run(tmp_path, html=SAMPLE_HTML, release=RELEASE):
    html_path = tmp_path / "index.html"
    html_path.write_text(html, encoding="utf-8")
    release_path = tmp_path / "release.json"
    release_path.write_text(json.dumps(release), encoding="utf-8")
    result = subprocess.run([sys.executable, str(SCRIPT), str(html_path), str(release_path)],
                            capture_output=True, text=True)
    return result, html_path.read_text(encoding="utf-8")


def test_every_hardcoded_value_is_replaced(tmp_path):
    result, out = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'id="chip-ver">v0.11.09<' in out
    assert 'id="dl-ver">v0.11.09<' in out
    assert 'id="dl-date">24 Aug 2026<' in out
    assert 'id="dl-size">130.5 MB · installer<' in out
    assert '>Windows portable</a><span class="sz">188.4 MB</span>' in out
    assert '>Linux AppImage</a><span class="sz">212.1 MB</span>' in out
    assert '>Linux portable</a><span class="sz">331.1 MB</span>' in out
    assert '>Android (experimental)</a><span class="sz">32.9 MB</span>' in out
    # Old values must not survive anywhere.
    assert "v0.9.8" not in out and "14 Aug 2026" not in out


def test_the_date_is_zero_padded_like_the_pages_own_live_fetch(tmp_path):
    """toLocaleDateString("en-GB", {day:"2-digit", ...}) zero-pads the day - a
    single-digit day must match that, not read "3 Aug"."""
    release = dict(RELEASE, publishedAt="2026-08-03T09:03:00Z")
    _, out = _run(tmp_path, release=release)
    assert 'id="dl-date">03 Aug 2026<' in out


def test_running_twice_on_the_same_release_changes_nothing_further(tmp_path):
    _, once = _run(tmp_path)
    html_path = tmp_path / "index.html"
    html_path.write_text(once, encoding="utf-8")
    result2 = subprocess.run(
        [sys.executable, str(SCRIPT), str(html_path), str(tmp_path / "release.json")],
        capture_output=True, text=True)
    assert result2.returncode == 0
    assert html_path.read_text(encoding="utf-8") == once


def test_a_missing_asset_leaves_that_one_value_untouched_and_warns(tmp_path):
    """A release missing the Android build (rare, but possible) must not corrupt
    the other rows or crash - it should say why the one value stayed put."""
    release = dict(RELEASE, assets=[a for a in RELEASE["assets"] if ".apk" not in a["name"]])
    result, out = _run(tmp_path, release=release)
    assert result.returncode == 0
    assert "no release asset matched" in result.stderr
    assert '>Android (experimental)</a><span class="sz">32.7 MB</span>' in out  # unchanged
    assert 'id="dl-ver">v0.11.09<' in out                                       # rest still updated


def test_markup_that_no_longer_matches_warns_instead_of_failing_silently(tmp_path):
    html = SAMPLE_HTML.replace('id="dl-ver"', 'id="download-version"')
    result, out = _run(tmp_path, html=html)
    assert result.returncode == 0
    assert "pattern not found" in result.stderr


def test_missing_arguments_exit_with_an_error(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 2
