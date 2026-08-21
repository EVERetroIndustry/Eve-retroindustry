"""Unit tests for pure calculation logic (no DB / no network)."""
import sqlite3


# ── planner ────────────────────────────────────────────────────────────────
def test_format_duration():
    from app.manufacturing.planner import format_duration
    assert format_duration(0) == "-"
    assert format_duration(-5) == "-"
    assert format_duration(60) == "1m"
    assert format_duration(3600) == "1h"
    assert format_duration(3661) == "1h 1m"
    assert format_duration(90061) == "1d 1h 1m"
    assert format_duration(86400) == "1d"


def test_calc_job_time():
    from app.manufacturing.planner import calc_job_time
    # No bonuses → base time × runs.
    assert calc_job_time(1000, runs=1, te=0, industry_level=0, adv_industry_level=0) == 1000
    assert calc_job_time(1000, runs=3, te=0, industry_level=0, adv_industry_level=0) == 3000
    # Blueprint TE 10 % → ×0.9.
    assert calc_job_time(1000, runs=1, te=10, industry_level=0, adv_industry_level=0) == 900
    # Industry V (-20 %) applies to manufacturing but NOT reactions.
    assert calc_job_time(1000, 1, 0, 5, 0) == 800
    assert calc_job_time(1000, 1, 0, 5, 0, is_reaction=True) == 1000


def test_calc_job_time_rounds_the_total_not_each_run():
    """Modifiers apply to base_time × runs, rounded once.

    Rounding per run and then multiplying by runs multiplies the rounding error by
    the run count. Real case that exposed it: Crystalline Carbonide Armor Plate,
    5625 runs, 17 s/run after TE+skills.
      per-run: round(17 × 0.96) × 5625 = 16 × 5625 = 90 000 s  (-5.88 %, not -4 %)
      total  : round(17 × 5625 × 0.96)              = 91 800 s  (-4.00 %, correct)
    """
    from app.manufacturing.planner import calc_job_time
    assert calc_job_time(17, runs=5625, te=0, industry_level=0, adv_industry_level=0,
                         implant_time_pct=4) == 91800
    # BX-801 on the same job must not round away to nothing.
    assert calc_job_time(17, 5625, 0, 0, 0, implant_time_pct=1) == 94669
    assert calc_job_time(17, 5625, 0, 0, 0) == 95625
    # A fractional per-run time must not be lost: 100 × 0.97 = 97 s/run exactly,
    # but 100 × 0.985 = 98.5 → per-run rounding would drift on every run.
    assert calc_job_time(100, runs=1000, te=0, industry_level=0, adv_industry_level=0,
                         science_skill_mult=0.985) == 98500
    # Single-run jobs are unaffected by the change (regression guard).
    assert calc_job_time(1000, 1, 10, 5, 5, implant_time_pct=4) == round(1000 * 0.9 * 0.8 * 0.85 * 0.96)


def test_calc_job_time_implant():
    from app.manufacturing.planner import (
        calc_job_time, MFG_IMPLANTS, MFG_IMPLANT_PCTS,
    )
    # BX-804 (-4 %) on its own.
    assert calc_job_time(1000, 1, 0, 0, 0, implant_time_pct=4) == 960
    # Stacks multiplicatively with Industry V: 1000 × 0.80 × 0.96.
    assert calc_job_time(1000, 1, 0, 5, 0, implant_time_pct=4) == 768
    # Reactions are unaffected - the attribute is "Manufacturing Time Bonus".
    assert calc_job_time(1000, 1, 0, 0, 0, implant_time_pct=4, is_reaction=True) == 1000
    # Default = no implant, so existing callers keep their numbers.
    assert calc_job_time(1000, 1, 0, 0, 0) == calc_job_time(1000, 1, 0, 0, 0, implant_time_pct=0)
    # The BX family really is 1/2/4 % - there is no BX-805.
    assert MFG_IMPLANT_PCTS == frozenset({1.0, 2.0, 4.0})
    assert {n for n, _p in MFG_IMPLANTS.values()} == {"BX-801", "BX-802", "BX-804"}


# ── security multiplier ──────────────────────────────────────────────────────
def test_security_multiplier():
    from app.web.location_resolver import security_multiplier
    assert security_multiplier(None) == 1.0        # unknown → highsec fallback
    assert security_multiplier(0.9) == 1.0         # highsec
    assert security_multiplier(0.4) == 1.9         # lowsec, manufacturing
    assert security_multiplier(-0.1) == 2.1        # null, manufacturing
    assert security_multiplier(0.4, is_reaction=True) == 1.0
    assert security_multiplier(-0.1, is_reaction=True) == 1.1


# ── external-link allowlist ──────────────────────────────────────────────────
def test_external_host_allowed(app_module):
    f = app_module._external_host_allowed
    assert f("https://github.com/x")
    assert f("https://api.github.com/x")           # subdomain of allowed host
    assert f("https://esi.evetech.net/latest")
    assert f("https://ko-fi.com/retrovisor")
    assert not f("https://evil.example.com")
    assert not f("https://github.com.attacker.com")  # not a real subdomain
    assert not f("file:///etc/passwd")
    assert not f("javascript:alert(1)")


# ── best public-contract price (single vs bundle) ────────────────────────────
def _contracts_db():
    from app.web import contracts_helper
    conn = sqlite3.connect(":memory:")
    contracts_helper.ensure_public_contract_tables(conn)
    return conn, contracts_helper


def _add_contract(conn, cid, price, items):
    conn.execute(
        "INSERT INTO public_contracts (contract_id, region_id, type, price) "
        "VALUES (?,?, 'item_exchange', ?)", (cid, 10000002, price))
    for type_id, qty, incl in items:
        conn.execute(
            "INSERT INTO public_contract_items (contract_id, type_id, quantity, is_included) "
            "VALUES (?,?,?,?)", (cid, type_id, qty, incl))
    conn.commit()


def test_best_contract_price_prefers_single():
    conn, helper = _contracts_db()
    _add_contract(conn, 1, 100.0, [(34, 10, 1)])                 # single → 10/unit
    _add_contract(conn, 2, 50.0, [(34, 5, 1), (35, 1, 1)])       # bundle → 10/unit
    best = helper.best_contract_price(conn, 10000002, 34)
    assert best is not None
    assert best["is_bundle"] is False
    assert best["price"] == 10.0
    assert best["single_count"] == 1


def test_best_contract_price_bundle_fallback():
    conn, helper = _contracts_db()
    _add_contract(conn, 2, 50.0, [(34, 5, 1), (35, 1, 1)])       # only a bundle exists
    best = helper.best_contract_price(conn, 10000002, 34)
    assert best is not None
    assert best["is_bundle"] is True


# ── rig ↔ product applicability (authoritative EVE Ref group map) ────────────
def test_rig_applies_to_product(app_module):
    from app.web.industry_helper import rig_applies_to_product as applies
    conn = app_module.get_conn()
    try:
        def tid(name):
            r = conn.execute("SELECT type_id FROM sde_types WHERE name=? AND published=1",
                             (name,)).fetchone()
            return r[0] if r else None

        def rig(group_id):
            r = conn.execute("SELECT type_id FROM sde_types WHERE group_id=? AND published=1 "
                             "ORDER BY type_id LIMIT 1", (group_id,)).fetchone()
            return r[0] if r else None

        r_large_ship = rig(1828)   # Basic Large Ship manufacturing rig
        r_equipment = rig(1816)    # Equipment manufacturing rig
        r_drone = rig(1822)        # Drone & Fighter manufacturing rig
        hyperion = tid("Hyperion")            # Battleship
        drone_amp = tid("Drone Damage Amplifier II")  # a MODULE (drone fitting)
        warrior = tid("Warrior II")           # a combat drone

        assert None not in (r_large_ship, r_equipment, r_drone, hyperion, drone_amp, warrior)
        # Battleship: large-ship rig applies, equipment rig does not.
        assert applies(conn, r_large_ship, hyperion) is True
        assert applies(conn, r_equipment, hyperion) is False
        # A drone-damage MODULE is Equipment, not a Drone - the old heuristic got this wrong.
        assert applies(conn, r_equipment, drone_amp) is True
        assert applies(conn, r_drone, drone_amp) is False
        # An actual drone is bonused by the Drone rig.
        assert applies(conn, r_drone, warrior) is True
    finally:
        conn.close()


def test_best_contract_price_none_when_absent():
    conn, helper = _contracts_db()
    _add_contract(conn, 1, 100.0, [(99, 10, 1)])                 # different product
    assert helper.best_contract_price(conn, 10000002, 34) is None


# ── manual ME must reach the Materials tab, not just the job steps ────────────
def test_build_plan_honours_me_override(tmp_path):
    """A typed-in ME must drive `materials`.

    build_plan re-resolves the BOM itself, so without me_override it derived ME
    from the owned blueprint only (0 when none) and the Materials tab disagreed
    with the Manufacturing steps. Wasp II needs 6 Morphite/run; at ME 7 in one
    batched job of 10 runs EVE needs ceil(10 × 6 × 0.93) = 56, not 60.
    """
    import os
    from app.manufacturing.planner import build_plan

    sde = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sde_base.db")
    WASP_II, MORPHITE = 2436, 11399

    def morphite(me_override):
        plan = build_plan(
            product_type_id=WASP_II, quantity=10, location_id=60003760,
            available_assets={}, blueprints=[], db_path=sde, mode="full",
            runs_per_job=None, me_override=me_override,
        )
        return next(m.required for m in plan.materials if m.type_id == MORPHITE)

    assert morphite(None) == 60      # no blueprint, no override → ME 0
    assert morphite(0) == 60
    assert morphite(7) == 56         # the in-game-verified number from v0.5.7
    assert morphite(10) == 54        # ceil(10 × 6 × 0.90)


def test_empty_form_field_falls_back_to_default_not_empty_string():
    """Guards the reason runs_per_job needs client-side normalisation.

    FastAPI treats a blank form value as missing and substitutes the field
    default, so the backend can never see "" - "empty = one batched job" has to
    be sent as the explicit 0. If this ever starts returning "", the normalising
    JS in plan.html can go away.
    """
    from fastapi import FastAPI, Form
    from fastapi.testclient import TestClient

    probe = FastAPI()

    @probe.post("/p")
    def _p(rpj: str = Form("1")):
        return {"rpj": rpj}

    c = TestClient(probe)
    assert c.post("/p", data={"rpj": ""}).json()["rpj"] == "1"
    assert c.post("/p", data={"rpj": "0"}).json()["rpj"] == "0"
