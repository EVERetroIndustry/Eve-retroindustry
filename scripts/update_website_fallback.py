#!/usr/bin/env python3
"""Update the hardcoded download-card fallback on the website after a release.

The website's index.html fetches version/date/sizes live from the GitHub API on
load, but the values baked into the HTML are what a visitor actually sees when
that fetch does not happen - no JS, a blocked API call, or a link-preview
crawler (Reddit, Discord, ...). Those values have to track the real latest
release, and doing that by hand after each tag is exactly the kind of step that
gets forgotten under a "vydej verzi" request - it did, at least once (site
still said v0.11.08 after v0.11.09 was tagged). Run automatically by
.github/workflows/release.yml right after a release is published; safe to run
by hand too - idempotent, and it warns rather than silently doing nothing if
the page's markup ever changes shape underneath it.

Usage: update_website_fallback.py <site_index_html> <release_json>
  <release_json> = output of:
    gh release view TAG --json tagName,publishedAt,assets
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone


def mb(byte_size: int) -> str:
    return f"{byte_size / 1_048_576:.1f} MB"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    html_path, release_path = argv[1], argv[2]

    html = open(html_path, encoding="utf-8").read()
    rel = json.load(open(release_path, encoding="utf-8"))

    version = rel["tagName"]                       # "v0.11.09"
    assets = {a["name"]: a["size"] for a in rel["assets"]}

    def asset_size(substr: str) -> int | None:
        for name, size in assets.items():
            if substr in name:
                return size
        return None

    published = datetime.fromisoformat(rel["publishedAt"].replace("Z", "+00:00"))
    published = published.astimezone(timezone.utc)
    # Zero-padded day, matching the page's own live-fetch formatting exactly
    # (toLocaleDateString("en-GB", {day:"2-digit", month:"short", year:"numeric"})).
    date_str = published.strftime("%d %b %Y")

    replacements: list[tuple[str, str]] = [
        (r'<span class="chip" id="chip-ver">v[\d.]+</span>',
         f'<span class="chip" id="chip-ver">{version}</span>'),
        (r'<span class="v gold" id="dl-ver">v[\d.]+</span>',
         f'<span class="v gold" id="dl-ver">{version}</span>'),
        (r'<span class="v" id="dl-date">[^<]*</span>',
         f'<span class="v" id="dl-date">{date_str}</span>'),
    ]

    setup_size = asset_size("win64-setup")
    if setup_size is not None:
        # The page's own middle dot is a raw UTF-8 character, not &middot; -
        # match it exactly, or a diff-only-in-encoding shows up on every release.
        replacements.append((
            r'<span class="v" id="dl-size">[^<]*</span>',
            f'<span class="v" id="dl-size">{mb(setup_size)} · installer</span>'))

    for label, substr in (
        ("Windows portable", "win64-portable"),
        ("Linux AppImage", "linux.AppImage"),
        ("Linux portable", "linux-portable"),
        ("Android (experimental)", ".apk"),
    ):
        size = asset_size(substr)
        if size is None:
            print(f"WARNING: no release asset matched {substr!r} - "
                  f"{label} size left untouched", file=sys.stderr)
            continue
        pattern = rf'(>{re.escape(label)}</a><span class="sz">)[^<]*(</span>)'
        replacements.append((pattern, rf'\g<1>{mb(size)}\g<2>'))

    total_hits = 0
    for pattern, repl in replacements:
        html, n = re.subn(pattern, repl, html)
        if n == 0:
            print(f"WARNING: pattern not found in {html_path} - the page's "
                  f"markup may have changed: {pattern[:70]}", file=sys.stderr)
        elif n > 1:
            print(f"WARNING: pattern matched {n} times, expected 1: "
                  f"{pattern[:70]}", file=sys.stderr)
        total_hits += n

    open(html_path, "w", encoding="utf-8").write(html)
    print(f"Updated {total_hits} value(s) on {html_path} to {version} / {date_str}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
