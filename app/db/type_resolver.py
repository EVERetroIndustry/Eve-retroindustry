"""
Resolving type_id → name.
Priority: local SDE -> SDE API (stored for next time, see _save_to_sde).
ESI dropped /universe/types at compatibility date 2026-11-19.
"""
import asyncio
import sqlite3
import httpx

from app.esi.static_data import fetch_type


def resolve_name_sync(conn: sqlite3.Connection, type_id: int) -> str | None:
    """Return the name from the local SDE. None if missing."""
    row = conn.execute("SELECT name FROM sde_types WHERE type_id=?", (type_id,)).fetchone()
    return row[0] if row else None


def _save_to_sde(conn: sqlite3.Connection, type_id: int, d: dict):
    """Store a type learned at runtime. It goes into discovered_types as well as
    sde_types: a bundle refresh replaces sde_types wholesale, and a name kept
    only there was lost on every SDE update (then fetched again)."""
    from app.web.prices_helper import record_discovered_type, _apply_discovered_types
    record_discovered_type(conn, type_id, d)
    _apply_discovered_types(conn)


async def _fetch_from_sde_api(client: httpx.AsyncClient, type_id: int) -> dict | None:
    return await fetch_type(client, type_id)


async def resolve_name(
    conn: sqlite3.Connection,
    type_id: int,
    client: httpx.AsyncClient,
) -> str:
    """
    Return the type name. If missing from the bundled SDE, ask the SDE API and store it.
    """
    name = resolve_name_sync(conn, type_id)
    if name:
        return name

    data = await _fetch_from_sde_api(client, type_id)
    if data:
        _save_to_sde(conn, type_id, data)
        return data["name"]

    return f"Unknown ({type_id})"


async def resolve_names_bulk(
    conn: sqlite3.Connection,
    type_ids: list[int],
    client: httpx.AsyncClient,
) -> dict[int, str]:
    """Translate a list of type_ids to names in parallel (bundled SDE + SDE API fallback)."""
    tasks = [resolve_name(conn, tid, client) for tid in type_ids]
    names = await asyncio.gather(*tasks)
    return dict(zip(type_ids, names))
