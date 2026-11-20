"""Fractured Skies (2026-11-10, SDE 3682781, follow-ups to 3720952) reaches the calculator.

The rig map once dropped a new product group without a word because EVE Ref,
where the generator took group categories from, had not caught up with the
patch: equipment rigs then gave no bonus to the new Synchronizer rigs.
"""
from __future__ import annotations

import os
import sqlite3

from app.web.industry_helper import rig_applies_to_product

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EQUIPMENT_ME_RIG = 43920          # Standup M-Set Equipment Manufacturing ME I
MEDIUM_SHIP_RIG_GROUP = 1826      # Basic Medium Ship
SYNCHRONIZER = 96130              # Small Imperial Navy Synchronizer
ORACLE_NAVY = 95629


def _sde():
    return sqlite3.connect(os.path.join(REPO, "sde_base.db"))


def test_equipment_rigs_bonus_synchronizer_rigs():
    assert rig_applies_to_product(_sde(), EQUIPMENT_ME_RIG, SYNCHRONIZER)


def test_navy_attack_battlecruisers_are_medium_ships():
    conn = _sde()
    rig = conn.execute("SELECT type_id FROM sde_types WHERE group_id=? AND published=1"
                       " LIMIT 1", (MEDIUM_SHIP_RIG_GROUP,)).fetchone()[0]
    assert rig_applies_to_product(conn, rig, ORACLE_NAVY)


def test_synchronizer_production_needs_the_new_skill():
    skills = {r[0] for r in _sde().execute(
        "SELECT t.name FROM sde_blueprint_products p"
        " JOIN sde_blueprint_skills s ON s.blueprint_type_id=p.blueprint_type_id"
        "  AND s.activity='manufacturing'"
        " JOIN sde_types t ON t.type_id=s.skill_type_id"
        " WHERE p.product_type_id=? AND p.activity='manufacturing'", (SYNCHRONIZER,))}
    assert skills == {"Industry", "Synchronization Rigging"}


def test_the_map_has_the_new_gates_and_names():
    conn = _sde()
    assert conn.execute("SELECT name FROM sde_systems WHERE system_id=30002960"
                        ).fetchone()[0] == "Starkman"
    for a, b in ((30004970, 30005308), (30003299, 30045313)):   # Renyn-Jufvitte, T-LIWS-Ienakkamon
        assert conn.execute("SELECT 1 FROM sde_stargate_links WHERE (a=? AND b=?) OR (a=? AND b=?)",
                            (a, b, b, a)).fetchone()


# ── follow-up fixes in SDE 3698585 - 3720952 (2026-11-13 to 11-19) ──────────

def test_z_grade_ore_reprocesses_into_nine_times_the_aberrant_mineral():
    """The launch build had Raspite Z-Grade give 25 Aberrant Raspite per 100;
    CCP raised it to 225 (X-Grade 10 -> 90). Ore is valued by its minerals in
    Deals and the contract appraisal, so a stale table undervalues it ~4x."""
    conn = _sde()
    mats = dict(conn.execute("SELECT material_type_id, quantity FROM sde_ore_reprocess"
                             " WHERE type_id=96286").fetchall())        # Raspite Z-Grade
    assert mats[97512] == 225                                           # Aberrant Raspite
    assert conn.execute("SELECT quantity FROM sde_ore_reprocess"
                        " WHERE type_id=97559 AND material_type_id=97512").fetchone()[0] == 90


def test_medium_republic_fleet_synchronizer_needs_its_trigger_units():
    """Missing from the launch build while Small and Large had it."""
    assert _sde().execute(
        "SELECT quantity FROM sde_blueprint_materials WHERE blueprint_type_id=96156"
        " AND activity='manufacturing' AND material_type_id=25593").fetchone()[0] == 60


def test_the_imperial_navy_webifier_is_buildable():
    conn = _sde()
    assert conn.execute("SELECT product_type_id FROM sde_blueprint_products"
                        " WHERE blueprint_type_id=98846 AND activity='manufacturing'"
                        ).fetchone()[0] == 98845
    assert conn.execute("SELECT name FROM sde_types WHERE type_id=97955"
                        ).fetchone()[0] == "Compact Imperial Navy Stasis Webifier"
