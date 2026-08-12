import httpx
import asyncio
import time
from typing import Optional

ESI_BASE = "https://esi.evetech.net/latest"
FUZZWORK_BASE = "https://www.fuzzwork.co.uk"
ESI_HOST = "esi.evetech.net"

# ESI date-based versioning: pin behavior to a fixed date (X-Compatibility-Date)
# so future breaking changes don't break us. Change the date only on a deliberate switch
# to newer API behavior. /latest in the URL still works; the header takes precedence.
ESI_COMPAT_DATE = "2026-07-17"


# The connection pool must cover our concurrency (semaphores up to 30), otherwise refresh
# is the bottleneck: httpx's default max_keepalive_connections=20 recycles only ~20
# connections and the rest pay the TLS handshake over and over — with keepalive 50 the bulk
# volume/orders refresh is ~2.8x faster (measured). We stay at 30 concurrent
# (semaphore), so under the ESI rate limit.
_ESI_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=50)


class ESIErrorLimited(httpx.HTTPStatusError):
    """Raised (via raise_for_status) when ESI returns HTTP 420 — the whole
    client is error-limited and no request will succeed until the window resets."""


# --- ESI error-limit governor -------------------------------------------------
# ESI keeps a per-client error budget in a ~60s sliding window and returns HTTP
# 420 ("error limited") for EVERY request once it is exhausted — so a single
# innocent GET (e.g. Plan → assets) fails only because something else (Sync All
# across many characters, a price refresh) burned the budget. Every ESI response
# carries X-ESI-Error-Limit-Remain / -Reset; we watch them across ALL esi_client()
# instances (process-global state) and self-throttle before hitting the cliff,
# then hard-wait through the reset window if we do hit a 420.
class _ErrorLimitGovernor:
    def __init__(self) -> None:
        self._pause_until = 0.0  # loop time.monotonic() deadline; 0 = clear

    async def wait(self) -> None:
        # Block new ESI requests while a pause is in effect. Re-check in short
        # slices so a concurrently-updated deadline is honored promptly.
        while True:
            delay = self._pause_until - time.monotonic()
            if delay <= 0:
                return
            await asyncio.sleep(min(delay, 2.0))

    def observe(self, remain: Optional[int], reset: Optional[int]) -> None:
        # Proactively back off when the budget is nearly gone, so we stop
        # BEFORE the 420 cliff rather than after.
        if remain is not None and reset is not None and remain <= 5:
            self._pause_until = max(self._pause_until, time.monotonic() + reset + 1)

    def blocked(self, reset: Optional[int]) -> None:
        # Got a 420: freeze all ESI traffic until the window resets.
        self._pause_until = max(self._pause_until, time.monotonic() + (reset or 60) + 1)


_ERROR_LIMIT = _ErrorLimitGovernor()


def _int_header(response: httpx.Response, name: str) -> Optional[int]:
    try:
        return int(response.headers[name])
    except (KeyError, ValueError, TypeError):
        return None


class _GovernedTransport(httpx.AsyncHTTPTransport):
    """Wraps the default async transport with the ESI error-limit governor.
    Only ESI hosts are governed; GitHub/image/Fuzzwork traffic passes straight
    through. On a 420 the request is retried a few times, waiting out each
    reset window, before finally surfacing the 420 to the caller."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != ESI_HOST:
            return await super().handle_async_request(request)

        last: Optional[httpx.Response] = None
        for attempt in range(4):
            await _ERROR_LIMIT.wait()
            response = await super().handle_async_request(request)
            reset = _int_header(response, "x-esi-error-limit-reset")
            if response.status_code == 420:
                _ERROR_LIMIT.blocked(reset)
                # Drain + close so the connection can be reused on retry.
                await response.aread()
                await response.aclose()
                last = response
                continue
            _ERROR_LIMIT.observe(_int_header(response, "x-esi-error-limit-remain"), reset)
            return response
        return last  # exhausted retries — hand the 420 back so raise_for_status fires


def esi_client(**kwargs) -> httpx.AsyncClient:
    """httpx.AsyncClient with a preset X-Compatibility-Date header, a
    connection pool sized for our concurrency (see _ESI_LIMITS), and a shared
    ESI error-limit governor (see _ErrorLimitGovernor). For non-ESI hosts
    (GitHub, images) both header and governor are harmless. Per-request headers
    are merged with the client header; the caller can override limits via kwargs."""
    headers = {"X-Compatibility-Date": ESI_COMPAT_DATE}
    headers.update(kwargs.pop("headers", None) or {})
    limits = kwargs.pop("limits", _ESI_LIMITS)
    # A custom transport takes over pool sizing, so feed it the limits. Passing
    # both transport= and limits= to AsyncClient would make httpx ignore limits.
    kwargs.setdefault("transport", _GovernedTransport(limits=limits))
    return httpx.AsyncClient(headers=headers, **kwargs)


def esi_error_message(exc: BaseException) -> Optional[str]:
    """Turn a raw httpx error into a short, user-facing ESI message, or None if
    it isn't a recognizable HTTP error. Used to replace httpx's default
    'Client error 420 ... developer.mozilla.org' text in the UI."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 420:
            return ("EVE's ESI API is rate-limiting this app right now "
                    "(too many requests in a short window). Wait a minute and try again.")
        if code in (502, 503, 504):
            return "EVE's ESI API is temporarily unavailable. Try again in a moment."
        if code == 403:
            return "ESI denied access (token expired or missing scope). Re-add the character."
    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout)):
        return "Couldn't reach EVE's ESI API (network/timeout). Check your connection and retry."
    return None

# Rate limiting: ESI allows ~150 req/s, Fuzzwork is slower
ESI_SEMAPHORE = asyncio.Semaphore(20)
FUZZ_SEMAPHORE = asyncio.Semaphore(5)


async def fetch_type_info(client: httpx.AsyncClient, type_id: int) -> Optional[dict]:
    """Fetches the type's name and category from ESI."""
    async with ESI_SEMAPHORE:
        r = await client.get(
            f"{ESI_BASE}/universe/types/{type_id}/",
            params={"datasource": "tranquility", "language": "en"},
            timeout=10,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def fetch_blueprint_data(client: httpx.AsyncClient, type_id: int) -> Optional[dict]:
    """
    Fetches blueprint data from the Fuzzwork API.
    type_id is the *product* ID (not the blueprint's).
    Returns manufacturing/reaction activities with a list of materials.
    """
    async with FUZZ_SEMAPHORE:
        r = await client.get(
            f"{FUZZWORK_BASE}/blueprint/",
            params={"typeID": type_id, "format": "json"},
            timeout=15,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        # Fuzzwork returns a dict where the key is the blueprint_type_id
        return data if data else None


async def search_type_by_name(client: httpx.AsyncClient, name: str) -> list[int]:
    """Converts a name to a type_id via ESI /universe/ids/ (POST)."""
    async with ESI_SEMAPHORE:
        r = await client.post(
            f"{ESI_BASE}/universe/ids/",
            params={"datasource": "tranquility", "language": "en"},
            json=[name],
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        types = data.get("inventory_types", [])
        return [t["id"] for t in types]


async def fetch_types_bulk(client: httpx.AsyncClient, type_ids: list[int]) -> dict[int, dict]:
    """Fetches information about multiple types at once."""
    tasks = [fetch_type_info(client, tid) for tid in type_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return {
        tid: res
        for tid, res in zip(type_ids, results)
        if isinstance(res, dict)
    }
