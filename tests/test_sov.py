"""Sovereignty: the map tables from the SDE, ESI's hubs and campaigns stored
whole or not at all, upgrades entered by hand, and the page that joins them."""
from __future__ import annotations

import asyncio
import io
import os
import sqlite3
import time
import zipfile

import httpx
import pytest
import yaml

from app.web import sov_helper as SOV

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALLY = 99000001
OTHER = 99000002


# ── The map from the SDE ─────────────────────────────────────────────────────

def _sde_zip(tmp_path) -> str:
    files = {
        "npcStations.yaml": {},
        "mapRegions.yaml": {10000001: {"name": {"en": "Region A"}}},
        "mapConstellations.yaml": {20000001: {"name": {"en": "Const A"}, "regionID": 10000001}},
        "mapSolarSystems.yaml": {
            30000001: {"name": {"en": "AAA"}, "constellationID": 20000001, "regionID": 10000001,
                       "securityStatus": -0.3, "starID": 40000001,
                       "position2D": {"x": 1.0, "y": 2.0}, "position": {"x": 1, "y": 2, "z": 3}},
            30000002: {"name": {"en": "BBB"}, "constellationID": 20000001, "regionID": 10000001,
                       "securityStatus": 0.02, "starID": 40000002,
                       "position": {"x": 4, "y": 5, "z": 6}},
        },
        "mapStars.yaml": {40000001: {"solarSystemID": 30000001,
                                     "statistics": {"spectralClass": "A0IV"}},
                          40000002: {"solarSystemID": 30000002,
                                     "statistics": {"spectralClass": "K2 V"}}},
        # Both directions of one gate pair, and a gate to itself.
        "mapStargates.yaml": {
            50000001: {"solarSystemID": 30000001, "destination": {"solarSystemID": 30000002}},
            50000002: {"solarSystemID": 30000002, "destination": {"solarSystemID": 30000001}},
            50000003: {"solarSystemID": 30000002, "destination": {"solarSystemID": 30000002}},
        },
        # Two type ids for one kind (11 and 56023 are both Temperate).
        "mapPlanets.yaml": {
            40000010: {"solarSystemID": 30000001, "typeID": 11},
            40000011: {"solarSystemID": 30000001, "typeID": 56023},
            40000012: {"solarSystemID": 30000001, "typeID": 13},
            40000013: {"solarSystemID": 30000002, "typeID": 2063},
        },
        "mapAsteroidBelts.yaml": {
            40000020: {"solarSystemID": 30000001}, 40000021: {"solarSystemID": 30000001},
        },
        "types.yaml": {
            11: {"groupID": 7, "name": {"en": "Planet (Temperate)"}},
            56023: {"groupID": 7, "name": {"en": "Planet (Temperate)"}},
            13: {"groupID": 7, "name": {"en": "Planet (Gas)"}},
            2063: {"groupID": 7, "name": {"en": "Planet (Plasma)"}},
            34: {"groupID": 18, "name": {"en": "Tritanium"}},
            81615: {"groupID": 4772, "name": {"en": "Cynosural Navigation"},
                    "description": {"en": "Enables a <a href=showinfo:35912>Standup cyno</a>.<br>Twice."}},
        },
    }
    path = tmp_path / "eve-online-static-data-1234567-yaml.zip"
    with zipfile.ZipFile(path, "w") as z:
        for name, body in files.items():
            z.writestr(name, yaml.safe_dump(body))
    return str(path)


def test_the_map_is_imported_from_the_sde(tmp_path):
    import import_sde
    conn = sqlite3.connect(":memory:")
    import_sde.import_map(conn, _sde_zip(tmp_path))
    rows = {r[0]: r[1:] for r in conn.execute(
        "SELECT system_id, constellation_id, region_id, security, star_class, x2d, y2d, belts"
        " FROM sde_map_systems")}
    assert rows[30000001] == (20000001, 10000001, -0.3, "A0IV", 1.0, 2.0, 2)
    assert rows[30000002][3] == "K2 V" and rows[30000002][4] is None and rows[30000002][6] == 0
    # One link for the pair, none for a gate to itself.
    assert conn.execute("SELECT a, b FROM sde_stargate_links").fetchall() == [(30000001, 30000002)]
    # Kinds by the type's name: both Temperate type ids count as one kind.
    assert sorted(conn.execute("SELECT * FROM sde_system_planets")) == [
        (30000001, "Gas", 1), (30000001, "Temperate", 2), (30000002, "Plasma", 1)]
    assert conn.execute("SELECT name FROM sde_regions").fetchall() == [("Region A",)]
    assert conn.execute("SELECT * FROM sde_sov_upgrades").fetchall() == [
        (81615, "Enables a Standup cyno.\nTwice.")]
    assert conn.execute("SELECT * FROM sde_constellations").fetchall() == [
        (20000001, "Const A", 10000001)]


def test_the_bundle_carries_the_map_and_installs_receive_it(app_module):
    main = app_module
    tables = ("sde_regions", "sde_constellations", "sde_map_systems",
              "sde_stargate_links", "sde_system_planets")
    conn = sqlite3.connect(f"file:{os.path.join(REPO, 'sde_base.db')}?immutable=1", uri=True)
    try:
        for t in tables:
            assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] > 0, t
        # The whole of New Eden, not a sample.
        assert conn.execute("SELECT COUNT(*) FROM sde_map_systems").fetchone()[0] > 8000
    finally:
        conn.close()
    # A missing table is what makes an existing install copy them over.
    assert set(tables) <= set(main._SDE_TABLES_TO_REFRESH)
    import scripts.build_sde_base as B
    assert set(tables) <= B.SDE_TABLES


@pytest.mark.parametrize("sec, shown", [
    (0.02, 0.1),           # lowsec must not read as nullsec
    (-0.04, 0.0),          # and not as "-0.0" either
    (-0.36, -0.4),
    (0.0, 0.0),
    (None, None),
])
def test_security_as_the_client_shows_it(sec, shown):
    got = SOV.security_display(sec)
    assert got == shown and str(got) == str(shown)


# ── ESI: whole or nothing ────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    SOV.ensure_sov_tables(conn)
    conn.execute("INSERT INTO sov_map_cache VALUES (30000001, ?, NULL, 1)", (ALLY,))
    conn.execute("INSERT INTO sov_hubs VALUES (1, 30000001, ?, 5.0, NULL, NULL, 1)", (ALLY,))
    conn.commit()
    return conn


def _esi(monkeypatch, answers):
    seen = []

    def handler(req):
        path = req.url.path.strip("/")
        seen.append((path, req.headers.get("X-Compatibility-Date")))
        status, body = answers[path]
        return httpx.Response(status, json=body)
    monkeypatch.setattr(SOV, "esi_client",
                        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return seen


_HUB = {"structure_id": 7, "structure_type_id": 32458, "solar_system_id": 30000002,
        "alliance_id": ALLY, "vulnerability_occupancy_level": 2.5,
        "vulnerable_start_time": "2026-10-21T18:00:00Z",
        "vulnerable_end_time": "2026-10-21T22:00:00Z"}
_MAP = [{"system_id": 30000002, "alliance_id": ALLY}, {"system_id": 30000003, "faction_id": 1}]
_CAMP = [{"campaign_id": 3, "solar_system_id": 30000002, "event_type": "ihub_defense",
          "defender_id": ALLY, "defender_score": 0.6, "attackers_score": 0.4,
          "start_time": "2026-10-21T18:33:54Z", "structure_id": 7}]


def test_a_refresh_replaces_hubs_campaigns_and_the_map(monkeypatch):
    seen = _esi(monkeypatch, {"sovereignty/map": (200, _MAP),
                              "sovereignty/structures": (200, [_HUB]),
                              "sovereignty/campaigns": (200, _CAMP)})
    conn = _db()
    assert asyncio.run(SOV.refresh_sov(conn)) == {"systems": 2, "hubs": 1, "campaigns": 1}
    assert conn.execute("SELECT structure_id, system_id, adm FROM sov_hubs").fetchall() == [
        (7, 30000002, 2.5)]
    assert conn.execute("SELECT system_id FROM sov_map_cache WHERE alliance_id=?",
                        (ALLY,)).fetchall() == [(30000002,)]
    assert conn.execute("SELECT campaign_id FROM sov_campaigns").fetchall() == [(3,)]
    # The map and the hubs exist only under the older compatibility date.
    assert all(d == SOV._SOV_COMPAT_DATE for _p, d in seen)


@pytest.mark.parametrize("broken", ["sovereignty/map", "sovereignty/structures",
                                    "sovereignty/campaigns"])
def test_a_failed_read_keeps_what_was_stored(monkeypatch, broken):
    answers = {"sovereignty/map": (200, _MAP), "sovereignty/structures": (200, [_HUB]),
               "sovereignty/campaigns": (200, _CAMP)}
    answers[broken] = (503, {"error": "down"})
    _esi(monkeypatch, answers)
    conn = _db()
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(SOV.refresh_sov(conn))
    assert conn.execute("SELECT structure_id FROM sov_hubs").fetchall() == [(1,)]
    assert conn.execute("SELECT system_id FROM sov_map_cache").fetchall() == [(30000001,)]


def test_an_empty_map_is_not_an_answer(monkeypatch):
    _esi(monkeypatch, {"sovereignty/map": (200, []), "sovereignty/structures": (200, []),
                       "sovereignty/campaigns": (200, [])})
    conn = _db()
    with pytest.raises(RuntimeError):
        asyncio.run(SOV.refresh_sov(conn))
    assert conn.execute("SELECT COUNT(*) FROM sov_hubs").fetchone()[0] == 1


# ── Joined per system ────────────────────────────────────────────────────────

def _bundle_db() -> sqlite3.Connection:
    """The bundled static data in memory, for the joins."""
    src = sqlite3.connect(f"file:{os.path.join(REPO, 'sde_base.db')}?immutable=1", uri=True)
    conn = sqlite3.connect(":memory:")
    src.backup(conn)
    src.close()
    SOV.ensure_sov_tables(conn)
    return conn


def _system(conn, name):
    return conn.execute("SELECT system_id FROM sde_systems WHERE name=?", (name,)).fetchone()[0]


def test_systems_join_what_every_source_says():
    conn = _bundle_db()
    held, other = _system(conn, "1DQ1-A"), _system(conn, "T5ZI-S")
    conn.executemany("INSERT INTO sov_map_cache VALUES (?,?,NULL,1)",
                     [(held, ALLY), (other, OTHER)])
    conn.execute("INSERT INTO sov_hubs VALUES (9, ?, ?, 4.5, '2026-10-21T18:00:00Z',"
                 " '2026-10-21T22:00:00Z', 1)", (held, ALLY))
    conn.execute("INSERT INTO sov_campaigns VALUES (1, ?, 0, 9, 'ihub_defense', ?, .6, .4,"
                 " '2026-10-21T18:00:00Z', 1)", (held, ALLY))
    up = SOV.upgrade_catalogue(conn)[0]["type_id"]
    SOV.set_manual_upgrades(conn, held, [up])
    from app.character.history_store import ensure_history_tables
    ensure_history_tables(conn)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    old = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 40 * 86400))
    conn.executemany("INSERT INTO mining_ledger VALUES (?,?,?,?,?)",
                     [(1, today, held, 1230, 500), (2, today, held, 1230, 250),
                      (1, old, held, 1230, 9999)])
    rows = SOV.systems_for(conn, [ALLY])
    assert [r["system_id"] for r in rows] == [held], "only the chosen alliance's systems"
    r = rows[0]
    assert (r["hub"], r["adm"], r["region"]) == (True, 4.5, "Delve")
    assert r["campaigns"][0]["event"] == "ihub_defense"
    assert [u["type_id"] for u in r["upgrades"]] == [up]
    assert r["mined"] == {"units": 750, "last": today}, "30 days, every character"
    assert sum(r["planets"].values()) > 0 and r["star_class"]


def test_only_hub_upgrades_can_be_marked():
    conn = _bundle_db()
    sid = _system(conn, "1DQ1-A")
    cat = [u["type_id"] for u in SOV.upgrade_catalogue(conn)]
    assert {4768, 4772, 4838, 4839} == {u["group_id"] for u in SOV.upgrade_catalogue(conn)}
    assert SOV.set_manual_upgrades(conn, sid, [cat[0], 34, cat[1]]) == sorted(cat[:2])
    # Saving again replaces, it does not add.
    assert SOV.set_manual_upgrades(conn, sid, [cat[2]]) == [cat[2]]
    assert conn.execute("SELECT type_id FROM sov_upgrades_manual").fetchall() == [(cat[2],)]


def test_the_region_map_shows_neighbours_and_inner_gates():
    conn = _bundle_db()
    held = _system(conn, "1DQ1-A")
    conn.execute("INSERT INTO sov_map_cache VALUES (?,?,NULL,1)", (held, ALLY))
    rid = conn.execute("SELECT region_id FROM sde_map_systems WHERE system_id=?",
                       (held,)).fetchone()[0]
    d = SOV.region_map(conn, rid)
    ids = {s["id"] for s in d["systems"]}
    assert held in ids and len(ids) > 50, "the whole region, not only what is held"
    assert next(s for s in d["systems"] if s["id"] == held)["alliance_id"] == ALLY
    assert d["links"] and all(a in ids and b in ids for a, b in d["links"])


# ── The page ─────────────────────────────────────────────────────────────────

@pytest.fixture
def held(app_module, monkeypatch):
    m = app_module

    async def mine(conn, corp_ids):
        return {c: ALLY for c in corp_ids}
    monkeypatch.setattr(m, "_corp_alliance_ids", mine)

    async def names(conn, ids):
        return {ALLY: {"name": "Test Alliance", "ticker": "TEST"}}
    monkeypatch.setattr(m.sov_api, "alliance_names", names)
    conn = m.get_conn()
    m.sov_api.ensure_sov_tables(conn)
    sid = _system(conn, "1DQ1-A")
    conn.execute("DELETE FROM sov_map_cache")
    conn.execute("DELETE FROM sov_hubs")
    conn.execute("DELETE FROM sov_upgrades_manual")
    conn.execute("INSERT INTO sov_map_cache VALUES (?,?,NULL,?)", (sid, ALLY, time.time()))
    conn.execute("INSERT INTO sov_hubs VALUES (9, ?, ?, 4.5, '2026-10-21T18:00:00Z',"
                 " '2026-10-21T22:00:00Z', ?)", (sid, ALLY, time.time()))
    conn.commit()
    conn.close()
    yield sid


def test_the_page_lists_the_alliance_systems(client, held):
    html = client.get("/sov").text
    assert 'id="sys-%d"' % held in html and "1DQ1-A" in html
    assert "Test Alliance" in html and "18:00-22:00" in html
    assert "ESI does not publish which upgrades" in html


def test_upgrades_are_saved_from_the_page(client, held):
    r = client.post("/api/sov/upgrades", json={"system_id": held, "type_ids": [81615, 34]})
    assert r.json() == {"ok": True, "type_ids": [81615]}
    assert 'data-upids="81615"' in client.get("/sov").text
    assert client.post("/api/sov/upgrades",
                       json={"system_id": 1, "type_ids": []}).status_code == 404
    assert client.post("/api/sov/upgrades", json={"type_ids": []}).status_code == 400


def test_another_alliance_is_not_served(client, held):
    assert client.get(f"/api/sov/systems?alliance={OTHER}").status_code == 404
    assert client.get(f"/api/sov/systems?alliance={ALLY}").json()["systems"][0]["system_id"] == held


def test_the_refresh_is_a_scheduled_job(client, app_module):
    keys = {j["key"] for j in client.get("/api/activity").json()["jobs"]}
    assert "sov" in keys


# ── Upgrades pasted as text ──────────────────────────────────────────────────

_PASTE = """Hub upgrades, as of today
1DQ1-A\tCynosural Navigation, Pyerite Prospecting Array II
T5ZI-S
  Major Threat Detection Array 3
  power monitoring division 1
Jita - Cynosural Suppression
some note with nothing in it
"""


def test_pasted_text_is_read_by_system(client, held):
    import json as _json
    d = client.post("/api/sov/upgrades/parse", json={"text": "Cynosural Navigation\n" + _PASTE}).json()
    by = {s["name"]: s for s in d["systems"]}
    assert by["1DQ1-A"]["names"] == ["Cynosural Navigation", "Pyerite Prospecting Array 2"]
    assert by["T5ZI-S"]["names"] == ["Major Threat Detection Array 3", "Power Monitoring Division 1"]
    assert by["1DQ1-A"]["held"] and not by["T5ZI-S"]["held"] and not by["Jita"]["held"]
    assert d["orphans"] == ["Cynosural Navigation"], "an upgrade before any system is not guessed"
    assert d["unread"] == ["Hub upgrades, as of today", "some note with nothing in it"]
    assert _json.dumps(d)                       # plain data for the dialog


def test_import_stores_only_held_systems_and_replaces_or_adds(client, held, app_module):
    m = app_module
    client.post("/api/sov/upgrades", json={"system_id": held, "type_ids": [81623]})
    r = client.post("/api/sov/upgrades/import", json={"text": _PASTE, "replace": False}).json()
    assert r["systems"] == 1 and set(r["skipped"]) == {"T5ZI-S", "Jita"}
    conn = m.get_conn()
    marked = lambda: sorted(t for (t,) in conn.execute(
        "SELECT type_id FROM sov_upgrades_manual WHERE system_id=?", (held,)))
    pyerite2 = conn.execute("SELECT type_id FROM sde_types WHERE name="
                            "'Pyerite Prospecting Array 2'").fetchone()[0]
    assert marked() == sorted([81615, 81623, pyerite2])
    client.post("/api/sov/upgrades/import", json={"text": _PASTE, "replace": True})
    assert marked() == sorted([81615, pyerite2])
    assert conn.execute("SELECT COUNT(*) FROM sov_upgrades_manual WHERE system_id != ?",
                        (held,)).fetchone()[0] == 0
    conn.close()


def test_a_system_name_inside_an_upgrade_name_is_not_a_system(monkeypatch):
    # None does today; a future upgrade named after a place must not switch
    # the system its own line belongs to.
    conn = _bundle_db()
    real = SOV.upgrade_catalogue(conn)
    monkeypatch.setattr(SOV, "upgrade_catalogue", lambda c: real + [
        {"type_id": 1, "name": "Jita Stability Generator", "group_id": 4839, "group": "x"}])
    d = SOV.parse_upgrade_text(conn, "1DQ1-A: Jita Stability Generator")
    assert [(s["name"], s["type_ids"]) for s in d["systems"]] == [("1DQ1-A", [1])]


# ── Any region's map ─────────────────────────────────────────────────────────

def test_without_an_alliance_the_map_of_any_region_is_offered(client, app_module, monkeypatch):
    m = app_module

    async def none(conn, corp_ids):
        return {}
    monkeypatch.setattr(m, "_corp_alliance_ids", none)
    monkeypatch.setattr(m.bg, "auto_update_enabled", lambda: False)
    html = client.get("/sov").text
    assert 'id="sov-map-pick"' in html and ">The Forge</option>" in html
    assert 'id="sov-table"' not in html and 'data-tab="list" type="button"' not in html
    # Known space only: no wormhole regions in the picker.
    assert "A-R00001" not in html


def test_the_region_map_says_who_holds_what(client, held, app_module, monkeypatch):
    m = app_module

    async def names(conn, ids):
        return {i: {"name": f"Alliance {i}", "ticker": "T"} for i in ids}
    monkeypatch.setattr(m.sov_api, "alliance_names", names)
    conn = m.get_conn()
    rid = conn.execute("SELECT region_id FROM sde_map_systems WHERE system_id=?",
                       (held,)).fetchone()[0]
    conn.close()
    d = client.get(f"/api/sov/region-map?region_id={rid}").json()
    me = next(s for s in d["systems"] if s["id"] == held)
    assert me["alliance_id"] == ALLY and d["names"][str(ALLY)]["name"] == f"Alliance {ALLY}"
    assert "faction_id" in me


# ── Phase 2: one system in detail ────────────────────────────────────────────

def test_upgrades_carry_ccps_description():
    conn = _bundle_db()
    cyno = next(u for u in SOV.upgrade_catalogue(conn) if u["type_id"] == 81615)
    assert "cynosural field generator" in cyno["description"]
    assert "<" not in cyno["description"], "showinfo links are stripped"
    conn.execute("DROP TABLE sde_sov_upgrades")
    assert SOV.upgrade_catalogue(conn)[0]["description"] == ""   # an older install


def test_upgrades_in_corporation_assets_are_shown_as_read(app_module):
    import json as _json
    conn = _bundle_db()
    held = _system(conn, "1DQ1-A")
    conn.execute("INSERT INTO sov_map_cache VALUES (?,?,NULL,1)", (held, ALLY))
    conn.execute("INSERT INTO sov_hubs VALUES (555, ?, ?, 4.5, NULL, NULL, 1)", (held, ALLY))
    conn.execute("CREATE TABLE corp_assets_cache (corporation_id INTEGER, data_json TEXT,"
                 " cached_at REAL)")
    conn.execute("INSERT INTO corp_assets_cache VALUES (1, ?, 1)", (_json.dumps([
        {"type_id": 81615, "location_id": 555, "location_flag": "StructureActive"},
        {"type_id": 82588, "location_id": 555, "location_flag": "StructureOffline"},
        {"type_id": 81615, "location_id": 999, "location_flag": "StructureActive"},  # not a hub
        {"type_id": 34, "location_id": 555, "location_flag": "StructureActive"},     # not an upgrade
    ]),))
    row = SOV.systems_for(conn, [ALLY])[0]
    assert sorted((u["type_id"], u["state"]) for u in row["detected"]) == [
        (81615, "active"), (82588, "offline")]
    assert row["upgrades"] == [], "detected is kept apart from what the user marked"


def test_the_detail_lists_ores_mined_and_neighbours(client, held, app_module):
    m = app_module
    conn = m.get_conn()
    from app.character.history_store import ensure_history_tables
    ensure_history_tables(conn)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    conn.execute("DELETE FROM mining_ledger")
    conn.executemany("INSERT INTO mining_ledger VALUES (?,?,?,?,?)",
                     [(900000001, today, held, 1230, 400), (900000002, today, held, 1230, 99),
                      (900000001, time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400)),
                       held, 1230, 1),
                      (900000001, today, held, 1228, 7)])
    conn.commit()
    conn.close()
    d = client.get(f"/api/sov/system/{held}").json()
    assert [(t["type_id"], t["units"], t["characters"]) for t in d["mined_types"]] == [
        (1230, 500, 2), (1228, 7, 1)]
    assert d["mined_types"][0]["name"] == "Veldspar"
    assert d["neighbours"] and all(n["name"] for n in d["neighbours"])
    assert client.get("/api/sov/system/30000142").status_code == 404   # Jita: not held


# ── Combat anomalies by band and space ───────────────────────────────────────

@pytest.mark.parametrize("sec, band", [
    (-0.044879, 0), (-0.25, 0), (-0.250001, 1), (-0.45, 1), (-0.5, 2), (-0.7, 3),
    (-0.85, 3), (-0.9, 4), (-1.0, 4), (0.3, None), (None, None),
])
def test_true_security_picks_the_band(sec, band):
    from app.web.sov_anomalies import band as _band
    assert _band(sec) == band


def test_anomalies_follow_band_space_and_what_is_installed():
    from app.web.sov_anomalies import for_system
    a = for_system(-0.9, "Delve", ["Major Threat Detection Array 3", "Cynosural Navigation"])
    assert (a["band"], a["space"]) == ("-0.85 to -1.0", "pirate")
    major3 = next(r for r in a["rows"] if r["upgrade"] == "Major Threat Detection Array 3")
    assert major3["installed"] and major3["count"] == 19
    assert dict(a["installed_total"]) == {"Haven": 8, "Sanctum": 4, "Forsaken Sanctum": 3,
                                          "Forsaken Hub": 2, "Forlorn Hub": 2}
    d = for_system(-0.9, "Oasa", ["Major Threat Detection Array 3", "Minor Threat Detection Array 1"])
    assert d["space"] == "drone"
    assert dict(d["installed_total"])["Drone Horde"] == 10
    assert dict(d["installed_total"])["Drone Gathering"] == 2
    two = for_system(-0.9, "Delve", ["Minor Threat Detection Array 2",
                                     "Minor Threat Detection Array 3"])
    assert dict(two["installed_total"])["Den"] == 4, "two arrays add up"
    # Major 1 is the September 2024 table, not the July one.
    m1 = next(r for r in for_system(-0.1, "Delve", [])["rows"]
              if r["upgrade"] == "Major Threat Detection Array 1")
    assert m1["sites"] == [(2, "Hub"), (3, "Hidden Hub"), (2, "Forsaken Hub")]


def test_the_detail_carries_the_anomalies(client, held):
    client.post("/api/sov/upgrades", json={"system_id": held, "type_ids": [82498]})  # Major 3
    d = client.get(f"/api/sov/system/{held}").json()
    a = d["anomalies"]
    assert a["installed_total"] and any(r["installed"] for r in a["rows"])
    assert "2024-09-18.1" in a["source"]
