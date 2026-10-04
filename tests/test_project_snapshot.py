"""A saved project is a frozen record.

The point of saving a plan is that it stops moving. Install fees depend on the
system cost index of the day and on the structure's bonus; material cost
depends on the market. Both drift constantly, so a project that recalculated
itself would be worthless as a build order - you could never tell whether a
number changed because you did something or because Jita did.

So: everything the project screen shows about time, ISK and the facility setup
is read out of the snapshot taken when the plan was saved. Ticking a job off
records progress and recalculates nothing.
"""
import json
import sqlite3
import time

import pytest

from app.web import projects_helper as ph


def _plan(total_time_s=3600, job_fee=1_000_000.0, mat_cost=50_000_000.0, buy=20_000_000.0):
    """A plan in the shape the Plan screen posts - trimmed to what is read back."""
    return {
        "product_type_id": 638, "product_name": "Raven", "quantity": 2,
        "mode": "full", "blueprint": {"kind": "BPC", "me": 10, "te": 20, "runs": 1},
        "total_buy": buy,
        "materials": [{"type_id": 34, "name": "Tritanium", "missing": 1000}],
        "manufacturing_steps": [
            {"step": 1, "is_final": False, "total_cost": 1.0, "jobs": [
                {"type_id": 11399, "name": "Morphite", "quantity": 10, "runs": 2,
                 "activity": "manufacturing", "job_duration_seconds": 600,
                 "job_fee": 250_000.0, "inputs": [
                     {"type_id": 34, "name": "Tritanium", "quantity": 500, "is_leaf": True,
                      "activity": ""}]},
                {"type_id": 11400, "name": "Nanotransistors", "quantity": 5, "runs": 1,
                 "activity": "reaction", "job_duration_seconds": 1800,
                 "job_fee": 150_000.0, "inputs": []},
            ]},
            {"step": 2, "is_final": True, "total_cost": 2.0, "jobs": [
                {"type_id": 638, "name": "Raven", "quantity": 2, "runs": 2,
                 "activity": "manufacturing", "job_duration_seconds": total_time_s - 1800,
                 "job_fee": job_fee - 400_000.0, "inputs": []},
            ]},
        ],
        "fees": {
            "total_time_s": total_time_s, "total_time": "1h",
            "total_job_fee": job_fee, "full_mat_cost": mat_cost,
            "mfg_sci": 0.0542, "rxn_sci": 0.0602,
            "facility_tax": 2.5, "rxn_facility_tax": 1.0,
            "mfg_cost_bonus_pct": 5, "rxn_cost_bonus_pct": 0,
            "mfg_me_pct": 12.96, "rxn_me_pct": 2.2,
            "mfg_te_pct": 86.3, "rxn_te_pct": 41.5,
            "sep_rxn_station": True, "solar_system_id": 30004616,
            "rxn_solar_system_id": 30004616,
            "profit_market": 1.0, "profit_stock": 2.0,
            "implant_mfg_pct": 0, "implant_mfg_name": None,
        },
    }


FACILITY = {
    "mfg": {"structure": "Azbel (L-Set)", "rigs": ["T2 ME Capital Ship"]},
    "rxn": {"structure": "Tatara", "rigs": ["T2 Composite Reactor"]},
}


@pytest.fixture
def conn():
    """Projects live in their own four tables and get_project_detail reads
    nothing else, so these tests need no app and no SDE. They also must not use
    the shared test database: the freeze test deletes prices on purpose."""
    c = sqlite3.connect(":memory:")
    ph.ensure_project_tables(c)
    c.execute("CREATE TABLE market_price_cache (type_id INTEGER, sell_price REAL)")
    c.execute("CREATE TABLE sde_blueprint_materials (blueprint_type_id INTEGER, quantity INTEGER)")
    c.execute("INSERT INTO market_price_cache VALUES (34, 5.0)")
    c.execute("INSERT INTO sde_blueprint_materials VALUES (681, 100)")
    c.commit()
    yield c
    c.close()


def _save(conn, name="proj", **kw):
    pid = ph.create_project(conn, name + str(time.time()))
    ph.add_plan_to_project(
        conn, pid, _plan(**kw), "7BX-6F - Construction", 2.5,
        rxn_station_name="C-N4OD VI - Reactions", facility=FACILITY,
        app_version="9.9.9")
    return pid


# ── what gets stored ─────────────────────────────────────────────────────────

def test_the_saved_plan_carries_time_cost_and_both_stations(conn):
    pid = _save(conn)
    plan = ph.get_project_detail(conn, pid)["plans"][0]
    assert plan["total_time_s"] == 3600
    assert plan["total_time"]                       # formatted, not just seconds
    assert plan["total_job_fee"] == 1_000_000.0
    assert plan["full_mat_cost"] == 50_000_000.0
    assert plan["total_buy"] == 20_000_000.0
    assert plan["mfg"]["station"] == "7BX-6F - Construction"
    assert plan["rxn"]["station"] == "C-N4OD VI - Reactions"
    assert plan["app_version"] == "9.9.9"


def test_the_facility_setup_is_recorded_by_name(conn):
    """The plan already stores what the setup DID (cost bonus, ME, TE). This
    stores which setup did it, so the record still reads years later."""
    pid = _save(conn)
    plan = ph.get_project_detail(conn, pid)["plans"][0]
    assert plan["mfg"]["structure"] == "Azbel (L-Set)"
    assert plan["mfg"]["rigs"] == ["T2 ME Capital Ship"]
    assert plan["rxn"]["structure"] == "Tatara"
    # ...alongside the numbers those choices produced
    assert plan["mfg"]["sci"] == 0.0542 and plan["rxn"]["sci"] == 0.0602
    assert plan["mfg"]["tax"] == 2.5 and plan["rxn"]["tax"] == 1.0
    assert plan["mfg"]["me_pct"] == 12.96 and plan["mfg"]["te_pct"] == 86.3
    assert plan["sep_rxn_station"] is True


def test_every_job_keeps_its_own_time_and_fee(conn):
    pid = _save(conn)
    steps = ph.get_project_detail(conn, pid)["steps"]
    by_name = {j["name"]: j for s in steps for j in s["jobs"]}
    assert by_name["Morphite"]["duration_s"] == 600
    assert by_name["Morphite"]["job_fee"] == 250_000.0
    assert by_name["Nanotransistors"]["duration_s"] == 1800
    assert all(j["duration"] for j in by_name.values())


def test_a_step_reports_its_longest_job_not_a_step_duration(conn):
    """Said as "longest job" on purpose: it is a fact about one job and claims
    nothing about how many run at once. The plan total stays the stored one."""
    pid = _save(conn)
    d = ph.get_project_detail(conn, pid)
    step1 = next(s for s in d["steps"] if s["step"] == 1)
    assert step1["longest_s"] == 1800                     # max(600, 1800), not 2400
    assert step1["job_fee"] == 400_000.0                  # fees DO add up
    assert d["total_time_s"] == 3600                      # the stored total, untouched


# ── the frozen part ──────────────────────────────────────────────────────────

def test_the_figures_do_not_move_when_the_market_does(conn):
    """The requirement in one test: wipe every price the app knows and put the
    cost indices somewhere else, then read the project again."""
    pid = _save(conn)
    before = ph.get_project_detail(conn, pid)

    conn.execute("DELETE FROM market_price_cache")
    conn.execute("UPDATE sde_blueprint_materials SET quantity = quantity * 7")
    try:
        conn.execute("DELETE FROM sci_cache")
    except Exception:
        pass
    conn.commit()

    after = ph.get_project_detail(conn, pid)
    for key in ("total_time_s", "total_job_fee", "full_mat_cost", "total_buy_cost"):
        assert before[key] == after[key], key
    pb, pa = before["plans"][0], after["plans"][0]
    for key in ("total_time_s", "total_job_fee", "full_mat_cost", "total_buy",
                "profit_stock", "profit_market"):
        assert pb[key] == pa[key], key
    assert pb["mfg"] == pa["mfg"] and pb["rxn"] == pa["rxn"]
    assert before["steps"] == after["steps"]


def test_ticking_a_job_off_changes_no_figure(conn):
    """Progress is progress. It must not touch a single number."""
    pid = _save(conn)
    before = ph.get_project_detail(conn, pid)
    job_id = before["steps"][0]["jobs"][0]["job_ids"][0]
    conn.execute("UPDATE project_jobs SET status='completed' WHERE id=?", (job_id,))
    conn.commit()

    after = ph.get_project_detail(conn, pid)
    assert after["done_jobs"] == before["done_jobs"] + 1       # progress moved
    assert after["total_time_s"] == before["total_time_s"]
    assert after["total_job_fee"] == before["total_job_fee"]
    assert after["full_mat_cost"] == before["full_mat_cost"]
    assert ([ (s["longest_s"], s["job_fee"]) for s in after["steps"] ]
            == [ (s["longest_s"], s["job_fee"]) for s in before["steps"] ])


# ── plans saved before the columns existed ───────────────────────────────────

def test_an_older_plan_gains_the_figures_without_being_saved_again(conn):
    """Everything needed was always inside plan_json, so a backfill is enough -
    no re-saving, and nothing recalculated from today's data."""
    pid = ph.create_project(conn, "legacy" + str(time.time()))
    conn.execute(
        "INSERT INTO project_plans (project_id,product_type_id,product_name,quantity,"
        "me,te,station_name,facility_tax,plan_json,status,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (pid, 638, "Raven", 2, 10, 20, "Old Station", 2.5,
         json.dumps(_plan(total_time_s=7200, job_fee=3_000_000.0)), "pending", time.time()),
    )
    conn.commit()
    assert conn.execute(
        "SELECT total_time_s FROM project_plans WHERE project_id=?", (pid,)
    ).fetchone()[0] is None

    assert ph._backfill_plan_columns(conn) >= 1

    plan = ph.get_project_detail(conn, pid)["plans"][0]
    assert plan["total_time_s"] == 7200
    assert plan["total_job_fee"] == 3_000_000.0
    assert plan["mfg"]["station"] == "Old Station"
    # It was saved before the reaction station was recorded - say so, do not invent one.
    assert plan["rxn"]["station"] == ""


# ── the page actually shows it ───────────────────────────────────────────────

def test_the_project_page_shows_time_cost_and_the_facility_setup(client, app_module):
    """A figure that is stored but never rendered helps nobody, and a template
    can break on its own - so this goes through the real route."""
    c = app_module.get_conn()
    try:
        ph.ensure_project_tables(c)
        pid = ph.create_project(c, "render-check")
        ph.add_plan_to_project(
            c, pid, _plan(), "7BX-6F - Construction", 2.5,
            rxn_station_name="C-N4OD VI - Reactions", facility=FACILITY,
            app_version="9.9.9")
    finally:
        c.close()

    html = client.get(f"/projects/{pid}").text
    for bit in ("job time", "install fees", "Manufacturing", "Reaction",
                "7BX-6F - Construction", "C-N4OD VI - Reactions",
                "Azbel (L-Set)", "T2 ME Capital Ship", "Tatara",
                "cost bonus", "longest job", "v9.9.9"):
        assert bit in html, bit
    # The index of the day is part of the record, not a live reading.
    assert "5.42%" in html and "6.02%" in html
    # And it must not claim a duration for a whole step.
    assert "step duration" not in html.lower()


# ── the listing ──────────────────────────────────────────────────────────────

def test_the_listing_counts_finished_plans_once(conn):
    """It used to join plans AND shopping in one aggregate, so a finished plan
    was counted once per shopping line: one plan with three things to buy read
    as "3 done" out of 1."""
    pid = ph.create_project(conn, "fanout")
    conn.execute(
        "INSERT INTO project_plans (project_id,product_type_id,product_name,quantity,me,te,"
        "station_name,facility_tax,plan_json,status,created_at,total_time_s,total_job_fee,"
        "full_mat_cost,total_buy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, 638, "Raven", 1, 0, 0, "S", 0.0, "{}", "completed", time.time(),
         3600, 1_000_000.0, 5_000_000.0, 2_000_000.0))
    for t in (34, 35, 36):
        conn.execute("INSERT INTO project_shopping VALUES (?,?,?,?,?)", (pid, t, f"m{t}", 10, 10))
    conn.commit()

    row = next(r for r in ph.list_projects(conn) if r["id"] == pid)
    assert row["plan_count"] == 1
    assert row["completed_plans"] == 1          # was 3
    assert row["shopping_done"] == 3 and row["shopping_total"] == 3


def test_the_listing_carries_the_saved_totals(conn):
    """So the index can show them without opening a 70 kB plan per project."""
    pid = _save(conn, name="totals")
    row = next(r for r in ph.list_projects(conn) if r["id"] == pid)
    assert row["total_time_s"] == 3600 and row["total_time"]
    assert row["total_job_fee"] == 1_000_000.0
    assert row["full_mat_cost"] == 50_000_000.0
