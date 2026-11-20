"""Jump counts come from the SDE gate network, not from ESI /route.

ESI's GET /route/{a}/{b}/ left the spec at compatibility date 2025-09-30 and
kept answering only under the legacy /latest/ prefix - still routing over the
gate network from before Fractured Skies. Its replacement, POST /route, is one
call per destination out of a 3600-token / 15 min bucket shared by the IP: on
a real account (930 asset systems) the second character ran into 429 and
five-minute waits.

A breadth-first walk over sde_stargate_links answers every system at once in
about a millisecond. Checked on that account against ESI POST /route: all 927
answers equal, including the 54 "no route" ones. The pairs below are real ones
from that check.
"""
from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
JITA, AMARR, THERA = 30000142, 30002187, 31000005


@pytest.fixture(scope="module")
def bundle():
    c = sqlite3.connect(f"file:{REPO / 'sde_base.db'}?mode=ro", uri=True)
    yield c
    c.close()


@pytest.mark.parametrize("a, b, jumps", [
    (JITA, JITA, 0),
    (JITA, AMARR, 11),
    (AMARR, JITA, 11),
    (JITA, 30004759, 42),                 # 1DQ1-A
    # over the Renyn - Jufvitte gate Fractured Skies opened; ESI's legacy GET
    # still said 40 and 29 on 2026-11-20
    (30003430, 30004593, 39),
    (30004600, 30004972, 28),
])
def test_jumps_match_esi(app_module, bundle, a, b, jumps):
    assert app_module.gate_jumps_from(bundle, a)[b] == jumps


def test_no_gate_route_is_absent(app_module, bundle):
    """Thera (wormhole space): ESI answers 404 "No route found"."""
    assert THERA not in app_module.gate_jumps_from(bundle, JITA)


def test_one_walk_reaches_known_space(app_module, bundle):
    reached = app_module.gate_jumps_from(bundle, JITA)
    assert len(reached) > 5000


def test_without_a_gate_table_only_the_origin_is_known(app_module):
    assert app_module.gate_jumps_from(sqlite3.connect(":memory:"), JITA) == {JITA: 0}


def test_nothing_asks_esi_for_a_route():
    bad = []
    for f in REPO.joinpath("app").rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in ("get", "post") \
                    and node.args and "/route/" in ast.unparse(node.args[0]):
                bad.append(f"{f.relative_to(REPO)}:{node.lineno}")
    assert not bad, bad
