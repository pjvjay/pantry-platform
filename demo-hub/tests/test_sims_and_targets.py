"""The mcp-sim runner client and the MCP target registry, against mocked HTTP."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from demo_hub.mcp_targets import McpTargetError, Targets, _HttpStatus, _leaf
from demo_hub.settings import Settings, _read_secret
from demo_hub.sims import PRESETS, SimsClient, SimsError

PAGE = '<html><meta name="mcpsim-token" content="tok-123"></html>'


def sims(handler: Any) -> SimsClient:
    return SimsClient(Settings(mcpsim_ui_url="http://runner.test"),
                      httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_start_reads_the_token_and_posts_the_preset() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/":
            return httpx.Response(200, text=PAGE)
        return httpx.Response(202, json={"job_id": "j1", "scenarios": ["cheapest-penne"]})

    out = asyncio.run(sims(handler).start(["cheapest-penne"], "gemini", modes=["free"]))
    assert out["job_id"] == "j1"
    post = seen[1]
    assert post.headers["x-mcpsim-token"] == "tok-123"
    body = json.loads(post.content)
    assert body == {"scenarios": ["cheapest-penne"], "repeat": 1, "dry_run": False,
                    "allow_same_judge": False, "models": PRESETS["gemini"]["models"], "modes": ["free"]}


def test_dry_and_local_presets() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, text=PAGE)
        bodies.append(json.loads(request.content))
        return httpx.Response(202, json={"job_id": "j"})

    client = sims(handler)
    asyncio.run(client.start("all", "dry"))
    asyncio.run(client.start("all", "local"))
    assert bodies[0]["dry_run"] is True and "models" not in bodies[0]
    assert bodies[1]["allow_same_judge"] is True
    assert set(bodies[1]["models"].values()) == {"ollama:command-r7b"}
    asyncio.run(client.start("all", "granite"))
    assert set(bodies[2]["models"].values()) == {"ollama:granite4.2:8b"} and bodies[2]["allow_same_judge"]


def test_unknown_preset_and_missing_token() -> None:
    with pytest.raises(SimsError, match="unknown preset") as info:
        asyncio.run(sims(lambda r: httpx.Response(200, text=PAGE)).start("all", "nope"))
    assert info.value.status == 422
    with pytest.raises(SimsError, match="carries no token"):
        asyncio.run(sims(lambda r: httpx.Response(200, text="<html></html>")).start("all", "gemini"))


def test_runner_errors_and_unreachable() -> None:
    with pytest.raises(SimsError, match="answered 409") as info:
        asyncio.run(sims(lambda r: httpx.Response(409, text="already running")).get("/api/x"))
    assert info.value.status == 409

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(SimsError, match="not reachable"):
        asyncio.run(sims(down).get("/api/scenarios"))


def test_cancel_posts_with_the_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=PAGE) if request.url.path == "/" else \
            httpx.Response(200, json={"cancelled": True})

    assert asyncio.run(sims(handler).cancel("j9")) == {"cancelled": True}
    assert seen[1].url.path == "/api/jobs/j9/cancel" and seen[1].headers["x-mcpsim-token"] == "tok-123"


# --- targets ----------------------------------------------------------------------------------

SERVERS = [{"id": "abc", "name": "pantry-sim"}, {"id": "def", "name": "pantry-recipes"}]


def targets(handler: Any, jwt: str = "jwt") -> Targets:
    settings = Settings(contextforge_url="http://cf.test", contextforge_jwt=jwt,
                        pantry_api_url="http://pantry.test", pantry_mcp_token="tok")
    return Targets(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_direct_targets_need_no_lookup() -> None:
    t = targets(lambda r: pytest.fail("no lookup expected"))
    pantry = asyncio.run(t.resolve("pantry"))
    anon = asyncio.run(t.resolve("pantry-anon"))
    assert (pantry.url, pantry.token) == ("http://pantry.test/mcp", "tok")
    assert (anon.url, anon.token) == ("http://pantry.test/mcp", "")
    assert "token" not in anon.public() and set(pantry.public()) == {"id", "label", "description", "auth", "url"}


def test_gateway_targets_are_looked_up_by_name_and_cached() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"servers": SERVERS})

    t = targets(handler)
    sim = asyncio.run(t.resolve("gateway-sim"))
    recipes = asyncio.run(t.resolve("gateway-recipes"))
    asyncio.run(t.resolve("gateway-sim"))
    assert sim.url == "http://cf.test/servers/abc/mcp" and sim.token == "jwt"
    assert recipes.url == "http://cf.test/servers/def/mcp"
    assert len(calls) == 2 and calls[0].headers["authorization"] == "Bearer jwt"


@pytest.mark.parametrize(("handler", "jwt", "needle", "status"), [
    (lambda r: httpx.Response(200, json=[]), "jwt", "no ContextForge virtual server named", 404),
    (lambda r: httpx.Response(401, text="no"), "jwt", "answered 401", 401),
    (lambda r: httpx.Response(200, json=[]), "", "no ContextForge JWT", 0),
])
def test_gateway_lookup_failures(handler: Any, jwt: str, needle: str, status: int) -> None:
    with pytest.raises(McpTargetError, match=needle) as info:
        asyncio.run(targets(handler, jwt).resolve("gateway-sim"))
    assert info.value.status == status


def test_unknown_target_and_unreachable_gateway() -> None:
    with pytest.raises(McpTargetError, match="unknown target") as info:
        asyncio.run(targets(lambda r: httpx.Response(200)).resolve("nope"))
    assert info.value.status == 404

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(McpTargetError, match="cannot reach ContextForge"):
        asyncio.run(targets(down).resolve("gateway-recipes"))


def test_http_status_records_post_refusals_only() -> None:
    status = _HttpStatus()
    request = httpx.Request("POST", "http://x")

    class Resp:
        def __init__(self, code: int, method: str = "POST") -> None:
            self.status_code = code
            self.request = httpx.Request(method, "http://x")

        async def aread(self) -> bytes:
            return b'{"error":  "invalid_token"}'

    asyncio.run(status.on_response(Resp(200)))
    asyncio.run(status.on_response(Resp(401, "GET")))
    assert status.status == 0
    asyncio.run(status.on_response(Resp(401)))
    assert (status.status, status.body) == (401, '{"error": "invalid_token"}')
    assert request.method == "POST"


def test_leaf_unwraps_single_exception_groups() -> None:
    assert _leaf(ExceptionGroup("g", [ValueError("inner")])) == "ValueError: inner"


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    secret = tmp_path / "token"
    secret.write_text("s3cret\n")
    monkeypatch.setenv("PANTRY_MCP_TOKEN_FILE", str(secret))
    monkeypatch.delenv("PANTRY_MCP_TOKEN", raising=False)
    monkeypatch.setenv("PANTRY_API_URL", "http://p.test/")
    monkeypatch.setenv("DEMO_AGENT_MAX_STEPS", "5")
    monkeypatch.setenv("DEMO_AGENT_FALLBACKS", "gemini:x, ollama:y ,")
    s = Settings.from_env()
    assert s.agent_fallbacks == ("gemini:x", "ollama:y")
    assert (s.pantry_mcp_token, s.pantry_api_url, s.agent_max_steps) == ("s3cret", "http://p.test", 5)
    assert "s3cret" not in repr(s)
    assert _read_secret(str(tmp_path / "missing")) == "" and _read_secret("") == ""
