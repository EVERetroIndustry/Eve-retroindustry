"""
Planetary Interaction - a character's planet colonies and extractor timers.

ESI: GET /characters/{id}/planets/            (colony list)
     GET /characters/{id}/planets/{planet_id}/ (pins incl. extractor expiry)
Scope: esi-planets.manage_planets.v1

The headline value (à la RIFT) is the extractor expiry countdown - PI is
"set and forget until the extractor program runs out", so knowing when to go
reset it is what matters.
"""
from __future__ import annotations
import httpx

ESI_BASE = "https://esi.evetech.net/latest"

PLANET_TYPES: dict[str, str] = {
    "temperate": "Temperate", "barren": "Barren", "oceanic": "Oceanic",
    "ice": "Ice", "gas": "Gas", "lava": "Lava", "storm": "Storm", "plasma": "Plasma",
}


def planet_type_label(t: str) -> str:
    return PLANET_TYPES.get(t, (t or "").title() or "Planet")


async def fetch_planets(client: httpx.AsyncClient, char_id: int, token: str):
    """Colony list for a character. Returns the list on success, the string
    "forbidden" if the token lacks the PI scope (so the caller can prompt a
    re-login), or None on any other error."""
    try:
        r = await client.get(
            f"{ESI_BASE}/characters/{char_id}/planets/",
            params={"datasource": "tranquility"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()
        if r.status_code == 403:
            return "forbidden"
    except Exception:
        pass
    return None


async def fetch_planet_detail(client: httpx.AsyncClient, char_id: int,
                              planet_id: int, token: str) -> dict | None:
    """Colony detail: {links, pins, routes}. Extractor pins carry
    `extractor_details` + `expiry_time`; factory pins carry `factory_details`."""
    try:
        r = await client.get(
            f"{ESI_BASE}/characters/{char_id}/planets/{planet_id}/",
            params={"datasource": "tranquility"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None
