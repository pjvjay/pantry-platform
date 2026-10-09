"""One Assistant run with everything recorded about it, from every system that saw it.

The hub's trace already holds the turn, the model steps (tokens, reading, writing, queueing,
reasoning), the tool calls, ContextForge's overhead, pantry's step timings, the online evals and
the browser's numbers. ``run_view`` adds what lives elsewhere, aligned on the trace's clock:

- **Burr**: each plan call's run from pantry's tracker files (``log.jsonl``): every action at its
  real start and end, its inputs, its result, any exception, and the state keys it changed;
- **ContextForge**: each tool call's gateway trace from its observability API: the request, the
  tool invocation, HTTP status, response size, the tool and gateway ids.

Either source being absent (Burr's files elsewhere, ContextForge's observability off) leaves its
part empty with a note, never the whole view.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from demo_hub.settings import Settings

BURR_PROJECT = "pantry-planner"
STATE_CHARS = 2_000          # each changed state value, as JSON, at most


def _clip(value: Any, limit: int = STATE_CHARS) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return value if len(text) <= limit else text[:limit] + "…"


def _ms_after(start: datetime, value: str, naive_is_local: bool) -> float | None:
    """Milliseconds from the trace's start to ``value``. Burr writes local times without a
    zone; ContextForge writes UTC without one."""
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone() if naive_is_local else dt.replace(tzinfo=UTC)
    return round((dt - start).total_seconds() * 1000, 1)


def burr_run(burr_dir: Path, app_id: str, start: datetime) -> dict[str, Any]:
    """One Burr run's actions from its tracker log, placed on the trace's clock."""
    log = burr_dir / BURR_PROJECT / app_id / "log.jsonl"
    if not log.is_file():
        return {"app_id": app_id, "steps": [], "note": f"no Burr log at {log}"}
    begins: dict[int, dict[str, Any]] = {}
    steps: list[dict[str, Any]] = []
    previous: dict[str, Any] = {}
    for raw in log.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if entry.get("type") == "begin_entry":
            begins[int(entry.get("sequence_id", -1))] = entry
        elif entry.get("type") == "end_entry":
            begin = begins.get(int(entry.get("sequence_id", -1)), {})
            state = entry.get("state") or {}
            changed = {k: _clip(v) for k, v in state.items()
                       if not k.startswith("__") and previous.get(k) != v}
            previous = state
            start_ms = _ms_after(start, begin.get("start_time", ""), naive_is_local=True)
            end_ms = _ms_after(start, entry.get("end_time", ""), naive_is_local=True)
            steps.append({
                "action": entry.get("action"), "sequence_id": entry.get("sequence_id"),
                "start_ms": start_ms, "end_ms": end_ms,
                "ms": round(end_ms - start_ms, 1) if start_ms is not None and end_ms is not None
                else None,
                "inputs": _clip(begin.get("inputs") or {}), "result": _clip(entry.get("result")),
                "exception": entry.get("exception"), "changed": changed})
    return {"app_id": app_id, "steps": steps}


async def gateway_trace(client: httpx.AsyncClient, base_url: str, token: str, trace_id: str,
                        start: datetime) -> dict[str, Any]:
    """One ContextForge trace with its spans, placed on the trace's clock."""
    try:
        response = await client.get(f"{base_url}/observability/traces/{trace_id}",
                                    headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        return {"trace_id": trace_id, "note": f"ContextForge unreachable: {type(exc).__name__}"}
    if response.status_code != 200:
        return {"trace_id": trace_id, "note": f"ContextForge answered HTTP {response.status_code}"}
    data = response.json()
    return {
        "trace_id": trace_id, "name": data.get("name"), "status": data.get("status"),
        "http_status": data.get("http_status_code"), "duration_ms": data.get("duration_ms"),
        "start_ms": _ms_after(start, data.get("start_time", ""), naive_is_local=False),
        "attributes": data.get("attributes") or {},
        "spans": [{"name": s.get("name"), "status": s.get("status"),
                   "duration_ms": s.get("duration_ms"),
                   "start_ms": _ms_after(start, s.get("start_time", ""), naive_is_local=False),
                   "attributes": s.get("attributes") or {}}
                  for s in data.get("spans") or []]}


def exact_pantry_steps(spans: list[dict[str, Any]], burr: list[dict[str, Any]]
                       ) -> list[dict[str, Any]]:
    """The trace's spans with each plan call's pantry steps at Burr's recorded times, in place of
    the ones laid end to end from their durations."""
    exact = {b["tool_span"]: b["steps"] for b in burr if b.get("steps")}
    out = [s for s in spans if not (s["kind"] == "pantry.step" and s.get("parent") in exact)]
    for tool_span, steps in exact.items():
        for step in steps:
            if step["start_ms"] is None or step["end_ms"] is None:
                continue
            out.append({
                "id": f"{tool_span}b{step['sequence_id']}", "parent": tool_span,
                "kind": "pantry.step", "name": f"pantry · {step['action']}",
                "start_ms": step["start_ms"], "end_ms": step["end_ms"], "duration_ms": step["ms"],
                "status": "error" if step.get("exception") else "ok",
                "attrs": {"source": "burr", "result": step.get("result"),
                          **({"exception": step["exception"]} if step.get("exception") else {})},
                "events": []})
    return out


def burr_dir(settings: Settings) -> Path | None:
    return Path(settings.burr_dir).expanduser() if settings.burr_dir else None


async def run_view(trace: dict[str, Any], settings: Settings,
                   client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    start = datetime.fromisoformat(trace["started_at"])
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    spans = trace.get("spans") or []
    tools = {s["id"]: s for s in spans if s["kind"] == "tool"}
    directory = burr_dir(settings)
    burr = []
    for span_id, span in tools.items():
        app_id = (span.get("attrs") or {}).get("burr_run")
        if not app_id:
            continue
        found = burr_run(directory, app_id, start) if directory else {
            "app_id": app_id, "steps": [], "note": "DEMO_BURR_DIR is not set"}
        burr.append({"tool_span": span_id, **found,
                     "ui_url": f"{settings.burr_url}/project/{BURR_PROJECT}/null/{app_id}"})
    gateway_ids = [(s.get("parent"), (s.get("attrs") or {}).get("gateway_trace"))
                   for s in spans if s["kind"] == "gateway"]
    gateway = []
    if any(g for _, g in gateway_ids):
        own = client is None
        client = client or httpx.AsyncClient(timeout=10)
        try:
            for parent, trace_id in gateway_ids:
                if trace_id and settings.contextforge_jwt:
                    gateway.append({"tool_span": parent, **await gateway_trace(
                        client, settings.contextforge_url, settings.contextforge_jwt,
                        trace_id, start)})
        finally:
            if own:
                await client.aclose()
    trace = {**trace, "spans": exact_pantry_steps(spans, burr)}
    return {"trace": trace, "burr": burr, "gateway": gateway,
            "links": {"burr_ui": settings.burr_url,
                      "contextforge": f"{settings.contextforge_url}/admin"}}
