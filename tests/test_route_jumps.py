"""Jump counts are kept until the gate network changes.

Fractured Skies (2026-11-10) opened Renyn - Jufvitte and Ienakkamon - T-LIWS;
counts saved before that were kept "permanently" and could stay too long.
"""
from __future__ import annotations

import sqlite3


def _db(M, links):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sde_stargate_links (a INTEGER, b INTEGER, PRIMARY KEY (a, b))")
    conn.executemany("INSERT INTO sde_stargate_links VALUES (?,?)", links)
    M.ensure_route_jump_table(conn)
    return conn


def test_counts_from_before_the_expansion_are_not_used(app_module):
    M = app_module
    conn = _db(M, [(1, 2)])
    conn.execute("INSERT INTO route_jump_cache VALUES (1, 9, 12, ?)",
                 (M._GATE_NETWORK_CHANGED - 3600,))
    conn.commit()
    M.save_route_jumps(conn, 1, {8: 4})
    M.ensure_route_jump_table(conn)
    assert M.load_route_jumps(conn, 1, [8, 9]) == {8: 4}


def test_new_gates_in_the_sde_forget_every_count(app_module):
    M = app_module
    conn = _db(M, [(1, 2)])
    M.save_route_jumps(conn, 1, {8: 4})
    M.ensure_route_jump_table(conn)
    assert M.load_route_jumps(conn, 1, [8]) == {8: 4}       # same network: kept
    conn.execute("INSERT INTO sde_stargate_links VALUES (2, 8)")
    M.ensure_route_jump_table(conn)
    assert M.load_route_jumps(conn, 1, [8]) == {}


# ── asking ESI: POST /route, and "no route" is an answer ─────────────────────
# GET /route/{a}/{b}/ left the ESI spec at compatibility date 2025-09-30 and
# only kept answering under the legacy /latest/ prefix (checked live
# 2026-11-20: 404 at the root under both 2026-07-17 and 2026-11-19).

import ast
import asyncio
from pathlib import Path

import pytest


class _R:
    def __init__(self, status, payload=None):
        self.status_code, self._p = status, payload

    def json(self):
        return self._p


class _Client:
    def __init__(self, answer):
        self.answer, self.sent = answer, []

    async def post(self, url, json=None, **kw):
        self.sent.append((url, json))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.mark.parametrize("answer, jumps", [
    (_R(200, {"route": [30000142, 30000144, 30002187]}), 2),
    (_R(200, {"route": [30000142]}), 0),
    (_R(404, {"status": 404, "error": "Not Found",
              "details": [{"message": "No route found"}]}), -1),
    (_R(503, {}), None),
    (_R(200, ["old", "GET", "shape"]), None),
    (OSError("network down"), None),
])
def test_route_jumps(app_module, answer, jumps):
    c = _Client(answer)
    assert asyncio.run(app_module.route_jumps(c, 30000142, 30002187)) == jumps
    url, body = c.sent[0]
    assert url == "https://esi.evetech.net/route/30000142/30002187" and body == {}


def test_no_route_is_kept_like_a_count(app_module):
    M = app_module
    conn = _db(M, [(1, 2)])
    M.save_route_jumps(conn, 1, {31000005: -1, 8: 4})
    assert M.load_route_jumps(conn, 1, [31000005, 8]) == {31000005: -1, 8: 4}


def test_nothing_asks_for_a_route_by_get():
    root = Path(__file__).resolve().parent.parent
    bad = []
    for f in root.joinpath("app").rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "get" \
                    and node.args and "/route/" in ast.unparse(node.args[0]):
                bad.append(f"{f.relative_to(root)}:{node.lineno}")
    assert not bad, bad
