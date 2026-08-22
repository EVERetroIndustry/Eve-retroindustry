"""
Contracts - personal, corporation and public (regional).

ESI endpoints:
  Character (scope esi-contracts.read_character_contracts.v1):
    GET /characters/{id}/contracts/                     → contracts (paginated)
    GET /characters/{id}/contracts/{cid}/items/         → items
  Corporation (scope esi-contracts.read_corporation_contracts.v1, role Accountant):
    GET /corporations/{id}/contracts/
    GET /corporations/{id}/contracts/{cid}/items/
  Public (no auth):
    GET /contracts/public/{region_id}/                  → metadata (paginated)
    GET /contracts/public/items/{cid}/                  → items
"""
from __future__ import annotations
import asyncio
from typing import NamedTuple

import httpx

ESI_BASE = "https://esi.evetech.net/latest"

CONTRACT_TYPE_LABELS: dict[str, str] = {
    "item_exchange": "Item Exchange",
    "auction": "Auction",
    "courier": "Courier",
    "loan": "Loan",
    "unknown": "Unknown",
}

CONTRACT_STATUS_LABELS: dict[str, str] = {
    "outstanding": "Outstanding",
    "in_progress": "In Progress",
    "finished_issuer": "Finished (issuer)",
    "finished_contractor": "Finished (contractor)",
    "finished": "Finished",
    "cancelled": "Cancelled",
    "rejected": "Rejected",
    "failed": "Failed",
    "deleted": "Deleted",
    "reversed": "Reversed",
}


def type_label(t: str) -> str:
    return CONTRACT_TYPE_LABELS.get(t, t or "Unknown")


def status_label(s: str) -> str:
    return CONTRACT_STATUS_LABELS.get(s, s or "")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


# ── Personal / corporation ────────────────────────────────────────────────────

async def _get_all_pages(client: httpx.AsyncClient, url: str, token: str | None = None,
                         max_pages: int = 30) -> list[dict]:
    out: list[dict] = []
    headers = _auth(token) if token else {"Accept": "application/json"}
    for page in range(1, max_pages + 1):
        try:
            r = await client.get(url, params={"page": page}, headers=headers, timeout=20)
        except Exception:
            break
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if page >= int(r.headers.get("x-pages", 1)):
            break
    return out


async def fetch_character_contracts(client, char_id: int, token: str) -> list[dict]:
    return await _get_all_pages(client, f"{ESI_BASE}/characters/{char_id}/contracts/", token)


async def fetch_corp_contracts(client, corp_id: int, token: str
                               ) -> tuple[list[dict] | None, str | None]:
    try:
        r = await client.get(f"{ESI_BASE}/corporations/{corp_id}/contracts/",
                             params={"page": 1}, headers=_auth(token), timeout=20)
    except Exception as exc:
        return None, str(exc)
    if r.status_code == 403:
        return None, "This character lacks the corporation role to read contracts (Accountant)."
    if r.status_code != 200:
        return None, f"ESI returned HTTP {r.status_code}."
    out = r.json()
    for page in range(2, int(r.headers.get("x-pages", 1)) + 1):
        try:
            rp = await client.get(f"{ESI_BASE}/corporations/{corp_id}/contracts/",
                                 params={"page": page}, headers=_auth(token), timeout=20)
        except Exception:
            break
        if rp.status_code != 200:
            break
        out.extend(rp.json())
    return out, None


async def fetch_character_contract_items(client, char_id: int, contract_id: int,
                                         token: str) -> list[dict]:
    try:
        r = await client.get(
            f"{ESI_BASE}/characters/{char_id}/contracts/{contract_id}/items/",
            headers=_auth(token), timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return []


class ItemFetch(NamedTuple):
    """Why a contract's contents did or did not arrive.

    Lumping these together is what made the alliance indexer misreport itself: a
    contract that no longer exists, the game server saying "stop spamming" for the
    next minute, and the ESI token bucket being empty need three different answers.
    """
    items: list[dict] | None    # the contents, or None when they did not arrive
    kind: str                   # ok | gone | throttled | limited | error
    wait: float = 0.0           # seconds the server asked us to wait, if it did


def _stop_spamming_wait(payload) -> float:
    """`{"error": "ConStopSpamming, details: {\"remainingTime\": 64226997}"}`.

    The number is microseconds (64226997 -> ~64 s). Measured against the live game
    server, not read in a document, so treat it as a hint: clamp it to something
    sane rather than trusting the unit blindly.
    """
    try:
        raw = str((payload or {}).get("error", ""))
        digits = "".join(ch for ch in raw.split("remainingTime")[-1] if ch.isdigit())
        if digits:
            return max(2.0, min(120.0, int(digits) / 1_000_000))
    except Exception:
        pass
    return 15.0


async def fetch_corp_contract_items(client, corp_id: int, contract_id: int,
                                    token: str) -> ItemFetch:
    """Contents of one corporation contract, with the reason when there are none."""
    try:
        r = await client.get(
            f"{ESI_BASE}/corporations/{corp_id}/contracts/{contract_id}/items/",
            headers=_auth(token), timeout=15)
    except Exception:
        return ItemFetch(None, "error")
    if r.status_code == 200:
        try:
            return ItemFetch(r.json(), "ok")
        except Exception:
            return ItemFetch(None, "error")
    body = {}
    try:
        body = r.json() or {}
    except Exception:
        pass
    if r.status_code == 404:
        # Accepted, expired or deleted between the listing and now: never coming.
        return ItemFetch(None, "gone")
    if r.status_code == 520 and "ConStopSpamming" in str(body.get("error", "")):
        # The GAME server's own throttle, unrelated to the ESI token bucket. It
        # tells us how long to hold off, and it is seconds - not the 15 minutes an
        # exhausted token bucket needs.
        return ItemFetch(None, "throttled", _stop_spamming_wait(body))
    if r.status_code in (420, 429):
        try:
            wait = float(r.headers.get("retry-after") or 0)
        except ValueError:
            wait = 0.0
        return ItemFetch(None, "limited", max(0.0, min(900.0, wait)))
    return ItemFetch(None, "error")


# ── Public (regional) ─────────────────────────────────────────────────────────

_PUB_SEM = asyncio.Semaphore(30)   # below the ESI rate-limit cliff (~45)


async def _fetch_public_page(client, region_id: int, page: int) -> tuple[list[dict], int]:
    async with _PUB_SEM:
        for attempt in range(3):
            try:
                r = await client.get(f"{ESI_BASE}/contracts/public/{region_id}/",
                                     params={"page": page}, timeout=25)
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
                return [], 0
            if r.status_code in (420, 429):
                await asyncio.sleep(min(int(r.headers.get("Retry-After", 30)), 60))
                continue
            if r.status_code != 200:
                return [], 0
            return r.json(), int(r.headers.get("x-pages", 1))
        return [], 0


async def fetch_public_contracts(client, region_id: int, progress_cb=None) -> list[dict]:
    """All public contracts in the region (metadata only, no items)."""
    first, total_pages = await _fetch_public_page(client, region_id, 1)
    if not first and total_pages == 0:
        return []
    pages: list[list[dict]] = [first]
    done = [1]
    lock = asyncio.Lock()

    async def _one(p: int):
        data, _ = await _fetch_public_page(client, region_id, p)
        async with lock:
            pages.append(data)
            done[0] += 1
            if progress_cb:
                res = progress_cb(done[0], total_pages)
                if asyncio.iscoroutine(res):
                    await res

    if progress_cb:
        res = progress_cb(1, total_pages)
        if asyncio.iscoroutine(res):
            await res
    await asyncio.gather(*[_one(p) for p in range(2, total_pages + 1)], return_exceptions=True)
    return [c for page in pages for c in page]


async def fetch_public_contract_items(client, contract_id: int) -> list[dict]:
    """Items of a public contract. 204/403/404 → empty list (courier with no
    items, expired, etc.)."""
    async with _PUB_SEM:
        try:
            r = await client.get(f"{ESI_BASE}/contracts/public/items/{contract_id}/",
                                 params={"page": 1}, timeout=15)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
    return []
