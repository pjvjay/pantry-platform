"""The MCP client half of the hub against a real MCP server: the same SDK (mcp 2.3.0) serving
streamable HTTP from a uvicorn thread on a free port, behind a bearer check like pantry's. Covers
opening a session, the catalog, a tool call (ok and failing), a resource, a template, a prompt,
and an anonymous client refused with 401."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from demo_hub import mcp_targets
from demo_hub.mcp_targets import McpTargetError, Target, open_session

TOKEN = "test-token-0123456789"


def build_server() -> MCPServer:
    server = MCPServer(name="fake-pantry")

    @server.tool()
    def find_product(query: str, limit: int = 3) -> dict[str, Any]:
        """Find a product."""
        return {"query": query, "match": "direct", "items": [{"name": "Penne", "price": 1.97}][:limit]}

    @server.tool()
    def broken(reason: str = "") -> dict[str, Any]:
        """Always fails."""
        raise ToolError(f"broken: {reason}")

    @server.resource("pantry://countries")
    def countries() -> str:
        """Country names."""
        return '{"canonical": ["Canada", "Italy"]}'

    @server.resource("pantry://recipes/{slug}")
    def recipe(slug: str) -> str:
        """One recipe."""
        return f'{{"slug": "{slug}"}}'

    @server.prompt()
    def plan_dinner(recipe: str, budget: str = "") -> str:
        """Plan a dinner."""
        return f"Plan dinner for: {recipe} under {budget or 'any budget'}."

    return server


class BearerGate:
    """401 for /mcp without the token, as pantry's mcp_auth does."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            headers = dict(scope.get("headers") or [])
            if headers.get(b"authorization") != f"Bearer {TOKEN}".encode():
                body = b'{"error": "invalid_token"}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def mcp_url() -> Iterator[str]:
    app = build_server().streamable_http_app(
        streamable_http_path="/mcp", json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(BearerGate(app), host="127.0.0.1", port=port,
                                           log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "the test MCP server did not start"
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=5)


def target(url: str, token: str = TOKEN) -> Target:
    return Target("t", "t", "", "bearer", url, token)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_catalog_lists_everything(mcp_url: str) -> None:
    async def go() -> dict[str, Any]:
        async with open_session(target(mcp_url)) as session:
            return await mcp_targets.catalog(session)

    cat = run(go())
    assert sorted(t["name"] for t in cat["tools"]) == ["broken", "find_product"]
    assert "inputSchema" in cat["tools"][0]
    assert [r["uri"] for r in cat["resources"]] == ["pantry://countries"]
    assert [t["uriTemplate"] for t in cat["resource_templates"]] == ["pantry://recipes/{slug}"]
    assert [p["name"] for p in cat["prompts"]] == ["plan_dinner"]
    assert cat["notes"] == []


def test_call_read_and_prompt(mcp_url: str) -> None:
    async def go() -> tuple[Any, Any, Any, Any, Any]:
        async with open_session(target(mcp_url)) as session:
            ok = await mcp_targets.call_tool(session, "find_product", {"query": "penne"})
            bad = await mcp_targets.call_tool(session, "broken", {"reason": "demo"})
            res = await mcp_targets.read_resource(session, "pantry://countries")
            tpl = await mcp_targets.read_resource(session, "pantry://recipes/tomato_penne")
            prompt = await mcp_targets.get_prompt(session, "plan_dinner", {"recipe": "tomato_penne"})
            return ok, bad, res, tpl, prompt

    ok, bad, res, tpl, prompt = run(go())
    assert ok["is_error"] is False and ok["structured"]["items"][0]["price"] == 1.97 and ok["ms"] >= 0
    assert bad["is_error"] is True and "broken: demo" in bad["text"]
    assert res["contents"][0]["text"] == '{"canonical": ["Canada", "Italy"]}'
    assert tpl["contents"][0]["text"] == '{"slug": "tomato_penne"}'
    assert "Plan dinner for: tomato_penne under any budget." in prompt["messages"][0]["content"]["text"]


def test_an_anonymous_client_is_refused_with_401(mcp_url: str) -> None:
    async def go() -> None:
        async with open_session(target(mcp_url, token="")):
            pass

    with pytest.raises(McpTargetError, match="HTTP 401") as info:
        run(go())
    assert info.value.status == 401 and "invalid_token" in str(info.value)


def test_an_unreachable_endpoint_is_named() -> None:
    async def go() -> None:
        async with open_session(target(f"http://127.0.0.1:{free_port()}/mcp")):
            pass

    with pytest.raises(McpTargetError, match="cannot reach") as info:
        run(go())
    assert info.value.status == 0


def test_an_error_inside_the_block_passes_through(mcp_url: str) -> None:
    async def go() -> None:
        async with open_session(target(mcp_url)):
            raise KeyError("caller's own bug")

    with pytest.raises(KeyError, match="caller's own bug"):
        run(go())
