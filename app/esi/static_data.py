"""Static game data that ESI no longer serves.

ESI compatibility date 2026-11-19 removed the static /universe routes (types,
groups, systems, constellations, stations, planets, regions, ...). CCP serves
the same data from the SDE API, https://sde.evetech.net, which publishes a new
release about ten minutes after every Tranquility deployment.

Order of lookup, everywhere:
  1. the bundled SDE (sde_* tables in the user's DB) - no network at all;
  2. the SDE API, one record per request - only for things newer than the
     bundle (a new item on the market, a station added by a patch);
  3. None, with a line in the log. A failure that only returns None looks
     exactly like "does not exist", and that has hidden bugs here before.

The helpers return the same shape the ESI routes did (snake_case keys, plain
English names), so callers did not have to change what they read.

Rules for the SDE API (checked live on 2026-11-20):
  * always go through `latest/`: it redirects to the current release, and only
    the current and the previous release are kept - a stored release URL dies;
  * a missing record is 404 {"error": "Not found"};
  * names are dicts of languages; we use "en", as ESI did by default;
  * NPC stations carry no name - their name comes from POST /universe/names,
    which ESI keeps (as it keeps /universe/ids and /universe/structures).
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import httpx

SDE_API = "https://sde.evetech.net/tranquility/latest"
ESI_NAMES = "https://esi.evetech.net/universe/names/"

_SEM = asyncio.Semaphore(20)

# A record the SDE does not have is not asked for again for a while: a type id
# in the assets that no release knows would otherwise cost a request on every
# page load. (ESI's 404 for /universe/types was kept 24 h by http_cache.) New
# content arrives with a release, so a few hours is plenty.
_MISSING_TTL = 6 * 3600
_missing: dict[tuple[str, int], float] = {}

# Wormhole and Abyssal regions: no public contracts there, never offered.
_LAST_KSPACE_REGION = 11_000_000


def _en(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("en")
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _log(msg: str) -> None:
    print(f"[static-data] {msg}", flush=True)


async def sde_record(client: httpx.AsyncClient, table: str, key: int) -> dict | None:
    """One record of the current SDE release. None when it does not exist.

    Raises on anything else (network, 5xx): the caller decides how to say so.
    """
    k = (table, int(key))
    if time.monotonic() - _missing.get(k, -_MISSING_TTL) < _MISSING_TTL:
        return None
    async with _SEM:
        r = await client.get(f"{SDE_API}/{table}/{int(key)}.json",
                             follow_redirects=True, timeout=15)
    if r.status_code == 404:
        _missing[k] = time.monotonic()
        return None
    r.raise_for_status()
    return r.json()


async def _record_or_none(client, table: str, key: int) -> dict | None:
    try:
        return await sde_record(client, table, key)
    except Exception as exc:                       # noqa: BLE001 - logged, not hidden
        _log(f"{table}/{key} failed: {type(exc).__name__}: {exc}")
        return None


# ── types and groups ─────────────────────────────────────────────────────────

def type_from_record(d: dict) -> dict | None:
    """SDE API type record -> the fields we used from ESI /universe/types/{id}."""
    name = _en(d.get("name"))
    if not name:
        return None
    return {
        "type_id": d.get("typeID"),
        "name": name,
        "group_id": d.get("groupID"),
        "market_group_id": d.get("marketGroupID"),
        "published": bool(d.get("published", False)),
        "volume": d.get("volume"),
        "packaged_volume": d.get("packagedVolume", d.get("volume")),
    }


async def fetch_type(client: httpx.AsyncClient, type_id: int) -> dict | None:
    """A type the bundled SDE may not have yet (new items trade before a rebuild)."""
    d = await _record_or_none(client, "types", type_id)
    return type_from_record(d) if d else None


async def fetch_group(client: httpx.AsyncClient, group_id: int) -> dict | None:
    d = await _record_or_none(client, "groups", group_id)
    if not d or not _en(d.get("name")):
        return None
    return {"group_id": d.get("groupID"), "name": _en(d["name"]),
            "category_id": d.get("categoryID"),
            "published": bool(d.get("published", False))}


# ── map ──────────────────────────────────────────────────────────────────────

def _bundled_system(conn: sqlite3.Connection | None, system_id: int) -> dict | None:
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT s.name, m.security, m.constellation_id, COALESCE(m.region_id, s.region_id)"
            "  FROM sde_systems s LEFT JOIN sde_map_systems m ON m.system_id = s.system_id"
            " WHERE s.system_id = ?", (system_id,)).fetchone()
    except sqlite3.OperationalError:
        return None                                # not refreshed from the bundle yet
    if not row:
        return None
    return {"system_id": system_id, "name": row[0], "security_status": row[1],
            "constellation_id": row[2], "region_id": row[3]}


async def fetch_system(client: httpx.AsyncClient | None, system_id: int,
                       conn: sqlite3.Connection | None = None) -> dict | None:
    """name, security_status, constellation_id, region_id of a solar system.

    The SDE record carries regionID itself, so the old system -> constellation
    -> region walk (two ESI calls) is one request now, and usually none.
    """
    local = _bundled_system(conn, system_id)
    if local and local["security_status"] is not None and local["region_id"]:
        return local
    if client is None:
        return local
    d = await _record_or_none(client, "mapSolarSystems", system_id)
    if not d:
        return local
    return {"system_id": system_id, "name": _en(d.get("name")),
            "security_status": d.get("securityStatus"),
            "constellation_id": d.get("constellationID"),
            "region_id": d.get("regionID")}


async def _esi_names(client: httpx.AsyncClient, ids: list[int]) -> dict[int, str]:
    """POST /universe/names - stays on ESI. Unknown ids make the whole call 404,
    so a failed batch is retried one id at a time."""
    out: dict[int, str] = {}
    if not ids:
        return out
    try:
        r = await client.post(ESI_NAMES, json=ids, timeout=15)
        if r.status_code == 200:
            return {it["id"]: it["name"] for it in r.json() if it.get("name")}
        if len(ids) == 1:
            return out
    except Exception as exc:                       # noqa: BLE001
        _log(f"universe/names failed for {len(ids)} id(s): {type(exc).__name__}: {exc}")
        return out
    for i in ids:
        out.update(await _esi_names(client, [i]))
    return out


async def fetch_station(client: httpx.AsyncClient | None, station_id: int,
                        conn: sqlite3.Connection | None = None) -> dict | None:
    """name, system_id, region_id of an NPC station (not a player structure)."""
    if conn is not None:
        try:
            row = conn.execute("SELECT name, system_id, region_id FROM sde_stations"
                               " WHERE station_id = ?", (station_id,)).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row and row[0] and row[1]:
            return {"station_id": station_id, "name": row[0], "system_id": row[1],
                    "region_id": row[2]}
    if client is None:
        return None
    d = await _record_or_none(client, "npcStations", station_id)
    if not d or not d.get("solarSystemID"):
        return None
    system_id = d["solarSystemID"]
    names = await _esi_names(client, [station_id])
    system = await fetch_system(client, system_id, conn)
    return {"station_id": station_id, "name": names.get(station_id),
            "system_id": system_id, "region_id": (system or {}).get("region_id")}


def _roman(n: int) -> str:
    out = ""
    for value, digits in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
                          (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
                          (5, "V"), (4, "IV"), (1, "I")):
        while n >= value:
            out += digits
            n -= value
    return out


async def fetch_planet_name(client: httpx.AsyncClient, planet_id: int,
                            conn: sqlite3.Connection | None = None) -> str | None:
    """"Jita IV", or the planet's own name where it has one ("Starkman Prime").

    Checked against ESI /universe/planets for 49 planets in high, null and
    wormhole space: the only one not "<system> <roman>" carried uniqueName.
    """
    d = await _record_or_none(client, "mapPlanets", planet_id)
    if not d:
        return None
    unique = _en(d.get("uniqueName"))
    if unique:
        return unique
    idx, sys_id = d.get("celestialIndex"), d.get("solarSystemID")
    if not idx or not sys_id:
        return None
    system = await fetch_system(client, sys_id, conn)
    sname = (system or {}).get("name")
    return f"{sname} {_roman(int(idx))}" if sname else None


def all_regions(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """Known-space regions (id, name) sorted by name, straight from the bundle."""
    try:
        rows = conn.execute("SELECT region_id, name FROM sde_regions WHERE region_id < ?",
                            (_LAST_KSPACE_REGION,)).fetchall()
    except sqlite3.OperationalError:
        return []
    return sorted(((r[0], r[1] or str(r[0])) for r in rows), key=lambda x: x[1])
