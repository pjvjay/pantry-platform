"""gcal_client.py on its own: Google's refusals as typed errors, retries that wait with the
injected sleep and give up after a minute, one token refresh on a 401, and the write throttle."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from demo_hub.gcal_client import (
    CalendarClient,
    QuotaExceeded,
    RateLimited,
    Unauthorized,
    error_for,
)


def test_backoff_gives_up_after_a_minute() -> None:
    google_waits: list[float] = []

    async def sleep(s: float) -> None:
        google_waits.append(s)

    def always_busy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}})

    async def token(force: bool) -> str:
        return "at"

    client = CalendarClient("https://calendar.fake/calendar/v3", token,
                            transport=httpx.MockTransport(always_busy), sleep=sleep,
                            jitter=lambda: 1.0)
    with pytest.raises(RateLimited):
        asyncio.run(client.calendars_get("c"))
    assert google_waits == [1.0, 2.0, 4.0, 8.0, 16.0, 16.0] and sum(google_waits) <= 60


def test_google_errors_are_typed() -> None:
    def resp(status: int, reason: str = "") -> httpx.Response:
        return httpx.Response(status, json={"error": {"errors": [{"reason": reason}]}},
                              request=httpx.Request("GET", "https://x"))

    assert type(error_for(resp(403, "quotaExceeded"))) is QuotaExceeded
    assert type(error_for(resp(403, "rateLimitExceeded"))) is RateLimited
    names = {s: type(error_for(resp(s))).__name__ for s in (400, 401, 403, 404, 409, 410, 412,
                                                            429, 500, 503)}
    assert names == {400: "BadRequest", 401: "Unauthorized", 403: "Forbidden",
                     404: "NotFound", 409: "Duplicate", 410: "NotFound",
                     412: "PreconditionFailed", 429: "RateLimited", 500: "Unavailable",
                     503: "Unavailable"}


def test_a_401_refreshes_the_token_once_then_gives_up() -> None:
    asked: list[bool] = []
    seen: list[str] = []
    current = ["stale"]

    async def token(force: bool) -> str:          # as the Connector: a refresh is kept
        asked.append(force)
        if force:
            current[0] = "fresh"
        return current[0]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        if request.headers["authorization"] == "Bearer stale":
            return httpx.Response(401, json={"error": {"errors": [{"reason": "authError"}]}})
        return httpx.Response(200, json={"id": "c"})

    client = CalendarClient("https://calendar.fake/calendar/v3", token,
                            transport=httpx.MockTransport(handler))
    assert asyncio.run(client.calendars_get("c")) == {"id": "c"}
    assert asked == [False, True, False] and seen[0] == "Bearer stale"

    async def always_stale(force: bool) -> str:
        return "stale"

    client = CalendarClient("https://calendar.fake/calendar/v3", always_stale,
                            transport=httpx.MockTransport(handler))
    with pytest.raises(Unauthorized):
        asyncio.run(client.calendars_get("c"))


def test_writes_carry_send_updates_none_and_if_match() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(204) if request.method == "DELETE" else \
            httpx.Response(200, json={"id": "e", "etag": "2"})

    async def token(force: bool) -> str:
        return "at"

    async def run() -> None:
        client = CalendarClient("https://calendar.fake/calendar/v3", token,
                                transport=httpx.MockTransport(handler))
        await client.events_insert("c@x", {"id": "pp1ab"})
        await client.events_update("c@x", "pp1ab", {"id": "pp1ab"}, '"1"')
        await client.events_delete("c@x", "pp1ab", '"2"')

    asyncio.run(run())
    assert [r.url.params["sendUpdates"] for r in sent] == ["none"] * 3
    assert [r.headers.get("if-match") for r in sent] == [None, '"1"', '"2"']
    assert sent[0].url.raw_path == b"/calendar/v3/calendars/c%40x/events?sendUpdates=none"

