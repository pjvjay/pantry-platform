"""``GET /hub/geocode?q=<address>``: the wizard's "Where you shop" looks an address up in
OpenStreetMap's Nominatim and gets back up to five places to choose from (docs in README.md),
each with a short ``label`` for the list ("Vancouver Art Gallery, 750 Hornby Street, Vancouver")
and Nominatim's whole ``display_name`` as ``full``.

An address is personal data, so the hub keeps it to what the lookup needs. ``q`` goes to
Nominatim and nowhere else: it is not logged (the access log shows ``q=***``, and httpx's log of
the upstream request too), not traced, not written to disk, and no error message repeats it. The
answers are cached in memory only, under a hash of the normalized query.

Nominatim's usage policy (operations.osmfoundation.org/policies/nominatim) is kept by the hub, not
left to the console: a User-Agent that names the app and nothing of the shopper, at most one
request a second for the whole process (callers queue on a lock, up to 3 s, then 429), no
autocomplete (the console searches on submit), and a cache of 256 queries for 24 hours. After a
429 from Nominatim itself the hub asks nothing for a minute. ``GEOCODER_URL`` points elsewhere,
and "" turns the route off (503 geocoder_off).

Refusals are ``{detail: {code, message}}`` like the other hub routes: 400 bad_query (under 3 or over
200 characters), 429 rate_limited, 502 geocoder_unavailable (an error, an unreadable answer or none
in 10 s), 503 geocoder_off.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from demo_hub import redact
from demo_hub.settings import Settings

USER_AGENT = "pantry-planner-demo-hub/1.0 (local demo)"
ATTRIBUTION = "© OpenStreetMap contributors"
SOURCE = "OpenStreetMap Nominatim"
MIN_CHARS, MAX_CHARS = 3, 200
MAX_RESULTS = 5
TIMEOUT_S = 10.0
SPACING_S = 1.0                  # between two requests to Nominatim
QUEUE_WAIT_S = 3.0               # how long a search waits its turn before 429
COOLDOWN_S = 60.0                # after Nominatim itself answers 429
CACHE_SIZE = 256
CACHE_TTL_S = 24 * 3600.0
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff"}


class GeocodeRefused(Exception):
    """A search the hub does not run or cannot answer: REST {code, message}."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _short(item: dict[str, Any], full: str) -> str:
    """A result's short label: its name when it has one (and it is not just the house number or
    the road), then "<house number> <road>" or the road alone, then the city (or town, village,
    municipality). Parts that are missing are skipped and repeats dropped; with nothing left, the
    first three parts of the full name."""
    address = item.get("address") if isinstance(item.get("address"), dict) else {}
    number, road = _text(address.get("house_number")), _text(address.get("road"))
    name = _text(item.get("name"))
    place = next((t for k in ("city", "town", "village", "municipality")
                  if (t := _text(address.get(k)))), "")
    parts: list[str] = []
    for part in (name if name not in (number, road) else "",
                 f"{number} {road}".strip() if road else "", place):
        if part and part.casefold() not in (p.casefold() for p in parts):
            parts.append(part)
    return ", ".join(parts) or ", ".join(full.split(", ")[:3])


def _place(item: Any) -> dict[str, Any] | None:
    """One Nominatim result as {label, full, lat, lon}, or None when it has no usable place."""
    if not isinstance(item, dict) or not isinstance(item.get("display_name"), str):
        return None
    try:
        lat, lon = float(item["lat"]), float(item["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    full = item["display_name"].strip()
    if not full or not (math.isfinite(lat) and math.isfinite(lon)
                        and -90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return {"label": _short(item, full)[:300], "full": full[:500], "lat": lat, "lon": lon}


class Geocoder:
    """Everything behind the route. Transport, sleep and clock are swappable for tests."""

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.url = settings.geocoder_url
        self.transport, self.sleep, self.clock = transport, sleep, clock
        self.timeout_s, self.queue_wait_s = TIMEOUT_S, QUEUE_WAIT_S
        self.cache_size = CACHE_SIZE
        self.lock = asyncio.Lock()
        self.last_at: float | None = None            # when the last request to Nominatim began
        self.cool_until = 0.0
        self.cache: OrderedDict[str, tuple[float, list[dict[str, Any]]]] = OrderedDict()
        redact.install("httpx")           # httpx logs each request's URL, query included

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def search(self, q: str) -> dict[str, Any]:
        if not self.enabled:
            raise GeocodeRefused(503, "geocoder_off", "Address search is turned off on this hub "
                                 "(GEOCODER_URL is empty).")
        if not MIN_CHARS <= len(q.strip()) <= MAX_CHARS:
            raise GeocodeRefused(400, "bad_query", f"Type an address of {MIN_CHARS} to "
                                 f"{MAX_CHARS} characters.")
        text = " ".join(q.split())
        key = hashlib.sha256(text.casefold().encode()).hexdigest()
        results = self._cached(key)
        if results is None:
            results = await self._ask(key, text)
        return {"results": [dict(r) for r in results], "attribution": ATTRIBUTION,
                "source": SOURCE}

    # the cache: a hash of the query -> (expires, results), least recently used out first

    def _cached(self, key: str) -> list[dict[str, Any]] | None:
        hit = self.cache.get(key)
        if hit is None:
            return None
        if self.clock() >= hit[0]:
            del self.cache[key]
            return None
        self.cache.move_to_end(key)
        return hit[1]

    def _remember(self, key: str, results: list[dict[str, Any]]) -> None:
        self.cache[key] = (self.clock() + CACHE_TTL_S, results)
        self.cache.move_to_end(key)
        while len(self.cache) > self.cache_size:
            self.cache.popitem(last=False)

    # one request to Nominatim at a time, a second apart

    async def _ask(self, key: str, text: str) -> list[dict[str, Any]]:
        try:
            async with asyncio.timeout(self.queue_wait_s):
                await self.lock.acquire()
        except TimeoutError:
            raise GeocodeRefused(429, "rate_limited", "Address search is busy: try again in a "
                                 "moment.") from None
        try:
            cached = self._cached(key)          # the same search finished while this one waited
            if cached is not None:
                return cached
            if self.clock() < self.cool_until:
                raise GeocodeRefused(429, "rate_limited", "The address service asked for a "
                                     "pause: try again in a minute.")
            if self.last_at is not None and (gap := SPACING_S - (self.clock() - self.last_at)) > 0:
                await self.sleep(gap)
            self.last_at = self.clock()
            results = await self._fetch(text)
            self._remember(key, results)
            return results
        finally:
            self.lock.release()

    async def _fetch(self, text: str) -> list[dict[str, Any]]:
        """Nominatim's search for ``text``. Whatever goes wrong is told without the text, and
        without the exception's own words (an httpx error can carry the URL)."""
        unavailable = GeocodeRefused(502, "geocoder_unavailable", "The address service did not "
                                     "answer: try again later.")
        try:
            async with asyncio.timeout(self.timeout_s), httpx.AsyncClient(
                    timeout=self.timeout_s, transport=self.transport,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"}) as http:
                r = await http.get(self.url, params={
                    "q": text, "format": "jsonv2", "limit": MAX_RESULTS, "addressdetails": 1,
                    "accept-language": "en"})
        except (httpx.HTTPError, TimeoutError):
            raise unavailable from None
        if r.status_code == 429:
            self.cool_until = self.clock() + COOLDOWN_S
            raise GeocodeRefused(429, "rate_limited", "The address service asked for a pause: "
                                 "try again in a minute.")
        if r.status_code != 200:
            raise unavailable
        try:
            body = r.json()
        except ValueError:
            raise unavailable from None
        if not isinstance(body, list):
            raise unavailable
        places = (_place(item) for item in body)
        return [p for p in places if p is not None][:MAX_RESULTS]


def router(geocoder: Geocoder) -> APIRouter:
    r = APIRouter()

    @r.get("/hub/geocode")
    async def geocode(q: str = "") -> JSONResponse:
        """Up to five places for an address: {results: [{label, lat, lon}], attribution, source}.
        The console calls it when the shopper submits, never as they type."""
        try:
            return JSONResponse(await geocoder.search(q), headers=NO_STORE)
        except GeocodeRefused as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.message},
                                headers=NO_STORE) from None

    return r
