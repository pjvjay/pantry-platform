"""The guard (guard.py) on every route that changes something, and the Host check on reads.

Each non-GET route is listed once in GUARDED; a route added to the app without being listed
fails test_every_route_that_changes_something_is_listed, so a new route cannot skip the guard
unnoticed.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from demo_hub import app as app_module
from demo_hub.guard import allowed_hosts, allowed_origins, refusal
from demo_hub.mcp_targets import McpTargetError
from demo_hub.settings import Settings

# (method, path, body) for every route that changes something; the pantry proxy stands for
# itself with each method it forwards.
GUARDED: list[tuple[str, str, Any]] = [
    ("POST", "/hub/mcp/pantry/call", {"tool": "find_product"}),
    ("POST", "/hub/mcp/pantry/read", {"uri": "pantry://recipes"}),
    ("POST", "/hub/mcp/pantry/prompt", {"name": "plan_dinner"}),
    ("POST", "/hub/agent/warm", {}),
    ("POST", "/hub/agent/chat", {"message": "hi"}),
    ("DELETE", "/hub/agent/conversations/c1", None),
    ("POST", "/hub/agent/conversations/c1/alternatives", {"ref": 0, "line_no": 1}),
    ("POST", "/hub/agent/conversations/c1/swap", {"ref": 0, "line_no": 1, "product_id": None}),
    ("POST", "/hub/telemetry", {"kind": "page"}),
    ("POST", "/hub/recipes/import", {"url": "https://example.com/r"}),
    ("POST", "/hub/recipes/import/video", {"video_id": "dQw4w9WgXcQ", "consent": True}),
    ("POST", "/hub/sims/run", {"scenarios": ["a"]}),
    ("POST", "/hub/sims/jobs/j1/cancel", None),
    ("POST", "/hub/demo/reset", None),
    ("POST", "/pantry/api/plan/nl", {"recipe_text": "x"}),
    ("PUT", "/pantry/api/settings/runtime", {}),
    ("PATCH", "/pantry/api/settings/runtime", {}),
    ("DELETE", "/pantry/api/things/1", None),
]
OWN = "127.0.0.1:8090"
CONSOLE = {"X-Pantry-Console": "1", "Content-Type": "application/json"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The real app with every upstream down and the agent's slow work faked, so a request the
    guard lets through gets a quick answer from its route (whatever it is)."""
    real = httpx.AsyncClient

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    monkeypatch.setattr(app_module.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(down)}))

    class Refusing:
        async def __aenter__(self) -> None:
            raise McpTargetError("down")

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(app_module, "open_session", lambda target: Refusing())
    monkeypatch.delenv("DEMO_RESET_SCRIPT", raising=False)
    app = app_module.create_app(Settings(pantry_api_url="http://pantry.test",
                                         mcpsim_ui_url="http://runner.test"))
    agent = app.state.agent

    async def fake_run(conv: Any, message: str, recipe_doc: Any = None) -> Any:
        yield {"type": "done", "steps": 0, "stop": "answered", "seconds": 0,
               "input_tokens": 0, "output_tokens": 0}

    async def fake_warm(*args: Any) -> dict[str, Any]:
        return {"warmed": True}

    monkeypatch.setattr(agent, "run", fake_run)
    monkeypatch.setattr(agent, "warm", fake_warm)
    return TestClient(app, base_url=f"http://{OWN}")


def send(client: TestClient, method: str, path: str, body: Any,
         headers: dict[str, str]) -> httpx.Response:
    content = None if body is None else json.dumps(body).encode()
    return client.request(method, path, content=content, headers=headers)


def refused_by_guard(r: httpx.Response, status: int) -> bool:
    return r.status_code == status and "reason" in r.json()


@pytest.mark.parametrize("method,path,body", GUARDED)
def test_the_guard_on_every_route_that_changes_something(
        client: TestClient, method: str, path: str, body: Any) -> None:
    def go(headers: dict[str, str]) -> httpx.Response:
        return send(client, method, path, body, headers)

    json_only = {"Content-Type": "application/json"}
    # a name that is not the hub's, and a rebound name on the hub's own port
    assert refused_by_guard(go({**CONSOLE, "Host": "evil.example"}), 403)
    assert refused_by_guard(go({**CONSOLE, "Host": "attacker.test:8090"}), 403)
    # a page on another site
    assert refused_by_guard(go({**CONSOLE, "Origin": "https://evil.example"}), 403)
    assert refused_by_guard(go({**CONSOLE, "Origin": "null"}), 403)
    # no console header (a form or a text/plain fetch cannot add one without a preflight)
    assert refused_by_guard(go(json_only), 403)
    # not JSON
    assert refused_by_guard(go({"X-Pantry-Console": "1", "Content-Type": "text/plain"}), 415)
    assert refused_by_guard(go({"X-Pantry-Console": "1"}), 415)
    # the console through Vite's dev proxy (it keeps the browser's Host), and a script
    for ok in (go({**CONSOLE, "Host": "localhost:5173", "Origin": "http://localhost:5173"}),
               go(CONSOLE)):
        assert ok.status_code not in (403, 415) or "reason" not in ok.json()


def test_reads_are_checked_for_the_host_only(client: TestClient) -> None:
    assert refused_by_guard(client.get("/hub/status", headers={"Host": "evil.example"}), 403)
    assert refused_by_guard(client.get("/hub/traces", headers={"Host": "attacker.test:8090"}), 403)
    assert refused_by_guard(client.get("/pantry/", headers={"Host": "evil.example"}), 403)
    # a read needs neither the console header nor JSON
    assert client.get("/hub/mcp/targets").status_code == 200
    assert client.get("/hub/mcp/targets", headers={"Host": "localhost:8090"}).status_code == 200
    assert client.get("/hub/mcp/targets", headers={"Host": "[::1]:8090"}).status_code == 200


def test_the_telemetry_beacon_needs_this_consoles_origin(client: TestClient) -> None:
    """navigator.sendBeacon cannot set a header, so /hub/telemetry takes JSON without it, but
    only from a page on this console's origin (which another site cannot forge)."""
    beacon = {"Content-Type": "application/json", "Origin": f"http://{OWN}"}
    assert send(client, "POST", "/hub/telemetry", {"kind": "page"}, beacon).status_code == 204
    assert refused_by_guard(send(client, "POST", "/hub/telemetry", {"kind": "page"},
                                 {"Content-Type": "application/json"}), 403)
    assert refused_by_guard(send(client, "POST", "/hub/telemetry", {"kind": "page"},
                                 {**beacon, "Content-Type": "text/plain"}), 415)
    # the exception is for that route alone
    assert refused_by_guard(send(client, "POST", "/hub/demo/reset", None, beacon), 403)


def test_every_route_that_changes_something_is_listed() -> None:
    app = app_module.create_app(Settings())
    listed = {(m, p) for m, p, _ in GUARDED}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(route.methods - {"GET", "HEAD"}):
            if route.path.startswith("/pantry/api/"):
                assert any(m == method and p.startswith("/pantry/api/") for m, p in listed), method
                continue
            concrete = route.path.replace("{target_id}", "pantry").replace(
                "{conversation_id}", "c1").replace("{job_id}", "j1")
            assert (method, concrete) in listed, f"{method} {route.path} is not in GUARDED"


def test_hosts_and_origins_from_settings() -> None:
    hosts = allowed_hosts(9000, ["Localhost:5173", " ", "hub.lan:9000"])
    assert hosts == {"127.0.0.1:9000", "localhost:9000", "[::1]:9000", "localhost:5173",
                     "hub.lan:9000"}
    origins = allowed_origins(hosts)
    assert "http://localhost:5173" in origins and "https://hub.lan:9000" in origins
    ok = {"host": "127.0.0.1:9000", "x-pantry-console": "1",
          "content-type": "application/json; charset=utf-8"}
    assert refusal("POST", "/hub/agent/chat", ok, hosts, origins) is None
    # the SPA and anything outside /hub and /pantry/api only need the right Host
    assert refusal("POST", "/elsewhere", {"host": "127.0.0.1:9000"}, hosts, origins) is None
    assert refusal("OPTIONS", "/hub/agent/chat", {"host": "127.0.0.1:9000",
                                                  "origin": "http://localhost:9000"},
                   hosts, origins)[0] == 403        # no preflight is ever answered
