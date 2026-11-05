"""One run from every system that saw it: Burr's steps and ContextForge's trace on the trace's
clock."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from demo_hub.runs import BURR_PROJECT, burr_run, run_view
from demo_hub.settings import Settings

START = datetime(2026, 10, 5, 8, 0, 0, tzinfo=UTC)


def local(seconds: float) -> str:
    """A time ``seconds`` after START as Burr writes it: local and without a zone."""
    return (START + timedelta(seconds=seconds)).astimezone().replace(tzinfo=None).isoformat()


def write_burr(root: Path, app_id: str) -> None:
    log = root / BURR_PROJECT / app_id / "log.jsonl"
    log.parent.mkdir(parents=True)
    entries = [
        {"type": "begin_entry", "start_time": local(1.0), "action": "load_recipe", "inputs": {},
         "sequence_id": 0},
        {"type": "end_entry", "end_time": local(1.002), "action": "load_recipe",
         "result": {"ingredient_count": 5}, "exception": None, "sequence_id": 0,
         "state": {"__SEQUENCE_ID": 0, "recipe": {"slug": "tomato_penne"}}},
        {"type": "begin_entry", "start_time": local(1.002), "action": "load_products", "inputs": {},
         "sequence_id": 1},
        {"type": "end_entry", "end_time": local(1.125), "action": "load_products", "result": None,
         "exception": "boom", "sequence_id": 1,
         "state": {"__SEQUENCE_ID": 1, "recipe": {"slug": "tomato_penne"}, "products": [1, 2]}},
    ]
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\nnot json\n")


def test_burr_steps_land_on_the_trace_clock_with_what_they_changed(tmp_path: Path) -> None:
    write_burr(tmp_path, "run-x")
    run = burr_run(tmp_path, "run-x", START)
    first, second = run["steps"]
    assert (first["action"], first["start_ms"], first["ms"]) == ("load_recipe", 1000.0, 2.0)
    assert first["changed"] == {"recipe": {"slug": "tomato_penne"}}
    assert second["changed"] == {"products": [1, 2]} and second["exception"] == "boom"
    assert burr_run(tmp_path, "run-missing", START)["note"].startswith("no Burr log")


TRACE: dict[str, Any] = {
    "id": "tr-1", "started_at": START.isoformat(), "spans": [
        {"id": "s1", "parent": None, "kind": "turn", "name": "turn", "start_ms": 0, "end_ms": 2000,
         "duration_ms": 2000, "status": "ok", "attrs": {}, "events": []},
        {"id": "s3", "parent": "s1", "kind": "tool", "name": "tool plan", "start_ms": 900,
         "end_ms": 1200, "duration_ms": 300, "status": "ok",
         "attrs": {"tool": "pantry-plan-recipe", "burr_run": "run-x"}, "events": []},
        {"id": "s3p0", "parent": "s3", "kind": "pantry.step", "name": "pantry · load_recipe",
         "start_ms": 900, "end_ms": 902, "duration_ms": 2, "status": "ok",
         "attrs": {"approx": True}, "events": []},
        {"id": "s3g", "parent": "s3", "kind": "gateway", "name": "gateway (ContextForge)",
         "start_ms": 910, "end_ms": 1190, "duration_ms": 280, "status": "ok",
         "attrs": {"gateway_trace": "cf-1"}, "events": []},
    ]}


def test_the_run_view_joins_burr_and_contextforge(tmp_path: Path) -> None:
    write_burr(tmp_path, "run-x")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "name": "POST /servers/x/mcp", "status": "ok", "http_status_code": 200,
            "duration_ms": 280.0, "start_time": "2026-10-05T08:00:00.910000",
            "attributes": {"http.route": "/servers/x/mcp"},
            "spans": [{"name": "tool.invoke", "status": "ok", "duration_ms": 230.0,
                       "start_time": "2026-10-05T08:00:00.950000",
                       "attributes": {"tool.name": "pantry-plan-recipe", "success": True}}]})

    settings = Settings(burr_dir=str(tmp_path), contextforge_jwt="t",
                        contextforge_url="http://cf.test", burr_url="http://burr.test")
    view = asyncio.run(run_view(TRACE, settings, httpx.AsyncClient(
        transport=httpx.MockTransport(handler))))
    assert seen[0].url.path == "/observability/traces/cf-1"
    assert seen[0].headers["authorization"] == "Bearer t"
    [gateway] = view["gateway"]
    assert gateway["tool_span"] == "s3" and gateway["start_ms"] == 910.0
    assert gateway["spans"][0]["start_ms"] == 950.0
    [burr] = view["burr"]
    assert burr["ui_url"] == f"http://burr.test/project/{BURR_PROJECT}/null/run-x"
    # pantry's steps at Burr's times replace the ones laid end to end
    steps = [s for s in view["trace"]["spans"] if s["kind"] == "pantry.step"]
    assert [(s["name"], s["start_ms"], s["status"]) for s in steps] == [
        ("pantry · load_recipe", 1000.0, "ok"), ("pantry · load_products", 1002.0, "error")]
    assert all(s["attrs"]["source"] == "burr" for s in steps)
    assert TRACE["spans"][2]["attrs"] == {"approx": True}          # the stored trace is untouched


def test_without_burr_files_or_a_token_the_view_still_comes_back() -> None:
    view = asyncio.run(run_view(TRACE, Settings()))
    assert view["burr"][0]["note"] == "DEMO_BURR_DIR is not set" and view["gateway"] == []
    assert [s["kind"] for s in view["trace"]["spans"]].count("pantry.step") == 1
