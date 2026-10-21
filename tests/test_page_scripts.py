"""Every inline script on every page parses.

A page whose script has a syntax error renders fine and passes every HTML
test - and then nothing on it works. Found that way: a second
`let lastMap` stopped the whole map page, and no test noticed. Needs Node.js
(GitHub's runners have it); skipped where it is missing.
"""
from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from tests.test_smoke import ROUTES

NODE = shutil.which("node")
_SCRIPT = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S | re.I)


@pytest.mark.skipif(not NODE, reason="Node.js is not installed")
@pytest.mark.parametrize("path", ROUTES + ["/sov"])
def test_inline_scripts_parse(client, path, tmp_path, monkeypatch, app_module):
    async def none(conn, corp_ids):
        return {}
    monkeypatch.setattr(app_module, "_corp_alliance_ids", none)
    html = client.get(path).text
    checked = 0
    for i, m in enumerate(_SCRIPT.finditer(html)):
        attrs = m.group("attrs").lower()
        if "src=" in attrs or "json" in attrs:
            continue
        f = tmp_path / f"s{i}.js"
        f.write_text(m.group("body"), encoding="utf-8")
        r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, f"{path}, script {i}:\n{r.stderr[:800]}"
        checked += 1
    assert checked, f"no inline script found on {path}"
