"""
Build sde_base.db - a clean, SDE-only database for bundling with PyInstaller.

Copies the SDE tables from the current eve_cache.db and strips all user data.
Run this before building the PyInstaller package.

Usage: python scripts/build_sde_base.py [--build 3470007]

The build number is stamped into an sde_meta table, so which SDE a shipped copy
was made from is answerable from the file itself instead of only from the notes.
If --build is not given it is inferred from the downloaded zip name
(eve-online-static-data-3470007-yaml.zip), which is where the number comes from
anyway; the URL also returns it as the x-sde-build-number header.
"""
import glob
import os
import re
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DB = os.path.join(ROOT, "eve_cache.db")
DST_DB = os.path.join(ROOT, "sde_base.db")

META_TABLE = "sde_meta"

SDE_TABLES = {
    "sde_types",
    "sde_groups",
    "sde_blueprints",
    "sde_blueprint_materials",
    "sde_blueprint_products",
    "sde_blueprint_skills",
    "sde_skill_time_bonus",
    "sde_planet_schematics",
    "sde_planet_schematic_materials",
    "sde_stations",
    "sde_systems",
    "rig_bonuses",
    META_TABLE,
}


def _build_number(argv: list[str]) -> str:
    """The SDE build: from --build, else from a downloaded zip name, else unknown."""
    if "--build" in argv:
        i = argv.index("--build")
        if i + 1 < len(argv):
            return argv[i + 1].strip()
    builds = set()
    for path in glob.glob(os.path.join(ROOT, "data", "*.zip")):
        m = re.search(r"(\d{6,})", os.path.basename(path))
        if m:
            builds.add(m.group(1))
    if len(builds) == 1:
        return builds.pop()
    return ""          # ambiguous or nothing to go on - better empty than wrong


def main() -> None:
    if not os.path.exists(SRC_DB):
        print(f"ERROR: {SRC_DB} not found. Run import_sde.py first.")
        sys.exit(1)

    # Verify SDE tables are populated
    conn_src = sqlite3.connect(SRC_DB)
    count = conn_src.execute("SELECT COUNT(*) FROM sde_types").fetchone()[0]
    if count == 0:
        print("ERROR: sde_types is empty. Run import_sde.py first.")
        conn_src.close()
        sys.exit(1)
    print(f"Source: {count} sde_types rows found.")

    if os.path.exists(DST_DB):
        os.remove(DST_DB)

    # Copy full db then delete user tables
    shutil.copy2(SRC_DB, DST_DB)
    conn_dst = sqlite3.connect(DST_DB)

    # Get all tables in the db
    all_tables = [
        r[0] for r in conn_dst.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]

    for tbl in all_tables:
        if tbl not in SDE_TABLES and tbl != "sqlite_sequence":
            conn_dst.execute(f"DROP TABLE IF EXISTS [{tbl}]")
            print(f"  Dropped user table: {tbl}")

    build = _build_number(sys.argv[1:])
    conn_dst.execute(
        f"CREATE TABLE IF NOT EXISTS {META_TABLE} (key TEXT PRIMARY KEY, value TEXT)")
    conn_dst.executemany(
        f"INSERT OR REPLACE INTO {META_TABLE} (key, value) VALUES (?,?)",
        [("sde_build", build),
         ("built_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
         ("type_count", str(count))])
    print(f"  Stamped sde_meta: build={build or 'unknown'}, {count} types")
    if not build:
        print("  (pass --build NNNNNNN to record it; inferred from data/*.zip otherwise)")

    # VACUUM cannot run inside a transaction, and the DROP/INSERT statements above
    # opened one (Python 3.12+ no longer commits implicitly before it). Without the
    # commit the whole step raised "cannot VACUUM from within a transaction" and
    # left a 28 MB file with an unstamped sde_meta - which is how a stale bundle
    # would have shipped.
    conn_dst.commit()
    conn_dst.execute("VACUUM")
    conn_dst.close()
    conn_src.close()

    size_mb = os.path.getsize(DST_DB) / 1_048_576
    print(f"\nCreated: {DST_DB} ({size_mb:.1f} MB)")
    print("Ready to bundle with PyInstaller.")


if __name__ == "__main__":
    main()
