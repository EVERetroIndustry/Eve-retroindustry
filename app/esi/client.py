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
# connections and the rest pay the TLS handshake over and over - with keepalive 50 the bulk
# volume/orders refresh is ~2.8x faster (measured). We stay at 30 concurrent
# (semaphore), so under the ESI rate limit.
_ESI_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=50)


class ESIErrorLimited(httpx.HTTPStatusError):
    """Raised (via raise_for_status) when ESI returns HTTP 420 - the whole
    client is error-limited and no request will succeed until the window resets."""


# --- ESI error-limit governor -------------------------------------------------
# ESI keeps a per-client error budget in a ~60s sliding window and returns HTTP
# 420 ("error limited") for EVERY request once it is exhausted - so a single
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


# --- ESI token-bucket rate limiter (the newer, per-group one) -----------------
# Alongside the old error limit, ESI now meters *successful* traffic with a token
# bucket and answers 429 + Retry-After when a bucket runs dry. Two properties
# shape this code:
#
#   * The bucket is per RATE LIMIT GROUP, not per client. Pausing everything on a
#     429 - the way the error limit governor has to - would let a drained market
#     bucket stop unrelated wallet or asset calls. So state is kept per group.
#   * A request does not say which group it belongs to; only the response does,
#     via X-Ratelimit-Group. We therefore learn the mapping from responses, keyed
#     by the URL path with its numeric segments collapsed, so
#     /markets/10000002/orders/ and /markets/10000043/orders/ share one entry.
#
# Costs (per CCP's docs): 2xx = 2 tokens, 3xx = 1, 4xx = 5, 5xx = 0, and a 429
# itself is free - so being throttled does not dig the hole deeper.
class _TokenBucketGovernor:
    # Below this share of the bucket, pause the group briefly between bursts.
    # The real protection is the 429 path; this only keeps us off the cliff, as
    # CCP's "Best Practices" ask ("Don't operate at the limit").
    _LOW_WATER = 0.10
    _BRAKE_SECONDS = 2.0

    def __init__(self) -> None:
        self._pause_until: dict[str, float] = {}   # group -> monotonic deadline
        self._group_of: dict[str, str] = {}        # path signature -> group
        self._headroom: dict[str, tuple[int, int]] = {}   # group -> (remaining, limit)

    @staticmethod
    def signature(url: httpx.URL) -> str:
        """Path with numeric segments collapsed, so all regions share a bucket key."""
        parts = ("{}" if seg.isdigit() else seg for seg in url.path.split("/"))
        return "/".join(parts)

    async def wait(self, sig: str) -> None:
        group = self._group_of.get(sig)
        if group is None:
            return
        while True:
            delay = self._pause_until.get(group, 0.0) - time.monotonic()
            if delay <= 0:
                return
            await asyncio.sleep(min(delay, 2.0))

    def observe(self, sig: str, response: httpx.Response) -> None:
        group = response.headers.get("x-ratelimit-group")
        if not group:
            return
        self._group_of[sig] = group
        remaining = _int_header(response, "x-ratelimit-remaining")
        limit = _limit_header_total(response.headers.get("x-ratelimit-limit"))
        if remaining is not None and limit:
            self._headroom[group] = (remaining, limit)
        if remaining is not None and limit and remaining <= limit * self._LOW_WATER:
            self._pause_until[group] = max(
                self._pause_until.get(group, 0.0), time.monotonic() + self._BRAKE_SECONDS
            )

    def blocked(self, sig: str, response: httpx.Response) -> float:
        """Record a 429 and return how long to wait."""
        # Some 429s come from a limiter deep in the game servers and carry no
        # rate-limit headers at all - CCP's docs say so explicitly - hence the
        # fallback group key and default delay.
        group = response.headers.get("x-ratelimit-group") or self._group_of.get(sig) or sig
        self._group_of[sig] = group
        try:
            delay = float(response.headers.get("retry-after", ""))
        except ValueError:
            delay = 60.0
        delay = max(1.0, min(delay, 300.0))
        self._pause_until[group] = max(
            self._pause_until.get(group, 0.0), time.monotonic() + delay
        )
        return delay


_TOKEN_LIMIT = _TokenBucketGovernor()


def _limit_header_total(value: Optional[str]) -> Optional[int]:
    """X-Ratelimit-Limit is "<tokens>/<window>", e.g. "12000/15m" - take the tokens."""
    if not value:
        return None
    try:
        return int(value.split("/", 1)[0])
    except (ValueError, AttributeError):
        return None


def _int_header(response: httpx.Response, name: str) -> Optional[int]:
    try:
        return int(response.headers[name])
    except (KeyError, ValueError, TypeError):
        return None


# --- market bucket isolation --------------------------------------------------
# Public routes are bucketed by <sourceIP>, which a desktop user shares with every
# other EVE tool on the machine (and, behind CGNAT, with strangers). Supplying an
# access token moves us to <sourceIP>:<applicationID> - the same size bucket, but
# ours alone. It does NOT multiply the budget: per CCP's docs only *authenticated
# routes* key on characterID, so rotating characters buys nothing here.
_market_token_provider: Optional[object] = None
_market_token_disabled_until = 0.0


def set_market_token_provider(fn) -> None:
    """Register a callable returning a valid access token, or None.

    Optional: without it market calls stay unauthenticated and simply share the
    per-IP bucket, exactly as before.
    """
    global _market_token_provider
    _market_token_provider = fn


def _market_auth_header(request: httpx.Request) -> Optional[str]:
    """Token to isolate the bucket for a public market call, or None."""
    if _market_token_provider is None or time.monotonic() < _market_token_disabled_until:
        return None
    if "/markets/" not in request.url.path or "authorization" in request.headers:
        return None
    try:
        token = _market_token_provider()
    except Exception:
        return None
    return f"Bearer {token}" if token else None


class _GovernedTransport(httpx.AsyncHTTPTransport):
    """Wraps the default async transport with both ESI rate-limit governors.

    Only ESI hosts are governed; GitHub/image/Fuzzwork traffic passes straight
    through. A 420 (old error limit, client-wide) pauses all ESI traffic; a 429
    (token bucket, per group) pauses only that group. Both are retried a few
    times before the status is finally handed back so raise_for_status fires.
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        global _market_token_disabled_until
        if request.url.host != ESI_HOST:
            return await super().handle_async_request(request)

        sig = _TokenBucketGovernor.signature(request.url)
        added_auth = False
        auth = _market_auth_header(request)
        if auth:
            request.headers["Authorization"] = auth
            added_auth = True

        last: Optional[httpx.Response] = None
        for attempt in range(4):
            await _ERROR_LIMIT.wait()
            await _TOKEN_LIMIT.wait(sig)
            response = await super().handle_async_request(request)

            # A stale or wrong token must never break a call that would have
            # worked unauthenticated - drop the header, stop using it for a
            # while, and retry as an anonymous request.
            if added_auth and response.status_code in (401, 403):
                await response.aread()
                await response.aclose()
                del request.headers["Authorization"]
                added_auth = False
                _market_token_disabled_until = time.monotonic() + 600
                print("[esi] market token rejected - falling back to unauthenticated "
                      "market calls for 10 minutes", flush=True)
                continue

            reset = _int_header(response, "x-esi-error-limit-reset")
            if response.status_code == 420:
                _ERROR_LIMIT.blocked(reset)
                # Drain + close so the connection can be reused on retry.
                await response.aread()
                await response.aclose()
                last = response
                continue
            if response.status_code == 429:
                delay = _TOKEN_LIMIT.blocked(sig, response)
                await response.aread()
                await response.aclose()
                last = response
                print(f"[esi] 429 on {sig} - waiting {delay:.0f}s", flush=True)
                continue

            _ERROR_LIMIT.observe(_int_header(response, "x-esi-error-limit-remain"), reset)
            _TOKEN_LIMIT.observe(sig, response)
            return response
        return last  # exhausted retries - hand it back so raise_for_status fires


def esi_throttle_status(url: str | None = None) -> dict:
    """Longest active pause across the shared ESI governors right now.

    Pass an example URL for one endpoint (region/type ids in it don't matter -
    signature() collapses numeric segments) to check that endpoint's specific
    rate-limit group; omit it to get the worst pause across every group seen so
    far. The old error-limit pause (blocks every ESI call, not just one group)
    is always folded in either way.

    This is what lets a slow, ESI-bound operation tell a user "this is being
    throttled by ESI, not stuck" - a long-running sweep that goes quiet for a
    minute at a time looks identical to a hang without it. Read-only; never
    blocks (that's what the governors' own .wait() does internally).
    """
    now = time.monotonic()
    remaining = max(0.0, _ERROR_LIMIT._pause_until - now)
    if url is not None:
        sig = _TokenBucketGovernor.signature(httpx.URL(url))
        group = _TOKEN_LIMIT._group_of.get(sig)
        if group:
            remaining = max(remaining, _TOKEN_LIMIT._pause_until.get(group, 0.0) - now)
    else:
        for until in _TOKEN_LIMIT._pause_until.values():
            remaining = max(remaining, until - now)
    return {"paused": remaining > 0.05, "seconds": int(remaining + 0.999) if remaining > 0 else 0}


def esi_budget_share(url: str) -> float | None:
    """Fraction of this endpoint's token bucket still unspent, or None if unknown.

    The buckets are large but finite over a long window (the history endpoint
    reports something like "12000/15m"), which a full region sweep of ~19k types
    simply cannot fit inside. That makes it essential for a background job to be
    able to see how much is left and leave a cushion: a user-initiated station
    load needs only a few hundred requests, so as long as the background stops
    well short of the floor, clicking Load never lands on a 429 the background
    earned. Read-only.
    """
    sig = _TokenBucketGovernor.signature(httpx.URL(url))
    group = _TOKEN_LIMIT._group_of.get(sig)
    if not group:
        return None
    pair = _TOKEN_LIMIT._headroom.get(group)
    if not pair or not pair[1]:
        return None
    return max(0.0, min(1.0, pair[0] / pair[1]))


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
        if code in (420, 429):
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
