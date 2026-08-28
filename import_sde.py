"""
Import the EVE Online SDE into a SQLite database.
Parses fsd/blueprints.yaml and fsd/types.yaml.
Usage: python import_sde.py
"""
import glob
import re
import yaml
import zipfile
import sqlite3
import os
import sys
import time
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn

# Matches "1% reduction in manufacturing time" or "...in reaction time".
# Reactions skill (45746) has "...reaction time per skill level" - without
# this alternation it would be silently dropped from sde_skill_time_bonus.
_BONUS_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*%\s*reduction\s+in\s+(?:manufacturing|reaction)\s+time',
    re.IGNORECASE,
)

console = Console()

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
# The SDE layout has moved twice: files used to live in fsd/, then in the zip
# root. Both are supported - but pick the NEWER types.yaml rather than preferring
# a directory, because a stale extraction left in data/fsd/ otherwise wins over
# a freshly unpacked one and the import silently rebuilds from old data. That
# happened: a rebuild "succeeded" with 52 863 types and none of the new build's
# changes in it.
def _pick_sde_dir() -> str:
    candidates = [d for d in (os.path.join(_DATA_DIR, "fsd"), _DATA_DIR)
                  if os.path.exists(os.path.join(d, "types.yaml"))]
    if not candidates:
        return _DATA_DIR
    return max(candidates, key=lambda d: os.path.getmtime(os.path.join(d, "types.yaml")))


SDE_DIR = _pick_sde_dir()
DB_PATH = os.path.join(os.path.dirname(__file__), "eve_cache.db")


def _yaml_load(f):
    """Load YAML via the libyaml C loader if available (orders of magnitude faster
    on the large types.yaml ~150 MB), otherwise the pure-Python SafeLoader."""
    try:
        from yaml import CSafeLoader as _Loader
    except ImportError:
        from yaml import SafeLoader as _Loader
    return yaml.load(f, Loader=_Loader)


BLUEPRINTS_YAML = os.path.join(SDE_DIR, "blueprints.yaml")
TYPES_YAML = os.path.join(SDE_DIR, "types.yaml")
GROUPS_YAML = os.path.join(SDE_DIR, "groups.yaml")
PLANET_SCHEMATICS_YAML = os.path.join(SDE_DIR, "planetSchematics.yaml")


def import_planet_schematics(conn: sqlite3.Connection):
    """PI factory schematics: inputs → output (type_ids + quantities) + cycle time.
    Powers the Planets production-chain view. Source: planetSchematics.yaml."""
    if not os.path.exists(PLANET_SCHEMATICS_YAML):
        console.print(f"[yellow]planetSchematics.yaml not found ({PLANET_SCHEMATICS_YAML}) - skipping[/]")
        return
    console.print("Loading planetSchematics.yaml…")
    with open(PLANET_SCHEMATICS_YAML, "r", encoding="utf-8") as f:
        data = _yaml_load(f)
    sch_rows, mat_rows = [], []
    for sid, info in (data or {}).items():
        if not isinstance(info, dict):
            continue
        nf = info.get("name", {})
        name = nf.get("en", "") if isinstance(nf, dict) else str(nf)
        out_tid, out_qty = None, 0
        for tid, td in (info.get("types") or {}).items():
            if not isinstance(td, dict):
                continue
            qty = td.get("quantity", 0)
            if td.get("isInput"):
                mat_rows.append((int(sid), int(tid), qty))
            else:
                out_tid, out_qty = int(tid), qty
        sch_rows.append((int(sid), name, info.get("cycleTime", 0), out_tid, out_qty))
    conn.executemany(
        "INSERT OR REPLACE INTO sde_planet_schematics "
        "(schematic_id, name, cycle_time, output_type_id, output_qty) VALUES (?,?,?,?,?)",
        sch_rows)
    conn.executemany(
        "INSERT OR REPLACE INTO sde_planet_schematic_materials "
        "(schematic_id, type_id, quantity) VALUES (?,?,?)",
        mat_rows)
    conn.commit()
    console.print(f"[green]Imported {len(sch_rows):,} planet schematics ({len(mat_rows):,} inputs)[/]")


_ROMAN = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
          (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]


def _roman(n: int) -> str:
    out = ""
    for value, sym in _ROMAN:
        while n >= value:
            out += sym
            n -= value
    return out


def _en(value):
    """SDE names are per-language dicts; everything else in the app is English."""
    return value.get("en") if isinstance(value, dict) else value


def _sde_zip() -> tuple[str, str] | None:
    """(path, layout) of the newest usable SDE zip in data/.

    Two layouts exist and both are supported, because which one a given zip has
    depends on when it was downloaded:
      "flat"   - everything in the zip root, stations in npcStations.yaml and the
                 map in mapSolarSystems/mapRegions (build 3482594 onwards)
      "nested" - fsd/ and bsd/ subdirectories, stations in bsd/staStations.yaml
                 and the map as a universe/ directory tree
    """
    best = None
    for zip_path in sorted(glob.glob(os.path.join(_DATA_DIR, "*.zip")), reverse=True):
        try:
            with zipfile.ZipFile(zip_path) as z:
                names = set(z.namelist())
                if "npcStations.yaml" in names and "mapSolarSystems.yaml" in names:
                    return zip_path, "flat"
                if "bsd/staStations.yaml" in names and best is None:
                    best = (zip_path, "nested")
        except zipfile.BadZipFile:
            continue
    return best


_TOP_KEY = re.compile(rb"^(\d+):\s*$")


def _unique_celestial_names(z, member: str, wanted: set[int]) -> dict[int, str]:
    """`uniqueName` for the celestials we care about, read as a stream.

    mapMoons.yaml is 218 MB and only fifteen of its moons have a proper name, so
    parsing the whole thing as YAML would cost minutes and gigabytes to learn
    almost nothing. A line scan does it in seconds.
    """
    out: dict[int, str] = {}
    current = None
    pending = False
    try:
        handle = z.open(member)
    except KeyError:
        return out
    with handle as f:
        for raw in f:
            m = _TOP_KEY.match(raw)
            if m:
                current = int(m.group(1))
                pending = False
                continue
            if raw.strip() == b"uniqueName:":
                pending = current in wanted
                continue
            if pending and raw.startswith(b"    en:"):
                out[current] = raw.split(b":", 1)[1].strip().decode("utf-8").strip("'\"")
                pending = False
    return out


def _universe_flat(z) -> tuple[list[tuple], list[tuple]]:
    """Stations and systems from the flat layout.

    Station NAMES are no longer shipped: npcStations.yaml carries the parts
    (which celestial it orbits, whose corporation, which operation) and the name
    is composed the way the client does it -

        <celestial> - <corporation> <operation>

    where <celestial> is the orbited body's `uniqueName` when it has one
    ("Amarr VIII (Oris)", "Kor-Azor Prime IV (Eclipticum) - Moon Griklaeum") and
    otherwise "<system> <roman(planet)>" plus " - Moon <n>". Verified against the
    5 154 names the previous SDE shipped ready-made: 5 154 exact matches, no
    misses, so this is the rule and not an approximation.
    """
    systems = _yaml_load(z.read("mapSolarSystems.yaml")) or {}
    stations = _yaml_load(z.read("npcStations.yaml")) or {}
    corps = _yaml_load(z.read("npcCorporations.yaml")) or {}
    operations = _yaml_load(z.read("stationOperations.yaml")) or {}

    orbits = {rec.get("orbitID") for rec in stations.values() if rec.get("orbitID")}
    named = _unique_celestial_names(z, "mapPlanets.yaml", orbits)
    named.update(_unique_celestial_names(z, "mapMoons.yaml", orbits))

    sys_rows = [(int(sid), _en(rec.get("name")) or "", rec.get("regionID"))
                for sid, rec in systems.items()]

    st_rows = []
    for sid, rec in stations.items():
        system_id = rec.get("solarSystemID")
        head = named.get(rec.get("orbitID"))
        if not head:
            system_name = _en((systems.get(system_id) or {}).get("name")) or ""
            index = rec.get("celestialIndex")
            head = f"{system_name} {_roman(index)}" if index else system_name
            moon = rec.get("orbitIndex")
            if moon:
                head += f" - Moon {moon}"
        corp = _en((corps.get(rec.get("ownerID")) or {}).get("name")) or ""
        operation = _en((operations.get(rec.get("operationID")) or {}).get("operationName")) or ""
        tail = f"{corp} {operation}".strip() if rec.get("useOperationName") else corp
        name = f"{head} - {tail}" if tail else head
        st_rows.append((int(sid), name, system_id,
                        (systems.get(system_id) or {}).get("regionID")))
    return st_rows, sys_rows


def _universe_nested(z) -> tuple[list[tuple], list[tuple]]:
    """Stations and systems from the older fsd/bsd layout, where station names
    were shipped ready-made and the map was a directory tree."""
    stations = _yaml_load(z.read("bsd/staStations.yaml")) or []
    st_rows = [(e["stationID"], e.get("stationName") or "",
                e.get("solarSystemID"), e.get("regionID"))
               for e in stations if e.get("stationID")]

    entries = z.namelist()
    region_of: dict[str, int] = {}
    sys_id_re = re.compile(rb"solarSystemID:\s*(\d+)")
    reg_id_re = re.compile(rb"regionID:\s*(\d+)")
    for name in entries:
        if name.endswith("/region.yaml"):
            m = reg_id_re.search(z.read(name))
            if m:
                region_of["/".join(name.split("/")[:3])] = int(m.group(1))
    sys_rows = []
    for name in entries:
        if not name.endswith("/solarsystem.yaml"):
            continue
        m = sys_id_re.search(z.read(name))
        if not m:
            continue
        parts = name.split("/")
        sys_rows.append((int(m.group(1)), parts[-2],
                         region_of.get("/".join(parts[:3]))))
    return st_rows, sys_rows


def import_universe(conn: sqlite3.Connection):
    """Station and solar-system names, so the app can search them offline.

    ESI's public /search/ endpoint is gone (it answers 404 for every query on
    every version), and its documented replacement, POST /universe/ids/, matches
    whole names only. Partial matching - typing "PR-8" and being offered PR-8CA -
    therefore cannot come from ESI at all any more. It can come from here: this
    is static data that changes with a patch, not with the market.

    Covers all of New Eden, k-space and wormhole space alike, and the region id
    is imported with it: that is what a station's prices are fetched against, so
    having it locally saves two ESI calls per load and works offline.
    """
    found = _sde_zip()
    if not found:
        console.print("[yellow]No usable SDE zip in data/ - skipping station and "
                      "system names[/]")
        return
    zip_path, layout = found

    # Rebuilt outright rather than merged: this is derived data, and a schema
    # that gained a column would otherwise keep the old shape for ever.
    conn.execute("DROP TABLE IF EXISTS sde_stations")
    conn.execute("DROP TABLE IF EXISTS sde_systems")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sde_stations (
            station_id INTEGER PRIMARY KEY,
            name       TEXT,
            system_id  INTEGER,
            region_id  INTEGER
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sde_systems (
            system_id INTEGER PRIMARY KEY,
            name      TEXT,
            region_id INTEGER
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sde_stations_system"
                 " ON sde_stations(system_id)")

    with zipfile.ZipFile(zip_path) as z, \
            Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                     BarColumn(), TimeElapsedColumn(), console=console) as prog:
        task = prog.add_task(f"Stations and systems ({layout} layout)", total=1)
        st_rows, sys_rows = (_universe_flat(z) if layout == "flat"
                             else _universe_nested(z))
        conn.executemany("INSERT OR REPLACE INTO sde_stations VALUES (?,?,?,?)", st_rows)
        conn.executemany("INSERT OR REPLACE INTO sde_systems VALUES (?,?,?)", sys_rows)
        prog.advance(task)

    conn.commit()
    n_st = conn.execute("SELECT COUNT(*) FROM sde_stations").fetchone()[0]
    n_sy = conn.execute("SELECT COUNT(*) FROM sde_systems").fetchone()[0]
    n_nr = conn.execute("SELECT COUNT(*) FROM sde_systems WHERE region_id IS NULL").fetchone()[0]
    n_nn = conn.execute("SELECT COUNT(*) FROM sde_stations WHERE name = ''").fetchone()[0]
    console.print(f"  [green]{n_st}[/] stations, [green]{n_sy}[/] solar systems"
                  + (f" ([yellow]{n_nr}[/] without a region)" if n_nr else "")
                  + (f" ([red]{n_nn}[/] without a name)" if n_nn else ""))


def init_db(conn: sqlite3.Connection):
    conn.executescript("""
        -- volume / packaged_volume: how much space one unit takes, in m3. The two
        -- differ for anything repackable - a Raven is 470 000 assembled and 50 000
        -- packaged - and the SDE gives both, so nothing has to be guessed from the
        -- item's group the way third-party tools do it.
        CREATE TABLE IF NOT EXISTS sde_types (
            type_id         INTEGER PRIMARY KEY,
            name            TEXT NOT NULL,
            group_id        INTEGER,
            published       INTEGER DEFAULT 1,
            market_group_id INTEGER,
            volume          REAL,
            packaged_volume REAL
        );

        CREATE TABLE IF NOT EXISTS sde_blueprint_materials (
            blueprint_type_id  INTEGER NOT NULL,
            activity           TEXT NOT NULL,   -- manufacturing / reaction
            material_type_id   INTEGER NOT NULL,
            quantity           INTEGER NOT NULL,
            PRIMARY KEY (blueprint_type_id, activity, material_type_id)
        );

        CREATE TABLE IF NOT EXISTS sde_blueprint_products (
            blueprint_type_id  INTEGER NOT NULL,
            activity           TEXT NOT NULL,
            product_type_id    INTEGER NOT NULL,
            quantity           INTEGER NOT NULL,
            probability        REAL DEFAULT 1.0,
            PRIMARY KEY (blueprint_type_id, activity, product_type_id)
        );

        CREATE TABLE IF NOT EXISTS sde_blueprints (
            blueprint_type_id  INTEGER PRIMARY KEY,
            max_production_limit INTEGER DEFAULT 1,
            manufacturing_time   INTEGER DEFAULT 0,
            reaction_time        INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS sde_blueprint_skills (
            blueprint_type_id  INTEGER NOT NULL,
            activity           TEXT NOT NULL,
            skill_type_id      INTEGER NOT NULL,
            required_level     INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (blueprint_type_id, activity, skill_type_id)
        );

        CREATE TABLE IF NOT EXISTS sde_skill_time_bonus (
            skill_type_id   INTEGER PRIMARY KEY,
            skill_name      TEXT NOT NULL,
            time_bonus_pct  REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sde_planet_schematics (
            schematic_id    INTEGER PRIMARY KEY,
            name            TEXT,
            cycle_time      INTEGER,
            output_type_id  INTEGER,
            output_qty      INTEGER
        );

        CREATE TABLE IF NOT EXISTS sde_planet_schematic_materials (
            schematic_id    INTEGER NOT NULL,
            type_id         INTEGER NOT NULL,
            quantity        INTEGER NOT NULL,
            PRIMARY KEY (schematic_id, type_id)
        );

        CREATE INDEX IF NOT EXISTS idx_bp_product ON sde_blueprint_products(product_type_id);
        CREATE INDEX IF NOT EXISTS idx_bp_materials ON sde_blueprint_materials(blueprint_type_id, activity);
        CREATE INDEX IF NOT EXISTS idx_bp_skills ON sde_blueprint_skills(blueprint_type_id, activity);
    """)
    # Re-running the import against a database from an older version: CREATE TABLE
    # IF NOT EXISTS leaves the old schema alone, so a column added later has to be
    # added here or the INSERT below fails with "no such column".
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sde_types)").fetchall()}
    for col in ("volume", "packaged_volume"):
        if col not in cols:
            conn.execute(f"ALTER TABLE sde_types ADD COLUMN {col} REAL")
    conn.commit()


def import_types(conn: sqlite3.Connection) -> dict:
    """Returns parsed types_data for reuse in skill bonus import."""
    console.print("[cyan]Loading types.yaml (147 MB, this takes a while)...[/]")
    t0 = time.time()

    with open(TYPES_YAML, "r", encoding="utf-8") as f:
        data = _yaml_load(f)

    console.print(f"[dim]YAML loaded in {time.time()-t0:.1f}s, importing {len(data):,} types...[/]")

    rows = []
    for type_id, info in data.items():
        if not isinstance(info, dict):
            continue
        name_field = info.get("name", {})
        name = name_field.get("en", "") if isinstance(name_field, dict) else str(name_field)
        if not name:
            continue
        vol = info.get("volume")
        packaged = info.get("packagedVolume")
        rows.append((
            int(type_id),
            name,
            info.get("groupID"),
            1 if info.get("published", True) else 0,
            info.get("marketGroupID"),
            float(vol) if isinstance(vol, (int, float)) else None,
            # Only stored when it actually differs; for everything that cannot be
            # repackaged the two are the same number and one column is enough.
            float(packaged) if isinstance(packaged, (int, float)) else None,
        ))

    conn.executemany(
        "INSERT OR REPLACE INTO sde_types (type_id, name, group_id, published,"
        " market_group_id, volume, packaged_volume) VALUES (?,?,?,?,?,?,?)",
        rows
    )
    conn.commit()
    console.print(f"[green]Imported {len(rows):,} types[/]")
    return data


_SKILL_EXCLUDE = {3380, 3388}  # Handled separately in calc_job_time
_IMPLANT_GROUP  = 743           # Zainou/manufacturing implants - not fetchable via ESI skills


def import_groups(conn: sqlite3.Connection):
    """Import groups.yaml → sde_groups (group_id, name en).

    Previously sde_groups was populated once via ESI (_ensure_groups_populated),
    which meant new groups (e.g. 5120 Command Carrier from Cradle of War)
    were never backfilled for existing users - rig_applies_to_product then
    returned False through its INNER JOIN and no rig applied to products from
    those groups.
    """
    if not os.path.exists(GROUPS_YAML):
        console.print(f"[yellow]groups.yaml not found ({GROUPS_YAML}) - skipping[/]")
        return
    console.print("Loading groups.yaml…")
    with open(GROUPS_YAML, "r", encoding="utf-8") as f:
        groups = _yaml_load(f)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sde_groups (
            group_id INTEGER PRIMARY KEY,
            name     TEXT NOT NULL
        );
    """)
    rows = []
    for gid, g in groups.items():
        name = (g.get("name") or {}).get("en") or f"Group {gid}"
        rows.append((int(gid), name))
    conn.executemany(
        "INSERT OR REPLACE INTO sde_groups (group_id, name) VALUES (?,?)", rows
    )
    conn.commit()
    console.print(f"  sde_groups: {len(rows)} groups")


def import_skill_time_bonuses(conn: sqlite3.Connection, types_data: dict):
    """Populate sde_skill_time_bonus from type descriptions."""
    rows = []
    for type_id, info in types_data.items():
        if not isinstance(info, dict):
            continue
        tid = int(type_id)
        if tid in _SKILL_EXCLUDE:
            continue
        if info.get("groupID") == _IMPLANT_GROUP:
            continue
        desc_field = info.get("description", {})
        desc_en = desc_field.get("en", "") if isinstance(desc_field, dict) else str(desc_field)
        m = _BONUS_RE.search(desc_en)
        if not m:
            continue
        bonus_pct = float(m.group(1))
        name_field = info.get("name", {})
        name = name_field.get("en", "") if isinstance(name_field, dict) else str(name_field)
        rows.append((tid, name, bonus_pct))

    conn.execute("DELETE FROM sde_skill_time_bonus")
    conn.executemany(
        "INSERT OR REPLACE INTO sde_skill_time_bonus VALUES (?,?,?)", rows
    )
    conn.commit()
    console.print(f"[green]Imported {len(rows)} skills with a time bonus[/]")


def import_blueprints(conn: sqlite3.Connection):
    console.print("[cyan]Loading blueprints.yaml...[/]")

    with open(BLUEPRINTS_YAML, "r", encoding="utf-8") as f:
        data = _yaml_load(f)

    console.print(f"[dim]Importing {len(data):,} blueprints...[/]")

    bp_rows, mat_rows, prod_rows, skill_rows = [], [], [], []

    for bp_type_id, info in data.items():
        if not isinstance(info, dict):
            continue

        activities = info.get("activities", {})
        max_limit = info.get("maxProductionLimit", 1)

        mfg_time = activities.get("manufacturing", {}).get("time", 0) if "manufacturing" in activities else 0
        rxn_time = activities.get("reaction", {}).get("time", 0) if "reaction" in activities else 0

        bp_rows.append((int(bp_type_id), max_limit, mfg_time, rxn_time))

        for activity_name in ("manufacturing", "reaction"):
            activity = activities.get(activity_name)
            if not activity:
                continue

            for mat in activity.get("materials") or []:
                mat_rows.append((
                    int(bp_type_id),
                    activity_name,
                    int(mat["typeID"]),
                    int(mat["quantity"]),
                ))

            for prod in activity.get("products") or []:
                prod_rows.append((
                    int(bp_type_id),
                    activity_name,
                    int(prod["typeID"]),
                    int(prod.get("quantity", 1)),
                    float(prod.get("probability", 1.0)),
                ))

            for skill in activity.get("skills") or []:
                skill_rows.append((
                    int(bp_type_id),
                    activity_name,
                    int(skill["typeID"]),
                    int(skill.get("level", 1)),
                ))

    conn.executemany(
        "INSERT OR REPLACE INTO sde_blueprints VALUES (?,?,?,?)",
        bp_rows
    )
    conn.executemany(
        "INSERT OR REPLACE INTO sde_blueprint_materials VALUES (?,?,?,?)",
        mat_rows
    )
    conn.executemany(
        "INSERT OR REPLACE INTO sde_blueprint_products VALUES (?,?,?,?,?)",
        prod_rows
    )
    conn.execute("DELETE FROM sde_blueprint_skills")
    conn.executemany(
        "INSERT OR REPLACE INTO sde_blueprint_skills VALUES (?,?,?,?)",
        skill_rows
    )
    conn.commit()

    console.print(f"[green]Imported: {len(bp_rows):,} blueprints, "
                  f"{len(mat_rows):,} material rows, "
                  f"{len(prod_rows):,} product rows, "
                  f"{len(skill_rows):,} skill rows[/]")


def main():
    console.print("[bold]EVE Retroindustry - Import SDE into SQLite[/]\n")

    if not os.path.exists(BLUEPRINTS_YAML):
        console.print(f"[red]Not found: {BLUEPRINTS_YAML}[/]")
        return
    if not os.path.exists(TYPES_YAML):
        console.print(f"[red]Not found: {TYPES_YAML}[/]")
        return

    console.print(f"[dim]reading YAML from {SDE_DIR}[/]")

    conn = sqlite3.connect(DB_PATH)

    # Station and system names only - lets a rebuild add them to an existing
    # database without re-parsing the 150 MB types.yaml.
    if "--universe-only" in sys.argv:
        t0 = time.time()
        import_universe(conn)
        conn.close()
        console.print(f"\n[bold green]Done in {time.time()-t0:.1f}s[/]")
        return

    init_db(conn)

    t_start = time.time()
    types_data = import_types(conn)
    import_skill_time_bonuses(conn, types_data)
    import_blueprints(conn)
    import_groups(conn)
    import_planet_schematics(conn)
    import_universe(conn)
    conn.close()

    console.print(f"\n[bold green]Done in {time.time()-t_start:.1f}s[/]")
    console.print(f"Database: {DB_PATH}")

    # Quick test - Nidhoggur
    console.print("\n[bold]Test - Nidhoggur (24483):[/]")
    conn = sqlite3.connect(DB_PATH)

    # Find the blueprint for Nidhoggur
    bp = conn.execute(
        "SELECT blueprint_type_id FROM sde_blueprint_products WHERE product_type_id=? AND activity='manufacturing'",
        (24483,)
    ).fetchone()

    if bp:
        bp_id = bp[0]
        bp_name = conn.execute("SELECT name FROM sde_types WHERE type_id=?", (bp_id,)).fetchone()
        console.print(f"  Blueprint: {bp_name[0] if bp_name else '?'} (ID: {bp_id})")

        materials = conn.execute("""
            SELECT t.name, m.quantity
            FROM sde_blueprint_materials m
            JOIN sde_types t ON t.type_id = m.material_type_id
            WHERE m.blueprint_type_id=? AND m.activity='manufacturing'
            ORDER BY m.quantity DESC
        """, (bp_id,)).fetchall()

        console.print(f"  Materials ({len(materials)}):")
        for name, qty in materials:
            console.print(f"    - {name}: {qty:,}")
    else:
        console.print("  [red]Blueprint not found[/]")

    conn.close()


if __name__ == "__main__":
    main()
