"""Traces, online evals, metrics and images: built from the agent's events, no model or network."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from demo_hub import app as app_module
from demo_hub.evals import evaluate
from demo_hub.images import ImageCache, ImageError, ingredient_query
from demo_hub.pricing import call_cost_usd
from demo_hub.settings import Settings
from demo_hub.telemetry import (
    HttpStats,
    TraceRecorder,
    TraceStore,
    add_gateway_spans,
    compute_metrics,
)

PLAN = {"summary": {
    "recipe_name": "Tomato Penne", "total_cost": 20.05, "origin_status": "verified",
    "coverage": {"spend_fraction": 0.96}, "not_stocked": [], "out_of_range": [], "skipped": [],
    "trip": {"stores": ["Pantry Mart Downtown"], "total_cost": 20.26},
    "lines": [{"ingredient": "Penne", "product": "Penne Rigate 500g", "confidence": 1.0,
               "match": "exact", "trip_store": "Pantry Mart Downtown", "trip_price": 2.51},
              {"ingredient": "Garlic", "product": "Fresh Garlic", "confidence": 0.8,
               "match": "generic", "trip_store": "Pantry Mart Downtown", "trip_price": 0.68}],
    "burr_run": "run-tomato_penne-20261005-101010-abc123",
    "pipeline": {"load_recipe": 3.0, "select_products": 40.0, "build_plan": 2.0},
    "llm_calls": [{"step": "select_products", "model": "gemini:gemini-3.1-flash-lite",
                   "total_ms": 3900, "attempts": 2, "status": 200, "server_ms": 3800,
                   "phases": [{"name": "waiting for Google", "ms": 3800, "attempt": 2}]}]}}

ANSWER = ("### Tomato Penne (2 servings)\n| Item | Product | Store | Price | Origin |\n|---|---|---|---|---|\n"
          "| Penne | Penne Rigate 500g | Pantry Mart Downtown | $2.51 | Italy |\n"
          "| Garlic | Fresh Garlic | Pantry Mart Downtown | $0.68 | conflicting |\n"
          "**Total:** $20.05")


def turn_events(answer: str = ANSWER, plan_args: dict[str, Any] | None = None,
                extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    args = plan_args if plan_args is not None else {"slug": "tomato_penne", "lat": 49.28,
                                                    "lon": -123.12, "max_km": 5}
    return [
        {"type": "start", "conversation_id": "c1", "tools": ["pantry-plan-recipe"],
         "available": 14, "disclosure": "progressive"},
        {"type": "observation", "observer": "menu_clerk", "condition": "dish_to_cook",
         "kind": "code", "when": "...", "value": True, "evidence": "matched 'plan'",
         "added": ["pantry-plan-recipe"], "removed": []},
        {"type": "thinking", "step": 1, "model": "ollama:granite4.2:8b#think=false"},
        {"type": "progress", "phase": "reading"},
        {"type": "progress", "phase": "writing", "tokens": 1},
        {"type": "llm_call", "step": 1, "model": "ollama:granite4.2:8b#think=false",
         "tool_calls": 1, "prompt_tokens": 3000, "new_tokens_est": 300, "prompt_s": 15.0,
         "output_tokens": 30, "gen_s": 10.0, "wall_s": 26.0, "reasoning": "I should plan it."},
        {"type": "tool_call", "id": "t1", "name": "pantry-plan-recipe", "arguments": args},
        {"type": "tool_result", "id": "t1", "name": "pantry-plan-recipe", "is_error": False,
         "structured": PLAN, "text": "", "ms": 280.0, "truncated": False, "model_chars": 1800},
        *(extra or []),
        {"type": "thinking", "step": 2, "model": "ollama:granite4.2:8b#think=false"},
        {"type": "llm_call", "step": 2, "model": "ollama:granite4.2:8b#think=false",
         "tool_calls": 0, "prompt_tokens": 3500, "new_tokens_est": 500, "prompt_s": 25.0,
         "output_tokens": 250, "gen_s": 100.0, "wall_s": 126.0},
        {"type": "assistant", "text": answer, "step": 2},
        {"type": "done", "steps": 2, "stop": "answered", "seconds": 160.0,
         "input_tokens": 6500, "output_tokens": 280},
    ]


def record(events: list[dict[str, Any]], model: str = "ollama:granite4.2:8b#think=false"
           ) -> tuple[TraceRecorder, list[dict[str, Any]]]:
    rec = TraceRecorder(conversation_id="c1", model=model, target="gateway-recipes",
                        message="Plan tomato penne")
    return rec, [rec.on(e) for e in events]


def test_a_turn_becomes_a_span_tree_with_every_layer() -> None:
    rec, stamped = record(turn_events())
    assert stamped[0]["trace_id"] == rec.id and all("ts" in e and "at" in e for e in stamped)
    trace = rec.to_dict()
    kinds = [s["kind"] for s in trace["spans"]]
    assert kinds[:3] == ["turn", "model", "tool"]
    assert kinds.count("pantry.step") == 3 and kinds.count("pantry.llm") == 1
    step = trace["spans"][1]
    a = step["attrs"]
    assert a["read_tok_s"] == 20.0 and a["write_tok_s"] == 3.0 and a["cached_share"] == 0.9
    assert a["reasoning"] == "I should plan it." and "first_token_ms" in a
    assert a["cost_usd"] == 0.0                                  # a local model costs nothing
    tool = trace["spans"][2]
    assert tool["parent"] == step["id"] and tool["attrs"]["model_chars"] == 1800
    assert tool["attrs"]["burr_run"].startswith("run-tomato_penne")
    assert tool["attrs"]["plan_confidence"] == {"min": 0.8, "mean": 0.9, "lines": 2}
    steps = [s for s in trace["spans"] if s["kind"] == "pantry.step"]
    assert [s["start_ms"] for s in steps] == sorted(s["start_ms"] for s in steps)   # end to end
    assert steps[1]["duration_ms"] == 40.0
    observed = trace["spans"][0]["events"][0]
    assert observed["name"] == "observation" and observed["attrs"]["observer"] == "menu_clerk"
    assert trace["status"] == "answered" and trace["steps"] == 2
    assert trace["tools"] == ["pantry-plan-recipe"]


def test_gemini_steps_carry_their_list_price_cost() -> None:
    assert call_cost_usd("gemini:gemini-3-flash-preview", 1_000_000, 100_000) == 0.8
    assert call_cost_usd("ollama:granite4.2:8b#think=false", 9_999, 999) == 0.0
    assert call_cost_usd("gemini:gemini-flash-latest", 10, 10) is None    # alias: not guessed
    events = [dict(e, model="gemini:gemini-3-flash-preview") if "model" in e else e
              for e in turn_events()]
    rec, _ = record(events, "gemini:gemini-3-flash-preview")
    trace = rec.to_dict()
    assert trace["cost_usd"] == pytest.approx((3000 + 3500) * 0.5e-6 + (30 + 250) * 3e-6)


def test_online_evals_pass_a_grounded_table_answer() -> None:
    events = turn_events()
    events[0]["tools"] = ["pantry-list-recipes"]       # plan_recipe arrives by an observer
    result = evaluate(events, ["Pantry Mart Downtown", "MegaSave Richmond"], "m")
    checks = {c["name"]: c["passed"] for c in result["checks"]}
    assert checks == {"finished": True, "known_tools": True, "valid_calls": True,
                      "grounded_money": True, "grounded_stores": True, "plan_table": True,
                      "kept_location": True, "no_scope_violation": True}
    assert result["answer_confidence"] == 1.0
    assert result["plan"] == {"lines": 2, "min_line_confidence": 0.8, "mean_line_confidence": 0.9,
                              "exact_share": 0.5, "origin_status": "verified",
                              "origin_spend_verified": 0.96, "left_out": 0}


def test_online_evals_catch_invented_stores_prices_a_missing_table_and_a_dropped_location() -> None:
    bad = ANSWER.replace("| Garlic | Fresh Garlic | Pantry Mart Downtown | $0.68 | conflicting |\n",
                         "") + "\nTomatoes are cheaper at MegaSave Richmond for $1.11."
    retried = [{"type": "tool_call", "id": "t2", "name": "pantry-plan-recipe",
                "arguments": {"slug": "tomato_penne"}},
               {"type": "tool_result", "id": "t2", "name": "pantry-plan-recipe", "is_error": False,
                "structured": PLAN, "text": "", "ms": 1.0, "model_chars": 10},
               {"type": "notice", "text": "scope violation: pantry-plan-week (not disclosed)"}]
    result = evaluate(turn_events(bad, extra=retried), ["Pantry Mart Downtown", "MegaSave Richmond"])
    failed = {c["name"] for c in result["checks"] if not c["passed"]}
    assert failed == {"grounded_money", "grounded_stores", "plan_table", "kept_location",
                      "no_scope_violation"}
    assert result["answer_confidence"] == round(3 / 8, 2)


def test_gateway_spans_join_each_tool_call_from_contextforge() -> None:
    rec, _ = record(turn_events())
    trace = rec.to_dict()
    tool = next(s for s in trace["spans"] if s["kind"] == "tool")
    from datetime import datetime, timedelta
    at = datetime.fromisoformat(trace["started_at"]) + timedelta(milliseconds=tool["start_ms"] + 5)
    stamp = at.strftime("%Y-%m-%dT%H:%M:%S.%f")              # ContextForge: naive UTC

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer jwt"
        if request.url.path == "/observability/traces":
            return httpx.Response(200, json=[{"trace_id": "g1", "start_time": stamp,
                                              "duration_ms": 100.0, "status": "ok",
                                              "http_status_code": 200}])
        return httpx.Response(200, json={"spans": [{"name": "tool.invoke", "start_time": stamp,
                                                    "duration_ms": 60.0, "status": "ok",
                                                    "attributes": {"tool.name": "pantry-plan-recipe"}}]})

    async def go() -> int:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await add_gateway_spans(trace, "http://cf.test", "jwt", client)

    assert asyncio.run(go()) == 1
    gateway = next(s for s in trace["spans"] if s["kind"] == "gateway")
    assert gateway["parent"] == tool["id"] and gateway["attrs"]["overhead_ms"] == 40.0
    assert next(s for s in trace["spans"] if s["kind"] == "gateway.tool")["parent"] == gateway["id"]
    assert tool["attrs"]["gateway_overhead_ms"] == 40.0


def test_the_store_keeps_traces_and_browser_measures_and_metrics_roll_them_up(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    rec, _ = record(turn_events())
    rec.evals = evaluate(rec.events, ["Pantry Mart Downtown"])
    trace = rec.to_dict()
    store.save(trace)
    store.add_browser({"kind": "chat", "trace_id": trace["id"], "ttfb_ms": 40, "first_event_ms": 55,
                       "lag_p95_ms": 8, "render_p95_ms": 12, "total_ms": 160_500})
    store.add_browser({"kind": "page", "ttfb_ms": 20, "lcp_ms": 900, "inp_ms": 120, "cls": 0.02,
                       "long_tasks": 3, "cls_sources": [{"node": "div.header-chips", "value": 0.02}]})
    store.add_browser({"kind": "api", "entries": [{"path": "/hub/traces", "ms": 12, "server_ms": 3,
                                                   "status": 200}]})
    again = TraceStore(tmp_path)                                  # reloads from disk
    kept = again.get(trace["id"])
    assert kept is not None and kept["browser"]["ttfb_ms"] == 40
    assert any(s["kind"] == "browser" for s in kept["spans"])
    assert again.list()[0]["answer_confidence"] == 1.0
    http = HttpStats()
    http.record("POST /hub/agent/chat", 30.0, 200)
    http.record("POST /hub/agent/chat", 50.0, 500)
    m = compute_metrics(list(again.recent.values()), http, list(again.frontend))
    model = m["models"][0]
    assert model["model"].startswith("ollama:granite") and model["turns"] == 1
    assert model["read_tok_s"] == 20.0 and model["answer_confidence"] == 1.0
    assert m["tools"][0]["tool"] == "plan_recipe" and m["tools"][0]["model_chars_p50"] == 1800
    assert {e["check"] for e in m["evals"]} >= {"grounded_money", "plan_table"}
    assert {p["step"] for p in m["pantry_steps"]} == {"load_recipe", "select_products", "build_plan"}
    assert m["observers"] == [{"condition": "menu_clerk.dish_to_cook", "fired": 1}]
    assert m["browser"]["ttfb_p50_ms"] == 40 and m["browser"]["lcp_p50_ms"] == 900
    assert m["browser"]["cls_sources"] == [{"node": "div.header-chips", "shift": 0.02}]
    assert m["browser"]["api"][0]["server_p50_ms"] == 3
    assert m["hub_http"][0]["errors"] == 1
    elsewhere = TraceStore(tmp_path)                              # another process: the bench
    rec2, _ = record(turn_events())
    elsewhere.save(rec2.to_dict())
    assert store.get(rec2.id) is not None                         # seen without a restart


def test_the_hub_takes_browser_telemetry_and_serves_metrics(tmp_path: Path) -> None:
    client = TestClient(app_module.create_app(Settings(traces_dir=str(tmp_path / "t"),
                                                       images_dir=str(tmp_path / "i"))))
    assert client.post("/hub/telemetry", json={"kind": "page", "lcp_ms": 800}).status_code == 204
    assert client.post("/hub/telemetry", json={"kind": "spy"}).status_code == 422
    assert client.post("/hub/telemetry", content=b"not json").status_code == 400
    m = client.get("/hub/metrics").json()
    assert m["browser"]["lcp_p50_ms"] == 800 and m["prices_usd_per_1m"]["gemini-3.1-flash-lite"]
    assert any(r["route"] == "POST /hub/telemetry" for r in m["hub_http"])


@pytest.mark.parametrize("name,query", [
    ("Canned Tomatoes", "tomato"), ("Extra Virgin Olive Oil 500ml", "olive oil"),
    ("Fresh Garlic (~50g)", "garlic"), ("Mozzarella Shredded 200g", "mozzarella"),
    ("Penne", "penne"), ("boneless skinless chicken thighs", "chicken thigh"),
])
def test_ingredient_names_become_lookup_words(name: str, query: str) -> None:
    assert ingredient_query(name) == query


def test_ingredient_thumbnails_come_from_wikipedia_once(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "en.wikipedia.org":
            assert request.url.params["titles"] == "Garlic"
            return httpx.Response(200, json={"query": {"pages": [{"title": "Garlic", "thumbnail": {
                "source": "https://upload.wikimedia.org/garlic.jpg"}}]}})
        return httpx.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})

    async def public(host: str) -> None:
        return None

    async def go() -> Any:
        cache = ImageCache(tmp_path, httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                           check_host=public)
        first = await cache.ingredient("Fresh Garlic")
        second = await ImageCache(tmp_path, check_host=public).ingredient("garlic bulb")
        return first, second

    first, second = asyncio.run(go())
    assert first[1] == "image/jpeg" and first[0].read_bytes() == b"\xff\xd8jpeg"
    assert second == first and len(calls) == 2                    # cached across instances


def test_remote_images_are_guarded(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/svg":
            return httpx.Response(200, content=b"<svg/>", headers={"content-type": "image/svg+xml"})
        if request.url.path == "/hop":
            return httpx.Response(302, headers={"location": "http://10.0.0.1/secret.png"})
        return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})

    async def guard(host: str) -> None:
        if host.startswith("10.") or host == "localhost":
            raise ImageError(f"{host} is not a public address", 403)

    async def go(url: str) -> Any:
        cache = ImageCache(tmp_path, httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                           check_host=guard)
        return await cache.remote(url)

    assert asyncio.run(go("https://example.org/pic.png"))[1] == "image/png"
    assert asyncio.run(go("https://example.org/svg")) is None          # SVG can carry script
    for url in ("http://localhost/x.png", "https://example.org/hop", "file:///etc/passwd"):
        with pytest.raises(ImageError):
            asyncio.run(go(url))


def test_trace_json_round_trips(tmp_path: Path) -> None:
    rec, _ = record(turn_events())
    assert json.loads(json.dumps(rec.to_dict()))["id"] == rec.id
