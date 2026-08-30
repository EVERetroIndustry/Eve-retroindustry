"""
Loading character assets from ESI (paginated).
Returns materials available at a given station/structure.
"""
from __future__ import annotations
from dataclasses import dataclass
import time
import sqlite3
import json
import httpx

ESI_BASE  = "https://esi.evetech.net/latest"
CACHE_TTL = 60 * 10  # 10 minutes (assets change)


@dataclass
class CharAsset:
    item_id:            int
    type_id:            int
    location_id:        int
    location_flag:      str
    quantity:           int
    is_singleton:       bool   # True = unique item (ship, fitted module…)
    is_blueprint_copy:  bool   # True = BPC (blueprint copy with no market price)


def ensure_assets_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS char_assets_cache (
            character_id INTEGER NOT NULL,
            data_json    TEXT NOT NULL,
            cached_at    REAL
        )
    """)
    conn.commit()


def _load_cache(conn: sqlite3.Connection, character_id: int) -> list[dict] | None:
    row = conn.execute(
        "SELECT data_json, cached_at FROM char_assets_cache WHERE character_id=?",
        (character_id,)
    ).fetchone()
    if row and (time.time() - (row[1] or 0)) < CACHE_TTL:
        return json.loads(row[0])
    return None


def _save_cache(conn: sqlite3.Connection, character_id: int, data: list[dict]):
    conn.execute("DELETE FROM char_assets_cache WHERE character_id=?", (character_id,))
    conn.execute(
        "INSERT INTO char_assets_cache (character_id, data_json, cached_at) VALUES (?,?,?)",
        (character_id, json.dumps(data), time.time())
    )
    conn.commit()


async def fetch_assets(
    client: httpx.AsyncClient,
    character_id: int,
    access_token: str,
    conn: sqlite3.Connection,
    force_refresh: bool = False,
) -> list[CharAsset]:
    """Loads all of the character's assets (paginated), with caching."""
    if not force_refresh:
        cached = _load_cache(conn, character_id)
        if cached is not None:
            return _parse_assets(cached)

    headers = {"Authorization": f"Bearer {access_token}"}
    all_items: list[dict] = []
    page = 1

    while True:
        r = await client.get(
            f"{ESI_BASE}/characters/{character_id}/assets/",
            params={"datasource": "tranquility", "page": page},
            headers=headers,
            timeout=20,
        )
        r.raise_for_status()
        items = r.json()
        all_items.extend(items)

        total_pages = int(r.headers.get("x-pages", 1))
        if page >= total_pages:
            break
        page += 1

    _save_cache(conn, character_id, all_items)
    return _parse_assets(all_items)


def _parse_assets(raw: list[dict]) -> list[CharAsset]:
    result = []
    for item in raw:
        result.append(CharAsset(
            item_id            = item["item_id"],
            type_id            = item["type_id"],
            location_id        = item["location_id"],
            location_flag      = item.get("location_flag", "Hangar"),
            quantity           = item.get("quantity", 1),
            is_singleton       = item.get("is_singleton", False),
            is_blueprint_copy  = item.get("is_blueprint_copy", False),
        ))
    return result


def ensure_corp_assets_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS corp_assets_cache (
            corporation_id INTEGER NOT NULL,
            data_json      TEXT NOT NULL,
            cached_at      REAL
        )
    """)
    conn.commit()


def _load_corp_cache(conn: sqlite3.Connection, corporation_id: int,
                     any_age: bool = False) -> list[dict] | None:
    """any_age serves a stale copy rather than nothing: used when a character has
    been refused access, where the alternative is not fresher data but no data."""
    row = conn.execute(
        "SELECT data_json, cached_at FROM corp_assets_cache WHERE corporation_id=?",
        (corporation_id,)
    ).fetchone()
    if row and (any_age or (time.time() - (row[1] or 0)) < CACHE_TTL):
        return json.loads(row[0])
    return None


def _save_corp_cache(conn: sqlite3.Connection, corporation_id: int, data: list[dict]):
    conn.execute("DELETE FROM corp_assets_cache WHERE corporation_id=?", (corporation_id,))
    conn.execute(
        "INSERT INTO corp_assets_cache (corporation_id, data_json, cached_at) VALUES (?,?,?)",
        (corporation_id, json.dumps(data), time.time())
    )
    conn.commit()


# How long a "this character may not read that corporation's assets" answer is
# trusted. Roles do change, so it is not permanent - but a role granted five
# minutes ago is not worth twelve wasted 403s on every sync in between.
_CORP_DENIED_TTL = 6 * 3600


def _ensure_corp_denied_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS corp_assets_denied (
            corp_id      INTEGER NOT NULL,
            character_id INTEGER NOT NULL,
            at           REAL    NOT NULL,
            PRIMARY KEY (corp_id, character_id)
        )""")
    conn.commit()


def _mark_corp_denied(conn: sqlite3.Connection, corp_id: int, character_id: int) -> None:
    try:
        _ensure_corp_denied_table(conn)
        conn.execute("INSERT OR REPLACE INTO corp_assets_denied VALUES (?,?,?)",
                     (corp_id, character_id, time.time()))
        conn.commit()
    except Exception:
        pass          # a cache that cannot be written must not break the fetch


def corp_access_denied(conn: sqlite3.Connection, corp_id: int,
                       character_id: int) -> bool:
    try:
        _ensure_corp_denied_table(conn)
        row = conn.execute(
            "SELECT at FROM corp_assets_denied WHERE corp_id=? AND character_id=?",
            (corp_id, character_id)).fetchone()
    except Exception:
        return False
    return bool(row and (time.time() - (row[0] or 0)) < _CORP_DENIED_TTL)


def clear_corp_denied(conn: sqlite3.Connection) -> None:
    """Forget every refusal - what re-adding a character or a manual sync does,
    so a newly granted role takes effect at once instead of after the TTL."""
    try:
        _ensure_corp_denied_table(conn)
        conn.execute("DELETE FROM corp_assets_denied")
        conn.commit()
    except Exception:
        pass


async def fetch_corp_assets(
    client: httpx.AsyncClient,
    character_id: int,
    access_token: str,
    conn: sqlite3.Connection,
    force_refresh: bool = False,
) -> tuple[int, list[CharAsset]]:
    """Fetch corporation assets. Returns (corp_id, assets). Empty list if no ESI access."""
    headers = {"Authorization": f"Bearer {access_token}"}
    char_r = await client.get(
        f"{ESI_BASE}/characters/{character_id}/",
        params={"datasource": "tranquility"},
        headers=headers,
        timeout=10,
    )
    char_r.raise_for_status()
    corp_id: int = char_r.json()["corporation_id"]

    if not force_refresh:
        cached = _load_corp_cache(conn, corp_id)
        if cached is not None:
            return corp_id, _parse_assets(cached)
    # Asked before and refused: do not spend a 4xx on finding out again.
    if corp_access_denied(conn, corp_id, character_id):
        cached = _load_corp_cache(conn, corp_id, any_age=True)
        return corp_id, _parse_assets(cached or [])

    all_items: list[dict] = []
    page = 1

    while True:
        r = await client.get(
            f"{ESI_BASE}/corporations/{corp_id}/assets/",
            params={"datasource": "tranquility", "page": page},
            headers=headers,
            timeout=20,
        )
        if r.status_code in (401, 403):
            # Remember the refusal. Without this every Sync All asked again for
            # every character that lacks the role - twelve characters, twelve
            # 403s, and a 4xx costs five rate-limit tokens plus a slot in the
            # error budget, which is GLOBAL: spend it and ESI's governor pauses
            # everything, including the dashboard, which then spins and gives up.
            # Corp divisions already had this negative cache; corp assets did not.
            _mark_corp_denied(conn, corp_id, character_id)
            return corp_id, []
        r.raise_for_status()
        items = r.json()
        all_items.extend(items)

        total_pages = int(r.headers.get("x-pages", 1))
        if page >= total_pages:
            break
        page += 1

    _save_corp_cache(conn, corp_id, all_items)
    return corp_id, _parse_assets(all_items)


def assets_at_location(assets: list[CharAsset], location_id: int) -> dict[int, int]:
    """
    Returns {type_id: total_quantity} for a given station/structure.
    Ignores singletons (ships, unique items).
    """
    result: dict[int, int] = {}
    for a in assets:
        if a.location_id != location_id or a.is_singleton:
            continue
        result[a.type_id] = result.get(a.type_id, 0) + a.quantity
    return result


def assets_at_locations(
    assets: list[CharAsset], location_ids: "set[int] | list[int]"
) -> dict[int, int]:
    """
    Returns {type_id: total_quantity} aggregated across MULTIPLE stations/structures.
    Ignores singletons. Used for selecting stock sources in the production plan
    (the user checks which stations the inventory should be counted from).
    """
    wanted = set(location_ids)
    result: dict[int, int] = {}
    for a in assets:
        if a.is_singleton or a.location_id not in wanted:
            continue
        result[a.type_id] = result.get(a.type_id, 0) + a.quantity
    return result
