"""A thin Google Calendar API v3 client over httpx: only the calls the sync makes, typed errors,
backoff and a write throttle. No google-api-python-client.

Errors: Duplicate (409, an event id already used), PreconditionFailed (412, the event changed
since its etag), NotFound (404 or 410), RateLimited (429, or 403 with a rate reason; retried),
QuotaExceeded (403 with a daily quota reason; not retried), Unauthorized (401; the access token
is refreshed once), Forbidden (other 403s), BadRequest (400) and Unavailable (5xx or no answer;
retried).

Retries wait 1, 2, 4, 8 then 16 s with jitter, or what Retry-After asks, and give up once the
waiting would pass 60 s. ``sleep`` and ``clock`` are injected, so tests never wait. Writes are
spaced at least 0.2 s apart (5 a second, under Google's per-user limit). Every write sends
``sendUpdates=none``, and no event body this hub builds has attendees.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx

RATE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded"})
MAX_WAIT_S = 60.0
WRITE_INTERVAL_S = 0.2
PAGE_SIZE = 250


class GoogleError(Exception):
    def __init__(self, status: int, reason: str, message: str,
                 retry_after: float | None = None) -> None:
        super().__init__(f"{status} {reason}: {message}")
        self.status, self.reason, self.message, self.retry_after = \
            status, reason, message, retry_after


class Duplicate(GoogleError): ...
class PreconditionFailed(GoogleError): ...
class NotFound(GoogleError): ...
class RateLimited(GoogleError): ...
class QuotaExceeded(GoogleError): ...
class Unauthorized(GoogleError): ...
class Forbidden(GoogleError): ...
class BadRequest(GoogleError): ...
class Unavailable(GoogleError): ...


RETRIED = (RateLimited, Unavailable)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after", "").strip()
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None


def error_for(response: httpx.Response) -> GoogleError:
    """The typed error for a refusal from the Calendar API."""
    reason, message = "", response.reason_phrase
    try:
        body = response.json().get("error", {})
        if isinstance(body, dict):
            message = str(body.get("message") or message)
            errors = body.get("errors") or []
            if errors and isinstance(errors[0], dict):
                reason = str(errors[0].get("reason", ""))
    except (ValueError, AttributeError):
        pass
    status, after = response.status_code, _retry_after(response)
    if status == 409:
        return Duplicate(status, reason or "duplicate", message)
    if status == 412:
        return PreconditionFailed(status, reason or "conditionNotMet", message)
    if status in (404, 410):
        return NotFound(status, reason or "notFound", message)
    if status == 429 or (status == 403 and reason in RATE_REASONS):
        return RateLimited(status, reason or "rateLimitExceeded", message, after)
    if status == 403 and reason in QUOTA_REASONS:
        return QuotaExceeded(status, reason, message)
    if status == 401:
        return Unauthorized(status, reason or "authError", message)
    if status == 403:
        return Forbidden(status, reason or "forbidden", message)
    if status >= 500:
        return Unavailable(status, reason or "backendError", message, after)
    return BadRequest(status, reason or "badRequest", message)


TokenSource = Callable[[bool], Awaitable[str]]


class CalendarClient:
    """``token(force)`` gives an access token; force=True after a 401 asks for a fresh one."""

    def __init__(self, base_url: str, token: TokenSource, *,
                 transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 jitter: Callable[[], float] = random.random,
                 max_wait_s: float = MAX_WAIT_S, write_interval_s: float = WRITE_INTERVAL_S,
                 page_size: int = PAGE_SIZE) -> None:
        self.base_url = base_url.rstrip("/")
        self.token, self.transport = token, transport
        self.sleep, self.clock, self.jitter = sleep, clock, jitter
        self.max_wait_s, self.write_interval_s, self.page_size = \
            max_wait_s, write_interval_s, page_size
        self._last_write: float | None = None
        self.waited_s = 0.0                     # total backoff, for the tests and the log

    async def _throttle(self) -> None:
        if self._last_write is not None:
            gap = self.write_interval_s - (self.clock() - self._last_write)
            if gap > 0:
                await self.sleep(gap)
        self._last_write = self.clock()

    async def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                       body: Any = None, headers: dict[str, str] | None = None) -> Any:
        write = method != "GET"
        waited, attempt, refreshed = 0.0, 0, False
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30,
                                     transport=self.transport, trust_env=False) as http:
            while True:
                if write:
                    await self._throttle()
                token = await self.token(False)
                try:
                    r = await http.request(method, path, params=params, json=body, headers={
                        "Authorization": f"Bearer {token}", **(headers or {})})
                    error = None if r.status_code < 300 else error_for(r)
                except httpx.HTTPError as exc:
                    r, error = None, Unavailable(0, "unreachable", type(exc).__name__)
                if error is None and r is not None:
                    if r.status_code == 204 or not r.content:
                        return None
                    return r.json()
                if isinstance(error, Unauthorized) and not refreshed:
                    refreshed = True
                    await self.token(True)
                    continue
                if not isinstance(error, RETRIED):
                    raise error
                delay = error.retry_after if error.retry_after is not None \
                    else min(16.0, 2.0 ** attempt) * (0.5 + 0.5 * self.jitter())
                if waited + delay > self.max_wait_s:
                    raise error
                await self.sleep(delay)
                waited += delay
                self.waited_s += delay
                attempt += 1

    @staticmethod
    def _cal(calendar_id: str) -> str:
        return f"/calendars/{quote(calendar_id, safe='')}"

    # calendars

    async def calendars_insert(self, summary: str, time_zone: str,
                               description: str = "") -> dict[str, Any]:
        return await self._request("POST", "/calendars", body={
            "summary": summary, "timeZone": time_zone, "description": description})

    async def calendars_get(self, calendar_id: str) -> dict[str, Any]:
        return await self._request("GET", self._cal(calendar_id))

    async def calendars_delete(self, calendar_id: str) -> None:
        await self._request("DELETE", self._cal(calendar_id))

    # events

    async def events_list(self, calendar_id: str, private: dict[str, str],
                          show_deleted: bool = True) -> list[dict[str, Any]]:
        """Every event carrying all of ``private`` (privateExtendedProperty), cancelled ones
        included when ``show_deleted``; every page."""
        out: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {
                "privateExtendedProperty": [f"{k}={v}" for k, v in private.items()],
                "showDeleted": "true" if show_deleted else "false",
                "maxResults": self.page_size, "singleEvents": "false"}
            if token:
                params["pageToken"] = token
            page = await self._request("GET", f"{self._cal(calendar_id)}/events", params=params)
            out += [e for e in (page or {}).get("items", []) if isinstance(e, dict)]
            token = (page or {}).get("nextPageToken")
            if not token:
                return out

    async def events_insert(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", f"{self._cal(calendar_id)}/events",
                                   params={"sendUpdates": "none"}, body=body)

    async def events_get(self, calendar_id: str, event_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"{self._cal(calendar_id)}/events/{quote(event_id, safe='')}")

    async def events_update(self, calendar_id: str, event_id: str, body: dict[str, Any],
                            etag: str | None) -> dict[str, Any]:
        """A full update, only while the event still has ``etag`` (If-Match; 412 otherwise)."""
        return await self._request(
            "PUT", f"{self._cal(calendar_id)}/events/{quote(event_id, safe='')}",
            params={"sendUpdates": "none"}, body=body,
            headers={"If-Match": etag} if etag else None)

    async def events_delete(self, calendar_id: str, event_id: str, etag: str | None) -> None:
        await self._request(
            "DELETE", f"{self._cal(calendar_id)}/events/{quote(event_id, safe='')}",
            params={"sendUpdates": "none"}, headers={"If-Match": etag} if etag else None)
