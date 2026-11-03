"""A second manufacturing station, chosen per item.

Capital production is often split: components in one structure, the hull in
another (supercapitals only in a Sotiyo with a Supercapital Shipyard). Every
item starts at the manufacturing station; the ones moved to the second
station take its structure, rigs, security and install fee rate.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess

import pytest

from app.bom.resolver import BOMResolver, StationFacility

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(REPO, "sde_base.db")
WARRIOR_I, WARRIOR_II = 2486, 2488
REVELATION = 19720
NPC_JITA, NPC_AMARR = 60003760, 60008494


def _resolve(**kw):
    r = BOMResolver(DB, **{k: v for k, v in kw.items() if k not in ("mfg", "rxn")})
    try:
        return r.resolve(WARRIOR_II, 100, me=0.0, mfg_facility=kw.get("mfg"),
                         rxn_facility=kw.get("rxn"))
    finally:
        r.close()


def _child(node, type_id):
    return next(c for c in node.children if c.type_id == type_id)


def test_items_moved_to_the_second_station_use_its_bonuses():
    sotiyo_like = StationFacility(structure_pct=10.0)        # a big bonus, easy to see
    base = _resolve()
    split = _resolve(mfg_facility2=sotiyo_like, station2_types={WARRIOR_I})
    # Warrior I (minerals in the thousands, so ME shows) moves; Warrior II stays.
    w1, w1_base = _child(split, WARRIOR_I), _child(base, WARRIOR_I)
    assert split.station == 1 and w1.station == 2
    assert sum(c.quantity for c in w1.children) < sum(c.quantity for c in w1_base.children)
    assert [c.quantity for c in split.children] == [c.quantity for c in base.children]


def test_without_a_second_station_the_choice_changes_nothing():
    base = _resolve()
    same = _resolve(station2_types={WARRIOR_II})            # no facility: ignored
    assert same.station == 1
    assert [c.quantity for c in same.children] == [c.quantity for c in base.children]


def test_the_install_fee_uses_the_second_stations_rate():
    adj = {}
    with sqlite3.connect(DB) as c:
        for (t,) in c.execute("SELECT type_id FROM sde_types LIMIT 50000"):
            adj[t] = 100.0
    one = _resolve(adjusted_prices=adj, rate_mfg=0.05)
    two = _resolve(adjusted_prices=adj, rate_mfg=0.05, mfg_facility2=StationFacility(),
                   station2_types={WARRIOR_II}, rate_mfg2=0.10)
    assert one.job_fee > 0
    assert two.job_fee == pytest.approx(one.job_fee * 2)
    assert _child(two, WARRIOR_I).job_fee == pytest.approx(_child(one, WARRIOR_I).job_fee)


def test_hauling_lists_built_items_that_change_station(app_module):
    m = app_module
    steps = [
        {"jobs": [{"type_id": 1, "station": 1, "inputs": [{"type_id": 34, "name": "Tritanium", "quantity": 5}]}]},
        {"jobs": [{"type_id": 2, "station": 2, "inputs": [
            {"type_id": 1, "name": "Part", "quantity": 7},
            {"type_id": 34, "name": "Tritanium", "quantity": 9}]}]},
    ]
    conn = m.get_conn()
    try:
        out = m._hauling_between_sites(conn, steps, {1: 10, 2: 20, 0: 10},
                                       {"1": "A", "2": "B", "0": "A"})
        same = m._hauling_between_sites(conn, steps, {1: 10, 2: 10, 0: 10},
                                        {"1": "A", "2": "A", "0": "A"})
    finally:
        conn.close()
    assert [(h["name"], h["from"], h["to"], h["quantity"]) for h in out] == [("Part", "A", "B", 7)]
    assert same == []


def test_the_choice_is_remembered_per_product(app_module):
    m = app_module
    conn = m.get_conn()
    try:
        m.plan_station2_save(conn, 777, {1, 2})
        m.plan_station2_save(conn, 778, set())
        assert m.plan_station2_load(conn, 777) == {1, 2}
        assert m.plan_station2_load(conn, 778) == set()
        assert m.plan_station2_load(conn, 779) == set()
    finally:
        conn.close()


def _site(conn, loc, structure, sec):
    from app.web.industry_helper import ensure_industry_tables
    ensure_industry_tables(conn)
    conn.execute("INSERT OR REPLACE INTO station_rigs (location_id, structure_type) VALUES (?,?)",
                 (loc, structure))
    conn.execute("INSERT OR REPLACE INTO location_name_cache (location_id, name, solar_system_id)"
                 " VALUES (?,?,?)", (loc, f"S{loc}", 39_000_000 + loc % 1000))
    conn.execute("INSERT OR REPLACE INTO solar_system_cache (system_id, security_status)"
                 " VALUES (?,?)", (39_000_000 + loc % 1000, sec))
    conn.commit()


def test_shipyard_warnings(app_module):
    from app.web.industry_helper import build_site_warning
    conn = app_module.get_conn()
    try:
        _site(conn, 1_000_000_000_101, "sotiyo", -0.4)
        _site(conn, 1_000_000_000_102, "sotiyo", 0.3)
        _site(conn, 1_000_000_000_103, "azbel", 0.8)
        _site(conn, 1_000_000_000_104, "raitaru", 0.2)
        titan, carrier = 30, 547
        assert build_site_warning(conn, 1_000_000_000_101, titan) is None
        assert "not in null-sec" in build_site_warning(conn, 1_000_000_000_102, titan)
        assert "set up as NPC station" in build_site_warning(conn, NPC_JITA, titan)
        assert build_site_warning(conn, 1_000_000_000_102, carrier) is None
        assert "high-sec" in build_site_warning(conn, 1_000_000_000_103, carrier)
        assert "Raitaru" in build_site_warning(conn, 1_000_000_000_104, carrier)
        assert build_site_warning(conn, NPC_JITA, 25) is None          # a frigate: no rule
    finally:
        conn.close()


def _plan(client, **extra):
    data = {"product": "Warrior II", "qty": "10", "station": str(NPC_JITA), "mode": "full"}
    data.update(extra)
    return client.post("/plan", data=data).text


def test_plan_page_with_a_second_station(client, app_module, tmp_path):
    html = _plan(client, second_station=str(NPC_AMARR), station2_set="1",
                 station2_types=str(WARRIOR_II))
    assert "Second station facility" in html
    assert "onclick=\"setSite(" in html and "Move between stations" in html
    assert "Warrior I" in html
    # Remembered: a plain Calculate keeps Warrior II on station 2.
    again = _plan(client, second_station=str(NPC_AMARR))
    assert re.search(r'"station2_types":\s*\[%d\]' % WARRIOR_II, again)
    # No second station: no switch, no hauling, the choice is kept for later.
    plain = _plan(client)
    assert "onclick=\"setSite(" not in plain and "Move between stations" not in plain
    conn = app_module.get_conn()
    try:
        assert app_module.plan_station2_load(conn, WARRIOR_II) == {WARRIOR_II}
        app_module.plan_station2_save(conn, WARRIOR_II, set())
    finally:
        conn.close()
    node = shutil.which("node")
    if node:
        for i, m in enumerate(re.finditer(r"<script(?P<a>[^>]*)>(?P<b>.*?)</script>", html, re.S)):
            if "src=" in m.group("a") or "json" in m.group("a"):
                continue
            f = tmp_path / f"s{i}.js"
            f.write_text(m.group("b"), encoding="utf-8")
            r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr[:800]
