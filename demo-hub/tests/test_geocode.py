"""``GET /hub/geocode`` (geocode.py) against a fake Nominatim on a mock transport: the answer's
shape, Nominatim's usage policy (a named User-Agent, one request a second, a cache), every
refusal, and the address staying out of everything the hub records. Never the real Nominatim."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from demo_hub import app as app_module
from demo_hub import geocode
from demo_hub.geocode import Geocoder, GeocodeRefused
from demo_hub.redact import RedactQuery
from demo_hub.settings import Settings
from tests.conftest import console_client

URL = "https://geo.test/search"
ADDRESS = "742 Evergreen Terrace, Springfield"
ENCODED = "742%20Evergreen%20Terrace"
PLACE = {"place_id": 1, "lat": "49.2827291", "lon": "-123.1207375",
         "display_name": "Vancouver, British Columbia, Canada", "category": "boundary"}


def places(n: int) -> list[dict[str, Any]]:
    return [{**PLACE, "place_id": i, "display_name": f"Place {i}, Canada",
             "lat": str(49 + i / 100), "lon": str(-123 - i / 100)} for i in range(n)]


@dataclass
class Nominatim:
    """What the hub sees of Nominatim: the requests it gets and the answer it is told to give."""
    answer: Callable[[httpx.Request], httpx.Response] = field(
        default=lambda r: httpx.Response(200, json=[PLACE]))
    requests: list[httpx.Request] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)

    def json(self, body: Any, status: int = 200) -> None:
        self.answer = lambda r: httpx.Response(status, json=body)


@dataclass
class Env:
    app: Any
    client: TestClient
    nominatim: Nominatim
    sleeps: list[float]
    now: list[float]

    @property
    def geocoder(self) -> Geocoder:
        return self.app.state.geocoder

    def get(self, q: str | None = ADDRESS, **kw: Any) -> httpx.Response:
        return self.client.get("/hub/geocode", params={} if q is None else {"q": q}, **kw)


def make_env(tmp_path: Path, **settings: Any) -> Env:
    app = app_module.create_app(Settings(
        traces_dir=str(tmp_path / "traces"), images_dir=str(tmp_path / "images"),
        usage_dir=str(tmp_path / "usage"), calendar_dir=str(tmp_path / "calendar"),
        timings_path=str(tmp_path / "timings.jsonl"), geocoder_url=URL, **settings))
    nominatim, sleeps, now = Nominatim(), [], [1000.0]

    async def sleep(seconds: float) -> None:        # never waits; the clock moves instead
        sleeps.append(seconds)
        now[0] += seconds

    g = app.state.geocoder
    g.transport, g.sleep, g.clock = nominatim.transport, sleep, lambda: now[0]
    return Env(app, console_client(app), nominatim, sleeps, now)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path)


def code(r: httpx.Response) -> str:
    return r.json()["detail"]["code"]


# --- the answer -------------------------------------------------------------------------------


def test_places_come_back_as_label_lat_lon_with_the_attribution(env: Env) -> None:
    r = env.get()
    assert r.status_code == 200
    assert r.json() == {
        "results": [{"label": "Vancouver, British Columbia, Canada", "lat": 49.2827291,
                     "lon": -123.1207375}],
        "attribution": "© OpenStreetMap contributors", "source": "OpenStreetMap Nominatim"}
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"


def test_at_most_five_and_only_places_that_can_be_used(env: Env) -> None:
    junk: list[Any] = [{"lat": "1", "lon": "2"}, {"display_name": "No coordinates"},
                       {"display_name": "Bad", "lat": "north", "lon": "2"},
                       {"display_name": "Far", "lat": "91", "lon": "0"},
                       {"display_name": "Wide", "lat": "0", "lon": "181"},
                       {"display_name": "Nan", "lat": "nan", "lon": "0"},
                       {"display_name": "  ", "lat": "1", "lon": "1"}, "text", None, 7]
    env.nominatim.json([*junk, *places(8)])
    results = env.get().json()["results"]
    assert [r["label"] for r in results] == [f"Place {i}, Canada" for i in range(5)]
    assert results[0] == {"label": "Place 0, Canada", "lat": 49.0, "lon": -123.0}


def test_nothing_found_is_an_empty_list_not_an_error(env: Env) -> None:
    env.nominatim.json([])
    r = env.get("Nowhere Land 99999")
    assert r.status_code == 200 and r.json()["results"] == []
    assert r.json()["attribution"] == "© OpenStreetMap contributors"


def test_nominatim_is_asked_as_its_policy_says(env: Env) -> None:
    env.get(f"  {ADDRESS}  ")
    [sent] = env.nominatim.requests
    assert sent.method == "GET" and str(sent.url).startswith(URL + "?")
    assert dict(sent.url.params.multi_items()) == {
        "q": ADDRESS, "format": "jsonv2", "limit": "5", "addressdetails": "0",
        "accept-language": "en"}
    # an app name, never the shopper: no email, no cookie, no referer, no auth
    assert sent.headers["user-agent"] == "pantry-planner-demo-hub/1.0 (local demo)"
    assert "@" not in sent.headers["user-agent"]
    assert {"cookie", "referer", "authorization", "x-forwarded-for"}.isdisjoint(sent.headers)


# --- what is refused --------------------------------------------------------------------------


@pytest.mark.parametrize("q", [None, "", "  ", "ab", "  ab  ", "a" * 201, f"  {'a' * 201}  "])
def test_a_query_under_3_or_over_200_characters_is_a_400(env: Env, q: str | None) -> None:
    r = env.get(q)
    assert r.status_code == 400 and code(r) == "bad_query"
    assert set(r.json()["detail"]) == {"code", "message"}
    assert env.nominatim.requests == []
    if q and q.strip():
        assert q.strip() not in r.text                    # the message does not repeat it


def test_the_limits_themselves_are_fine(env: Env) -> None:
    assert env.get("abc").status_code == 200
    assert env.get("  " + "b" * 200 + "  ").status_code == 200


def test_the_route_is_off_when_the_url_is_empty(tmp_path: Path) -> None:
    off = make_env(tmp_path)
    off.geocoder.url = ""
    r = off.get()
    assert r.status_code == 503 and code(r) == "geocoder_off"
    assert off.get("ab").status_code == 503 and off.nominatim.requests == []


def test_settings_read_geocoder_url_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEOCODER_URL", raising=False)
    assert Settings.from_env().geocoder_url == "https://nominatim.openstreetmap.org/search"
    monkeypatch.setenv("GEOCODER_URL", "")
    assert Settings.from_env().geocoder_url == ""
    monkeypatch.setenv("GEOCODER_URL", "http://127.0.0.1:9/search")
    assert Settings.from_env().geocoder_url == "http://127.0.0.1:9/search"


def test_a_page_on_another_host_cannot_use_it(env: Env) -> None:
    r = env.get(headers={"Host": "evil.example"})
    assert r.status_code == 403 and env.nominatim.requests == []


@pytest.mark.parametrize("answer", [
    lambda r: httpx.Response(500, text="boom"),
    lambda r: httpx.Response(403, json={"error": "blocked"}),
    lambda r: httpx.Response(200, text="<html>not json</html>"),
    lambda r: httpx.Response(200, json={"results": []}),
    lambda r: (_ for _ in ()).throw(httpx.ConnectError("down", request=r)),
    lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r)),
])
def test_an_upstream_failure_is_a_502_that_says_nothing_of_the_address(
        env: Env, answer: Callable[[httpx.Request], httpx.Response]) -> None:
    env.nominatim.answer = answer
    r = env.get()
    assert r.status_code == 502 and code(r) == "geocoder_unavailable"
    assert "Evergreen" not in r.text and "geo.test" not in r.text
    env.nominatim.json(places(1))                         # a failure is not cached
    assert env.get().status_code == 200


def test_an_answer_that_takes_over_the_limit_is_a_502(tmp_path: Path) -> None:
    slow = make_env(tmp_path)
    slow.geocoder.timeout_s = 0.05
    release = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(200, json=[PLACE])

    slow.geocoder.transport = httpx.MockTransport(hang)
    r = slow.get()
    assert r.status_code == 502 and code(r) == "geocoder_unavailable"
    assert geocode.TIMEOUT_S == 10.0


def test_nominatim_saying_429_is_a_429_and_the_hub_then_leaves_it_alone(env: Env) -> None:
    env.nominatim.json({"error": "slow down"}, 429)
    r = env.get("first address")
    assert r.status_code == 429 and code(r) == "rate_limited"
    env.nominatim.json(places(1))
    before = len(env.nominatim.requests)
    r = env.get("second address")                         # inside the minute: not even tried
    assert r.status_code == 429 and len(env.nominatim.requests) == before
    env.now[0] += 61
    assert env.get("second address").status_code == 200


# --- one a second, and the cache --------------------------------------------------------------


def test_requests_to_nominatim_are_a_second_apart(env: Env) -> None:
    env.get("first address")
    assert env.sleeps == []                               # the first goes at once
    env.now[0] += 0.25
    env.get("second address")
    assert env.sleeps == [pytest.approx(0.75)]
    env.now[0] += 5                                       # long enough ago: no waiting
    env.get("third address")
    assert len(env.sleeps) == 1 and len(env.nominatim.requests) == 3


def test_the_same_query_is_asked_once_for_a_day(env: Env) -> None:
    env.get(ADDRESS)
    for again in (ADDRESS.upper(), f"  {ADDRESS.replace(' ', '   ')}  ", ADDRESS.lower()):
        r = env.get(again)
        assert r.status_code == 200 and r.json()["results"][0]["lat"] == 49.2827291
    assert len(env.nominatim.requests) == 1 and env.sleeps == []
    env.now[0] += 24 * 3600 - 1
    env.get(ADDRESS)
    assert len(env.nominatim.requests) == 1
    env.now[0] += 2
    env.nominatim.json(places(2))
    assert len(env.get(ADDRESS).json()["results"]) == 2    # asked again after 24 h
    assert len(env.nominatim.requests) == 2


def test_an_empty_answer_is_cached_too_and_the_cache_forgets_the_least_recent(env: Env) -> None:
    env.geocoder.cache_size = 2
    env.nominatim.json([])
    env.get("aaa one")
    env.get("bbb two")
    env.get("aaa one")                                     # touched: bbb is now the oldest
    env.get("ccc three")                                   # evicts bbb
    assert len(env.nominatim.requests) == 3
    env.get("aaa one")
    env.get("ccc three")
    assert len(env.nominatim.requests) == 3
    env.get("bbb two")
    assert len(env.nominatim.requests) == 4
    assert len(env.geocoder.cache) == 2


def test_the_default_cache_is_256_queries_for_24_hours() -> None:
    assert geocode.CACHE_SIZE == 256 and geocode.CACHE_TTL_S == 24 * 3600
    assert geocode.SPACING_S == 1.0 and geocode.QUEUE_WAIT_S == 3.0


def test_a_search_that_waits_too_long_for_its_turn_is_a_429(tmp_path: Path) -> None:
    e = make_env(tmp_path)
    g = e.geocoder
    g.queue_wait_s = 0.05
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return httpx.Response(200, json=[PLACE])

    g.transport = httpx.MockTransport(slow)

    async def run() -> tuple[Any, GeocodeRefused]:
        first = asyncio.ensure_future(g.search("first address"))
        await entered.wait()
        with pytest.raises(GeocodeRefused) as busy:
            await g.search("second address")
        release.set()
        return await first, busy.value

    answer, busy = asyncio.run(run())
    assert len(answer["results"]) == 1
    assert busy.status == 429 and busy.code == "rate_limited" and "second" not in busy.message


def test_the_same_search_made_twice_at_once_is_asked_once(tmp_path: Path) -> None:
    e = make_env(tmp_path)
    g, calls = e.geocoder, []
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, json=[PLACE])

    g.transport = httpx.MockTransport(slow)

    async def run() -> list[Any]:
        a = asyncio.ensure_future(g.search(ADDRESS))
        await entered.wait()
        b = asyncio.ensure_future(g.search(ADDRESS.upper()))     # waits for the lock
        await asyncio.sleep(0.01)
        release.set()
        return [await a, await b]

    first, second = asyncio.run(run())
    assert first == second and len(calls) == 1


# --- the address is nobody's but the shopper's ------------------------------------------------


def test_the_address_is_not_logged_traced_or_stored(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    e = make_env(tmp_path)
    caplog.set_level(logging.DEBUG)
    marker = "Zorglub"
    address = f"742 {marker} Lane, Springfield"
    e.get(address)                                                     # an answer
    e.get(address)                                                     # a cached one
    e.nominatim.json({"error": "x"}, 500)
    refused = [e.get(f"9 {marker} Road"), e.get(f"{marker}{marker}"), e.get("ab")]
    e.client.post("/hub/telemetry", json={"kind": "api", "entries": [
        {"path": f"/hub/geocode?q={marker}", "ms": 1, "server_ms": 1, "status": 200}]})
    # every record any logger made (httpx2 is the test client's own log of its calls to the hub);
    # the request to Nominatim is logged by httpx, with its query masked
    logs = "\n".join(r.getMessage() for r in caplog.records if r.name != "httpx2")
    assert "https://geo.test/search?q=***&format=jsonv2" in logs
    assert marker not in logs and "742" not in logs
    for r in refused:
        assert marker not in r.text and marker not in str(r.headers)
    # nothing the hub keeps: its request timings, its trace store, its files
    stats = e.app.state.http_stats
    assert "GET /hub/geocode" in stats.samples and all(marker not in route for route in stats.samples)
    assert marker not in json.dumps(list(e.app.state.traces.frontend))
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written, "the telemetry beacon was stored, as before"
    for p in written:
        assert marker not in p.read_text(errors="replace"), p
    # and the cache holds a hash of the query, not the query
    assert len(e.geocoder.cache) == 1 and all(marker not in key for key in e.geocoder.cache)
    # the access log, as uvicorn writes it, is masked by the filter main() installs
    access = logging.LogRecord("uvicorn.access", logging.INFO, "f", 1,
                               '%s - "%s %s HTTP/%s" %d', None, None)
    access.args = ("127.0.0.1:5555", "GET", f"/hub/geocode?q={ENCODED}", "1.1", 200)
    RedactQuery().filter(access)
    assert "Evergreen" not in access.getMessage() and "q=***" in access.getMessage()


def test_httpx_is_told_to_mask_the_query_in_its_own_log(env: Env) -> None:
    assert any(isinstance(f, RedactQuery) for f in logging.getLogger("httpx").filters)
