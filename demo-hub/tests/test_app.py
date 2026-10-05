"""The hub's HTTP routes with every upstream mocked: status, the pantry proxy, MCP routes, the
Assistant's event stream, the simulations proxy, demo reset and the SPA mount."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from demo_hub import app as app_module
from demo_hub import mcp_targets
from demo_hub.mcp_targets import McpTargetError
from demo_hub.settings import Settings
from demo_hub.sims import SimsError

SETTINGS = Settings(pantry_api_url="http://pantry.test", contextforge_url="http://cf.test",
                    fetch_url="http://fetch.test", mcpsim_ui_url="http://runner.test",
                    ollama_url="http://ollama.test", burr_url="http://burr.test",
                    gemini_api_key="k", pantry_mcp_token="tok", contextforge_jwt="jwt")


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Every httpx.AsyncClient the app builds talks to this handler."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        host, path = request.url.host, request.url.path
        if host == "pantry.test" and path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if host == "pantry.test" and path == "/settings/runtime":
            return httpx.Response(200, json={"demo_mode": True, "models": {}})
        if host == "pantry.test" and path == "/plan/tomato_penne":
            return httpx.Response(200, json={"echo": dict(request.url.params.multi_items()),
                                             "body": request.content.decode()},
                                  headers={"x-upstream": "yes"})
        if host == "pantry.test" and path == "/stores":
            return httpx.Response(200, json=[{"id": 1, "name": "Pantry Mart Downtown", "lat": 49.28,
                                              "lon": -123.12, "address": ""}])
        if host == "pantry.test" and path == "/plan/nl":
            return httpx.Response(409, json={"detail": {"aborted": {"code": "budget_infeasible"}}})
        if host == "cf.test" and path == "/health":
            return httpx.Response(200, json={"status": "healthy"})
        if host == "cf.test" and path == "/servers":
            return httpx.Response(200, json=[{"name": "pantry-sim", "id": "a", "associatedTools": [1, 2]}])
        if host == "ollama.test":
            return httpx.Response(200, json={"models": [{"name": "command-r7b:latest"}]})
        if host == "runner.test":
            return httpx.Response(200, json={"skill": {"path": "/skills/local"}})
        raise httpx.ConnectError("down", request=request)

    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(app_module.httpx, "AsyncClient", factory)
    return seen


def make_client(settings: Settings = SETTINGS) -> TestClient:
    return TestClient(app_module.create_app(settings))


def test_status_reports_every_service(upstream: list[httpx.Request]) -> None:
    body = make_client().get("/hub/status").json()
    services = {s["id"]: s for s in body["services"]}
    assert services["pantry-api"]["ok"] and services["pantry-api"]["runtime"] == {"demo_mode": True, "models": {}}
    assert services["contextforge"]["servers"] == [{"name": "pantry-sim", "id": "a", "tools": 2}]
    assert services["fetch"]["ok"] is False and services["burr"]["ok"] is False
    assert services["ollama"]["models"] == ["command-r7b:latest"]
    assert services["mcp-sim"]["skill"] == "/skills/local"
    assert body["keys"] == {"gemini": True, "pantry_token": True, "contextforge_jwt": True}
    cf_lists = [r for r in upstream if r.url.path == "/servers"]
    assert cf_lists[0].headers["authorization"] == "Bearer jwt"


def test_the_pantry_proxy_forwards_method_path_query_and_body(upstream: list[httpx.Request]) -> None:
    client = make_client()
    r = client.post("/pantry/api/plan/tomato_penne?exclude_origin=United%20States&exclude_origin=Mexico",
                    content=b'{"x": 1}', headers={"content-type": "application/json"})
    assert r.status_code == 200 and r.headers["x-upstream"] == "yes"
    assert r.json()["body"] == '{"x": 1}'
    sent = next(q for q in upstream if q.url.path == "/plan/tomato_penne")
    assert sent.url.params.get_list("exclude_origin") == ["United States", "Mexico"]
    # A gate's 409 passes through untouched.
    assert client.post("/pantry/api/plan/nl", json={}).status_code == 409


def test_the_pantry_proxy_reports_a_down_api(monkeypatch: pytest.MonkeyPatch) -> None:
    real = httpx.AsyncClient

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(app_module.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(down)}))
    r = make_client().get("/pantry/api/health")
    assert r.status_code == 502 and "not reachable" in r.json()["detail"]


def test_mcp_routes_map_target_errors_to_http(monkeypatch: pytest.MonkeyPatch, upstream: Any) -> None:
    client = make_client()
    assert [t["id"] for t in client.get("/hub/mcp/targets").json()] == [
        "pantry", "pantry-anon", "gateway-sim", "gateway-recipes"]
    assert client.get("/hub/mcp/nope/catalog").status_code == 404

    class Refusing:
        async def __aenter__(self) -> None:
            raise McpTargetError("HTTP 401 from http://pantry.test/mcp", 401)

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(app_module, "open_session", lambda target: Refusing())
    r = client.get("/hub/mcp/pantry-anon/catalog")
    assert r.status_code == 401 and "HTTP 401" in r.json()["detail"]

    class Unreachable(Refusing):
        async def __aenter__(self) -> None:
            raise McpTargetError("cannot reach it")

    monkeypatch.setattr(app_module, "open_session", lambda target: Unreachable())
    assert client.post("/hub/mcp/pantry/call", json={"tool": "x"}).status_code == 502


def test_mcp_routes_run_the_operation_on_a_session(monkeypatch: pytest.MonkeyPatch, upstream: Any) -> None:
    class Opened:
        async def __aenter__(self) -> str:
            return "session"

        async def __aexit__(self, *exc: object) -> None:
            return None

    async def fake_catalog(session: str) -> dict[str, Any]:
        return {"tools": [{"name": "t"}], "resources": [], "resource_templates": [], "prompts": [], "notes": []}

    async def fake_call(session: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"name": name, "args": args, "session": session}

    async def fake_read(session: str, uri: str) -> dict[str, Any]:
        return {"uri": uri}

    async def fake_prompt(session: str, name: str, args: dict[str, str]) -> dict[str, Any]:
        return {"name": name, "args": args}

    monkeypatch.setattr(app_module, "open_session", lambda target: Opened())
    monkeypatch.setattr(mcp_targets, "catalog", fake_catalog)
    monkeypatch.setattr(mcp_targets, "call_tool", fake_call)
    monkeypatch.setattr(mcp_targets, "read_resource", fake_read)
    monkeypatch.setattr(mcp_targets, "get_prompt", fake_prompt)
    client = make_client()
    cat = client.get("/hub/mcp/pantry/catalog").json()
    assert cat["tools"] == [{"name": "t"}] and cat["target"]["id"] == "pantry" and "token" not in cat["target"]
    assert client.post("/hub/mcp/pantry/call", json={"tool": "find_product", "arguments": {"query": "x"}}).json() == {
        "name": "find_product", "args": {"query": "x"}, "session": "session"}
    assert client.post("/hub/mcp/pantry/read", json={"uri": "pantry://recipes"}).json() == {"uri": "pantry://recipes"}
    assert client.post("/hub/mcp/pantry/prompt", json={"name": "p", "arguments": {"a": "b"}}).json() == {
        "name": "p", "args": {"a": "b"}}
    assert client.post("/hub/mcp/pantry/call", json={}).status_code == 422


def test_agent_options_and_stream(monkeypatch: pytest.MonkeyPatch, upstream: Any, tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("body")
    client = make_client(Settings(**{**SETTINGS.__dict__, "recipe_shopper_skill": str(skill),
                                     "traces_dir": str(tmp_path / "traces")}))
    options = client.get("/hub/agent/options").json()
    assert options["default_model"] == "gemini:gemini-3-flash-preview" and options["skill_loaded"] is True
    assert [t["id"] for t in options["targets"]] == ["gateway-recipes", "pantry", "gateway-sim"]

    async def fake_run(conv: Any, message: str) -> Any:
        yield {"type": "start", "conversation_id": conv.id}
        yield {"type": "assistant", "text": f"you said {message}"}
        yield {"type": "done", "steps": 1, "stop": "answered", "seconds": 0.1,
               "input_tokens": 3, "output_tokens": 2}

    agent = client.app.state.agent  # type: ignore[attr-defined]
    monkeypatch.setattr(agent, "run", fake_run)
    r = client.post("/hub/agent/chat", json={"message": "hi"})
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["server-timing"].startswith("hub;dur=")
    events = [json.loads(line[6:]) for line in r.text.split("\n\n") if line.startswith("data: ")]
    # every event is stamped for the browser: ms since the turn began, and epoch ms
    assert all(isinstance(e["ts"], float) and e["at"] > 1.7e12 for e in events)
    assert {k: v for k, v in events[1].items() if k not in ("ts", "at")} == \
        {"type": "assistant", "text": "you said hi"}
    trace_id = events[0]["trace_id"]
    # the answer's evals arrive just before "done", and the turn is kept as a trace
    assert [e["type"] for e in events[-2:]] == ["evals", "done"]
    assert events[-2]["trace_id"] == trace_id and events[-2]["answer_confidence"] is not None
    # graded with the turn's end in view: a finished turn passes "finished"
    assert {c["name"]: c["passed"] for c in events[-2]["checks"]}["finished"] is True
    listed = client.get("/hub/traces").json()
    assert listed[0]["id"] == trace_id and listed[0]["status"] == "answered"
    assert client.get(f"/hub/traces/{trace_id}").json()["evals"]["checks"]
    assert client.get("/hub/traces/tr-missing").status_code == 404
    conv_id = events[0]["conversation_id"]
    assert client.delete(f"/hub/agent/conversations/{conv_id}").json() == {"forgotten": True}
    assert client.delete(f"/hub/agent/conversations/{conv_id}").json() == {"forgotten": False}
    assert client.post("/hub/agent/chat", json={"message": "hi", "model": "gpt-4"}).status_code == 422
    assert client.post("/hub/agent/chat", json={"message": ""}).status_code == 422


def test_sims_routes_proxy_and_map_errors(monkeypatch: pytest.MonkeyPatch, upstream: Any) -> None:
    client = make_client()
    sims_client = app_module.SimsClient

    async def fake_get(self: Any, path: str) -> Any:
        if path.endswith("/missing"):
            raise SimsError("mcp-sim runner answered 404: unknown", 404)
        return {"path": path}

    async def fake_start(self: Any, scenarios: Any, preset: str, repeat: int = 1, modes: Any = None) -> Any:
        return {"job_id": "j", "scenarios": scenarios, "preset": preset, "repeat": repeat}

    async def fake_cancel(self: Any, job_id: str) -> Any:
        return {"cancelled": job_id}

    monkeypatch.setattr(sims_client, "get", fake_get)
    monkeypatch.setattr(sims_client, "start", fake_start)
    monkeypatch.setattr(sims_client, "cancel", fake_cancel)
    assert client.get("/hub/sims/scenarios").json() == {"path": "/api/scenarios"}
    assert client.get("/hub/sims/scenarios/a").json() == {"path": "/api/scenarios/a"}
    assert client.get("/hub/sims/runs/a/r1").json() == {"path": "/api/runs/a/r1"}
    assert client.get("/hub/sims/runs/a/r1/transcripts/t.jsonl").json() == {"path": "/api/runs/a/r1/transcripts/t.jsonl"}
    assert client.get("/hub/sims/jobs/j").json() == {"path": "/api/jobs/j"}
    assert client.get("/hub/sims/scenarios/missing").status_code == 404
    assert client.post("/hub/sims/run", json={"scenarios": ["a"], "preset": "hybrid"}).json()["preset"] == "hybrid"
    assert client.post("/hub/sims/run", json={"scenarios": "all", "repeat": 9}).status_code == 422
    assert client.post("/hub/sims/jobs/j/cancel").json() == {"cancelled": "j"}
    presets = client.get("/hub/sims/presets").json()
    assert set(presets["presets"]) == {"gemini", "hybrid", "granite-agent", "granite", "local", "dry"} and presets["runner_url"] == "http://runner.test"


def test_demo_reset(monkeypatch: pytest.MonkeyPatch, upstream: Any, tmp_path: Path) -> None:
    client = make_client()
    monkeypatch.delenv("DEMO_RESET_SCRIPT", raising=False)
    assert client.post("/hub/demo/reset").status_code == 501
    ok = tmp_path / "ok.sh"
    ok.write_text("#!/bin/sh\necho reseeded\n")
    ok.chmod(0o755)
    monkeypatch.setenv("DEMO_RESET_SCRIPT", str(ok))
    assert client.post("/hub/demo/reset").json() == {"ok": True, "output": "reseeded\n"}
    bad = tmp_path / "bad.sh"
    bad.write_text("#!/bin/sh\necho broken >&2\nexit 3\n")
    bad.chmod(0o755)
    monkeypatch.setenv("DEMO_RESET_SCRIPT", str(bad))
    r = client.post("/hub/demo/reset")
    assert r.status_code == 500 and "broken" in r.json()["detail"]


def test_root_redirects_and_the_spa_is_served(upstream: Any, tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html>pantry</html>")
    client = make_client(Settings(**{**SETTINGS.__dict__, "spa_dist": str(tmp_path)}))
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/pantry/"
    page = client.get("/pantry/")
    assert "pantry" in page.text and page.headers["cache-control"] == "no-cache"
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-abc123.js").write_text("1")
    assert "immutable" in client.get("/pantry/assets/index-abc123.js").headers["cache-control"]
    # The API proxy is matched before the SPA mount.
    assert client.get("/pantry/api/health").json() == {"status": "ok"}


def test_a_run_comes_back_from_every_source_and_warming_is_shared(
        monkeypatch: pytest.MonkeyPatch, upstream: Any, tmp_path: Path) -> None:
    client = make_client(Settings(**{**SETTINGS.__dict__, "traces_dir": str(tmp_path / "traces")}))
    client.app.state.traces.save({  # type: ignore[attr-defined]
        "id": "tr-run", "started_at": "2026-10-05T08:00:00+00:00", "spans": []})
    run = client.get("/hub/runs/tr-run").json()
    assert run["trace"]["id"] == "tr-run" and run["burr"] == [] and run["gateway"] == []
    assert set(run["links"]) == {"burr_ui", "contextforge"}
    assert client.get("/hub/runs/tr-missing").status_code == 404

    calls: list[tuple[str, str, Any]] = []

    async def fake_warm(model: str, target: str, disclosure: Any = None) -> dict[str, Any]:
        calls.append((model, target, disclosure))
        return {"model": model, "prompt_tokens": 1500}

    monkeypatch.setattr(client.app.state.agent, "warm", fake_warm)  # type: ignore[attr-defined]
    r = client.post("/hub/agent/warm", json={"model": "ollama:granite4.2:8b"})
    assert r.json() == {"model": "ollama:granite4.2:8b", "prompt_tokens": 1500}
    assert calls == [("ollama:granite4.2:8b", "gateway-recipes", None)]
