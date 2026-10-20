"""
Market orders - active and historical, for character and corporation.

ESI endpoints:
  Character:
    GET /characters/{id}/orders/           → active orders
    GET /characters/{id}/orders/history/   → last ~90 days (paginated)
  Corporation (requires role Accountant/Trader → otherwise 403):
    GET /corporations/{id}/orders/
    GET /corporations/{id}/orders/history/

Scope: esi-markets.read_character_orders.v1 / esi-markets.read_corporation_orders.v1
"""
from __future__ import annotations
import httpx

from app.esi.client import IncompleteAnswer

ESI_BASE = "https://esi.evetech.net/latest"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


async def _get_all(client: httpx.AsyncClient, url: str, token: str, pages: int = 5) -> list[dict]:
    """Every page, or IncompleteAnswer - never the pages before a failure."""
    out: list[dict] = []
    for page in range(1, pages + 1):
        try:
            r = await client.get(url, params={"page": page}, headers=_auth(token), timeout=15)
        except Exception as exc:
            raise IncompleteAnswer(f"ESI did not answer ({type(exc).__name__}).") from exc
        if r.status_code != 200:
            raise IncompleteAnswer(f"ESI returned HTTP {r.status_code}.")
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if page >= int(r.headers.get("x-pages", 1)):
            break
    return out


async def fetch_orders(client, char_id: int, token: str) -> list[dict]:
    """Active orders of a character (single page). IncompleteAnswer on failure:
    an empty list is a character with no orders, nothing else."""
    try:
        r = await client.get(f"{ESI_BASE}/characters/{char_id}/orders/",
                             headers=_auth(token), timeout=15)
    except Exception as exc:
        raise IncompleteAnswer(f"ESI did not answer ({type(exc).__name__}).") from exc
    if r.status_code != 200:
        raise IncompleteAnswer(f"ESI returned HTTP {r.status_code}.")
    return r.json()


async def fetch_orders_history(client, char_id: int, token: str) -> list[dict]:
    return await _get_all(client, f"{ESI_BASE}/characters/{char_id}/orders/history/", token)


async def fetch_corp_orders(client, corp_id: int, token: str
                            ) -> tuple[list[dict] | None, str | None]:
    try:
        r = await client.get(f"{ESI_BASE}/corporations/{corp_id}/orders/",
                             params={"page": 1}, headers=_auth(token), timeout=15)
        if r.status_code == 200:
            out = r.json()
            for page in range(2, int(r.headers.get("x-pages", 1)) + 1):
                rp = await client.get(f"{ESI_BASE}/corporations/{corp_id}/orders/",
                                     params={"page": page}, headers=_auth(token), timeout=15)
                if rp.status_code != 200:
                    # Not the pages so far: those would read as the whole list.
                    return None, f"ESI returned HTTP {rp.status_code} for page {page}."
                out.extend(rp.json())
            return out, None
        if r.status_code == 403:
            return None, "This character lacks the corporation role to read market orders (Accountant / Trader)."
        return None, f"ESI returned HTTP {r.status_code}."
    except Exception as exc:
        return None, str(exc)


async def fetch_corp_orders_history(client, corp_id: int, token: str) -> list[dict]:
    return await _get_all(client, f"{ESI_BASE}/corporations/{corp_id}/orders/history/", token)
