"""``GET /hub/tiles/{z}/{x}/{y}.png`` (tiles.py) against a fake tile server on a mock transport:
OpenStreetMap's tile usage policy kept by the hub (a named User-Agent, a disk cache honouring the
server's headers with a 7-day floor, at most 2 requests at once, only the tile asked for), the
tile range, and every refusal. Never the real tile server."""

from __future__ import annotations

import asyncio
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import formatdate
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from demo_hub import app as app_module
from demo_hub import geocode, tiles
from demo_hub.settings import Settings
from demo_hub.tiles import TileRefused, Tiles
from tests.conftest import console_client

URL = "https://tiles.test/{z}/{x}/{y}.png"
PNG = b"\x89PNG\r\n\x1a\ntile"
DAY = 24 * 3600.0
T0 = 1_760_000_000.0


@dataclass
class TileServer:
    """What the hub sees of the tile server: the requests it gets and the answer it gives."""
    answer: Callable[[httpx.Request], httpx.Response] = field(
        default=lambda r: httpx.Response(200, content=PNG, headers={
            "content-type": "image/png", "cache-control": "max-age=3600", "etag": '"v1"'}))
    requests: list[httpx.Request] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


@dataclass
class Env:
    app: Any
    client: TestClient
    server: TileServer
    now: list[float]
    dir: Path

    def get(self, path: str) -> httpx.Response:
        return self.client.get(f"/hub/tiles/{path}")


def make_env(tmp_path: Path, **settings: Any) -> Env:
    app = app_module.create_app(Settings(
        traces_dir=str(tmp_path / "traces"), images_dir=str(tmp_path / "images"),
        usage_dir=str(tmp_path / "usage"), calendar_dir=str(tmp_path / "calendar"),
        timings_path=str(tmp_path / "timings.jsonl"), tile_dir=str(tmp_path / "tiles"),
        **{"tiles_url": URL, **settings}))
    server, now = TileServer(), [T0]
    t = app.state.tiles
    t.transport, t.clock = server.transport, lambda: now[0]
    return Env(app, console_client(app), server, now, tmp_path / "tiles")


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path)


def code(r: httpx.Response) -> str:
    return r.json()["detail"]["code"]


# --- a tile ------------------------------------------------------------------------------------


def test_a_tile_is_fetched_once_with_the_hubs_user_agent_and_served_from_disk(env: Env) -> None:
    r = env.get("12/650/1404.png")
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == f"public, max-age={int(7 * DAY)}"
    assert r.headers["x-content-type-options"] == "nosniff"
    [sent] = env.server.requests
    assert str(sent.url) == "https://tiles.test/12/650/1404.png"
    assert sent.headers["user-agent"] == geocode.USER_AGENT == tiles.USER_AGENT
    assert not {"cookie", "referer", "if-none-match"} & set(sent.headers)
    # the copy on disk: private folders, the tile and what it is
    assert (env.dir / "12" / "650" / "1404.png").read_bytes() == PNG
    for folder in (env.dir, env.dir / "12", env.dir / "12" / "650"):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700, folder
    assert stat.S_IMODE((env.dir / "12" / "650" / "1404.png").stat().st_mode) == 0o600
    # a second look, and one from a new hub on the same folder, asks the server nothing
    env.now[0] += DAY
    assert env.get("12/650/1404.png").content == PNG
    again = make_env(env.dir.parent)
    again.now[0] = T0 + 2 * DAY
    assert again.get("12/650/1404.png").content == PNG
    assert len(env.server.requests) == 1 and again.server.requests == []


def test_only_the_tile_asked_for_is_fetched(env: Env) -> None:
    for path in ("3/2/5.png", "3/2/5.png", "0/0/0.png", "19/524287/0.png"):
        assert env.get(path).status_code == 200
    assert env.server.paths() == ["/3/2/5.png", "/0/0/0.png", "/19/524287/0.png"]


@pytest.mark.parametrize("path", [
    "20/0/0.png", "-1/0/0.png", "1/2/0.png", "1/0/2.png", "3/8/1.png", "19/524288/0.png",
    "a/0/0.png", "1/0/0x1.png", "1.5/0/0.png", "12345678/0/0.png"])
def test_a_tile_outside_the_grid_is_400(env: Env, path: str) -> None:
    r = env.get(path)
    assert r.status_code == 400 and code(r) == "bad_tile"
    assert env.server.requests == []


def test_tiles_off_is_503_and_the_status_says_so(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    real = httpx.AsyncClient

    def offline(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        """The status page's probes of the other services, answered here: no real port."""
        kwargs.setdefault("transport", httpx.MockTransport(lambda r: httpx.Response(503)))
        return real(*args, **kwargs)

    monkeypatch.setattr(app_module.httpx, "AsyncClient", offline)
    on = make_env(tmp_path / "on")
    assert on.client.get("/hub/status").json()["tiles"] is True
    for url in ("", "https://tiles.test/static.png"):
        off = make_env(tmp_path / "off", tiles_url=url)
        r = off.get("1/0/0.png")
        assert r.status_code == 503 and code(r) == "tiles_off"
        assert off.client.get("/hub/status").json()["tiles"] is False
        assert off.server.requests == []


# --- the cache follows the server's headers, never less than 7 days -------------------------------


def test_a_tile_is_fresh_for_seven_days_at_least_then_asked_again_with_its_etag(env: Env) -> None:
    env.get("5/1/2.png")
    env.now[0] = T0 + 7 * DAY - 60
    assert env.get("5/1/2.png").status_code == 200 and len(env.server.requests) == 1
    env.now[0] = T0 + 7 * DAY + 60
    env.server.answer = lambda r: httpx.Response(304, headers={"cache-control": "max-age=60"})
    r = env.get("5/1/2.png")
    assert r.status_code == 200 and r.content == PNG
    revalidate = env.server.requests[-1]
    assert revalidate.headers["if-none-match"] == '"v1"'
    # the 304 starts another seven days
    env.now[0] += 7 * DAY - 120
    env.get("5/1/2.png")
    assert len(env.server.requests) == 2


def test_a_server_that_says_longer_is_believed(env: Env) -> None:
    env.server.answer = lambda r: httpx.Response(200, content=PNG, headers={
        "content-type": "image/png", "cache-control": "public, max-age=2592000"})
    r = env.get("5/1/2.png")
    assert r.headers["cache-control"] == "public, max-age=2592000"
    env.now[0] = T0 + 29 * DAY
    env.get("5/1/2.png")
    assert len(env.server.requests) == 1


def test_expires_counts_when_there_is_no_max_age(env: Env) -> None:
    env.server.answer = lambda r: httpx.Response(200, content=PNG, headers={
        "content-type": "image/png", "date": formatdate(T0, usegmt=True),
        "expires": formatdate(T0 + 10 * DAY, usegmt=True), "last-modified": "Mon, 01 Sep 2025 "
        "00:00:00 GMT"})
    env.get("5/1/2.png")
    env.now[0] = T0 + 9 * DAY
    env.get("5/1/2.png")
    assert len(env.server.requests) == 1
    env.now[0] = T0 + 11 * DAY
    env.get("5/1/2.png")
    assert env.server.requests[-1].headers["if-modified-since"] == "Mon, 01 Sep 2025 00:00:00 GMT"


@pytest.mark.parametrize(("headers", "seconds"), [
    ({"cache-control": "max-age=600"}, 600),
    ({"cache-control": "no-transform, max-age=\"86400\""}, 86400),
    ({"cache-control": "s-maxage=7200, max-age=60"}, 7200),
    ({"expires": formatdate(T0 + 3600, usegmt=True), "date": formatdate(T0, usegmt=True)}, 3600),
    ({"expires": formatdate(T0 + 3600, usegmt=True)}, 3600),
    ({"expires": "0"}, 0),
    ({"cache-control": "no-cache"}, 0),
    ({}, 0),
])
def test_the_servers_lifetime(headers: dict[str, str], seconds: int) -> None:
    assert tiles.lifetime(httpx.Headers(headers), T0) == seconds


# --- a server that cannot answer ------------------------------------------------------------------


@pytest.mark.parametrize("answer", [
    lambda r: httpx.Response(503),
    lambda r: httpx.Response(429),
    lambda r: httpx.Response(200, content=b"<html>blocked</html>",
                             headers={"content-type": "text/html"}),
    lambda r: httpx.Response(200, content=b"x" * (tiles.MAX_BYTES + 1),
                             headers={"content-type": "image/png"}),
    lambda r: (_ for _ in ()).throw(httpx.ConnectError("no route")),
])
def test_without_a_copy_a_failed_fetch_is_502_and_with_one_the_stale_copy_is_served(
        tmp_path: Path, answer: Callable[[httpx.Request], httpx.Response]) -> None:
    env = make_env(tmp_path)
    env.server.answer = answer
    r = env.get("8/40/87.png")
    assert r.status_code == 502 and code(r) == "tiles_unavailable"
    assert not (env.dir / "8" / "40" / "87.json").exists()
    env.server.answer = TileServer().answer
    env.get("8/40/87.png")
    env.now[0] = T0 + 30 * DAY
    env.server.answer = answer
    stale = env.get("8/40/87.png")
    assert stale.status_code == 200 and stale.content == PNG
    assert stale.headers["cache-control"] == f"public, max-age={tiles.STALE_MAX_AGE_S}"


def test_a_cache_that_cannot_be_written_still_serves_the_tile(tmp_path: Path) -> None:
    (tmp_path / "tiles").write_text("a file where the folder should be")
    env = make_env(tmp_path)
    for _ in range(2):
        r = env.get("2/1/1.png")
        assert r.status_code == 200 and r.content == PNG
    assert len(env.server.requests) == 2                      # nothing cached, nothing broken


def test_a_tile_the_server_does_not_have_is_404(env: Env) -> None:
    env.server.answer = lambda r: httpx.Response(404)
    r = env.get("4/3/2.png")
    assert r.status_code == 404 and code(r) == "no_tile"


# --- at most two requests at once ----------------------------------------------------------------


def test_at_most_two_requests_reach_the_server_at_once_and_one_per_tile(tmp_path: Path) -> None:
    load = {"now": 0, "most": 0}
    sent: list[str] = []

    async def slow(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        load["now"] += 1
        load["most"] = max(load["most"], load["now"])
        await asyncio.sleep(0.01)
        load["now"] -= 1
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    t = Tiles(Settings(tiles_url=URL), tmp_path, transport=httpx.MockTransport(slow),
              clock=lambda: T0)

    async def many() -> list[tuple[bytes, str, int]]:
        wanted = [("6", "1", str(y)) for y in range(8)] + [("6", "1", "0")] * 4
        return await asyncio.gather(*(t.tile(*zxy) for zxy in wanted))

    got = asyncio.run(many())
    assert all(content == PNG for content, _type, _age in got)
    assert load["most"] == tiles.MAX_UPSTREAM == 2
    assert sorted(sent) == sorted(f"/6/1/{y}.png" for y in range(8))      # /6/1/0 once


def test_a_refusal_is_raised_before_anything_is_asked(tmp_path: Path) -> None:
    t = Tiles(Settings(tiles_url=URL), tmp_path, transport=TileServer().transport)
    with pytest.raises(TileRefused) as exc:
        asyncio.run(t.tile("2", "4", "0"))
    assert (exc.value.status, exc.value.code) == (400, "bad_tile")
    assert not tmp_path.joinpath("2").exists()
