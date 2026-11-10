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
