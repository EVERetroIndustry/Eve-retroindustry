"""Storage for Production Projects.

A saved project is a FROZEN record. The whole computed plan goes into
plan_json and is only ever read back - prices, system cost indices and the
facility setup are the ones from the moment it was saved, so a project does
not quietly change its numbers when the market or the indices move. Ticking
something off records progress and recalculates nothing.
"""
import sqlite3
import time
import json
from collections import defaultdict

from app.manufacturing.planner import format_duration


def ensure_project_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS production_projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS project_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        product_type_id INTEGER NOT NULL,
        product_name TEXT NOT NULL,
        quantity INTEGER NOT NULL DEFAULT 1,
        me INTEGER DEFAULT 0,
        te INTEGER DEFAULT 0,
        station_name TEXT,
        facility_tax REAL DEFAULT 0,
        plan_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at REAL NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS project_shopping (
        project_id INTEGER NOT NULL,
        type_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        needed INTEGER NOT NULL DEFAULT 0,
        purchased INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(project_id, type_id)
    )""")
    # Columns added after the feature shipped. A saved plan is a FROZEN record,
    # so these are filled from the plan_json that is already stored rather than
    # recomputed from today's prices - an old project must keep reading exactly
    # what it cost when it was saved.
    _migrate_plan_columns(conn)

    conn.execute("""CREATE TABLE IF NOT EXISTS project_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER NOT NULL,
        project_id INTEGER NOT NULL,
        type_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        quantity INTEGER NOT NULL DEFAULT 1,
        runs INTEGER NOT NULL DEFAULT 1,
        step INTEGER NOT NULL DEFAULT 1,
        activity TEXT NOT NULL DEFAULT 'manufacturing',
        status TEXT NOT NULL DEFAULT 'pending'
    )""")
    conn.commit()


# Denormalised copies of numbers that live inside plan_json. They are here so
# the projects LIST can show totals without parsing a 70 kB document per plan,
# and so a future change to the plan format cannot silently move them.
_PLAN_EXTRA_COLUMNS = {
    "rxn_station_name": "TEXT",     # the reaction station; station_name is manufacturing
    "facility_json":    "TEXT",     # structure type + rigs per activity, as chosen
    "total_time_s":     "INTEGER",
    "total_job_fee":    "REAL",
    "full_mat_cost":    "REAL",
    "total_buy":        "REAL",
    "app_version":      "TEXT",     # what computed it, so a format change is visible
    "saved_at":         "REAL",
}


def _migrate_plan_columns(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(project_plans)").fetchall()}
    added = [c for c in _PLAN_EXTRA_COLUMNS if c not in cols]
    for col in added:
        conn.execute(f"ALTER TABLE project_plans ADD COLUMN {col} {_PLAN_EXTRA_COLUMNS[col]}")
    if added:
        conn.commit()
        _backfill_plan_columns(conn)


def _backfill_plan_columns(conn: sqlite3.Connection) -> int:
    """Fill the new columns for plans saved before they existed.

    Everything needed is already inside plan_json - it has been stored in full
    since the first version - so an existing project gains the figures without
    being saved again, and without anything being recalculated.
    """
    rows = conn.execute(
        "SELECT id, plan_json FROM project_plans WHERE total_time_s IS NULL"
    ).fetchall()
    done = 0
    for plan_id, raw in rows:
        try:
            pd = json.loads(raw)
        except Exception:
            continue
        fees = pd.get("fees") or {}
        conn.execute(
            "UPDATE project_plans SET total_time_s=?, total_job_fee=?, full_mat_cost=?,"
            " total_buy=?, saved_at=COALESCE(saved_at, created_at) WHERE id=?",
            (fees.get("total_time_s") or 0, fees.get("total_job_fee") or 0.0,
             fees.get("full_mat_cost") or 0.0, pd.get("total_buy") or 0.0, plan_id),
        )
        done += 1
    if done:
        conn.commit()
    return done


def list_projects(conn: sqlite3.Connection) -> list[dict]:
    """One row per project for the listing, with the saved totals.

    Subqueries rather than a join across plans AND shopping: joining both fans
    the rows out, and `completed_plans` was being multiplied by the number of
    shopping lines (one finished plan with three things to buy read as 3 of 1).

    The time and fee columns are the denormalised copies, which is what they
    are for - the alternative is parsing a ~70 kB plan document per plan to add
    up two numbers.
    """
    rows = conn.execute("""
        SELECT p.id, p.name, p.created_at, p.updated_at,
               (SELECT COUNT(*) FROM project_plans   WHERE project_id = p.id),
               (SELECT COUNT(*) FROM project_plans   WHERE project_id = p.id AND status='completed'),
               (SELECT COUNT(*) FROM project_shopping WHERE project_id = p.id),
               (SELECT COUNT(*) FROM project_shopping WHERE project_id = p.id
                                                        AND purchased >= needed AND needed > 0),
               (SELECT COALESCE(SUM(total_time_s), 0)  FROM project_plans WHERE project_id = p.id),
               (SELECT COALESCE(SUM(total_job_fee), 0) FROM project_plans WHERE project_id = p.id),
               (SELECT COALESCE(SUM(full_mat_cost), 0) FROM project_plans WHERE project_id = p.id),
               (SELECT COALESCE(SUM(total_buy), 0)     FROM project_plans WHERE project_id = p.id)
        FROM production_projects p ORDER BY p.updated_at DESC
    """).fetchall()
    return [
        {
            "id": r[0], "name": r[1], "created_at": r[2], "updated_at": r[3],
            "plan_count": r[4] or 0, "completed_plans": r[5] or 0,
            "shopping_total": r[6] or 0, "shopping_done": r[7] or 0,
            "total_time_s": r[8] or 0,
            "total_time": format_duration(r[8]) if r[8] else None,
            "total_job_fee": r[9] or 0.0,
            "full_mat_cost": r[10] or 0.0,
            "total_buy": r[11] or 0.0,
        }
        for r in rows
    ]


def create_project(conn: sqlite3.Connection, name: str) -> int:
    now = time.time()
    cur = conn.execute(
        "INSERT INTO production_projects (name,created_at,updated_at) VALUES (?,?,?)",
        (name, now, now),
    )
    conn.commit()
    return cur.lastrowid


def add_plan_to_project(
    conn: sqlite3.Connection,
    project_id: int,
    plan_data: dict,
    station_name: str,
    facility_tax: float,
    rxn_station_name: str = "",
    facility: dict | None = None,
    app_version: str = "",
) -> int:
    """Store a plan as a frozen record. Nothing here is ever recalculated.

    plan_data is the whole computed plan, exactly as the Plan screen rendered
    it: materials, steps, every job's duration and install fee, and the `fees`
    block with the totals and the system cost indices of that moment. The
    station names and the structure/rig choice come separately because they are
    the user's input, not an output of the calculation.
    """
    now = time.time()
    bp = plan_data.get("blueprint") or {}
    fees = plan_data.get("fees") or {}
    cur = conn.execute(
        """
        INSERT INTO project_plans
        (project_id,product_type_id,product_name,quantity,me,te,station_name,facility_tax,plan_json,status,created_at,
         rxn_station_name,facility_json,total_time_s,total_job_fee,full_mat_cost,total_buy,app_version,saved_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            project_id,
            plan_data["product_type_id"],
            plan_data["product_name"],
            plan_data["quantity"],
            bp.get("me", 0),
            bp.get("te", 0),
            station_name,
            facility_tax,
            json.dumps(plan_data, default=str),
            "pending",
            now,
            rxn_station_name or "",
            json.dumps(facility or {}, default=str),
            fees.get("total_time_s") or 0,
            fees.get("total_job_fee") or 0.0,
            fees.get("full_mat_cost") or 0.0,
            plan_data.get("total_buy") or 0.0,
            app_version,
            now,
        ),
    )
    plan_id = cur.lastrowid

    for mat in plan_data.get("materials", []):
        missing = mat.get("missing") or 0
        if missing > 0:
            conn.execute(
                """
                INSERT INTO project_shopping (project_id,type_id,name,needed,purchased) VALUES (?,?,?,?,0)
                ON CONFLICT(project_id,type_id) DO UPDATE SET needed=needed+excluded.needed, name=excluded.name
                """,
                (project_id, mat["type_id"], mat["name"], missing),
            )

    for step_data in plan_data.get("manufacturing_steps", []):
        for job in step_data.get("jobs", []):
            conn.execute(
                """
                INSERT INTO project_jobs (plan_id,project_id,type_id,name,quantity,runs,step,activity,status)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    plan_id,
                    project_id,
                    job["type_id"],
                    job["name"],
                    job.get("quantity", 1),
                    job.get("runs", 1),
                    step_data["step"],
                    job.get("activity", "manufacturing"),
                    "pending",
                ),
            )

    conn.execute(
        "UPDATE production_projects SET updated_at=? WHERE id=?", (now, project_id)
    )
    conn.commit()
    return plan_id


def get_project_detail(conn: sqlite3.Connection, project_id: int) -> dict | None:
    proj = conn.execute(
        "SELECT id,name,created_at,updated_at FROM production_projects WHERE id=?",
        (project_id,),
    ).fetchone()
    if not proj:
        return None

    plans = []
    for r in conn.execute(
        """
        SELECT id,product_type_id,product_name,quantity,me,te,station_name,facility_tax,status,created_at,
               rxn_station_name,facility_json,total_time_s,total_job_fee,full_mat_cost,total_buy,app_version,saved_at,
               plan_json
        FROM project_plans WHERE project_id=? ORDER BY created_at
        """,
        (project_id,),
    ).fetchall():
        try:
            pd = json.loads(r[18])
        except Exception:
            pd = {}
        fees = pd.get("fees") or {}
        try:
            facility = json.loads(r[11]) if r[11] else {}
        except Exception:
            facility = {}
        plans.append({
            "id": r[0], "product_type_id": r[1], "product_name": r[2],
            "quantity": r[3], "me": r[4], "te": r[5],
            "station_name": r[6], "facility_tax": r[7], "status": r[8], "created_at": r[9],
            "rxn_station_name": r[10] or "",
            "facility": facility,
            # Totals as they were computed. Read, never recomputed - which is the
            # whole point of a saved project.
            "total_time_s": r[12] or 0,
            "total_time": format_duration(r[12]) if r[12] else None,
            "total_job_fee": r[13] or 0.0,
            "full_mat_cost": r[14] or 0.0,
            "total_buy": r[15] or 0.0,
            "app_version": r[16] or "",
            "saved_at": r[17] or r[9],
            "blueprint_kind": (pd.get("blueprint") or {}).get("kind"),
            "mode": pd.get("mode"),
            "sell_price": pd.get("sell_price"),
            "revenue": pd.get("revenue"),
            "profit_market": fees.get("profit_market"),
            "profit_stock": fees.get("profit_stock"),
            "sep_rxn_station": bool(fees.get("sep_rxn_station")),
            "mfg": {
                "station": r[6] or "",
                "tax": fees.get("facility_tax"),
                "sci": fees.get("mfg_sci"),
                "cost_bonus_pct": fees.get("mfg_cost_bonus_pct"),
                "me_pct": fees.get("mfg_me_pct"),
                "te_pct": fees.get("mfg_te_pct"),
                "structure": (facility.get("mfg") or {}).get("structure"),
                "rigs": (facility.get("mfg") or {}).get("rigs") or [],
            },
            "rxn": {
                "station": r[10] or "",
                "tax": fees.get("rxn_facility_tax"),
                "sci": fees.get("rxn_sci"),
                "cost_bonus_pct": fees.get("rxn_cost_bonus_pct"),
                "me_pct": fees.get("rxn_me_pct"),
                "te_pct": fees.get("rxn_te_pct"),
                "structure": (facility.get("rxn") or {}).get("structure"),
                "rigs": (facility.get("rxn") or {}).get("rigs") or [],
            },
            "implant_name": fees.get("implant_mfg_name"),
            "implant_pct": fees.get("implant_mfg_pct") or 0,
        })

    shopping = [
        {"type_id": r[0], "name": r[1], "needed": r[2], "purchased": r[3]}
        for r in conn.execute(
            "SELECT type_id,name,needed,purchased FROM project_shopping WHERE project_id=? ORDER BY name",
            (project_id,),
        ).fetchall()
    ]

    # Load each job's inputs from the stored plan_json (aggregate across plans)
    # (step, type_id) -> {input_type_id: {name, quantity, is_leaf, activity}}
    plan_input_map: dict = {}
    plan_job_facts: dict = {}          # (step, type_id) -> {duration_s, job_fee}
    for plan_id_row, plan_json_str in conn.execute(
        "SELECT id, plan_json FROM project_plans WHERE project_id=?", (project_id,)
    ).fetchall():
        try:
            pd = json.loads(plan_json_str)
        except Exception:
            continue
        for step_data in pd.get("manufacturing_steps", []):
            sn = step_data["step"]
            for job in step_data.get("jobs", []):
                key = (sn, job["type_id"])
                # The job's own duration and install fee, exactly as computed
                # when the plan was saved. Summed when the same item is built in
                # more than one plan of this project, because those are separate
                # jobs with separate fees.
                facts = plan_job_facts.setdefault(key, {"duration_s": 0, "job_fee": 0.0})
                facts["duration_s"] += job.get("job_duration_seconds") or 0
                facts["job_fee"] += job.get("job_fee") or 0.0
                if key not in plan_input_map:
                    plan_input_map[key] = {}
                for inp in job.get("inputs", []):
                    tid = inp["type_id"]
                    if tid not in plan_input_map[key]:
                        plan_input_map[key][tid] = {
                            "type_id": tid,
                            "name": inp["name"],
                            "quantity": inp.get("quantity", 0),
                            "is_leaf": inp.get("is_leaf", True),
                            "activity": inp.get("activity", ""),
                        }
                    else:
                        plan_input_map[key][tid]["quantity"] += inp.get("quantity", 0)

    # Jobs grouped by step, then merged by type_id within step
    jobs_raw = conn.execute(
        """
        SELECT id,plan_id,type_id,name,quantity,runs,step,activity,status
        FROM project_jobs WHERE project_id=? ORDER BY step,name
        """,
        (project_id,),
    ).fetchall()

    # Merge jobs with same type_id+step
    merged: dict = {}  # (step, type_id) -> job dict
    for r in jobs_raw:
        jd = {
            "id": r[0], "plan_id": r[1], "type_id": r[2], "name": r[3],
            "quantity": r[4], "runs": r[5], "step": r[6], "activity": r[7], "status": r[8],
        }
        key = (jd["step"], jd["type_id"])
        if key not in merged:
            merged[key] = {**jd, "job_ids": [jd["id"]], "completed": jd["status"] == "completed"}
        else:
            merged[key]["quantity"] += jd["quantity"]
            merged[key]["runs"] += jd["runs"]
            merged[key]["job_ids"].append(jd["id"])
            if jd["status"] != "completed":
                merged[key]["completed"] = False

    # Add inputs and the frozen time/fee to each merged job
    for key, job in merged.items():
        inputs = plan_input_map.get(key, {})
        job["inputs"] = sorted(inputs.values(), key=lambda x: x["name"])
        facts = plan_job_facts.get(key) or {}
        job["duration_s"] = facts.get("duration_s") or 0
        job["duration"] = format_duration(job["duration_s"]) if job["duration_s"] else None
        job["job_fee"] = facts.get("job_fee") or 0.0

    steps_map: dict = defaultdict(list)
    for key, job in merged.items():
        steps_map[key[0]].append(job)

    steps = []
    for step_num in sorted(steps_map.keys()):
        step_jobs = sorted(steps_map[step_num], key=lambda j: j["name"])
        longest = max((j["duration_s"] for j in step_jobs), default=0)
        steps.append({
            "step": step_num,
            "jobs": step_jobs,
            "all_done": all(j["completed"] for j in step_jobs),
            "job_fee": sum(j["job_fee"] for j in step_jobs),
            # The longest single job in the step. Said that way on purpose: it is
            # a fact about one job and claims nothing about how many of them run
            # at once. The plan's own total is the stored one, never a sum of these.
            "longest_s": longest,
            "longest": format_duration(longest) if longest else None,
        })

    total_jobs = sum(len(s["jobs"]) for s in steps)
    done_jobs = sum(1 for s in steps for j in s["jobs"] if j["completed"])

    # Project totals = the sum of what each plan stored. Adding up the saved
    # numbers keeps the project honest even if the plan format changes later.
    proj_time_s = sum(p["total_time_s"] for p in plans)
    return {
        "id": proj[0], "name": proj[1], "created_at": proj[2], "updated_at": proj[3],
        "plans": plans, "shopping": shopping, "steps": steps,
        "total_jobs": total_jobs, "done_jobs": done_jobs,
        "total_time_s": proj_time_s,
        "total_time": format_duration(proj_time_s) if proj_time_s else None,
        "total_job_fee": sum(p["total_job_fee"] for p in plans),
        "full_mat_cost": sum(p["full_mat_cost"] for p in plans),
        "total_buy_cost": sum(p["total_buy"] for p in plans),
        "shopping_done": sum(
            1 for s in shopping if s["purchased"] >= s["needed"] and s["needed"] > 0
        ),
    }
