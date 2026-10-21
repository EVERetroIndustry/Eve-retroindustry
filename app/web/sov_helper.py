"""Sovereignty: which systems an alliance holds, what stands in them, and what
the map says about each one.

Sources, and what each one can and cannot say:

* ESI `/sovereignty/map` - who holds every system (cached by contracts_helper,
  which uses it for its own rule about prices in sov space).
* ESI `/sovereignty/structures` - every Sovereignty Hub with its Activity
  Defense Multiplier ("vulnerability_occupancy_level") and vulnerability window.
  Since Equinox the hub is the only sovereignty structure; there are no TCUs.
* ESI `/sovereignty/campaigns` - running and upcoming fights over hubs.
* The SDE - region, constellation, security, star, planets by kind, belts and
  gate links (import_sde.import_map).

What none of them says: which upgrades are installed in a hub. ESI has no
endpoint for it, so upgrades are entered by hand here, and the page says so.
Nor does anything report how much power or workforce a system yields - the SDE
carries the planets and the star, not the numbers the client computes from
them, so the page shows the planets and makes no claim about the totals.

/sovereignty/map and /sovereignty/structures answer 404 under our pinned
compatibility date (2026-07-17) and under every newer one; they exist only under
an older date. /sovereignty/campaigns answers under all of them.
"""
from __future__ import annotations

import re
import sqlite3
import time

import httpx

from app.esi.client import esi_client
from app.web.contracts_helper import _SOV_COMPAT_DATE, ensure_public_contract_tables

_BASE = "https://esi.evetech.net"
SOV_TTL = 60 * 60            # ESI caches all three for an hour

# Hub upgrade groups in the SDE. Read by group, not by a list of type ids, so a
# new upgrade CCP adds turns up without a code change.
UPGRADE_GROUPS = {
    4768: "Site detection",
    4772: "Service infrastructure",
    4838: "Colony resources",
    4839: "System effect",
}


def ensure_sov_tables(conn: sqlite3.Connection) -> None:
    ensure_public_contract_tables(conn)          # owns sov_map_cache
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sov_hubs (
            structure_id INTEGER PRIMARY KEY,
            system_id    INTEGER,
            alliance_id  INTEGER,
            adm          REAL,
            vuln_start   TEXT,
            vuln_end     TEXT,
            cached_at    REAL
        );
        CREATE INDEX IF NOT EXISTS idx_sov_hubs_system ON sov_hubs(system_id);
        CREATE TABLE IF NOT EXISTS sov_campaigns (
            campaign_id      INTEGER PRIMARY KEY,
            system_id        INTEGER,
            constellation_id INTEGER,
            structure_id     INTEGER,
            event_type       TEXT,
            defender_id      INTEGER,
            defender_score   REAL,
            attackers_score  REAL,
            start_time       TEXT,
            cached_at        REAL
        );
        -- Entered by the user: ESI does not publish installed upgrades.
        CREATE TABLE IF NOT EXISTS sov_upgrades_manual (
            system_id INTEGER,
            type_id   INTEGER,
            added_at  REAL,
            PRIMARY KEY (system_id, type_id)
        );
        CREATE TABLE IF NOT EXISTS sov_alliance_names (
            alliance_id INTEGER PRIMARY KEY,
            name        TEXT,
            ticker      TEXT,
            cached_at   REAL
        );
    """)
    conn.commit()


async def _get(client: httpx.AsyncClient, path: str) -> list[dict]:
    """The whole answer or an exception - never a partial or empty stand-in,
    because the caller replaces its table with whatever comes back."""
    r = await client.get(f"{_BASE}/{path}",
                         headers={"X-Compatibility-Date": _SOV_COMPAT_DATE})
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError(f"ESI /{path} answered with something that is not a list")
    return data


async def refresh_sov(conn: sqlite3.Connection, report=None) -> dict:
    """Hubs, campaigns and the sovereignty map, each stored whole or not at all.

    The map is read here as well (not left to its 6-hour cache in
    contracts_helper) because a hub list from now against a map from hours ago
    would put a hub in a system its alliance no longer holds.
    """
    ensure_sov_tables(conn)
    report = report or (lambda **kw: None)
    async with esi_client(timeout=30) as client:
        report(pct=10, detail="sovereignty map")
        smap = await _get(client, "sovereignty/map")
        report(pct=40, detail="hubs")
        hubs = await _get(client, "sovereignty/structures")
        report(pct=70, detail="campaigns")
        camps = await _get(client, "sovereignty/campaigns")
    if not smap:
        # Every system in New Eden has an entry; an empty map is a broken answer.
        raise RuntimeError("ESI returned an empty sovereignty map")
    now = time.time()
    conn.execute("DELETE FROM sov_map_cache")
    conn.executemany(
        "INSERT OR REPLACE INTO sov_map_cache (system_id, alliance_id, faction_id, cached_at)"
        " VALUES (?,?,?,?)",
        [(e.get("system_id"), e.get("alliance_id"), e.get("faction_id"), now)
         for e in smap if e.get("system_id")])
    conn.execute("DELETE FROM sov_hubs")
    conn.executemany(
        "INSERT OR REPLACE INTO sov_hubs VALUES (?,?,?,?,?,?,?)",
        [(h.get("structure_id"), h.get("solar_system_id"), h.get("alliance_id"),
          h.get("vulnerability_occupancy_level"), h.get("vulnerable_start_time"),
          h.get("vulnerable_end_time"), now)
         for h in hubs if h.get("structure_id")])
    conn.execute("DELETE FROM sov_campaigns")
    conn.executemany(
        "INSERT OR REPLACE INTO sov_campaigns VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(c.get("campaign_id"), c.get("solar_system_id"), c.get("constellation_id"),
          c.get("structure_id"), c.get("event_type"), c.get("defender_id"),
          c.get("defender_score"), c.get("attackers_score"), c.get("start_time"), now)
         for c in camps if c.get("campaign_id")])
    conn.commit()
    report(pct=100, detail="")
    return {"systems": len(smap), "hubs": len(hubs), "campaigns": len(camps)}


def sov_last_ok(conn: sqlite3.Connection) -> float | None:
    ensure_sov_tables(conn)
    row = conn.execute("SELECT MAX(cached_at) FROM sov_hubs").fetchone()
    return row[0] if row and row[0] else None


async def alliance_names(conn: sqlite3.Connection, ids) -> dict[int, dict]:
    """alliance_id -> {name, ticker}; a week in the cache, stale kept on failure."""
    ensure_sov_tables(conn)
    ids = sorted({int(i) for i in ids if i})
    out: dict[int, dict] = {}
    miss = []
    now = time.time()
    for aid in ids:
        row = conn.execute("SELECT name, ticker, cached_at FROM sov_alliance_names"
                           " WHERE alliance_id=?", (aid,)).fetchone()
        if row:
            out[aid] = {"name": row[0], "ticker": row[1]}
        if not row or now - (row[2] or 0) > 7 * 86400:
            miss.append(aid)
    if miss:
        try:
            import asyncio
            sem = asyncio.Semaphore(20)
            async with esi_client(timeout=10) as client:
                async def one(aid):
                    async with sem:
                        try:
                            return aid, await client.get(f"{_BASE}/latest/alliances/{aid}/")
                        except Exception:
                            return aid, None
                for aid, r in await asyncio.gather(*[one(a) for a in miss]):
                    if r is None or r.status_code != 200:
                        continue
                    d = r.json()
                    out[aid] = {"name": d.get("name") or str(aid), "ticker": d.get("ticker") or ""}
                    conn.execute("INSERT OR REPLACE INTO sov_alliance_names VALUES (?,?,?,?)",
                                 (aid, out[aid]["name"], out[aid]["ticker"], time.time()))
            conn.commit()
        except Exception:
            pass                          # names are cosmetic; ids still work
    return out


def upgrade_catalogue(conn: sqlite3.Connection) -> list[dict]:
    """Every published hub upgrade in the static data, grouped as the SDE does,
    with CCP's own description of it (empty on a database without the table)."""
    ph = ",".join("?" * len(UPGRADE_GROUPS))
    try:
        rows = conn.execute(
            f"SELECT t.type_id, t.name, t.group_id, d.description FROM sde_types t"
            f" LEFT JOIN sde_sov_upgrades d ON d.type_id = t.type_id"
            f" WHERE t.group_id IN ({ph}) AND t.published=1 ORDER BY t.group_id, t.name",
            list(UPGRADE_GROUPS)).fetchall()
    except sqlite3.OperationalError:
        rows = [r + ("",) for r in conn.execute(
            f"SELECT type_id, name, group_id FROM sde_types"
            f" WHERE group_id IN ({ph}) AND published=1 ORDER BY group_id, name",
            list(UPGRADE_GROUPS))]
    return [{"type_id": t, "name": n, "group_id": g, "group": UPGRADE_GROUPS[g],
             "description": d or ""} for t, n, g, d in rows]


# Location flags ESI gives items inside a structure. That an installed hub
# upgrade carries one of these, with the hub as its location, is what the spec
# suggests; it could not be checked on a real hub (it needs a Director of the
# corporation that owns one), so nothing is shown unless such rows exist.
_STRUCTURE_FLAGS = {"StructureActive": "active", "StructureInactive": "inactive",
                    "StructureOffline": "offline"}


def detected_upgrades(conn: sqlite3.Connection) -> dict[int, list[dict]]:
    """system_id -> upgrades found in stored corporation assets, located in a
    known Sovereignty Hub. No ESI call: these assets are read for Assets anyway."""
    import json
    try:
        hubs = dict(conn.execute("SELECT structure_id, system_id FROM sov_hubs"))
        rows = conn.execute("SELECT corporation_id, data_json FROM corp_assets_cache").fetchall()
    except sqlite3.OperationalError:
        return {}
    if not hubs or not rows:
        return {}
    cat = {u["type_id"]: u for u in upgrade_catalogue(conn)}
    out: dict[int, list[dict]] = {}
    for corp, data in rows:
        try:
            items = json.loads(data) or []
        except (TypeError, ValueError):
            continue
        for a in items:
            sid = hubs.get(a.get("location_id"))
            u = cat.get(a.get("type_id"))
            if sid and u:
                flag = a.get("location_flag") or ""
                out.setdefault(sid, []).append(
                    {**u, "flag": flag, "state": _STRUCTURE_FLAGS.get(flag, flag),
                     "corporation_id": corp})
    return out


def set_manual_upgrades(conn: sqlite3.Connection, system_id: int, type_ids) -> list[int]:
    """Replace what the user says is installed in `system_id`. Only hub upgrades
    from the catalogue are accepted."""
    ensure_sov_tables(conn)
    valid = {u["type_id"] for u in upgrade_catalogue(conn)}
    keep = sorted({int(t) for t in type_ids} & valid)
    conn.execute("DELETE FROM sov_upgrades_manual WHERE system_id=?", (system_id,))
    now = time.time()
    conn.executemany("INSERT INTO sov_upgrades_manual VALUES (?,?,?)",
                     [(system_id, t, now) for t in keep])
    conn.commit()
    return keep


_ICE_COL = "ib.ice_belts"


def _ice_join(conn: sqlite3.Connection) -> str:
    """Ice belts come from DOTLAN, one-time, into the bundled static data; an
    install that has not refreshed its static data yet has no such table."""
    has = conn.execute("SELECT 1 FROM sqlite_master WHERE name='sde_ice_belts'").fetchone()
    return ("LEFT JOIN sde_ice_belts ib ON ib.system_id = ms.system_id" if has
            else "LEFT JOIN (SELECT NULL AS system_id, NULL AS ice_belts) ib ON 0")


def ice_source(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM sde_meta WHERE key='ice_source'").fetchone() \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='sde_meta'").fetchone() else None
    return row[0] if row else ""


def star_label(type_name: str | None) -> str:
    """'Sun A0 (Blue Small)' -> 'A0 (Blue Small)', as the client names the star."""
    if not type_name:
        return ""
    return type_name[4:] if type_name.startswith("Sun ") else type_name


def is_a0(type_name: str | None) -> bool:
    """An A0 star by its TYPE, which is what the client shows. The spectral class
    in the star's statistics disagrees for 122 null-sec systems (F or G)."""
    return star_label(type_name).startswith("A0")


def security_display(sec: float | None) -> float | None:
    """The security status as the client shows it: rounded to one decimal,
    except that anything above 0.0 and below 0.05 shows as 0.1 - otherwise a
    lowsec system would read as 0.0, which is nullsec."""
    if sec is None:
        return None
    if 0.0 < sec < 0.05:
        return 0.1
    return round(sec, 1) + 0.0          # + 0.0 turns -0.0 into 0.0


def systems_for(conn: sqlite3.Connection, alliance_ids) -> list[dict]:
    """Every system held by any of `alliance_ids`, with what the map, ESI and
    the user's own records say about it."""
    ids = [int(a) for a in alliance_ids if a]
    if not ids:
        return []
    return _systems(conn, f"m.alliance_id IN ({','.join('?' * len(ids))})", ids)


def _systems(conn: sqlite3.Connection, where: str, params) -> list[dict]:
    ensure_sov_tables(conn)
    rows = conn.execute(f"""
        SELECT ms.system_id, m.alliance_id, s.name, ms.security, st.name,
               ms.belts, ms.x2d, ms.y2d, c.name, r.name, ms.region_id,
               ms.constellation_id, ms.star_class, {_ICE_COL}
          FROM sde_map_systems ms
          LEFT JOIN sde_types st ON st.type_id = ms.star_type_id
          {_ice_join(conn)}
          LEFT JOIN sov_map_cache m ON m.system_id = ms.system_id
          LEFT JOIN sde_systems s ON s.system_id = ms.system_id
          LEFT JOIN sde_constellations c ON c.constellation_id = ms.constellation_id
          LEFT JOIN sde_regions r ON r.region_id = ms.region_id
         WHERE {where}
         ORDER BY r.name, s.name""", list(params)).fetchall()
    if not rows:
        return []
    sids = [r[0] for r in rows]
    sph = ",".join("?" * len(sids))

    hubs = {r[0]: r[1:] for r in conn.execute(
        f"SELECT system_id, structure_id, adm, vuln_start, vuln_end FROM sov_hubs"
        f" WHERE system_id IN ({sph})", sids)}
    camps: dict[int, list] = {}
    for r in conn.execute(
            f"SELECT system_id, event_type, start_time, defender_score, attackers_score"
            f" FROM sov_campaigns WHERE system_id IN ({sph}) ORDER BY start_time", sids):
        camps.setdefault(r[0], []).append({"event": r[1], "start": r[2],
                                           "defender": r[3], "attackers": r[4]})
    planets: dict[int, dict] = {}
    for sid, kind, n in conn.execute(
            f"SELECT system_id, kind, count FROM sde_system_planets"
            f" WHERE system_id IN ({sph})", sids):
        planets.setdefault(sid, {})[kind] = n
    cat = {u["type_id"]: u for u in upgrade_catalogue(conn)}
    upgrades: dict[int, list] = {}
    for sid, tid in conn.execute(
            f"SELECT system_id, type_id FROM sov_upgrades_manual WHERE system_id IN ({sph})",
            sids):
        if tid in cat:
            upgrades.setdefault(sid, []).append(cat[tid])
    detected = detected_upgrades(conn)
    from app.character.client_logs import last_mined
    logged = last_mined(conn)
    mined: dict[int, dict] = {}
    try:
        cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 30 * 86400))
        for sid, n, last in conn.execute(
                f"SELECT solar_system_id, SUM(quantity), MAX(day) FROM mining_ledger"
                f" WHERE solar_system_id IN ({sph}) AND day >= ? GROUP BY solar_system_id",
                sids + [cutoff]):
            mined[sid] = {"units": n, "last": last}
    except sqlite3.OperationalError:
        pass                              # no ledger read yet

    out = []
    for (sid, aid, name, sec, star, belts, x, y, const, region, rid, cid, spectral, ice) in rows:
        hub = hubs.get(sid)
        out.append({
            "system_id": sid, "alliance_id": aid, "name": name or str(sid),
            "security": sec, "security_shown": security_display(sec),
            "star_class": star_label(star), "spectral": spectral or "", "a0": is_a0(star),
            "belts": belts or 0, "ice_belts": ice or 0, "x": x, "y": y,
            "constellation": const or "", "constellation_id": cid,
            "region": region or "", "region_id": rid,
            "hub": bool(hub), "adm": hub[1] if hub else None,
            "vuln_start": hub[2] if hub else None, "vuln_end": hub[3] if hub else None,
            "campaigns": camps.get(sid, []),
            "planets": planets.get(sid, {}),
            "upgrades": sorted(upgrades.get(sid, []), key=lambda u: (u["group_id"], u["name"])),
            "detected": sorted(detected.get(sid, []), key=lambda u: (u["group_id"], u["name"])),
            "mined": mined.get(sid),
            "logs_last": logged.get(sid),
        })
    return out


def region_map(conn: sqlite3.Connection, region_id: int) -> dict:
    """Every system of a region with who holds it, and the gates inside it -
    the alliance's systems drawn among their neighbours, as the client's region
    map shows them."""
    ensure_sov_tables(conn)
    systems = [
        {"id": sid, "name": name or str(sid), "x": x, "y": y, "alliance_id": aid,
         "faction_id": fid, "security": security_display(sec), "star_class": star_label(star),
         "a0": is_a0(star)}
        for sid, name, x, y, aid, fid, sec, star in conn.execute("""
            SELECT ms.system_id, s.name, ms.x2d, ms.y2d, m.alliance_id, m.faction_id,
                   ms.security, (SELECT st.name FROM sde_types st WHERE st.type_id = ms.star_type_id)
              FROM sde_map_systems ms
              LEFT JOIN sde_systems s ON s.system_id = ms.system_id
              LEFT JOIN sov_map_cache m ON m.system_id = ms.system_id
             WHERE ms.region_id = ? AND ms.x2d IS NOT NULL""", (region_id,))]
    ids = {s["id"] for s in systems}
    links = [(a, b) for a, b in conn.execute("SELECT a, b FROM sde_stargate_links")
             if a in ids and b in ids]
    return {"systems": systems, "links": links}


# ── Upgrades pasted as text ──────────────────────────────────────────────────

_ROMAN = {"I": "1", "II": "2", "III": "3"}
_SEP = r"(?<![\w-])({})(?![\w-])"


def _name_pattern(names) -> re.Pattern:
    alts = sorted({n for n in names if n}, key=len, reverse=True)
    return re.compile(_SEP.format("|".join(re.escape(n) for n in alts)), re.IGNORECASE)


def parse_upgrade_text(conn: sqlite3.Connection, text: str, allowed_systems=None) -> dict:
    """Read systems and hub upgrades out of pasted text.

    No single format is assumed - a spreadsheet row ("1DQ1-A<TAB>Cynosural
    Navigation, Pyerite Prospecting Array 2"), a block per system (the name on
    its own line, upgrades under it) and a mix of both all read the same way:
    a system name sets the system that the upgrade names after it belong to,
    on that line and on the lines below, until the next system name.

    Upgrade levels written as Roman numerals ("... Array II") are read as the
    numbers the static data uses. Nothing is guessed beyond that: a line with
    neither a known system nor a known upgrade is returned as unread, and a
    system the alliance does not hold is reported, not imported.
    """
    cat = upgrade_catalogue(conn)
    by_name = {u["name"].lower(): u for u in cat}
    up_re = _name_pattern(by_name)
    sys_rows = conn.execute("SELECT system_id, name FROM sde_systems").fetchall()
    sys_by_name = {n.lower(): (sid, n) for sid, n in sys_rows if n}
    sys_re = _name_pattern(n for _s, n in sys_rows)
    roman_re = re.compile(r"\b(Array|Detector|Division|Mecha-Tooling)\s+(III|II|I)\b",
                          re.IGNORECASE)

    found: dict[int, dict] = {}
    order: list[int] = []
    unread: list[str] = []
    orphans: list[str] = []
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        line_n = roman_re.sub(lambda m: f"{m.group(1)} {_ROMAN[m.group(2).upper()]}", line)
        hits = []
        for m in sys_re.finditer(line_n):
            hits.append((m.start(), "sys", sys_by_name[m.group(1).lower()]))
        for m in up_re.finditer(line_n):
            hits.append((m.start(), "up", by_name[m.group(1).lower()]))
        # A system name inside an upgrade name is not a system.
        ups_spans = [(m.start(), m.end()) for m in up_re.finditer(line_n)]
        hits = [h for h in hits
                if h[1] == "up" or not any(a <= h[0] < b for a, b in ups_spans)]
        if not hits:
            unread.append(line)
            continue
        for _pos, kind, val in sorted(hits, key=lambda h: h[0]):
            if kind == "sys":
                current = val[0]
                if current not in found:
                    found[current] = {"system_id": current, "name": val[1], "type_ids": []}
                    order.append(current)
            elif current is None:
                orphans.append(val["name"])
            elif val["type_id"] not in found[current]["type_ids"]:
                found[current]["type_ids"].append(val["type_id"])
    systems = []
    for sid in order:
        s = found[sid]
        info = {u["type_id"]: u for u in cat}
        s["names"] = [info[t]["name"] for t in s["type_ids"]]
        s["group_ids"] = [info[t]["group_id"] for t in s["type_ids"]]
        s["held"] = allowed_systems is None or sid in allowed_systems
        systems.append(s)
    return {"systems": systems, "unread": unread, "orphans": orphans}


def import_upgrades(conn: sqlite3.Connection, items, replace: bool = True) -> int:
    """Store parsed upgrades. `replace` makes the text the whole truth for each
    system it names; otherwise its upgrades are added to what is marked."""
    ensure_sov_tables(conn)
    n = 0
    for it in items:
        sid = int(it["system_id"])
        tids = [int(t) for t in it.get("type_ids") or []]
        if not replace:
            tids += [r[0] for r in conn.execute(
                "SELECT type_id FROM sov_upgrades_manual WHERE system_id=?", (sid,))]
        set_manual_upgrades(conn, sid, tids)
        n += 1
    return n


def map_regions(conn: sqlite3.Connection) -> list[dict]:
    """Regions that can be drawn: known space with flattened map positions."""
    return [{"id": rid, "name": name} for rid, name in conn.execute("""
        SELECT r.region_id, r.name FROM sde_regions r
         WHERE r.region_id < 11000000
           AND EXISTS (SELECT 1 FROM sde_map_systems ms
                        WHERE ms.region_id = r.region_id AND ms.x2d IS NOT NULL)
         ORDER BY r.name""")]


def system_detail(conn: sqlite3.Connection, system_id: int, alliance_ids) -> dict | None:
    """One system of known space, with what was mined there and its gate
    neighbours. `own` says whether one of `alliance_ids` holds it."""
    rows = _systems(conn, "ms.system_id = ?", [system_id])
    if not rows:
        return None
    row = rows[0]
    row["own"] = row["alliance_id"] in {int(a) for a in alliance_ids if a}
    fac = conn.execute("SELECT faction_id FROM sov_map_cache WHERE system_id=?",
                       (system_id,)).fetchone()
    row["faction_id"] = fac[0] if fac else None
    try:
        cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 30 * 86400))
        row["mined_types"] = [
            {"type_id": t, "name": n or str(t), "units": q, "last": last, "characters": k}
            for t, n, q, last, k in conn.execute("""
                SELECT l.type_id, st.name, SUM(l.quantity), MAX(l.day),
                       COUNT(DISTINCT l.character_id)
                  FROM mining_ledger l LEFT JOIN sde_types st ON st.type_id = l.type_id
                 WHERE l.solar_system_id = ? AND l.day >= ?
                 GROUP BY l.type_id ORDER BY SUM(l.quantity) DESC""", (system_id, cutoff))]
    except sqlite3.OperationalError:
        row["mined_types"] = []
    near = []
    for (other,) in conn.execute(
            "SELECT b FROM sde_stargate_links WHERE a=? UNION SELECT a FROM sde_stargate_links WHERE b=?",
            (system_id, system_id)):
        r = conn.execute("""SELECT s.name, ms.security, m.alliance_id, m.faction_id, rg.name
                              FROM sde_map_systems ms
                              LEFT JOIN sde_systems s ON s.system_id = ms.system_id
                              LEFT JOIN sov_map_cache m ON m.system_id = ms.system_id
                              LEFT JOIN sde_regions rg ON rg.region_id = ms.region_id
                             WHERE ms.system_id=?""", (other,)).fetchone()
        if r:
            near.append({"system_id": other, "name": r[0], "security": security_display(r[1]), "true_sec": r[1],
                         "alliance_id": r[2], "faction_id": r[3], "region": r[4]})
    row["neighbours"] = sorted(near, key=lambda n: n["name"] or "")
    from app.character.client_logs import sessions_in
    row["log_sessions"] = sessions_in(conn, system_id)
    from app.web.sov_anomalies import for_system
    row["anomalies"] = for_system(
        row["security"], row["region"],
        [u["name"] for u in row["upgrades"] + row["detected"]])
    return row


# ── Map overlays: activity in each system ───────────────────────────────────
#
# What DOTLAN colours its maps by, from the same public ESI endpoints: jumps,
# ship / NPC / pod kills in the last hour, plus incursions. ESI rebuilds them
# hourly. A system missing from a list had none (the lists only carry systems
# with activity), so after a successful read a missing system is 0, not
# unknown.

def ensure_activity_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS map_activity (
            system_id  INTEGER PRIMARY KEY,
            ship_jumps INTEGER,
            ship_kills INTEGER,
            npc_kills  INTEGER,
            pod_kills  INTEGER
        );
        CREATE TABLE IF NOT EXISTS map_activity_meta (
            kind       TEXT PRIMARY KEY,
            fetched_at REAL,
            data_time  TEXT
        );
        CREATE TABLE IF NOT EXISTS map_incursions (
            system_id INTEGER PRIMARY KEY,
            state     TEXT,
            staging   INTEGER,
            has_boss  INTEGER
        );
    """)


async def refresh_activity(conn: sqlite3.Connection, report=None) -> dict:
    """Jumps, kills and incursions; each list replaced only when read whole."""
    ensure_activity_tables(conn)
    report = report or (lambda **kw: None)
    out = {}
    async with esi_client(timeout=30) as client:
        async def get(path):
            r = await client.get(f"{_BASE}/{path}")
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                raise RuntimeError(f"ESI /{path} answered with something that is not a list")
            return data, r.headers.get("last-modified")
        report(pct=10, detail="jumps")
        jumps, jt = await get("universe/system_jumps")
        report(pct=40, detail="kills")
        kills, kt = await get("universe/system_kills")
        report(pct=70, detail="incursions")
        incs, it = await get("incursions")
    now = time.time()
    rows: dict[int, list] = {}
    for j in jumps:
        rows.setdefault(j["system_id"], [0, 0, 0, 0])[0] = j.get("ship_jumps") or 0
    for k in kills:
        r = rows.setdefault(k["system_id"], [0, 0, 0, 0])
        r[1], r[2], r[3] = k.get("ship_kills") or 0, k.get("npc_kills") or 0, k.get("pod_kills") or 0
    conn.execute("DELETE FROM map_activity")
    conn.executemany("INSERT INTO map_activity VALUES (?,?,?,?,?)",
                     [(sid, *v) for sid, v in rows.items()])
    conn.execute("DELETE FROM map_incursions")
    for inc in incs:
        staging = inc.get("staging_solar_system_id")
        for sid in inc.get("infested_solar_systems") or []:
            conn.execute("INSERT OR REPLACE INTO map_incursions VALUES (?,?,?,?)",
                         (sid, inc.get("state"), 1 if sid == staging else 0,
                          1 if inc.get("has_boss") else 0))
    conn.executemany("INSERT OR REPLACE INTO map_activity_meta VALUES (?,?,?)",
                     [("jumps", now, jt), ("kills", now, kt), ("incursions", now, it)])
    conn.commit()
    report(pct=100, detail="")
    out.update(systems=len(rows), incursions=len(incs))
    return out


def activity_last_ok(conn: sqlite3.Connection) -> float | None:
    ensure_activity_tables(conn)
    row = conn.execute("SELECT MIN(fetched_at) FROM map_activity_meta").fetchone()
    return row[0] if row and row[0] else None


def region_overlays(conn: sqlite3.Connection, system_ids) -> dict:
    """Per system, every value the map can colour by, and when each was read.

    Reads whole tables and keeps the wanted systems in Python: the whole of
    New Eden is ~5 500 ids, past the 999 bound parameters some SQLite builds
    (Android's among them) allow in one statement.
    """
    ensure_activity_tables(conn)
    ensure_sov_tables(conn)
    ids = set(system_ids)
    if not ids:
        return {"values": {}, "meta": {}}
    vals: dict[int, dict] = {sid: {} for sid in ids}

    def rows(sql, *params):
        try:
            return [r for r in conn.execute(sql, params) if r[0] in ids]
        except sqlite3.OperationalError:
            return []

    have_activity = conn.execute("SELECT COUNT(*) FROM map_activity_meta"
                                 " WHERE kind IN ('jumps','kills')").fetchone()[0] == 2
    if have_activity:
        for sid in ids:
            vals[sid].update(jumps=0, ship_kills=0, npc_kills=0, pod_kills=0)
        for sid, j, sk, nk, pk in rows("SELECT * FROM map_activity"):
            vals[sid].update(jumps=j, ship_kills=sk, npc_kills=nk, pod_kills=pk)
    for sid, adm in rows("SELECT system_id, adm FROM sov_hubs"):
        vals[sid]["adm"] = adm
    for sid, act, ci in rows("SELECT solar_system_id, activity, cost_index FROM sci_cache"
                             " WHERE activity IN ('manufacturing','reaction','invention')"):
        vals[sid]["sci_" + act] = ci
    for sid, n in rows("SELECT system_id, COUNT(*) FROM sde_stations GROUP BY system_id"):
        vals[sid]["stations"] = n
    for (sid,) in rows("SELECT DISTINCT system_id FROM sov_campaigns"):
        vals[sid]["contested"] = 1
    for sid, state, staging in rows("SELECT system_id, state, staging FROM map_incursions"):
        vals[sid]["incursion"] = state + (" (staging)" if staging else "")
    for sid, belts in rows("SELECT system_id, belts FROM sde_map_systems"):
        vals[sid]["belts"] = belts
    for sid, n in rows("SELECT system_id, ice_belts FROM sde_ice_belts"):
        vals[sid]["ice_belts"] = n
    from app.character.client_logs import last_mined
    for sid, ts in last_mined(conn).items():
        if sid in vals:
            vals[sid]["last_mined"] = ts
    for sid, ts in ice_last_mined(conn, ids).items():
        vals[sid]["ice_last"] = ts
    for sid, n in rows("SELECT system_id, COUNT(*) FROM sov_upgrades_manual GROUP BY system_id"):
        vals[sid]["upgrades"] = n
    meta = {k: {"fetched_at": f, "data_time": d} for k, f, d in
            conn.execute("SELECT kind, fetched_at, data_time FROM map_activity_meta")}
    try:
        meta["sci"] = {"fetched_at": conn.execute("SELECT MAX(cached_at) FROM sci_cache").fetchone()[0]}
    except sqlite3.OperationalError:
        pass
    return {"values": {str(k): v for k, v in vals.items() if v}, "meta": meta}


def universe_map(conn: sqlite3.Connection) -> dict:
    """Every system of known space that has a map position, with its region,
    who holds it and its star, and every gate between two of them."""
    ensure_sov_tables(conn)
    systems = [
        {"id": sid, "name": name or str(sid), "x": x, "y": y, "region_id": rid,
         "alliance_id": aid, "faction_id": fid, "security": security_display(sec),
         "true_sec": sec,
         "star_class": star_label(star), "a0": is_a0(star), "ice": ice or 0}
        for sid, name, x, y, rid, aid, fid, sec, star, ice in conn.execute(f"""
            SELECT ms.system_id, s.name, ms.x2d, ms.y2d, ms.region_id, m.alliance_id,
                   m.faction_id, ms.security, st.name, {_ICE_COL}
              FROM sde_map_systems ms
              {_ice_join(conn)}
              LEFT JOIN sde_systems s ON s.system_id = ms.system_id
              LEFT JOIN sov_map_cache m ON m.system_id = ms.system_id
              LEFT JOIN sde_types st ON st.type_id = ms.star_type_id
             WHERE ms.x2d IS NOT NULL AND ms.region_id < 11000000""")]
    ids = {s["id"] for s in systems}
    links = [(a, b) for a, b in conn.execute("SELECT a, b FROM sde_stargate_links")
             if a in ids and b in ids]
    regions = {rid: name for rid, name in conn.execute("SELECT region_id, name FROM sde_regions")}
    return {"systems": systems, "links": links, "ice_source": ice_source(conn),
            "regions": {str(r): regions.get(r, str(r)) for r in {s["region_id"] for s in systems}}}


ICE_GROUP = 465


def ice_last_mined(conn: sqlite3.Connection, system_ids) -> dict[int, float]:
    """system_id -> when your characters last mined ice there, from the
    client's logs. Ice fields are anomalies the server spawns: no static data
    or client file says where they are, so this is the only honest answer."""
    ids = set(system_ids)
    try:
        ice = {n for (n,) in conn.execute("SELECT name FROM sde_types WHERE group_id=?",
                                          (ICE_GROUP,))}
        rows = conn.execute("SELECT system_id, end_ts, types_json FROM client_mining_sessions"
                            ).fetchall()
    except sqlite3.OperationalError:
        return {}
    import json
    out: dict[int, float] = {}
    for sid, end, types in rows:
        if sid in ids and ice & set(json.loads(types or "{}")):
            out[sid] = max(out.get(sid, 0), end)
    return out
