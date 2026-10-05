"""Traces and metrics for every layer of the Assistant's workflow.

One Assistant turn is one trace: a tree of spans built from the agent's own event stream
(``TraceRecorder.on``), so the agent loop carries no tracing code:

    turn                                   the shopper's message to the answer
    ├─ observers                           an LLM judge's call (code observers are events)
    ├─ step N · <model>                    one model call: tokens read (cached / new) and written,
    │                                      time to first token, read and write rates, load time,
    │                                      the model's reasoning when it shows it, its text
    │  └─ tool <name>                      one MCP call: arguments, result size, what the model
    │     │                                reads of it, error, plan confidence
    │     ├─ gateway (ContextForge)        added after the turn from ContextForge's own traces:
    │     │  └─ gateway → tool             its request and tool invocation, so its overhead shows
    │     ├─ pantry · <step>               the plan's Burr steps (laid end to end: approximate)
    │     └─ pantry · LLM <step>           pantry's own LLM calls, phase by phase
    └─ browser                             what the shopper's browser measured: time to first
                                           byte, to the first event, stream lag, render time

The answer's evals (evals.py) and confidence are kept on the trace. Traces are stored as JSON
lines (``TraceStore``); ``compute_metrics`` rolls the recent ones up per model, tool, observer,
eval check, pantry step and browser measure, with the hub's own HTTP timings (``HttpStats``).
"""

from __future__ import annotations

import json
import statistics
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from demo_hub.pricing import call_cost_usd

PREVIEW_CHARS = 2_000          # a tool result's preview kept in its span
GATEWAY_MATCH_S = 3.0          # a gateway trace this close to a tool call's start is that call


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


def canonical(name: str) -> str:
    return str(name).removeprefix("pantry-").replace("-", "_")


@dataclass
class Span:
    id: str
    parent: str | None
    kind: str          # turn | observers | model | tool | gateway | gateway.tool | pantry.step
    name: str          # | pantry.llm | browser
    start_ms: float
    end_ms: float | None = None
    status: str = "ok"
    attrs: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration_ms(self) -> float | None:
        return None if self.end_ms is None else round(self.end_ms - self.start_ms, 1)


class TraceRecorder:
    """Builds one turn's trace from the agent's events, as they stream to the browser. ``on``
    returns each event stamped with ``ts`` (ms since the turn started) and ``at`` (epoch ms), so
    the browser can measure how late it received it; the ``start`` event also gets the trace id.
    """

    def __init__(self, *, conversation_id: str, model: str, target: str, message: str,
                 disclosure: str = "", turn: int = 1) -> None:
        self.id = f"tr-{uuid.uuid4().hex[:12]}"
        self._t0 = time.perf_counter()
        self.started_at = datetime.now(UTC)
        self.meta: dict[str, Any] = {"conversation_id": conversation_id, "model": model,
                                     "target": target, "disclosure": disclosure, "turn": turn,
                                     "message": message[:500]}
        self.spans: list[Span] = []
        self.events: list[dict[str, Any]] = []      # the raw events, for the evals
        self.evals: dict[str, Any] | None = None
        self.browser: dict[str, Any] | None = None
        self.status = "running"
        self._root = self._open("turn", f"turn {turn}", None)
        self._step: Span | None = None
        self._observers: Span | None = None
        self._tools: dict[str, Span] = {}

    # --- spans -----------------------------------------------------------------------------------

    def now_ms(self) -> float:
        return _ms(time.perf_counter() - self._t0)

    def _open(self, kind: str, name: str, parent: Span | None, start_ms: float | None = None,
              **attrs: Any) -> Span:
        span = Span(f"s{len(self.spans) + 1}", parent.id if parent else None, kind, name,
                    self.now_ms() if start_ms is None else start_ms, attrs=dict(attrs))
        self.spans.append(span)
        return span

    def _close(self, span: Span | None, status: str = "ok", **attrs: Any) -> None:
        if span is None or span.end_ms is not None:
            return
        span.end_ms = self.now_ms()
        span.status = status
        span.attrs.update(attrs)

    def _event(self, span: Span, name: str, **attrs: Any) -> None:
        span.events.append({"at_ms": self.now_ms(), "name": name, "attrs": attrs})

    # --- the event stream ------------------------------------------------------------------------

    def on(self, event: dict[str, Any]) -> dict[str, Any]:
        kind = event.get("type")
        self.events.append(event)
        if kind not in ("observation", "goal_enabled", "tools_offered", "notice", "progress"):
            self._close(self._observers)
            self._observers = None
        parent = self._step or self._root
        if kind == "start":
            self._root.attrs.update(tools_offered=event.get("tools"),
                                    tools_available=event.get("available"),
                                    disclosure=event.get("disclosure"))
            self.meta["disclosure"] = event.get("disclosure") or self.meta["disclosure"]
        elif kind == "observing":
            self._observers = self._open("observers", f"observers ({event.get('trigger')})",
                                         parent, model=event.get("model"),
                                         observers=event.get("observers"))
        elif kind == "observation":
            self._event(self._observers or parent, "observation", **{
                k: event.get(k) for k in ("observer", "condition", "kind", "when", "value",
                                          "evidence", "added", "removed")})
        elif kind in ("goal_enabled", "tools_offered", "notice"):
            self._event(parent, kind, **{k: v for k, v in event.items() if k != "type"})
            if kind == "notice" and str(event.get("text", "")).startswith("scope violation"):
                parent.status = "warning"
        elif kind == "thinking":
            self._close(self._step)
            self._step = self._open("model", f"step {event.get('step')} · {event.get('model')}",
                                    self._root, step=event.get("step"), model=event.get("model"))
        elif kind == "progress" and self._step is not None:
            if event.get("phase") == "writing" and "first_token_ms" not in self._step.attrs:
                self._step.attrs["first_token_ms"] = round(self.now_ms() - self._step.start_ms, 1)
        elif kind == "llm_call" and self._step is not None:
            metrics = {k: v for k, v in event.items() if k not in ("type", "step", "model")}
            self._step.attrs.update(metrics)
            self._step.attrs.update(_rates(metrics))
            self._step.attrs["cost_usd"] = call_cost_usd(
                str(event.get("model")), int(metrics.get("prompt_tokens") or 0),
                int(metrics.get("output_tokens") or 0))
            self._step.end_ms = self.now_ms()
        elif kind == "assistant" and self._step is not None:
            self._step.attrs["text"] = str(event.get("text") or "")[:4_000]
        elif kind == "tool_call":
            span = self._open("tool", f"tool {event.get('name')}", self._step or self._root,
                              tool=event.get("name"), arguments=event.get("arguments"))
            self._tools[str(event.get("id"))] = span
        elif kind == "tool_result":
            self._tool_result(event)
        elif kind == "error":
            self._root.status = "error"
            self._root.attrs["error"] = str(event.get("message"))
        elif kind == "done":
            self._close(self._step)
            self.status = str(event.get("stop"))
            self._close(self._root, "ok" if self.status == "answered" else "error",
                        steps=event.get("steps"), input_tokens=event.get("input_tokens"),
                        output_tokens=event.get("output_tokens"))
        stamped = {**event, "ts": self.now_ms(), "at": round(time.time() * 1000)}
        if kind == "start":
            stamped["trace_id"] = self.id
        return stamped

    def _tool_result(self, event: dict[str, Any]) -> None:
        span = self._tools.pop(str(event.get("id")), None)
        if span is None:
            return
        structured, text = event.get("structured"), str(event.get("text") or "")
        body = json.dumps(structured, default=str) if structured is not None else text
        self._close(span, "error" if event.get("is_error") else "ok",
                    mcp_ms=event.get("ms"), result_chars=len(body),
                    model_chars=event.get("model_chars"), truncated=event.get("truncated"),
                    preview=body[:PREVIEW_CHARS])
        summary = structured.get("summary") if isinstance(structured, dict) else None
        if isinstance(summary, dict):
            self._pantry_children(span, summary)

    def _pantry_children(self, tool: Span, summary: dict[str, Any]) -> None:
        """A plan's own steps and LLM calls under its tool span, laid end to end from the tool
        call's start (Burr times each step but not where it sat in the call: approximate)."""
        if summary.get("burr_run"):
            tool.attrs["burr_run"] = summary["burr_run"]
        lines = summary.get("lines") or []
        conf = [float(x["confidence"]) for x in lines if x.get("confidence") is not None]
        if conf:
            tool.attrs["plan_confidence"] = {"min": round(min(conf), 2),
                                             "mean": round(sum(conf) / len(conf), 2),
                                             "lines": len(lines)}
        at = tool.start_ms
        pipeline = summary.get("pipeline") or []
        if isinstance(pipeline, dict):           # the MCP summary's compact {step: ms}
            pipeline = [{"step": k, "ms": v} for k, v in pipeline.items()]
        for step in pipeline:
            ms = float(step.get("ms") or 0)
            child = self._open("pantry.step", f"pantry · {step.get('step')}", tool, start_ms=at,
                               approx=True)
            child.end_ms = round(at + ms, 1)
            child.status = "error" if step.get("error") else "ok"
            at += ms
        for call in summary.get("llm_calls") or []:
            child = self._open("pantry.llm", f"pantry · LLM {call.get('step')}", tool,
                               start_ms=tool.start_ms, model=call.get("model"),
                               attempts=call.get("attempts"), status_code=call.get("status"),
                               server_ms=call.get("server_ms"), phases=call.get("phases"),
                               approx=True)
            child.end_ms = round(tool.start_ms + float(call.get("total_ms") or 0), 1)

    # --- the finished trace ----------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        spans = []
        for s in self.spans:
            d = asdict(s)
            d["duration_ms"] = s.duration_ms
            spans.append(d)
        root = self.spans[0]
        steps = [s for s in self.spans if s.kind == "model"]
        return {
            "id": self.id, "started_at": self.started_at.isoformat(), **self.meta,
            "status": self.status, "wall_ms": root.duration_ms,
            "steps": len(steps),
            "input_tokens": root.attrs.get("input_tokens"),
            "output_tokens": root.attrs.get("output_tokens"),
            "cost_usd": round(sum(float(s.attrs.get("cost_usd") or 0) for s in steps), 6),
            "tools": [s.attrs.get("tool") for s in self.spans if s.kind == "tool"],
            "evals": self.evals, "browser": self.browser, "spans": spans,
        }


def _rates(m: dict[str, Any]) -> dict[str, Any]:
    """Read and write rates, and the share of the prompt that came from the model's cache."""
    out: dict[str, Any] = {}
    prompt, new = m.get("prompt_tokens"), m.get("new_tokens_est")
    if m.get("prompt_s") and new:
        out["read_tok_s"] = round(float(new) / float(m["prompt_s"]), 1)
    if m.get("gen_s") and m.get("output_tokens"):
        out["write_tok_s"] = round(float(m["output_tokens"]) / float(m["gen_s"]), 2)
    if prompt and new is not None:
        out["cached_share"] = round(max(0.0, 1 - float(new) / float(prompt)), 3)
    return out


# --- gateway spans from ContextForge -------------------------------------------------------------

def _utc(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def add_gateway_spans(trace: dict[str, Any], base_url: str, token: str,
                            client: httpx.AsyncClient | None = None) -> int:
    """Attach ContextForge's own spans to each tool span of ``trace``: the gateway's request and
    its tool invocation, matched by tool name and start time. Returns how many were attached.
    ContextForge records them only with OBSERVABILITY_ENABLED=true; without, nothing changes."""
    tools = [s for s in trace["spans"] if s["kind"] == "tool"]
    if not tools or not token:
        return 0
    started = _utc(trace["started_at"])
    own = client is None
    client = client or httpx.AsyncClient(timeout=10)
    attached = 0
    try:
        headers = {"Authorization": f"Bearer {token}"}
        listed = await client.get(f"{base_url}/observability/traces", params={"limit": 100},
                                  headers=headers)
        if listed.status_code != 200:
            return 0
        window = (started - timedelta(seconds=5),
                  started + timedelta(milliseconds=(trace.get("wall_ms") or 0) + 5_000))
        candidates = [t for t in listed.json()
                      if window[0] <= _utc(t["start_time"]) <= window[1]]
        used: set[str] = set()
        for span in tools:
            at = started + timedelta(milliseconds=span["start_ms"])
            best = None
            for t in candidates:
                if t["trace_id"] in used:
                    continue
                gap = abs((_utc(t["start_time"]) - at).total_seconds())
                if gap <= GATEWAY_MATCH_S and (best is None or gap < best[0]):
                    best = (gap, t)
            if best is None:
                continue
            detail = await client.get(f"{base_url}/observability/traces/{best[1]['trace_id']}",
                                      headers=headers)
            if detail.status_code != 200:
                continue
            spans = detail.json().get("spans") or []
            invoke = next((s for s in spans if s.get("name") == "tool.invoke"
                           and s.get("attributes", {}).get("tool.name") == span["attrs"].get("tool")),
                          None)
            if invoke is None:
                continue
            used.add(best[1]["trace_id"])
            request_ms = float(best[1].get("duration_ms") or 0)
            invoke_ms = float(invoke.get("duration_ms") or 0)
            offset = (_utc(best[1]["start_time"]) - started).total_seconds() * 1000
            gw_id = f"{span['id']}g"
            trace["spans"].append({
                "id": gw_id, "parent": span["id"], "kind": "gateway",
                "name": "gateway (ContextForge)", "start_ms": round(offset, 1),
                "end_ms": round(offset + request_ms, 1), "duration_ms": round(request_ms, 1),
                "status": "ok" if str(best[1].get("status")) == "ok" else "error",
                "attrs": {"gateway_trace": best[1]["trace_id"],
                          "http_status": best[1].get("http_status_code"),
                          "overhead_ms": round(max(request_ms - invoke_ms, 0.0), 1)},
                "events": []})
            inv_off = (_utc(invoke["start_time"]) - started).total_seconds() * 1000
            trace["spans"].append({
                "id": f"{span['id']}t", "parent": gw_id, "kind": "gateway.tool",
                "name": f"gateway → {span['attrs'].get('tool')}", "start_ms": round(inv_off, 1),
                "end_ms": round(inv_off + invoke_ms, 1), "duration_ms": round(invoke_ms, 1),
                "status": str(invoke.get("status") or "ok"), "attrs": {}, "events": []})
            span["attrs"]["gateway_overhead_ms"] = round(max(request_ms - invoke_ms, 0.0), 1)
            attached += 1
    except (httpx.HTTPError, ValueError, KeyError):
        return attached
    finally:
        if own:
            await client.aclose()
    return attached


# --- storage -------------------------------------------------------------------------------------

class TraceStore:
    """Traces as JSON lines, one file a day, and the most recent in memory. An update is another
    line with the same id; the last one wins."""

    def __init__(self, directory: str | Path, keep: int = 500) -> None:
        self.dir = Path(directory).expanduser()
        self.keep = keep
        self.recent: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.frontend: deque[dict[str, Any]] = deque(maxlen=5_000)
        self._read: dict[Path, int] = {}        # bytes of each file already read
        self.refresh()

    def refresh(self) -> None:
        """Read what was appended since the last read, by this process or another (the bench
        writes its runs here too), so the metrics see them without a restart."""
        if not self.dir.is_dir():
            return
        for pattern, keep, into in (("traces-*.jsonl", 7, self._remember),
                                    ("browser-*.jsonl", 2, self.frontend.append)):
            for path in sorted(self.dir.glob(pattern))[-keep:]:
                size = path.stat().st_size
                start = self._read.get(path, 0)
                if size <= start:
                    continue
                with path.open("rb") as fh:
                    fh.seek(start)
                    chunk = fh.read(size - start)
                end = chunk.rfind(b"\n") + 1           # a line still being written waits
                self._read[path] = start + end
                for line in chunk[:end].decode("utf-8", errors="replace").splitlines():
                    try:
                        into(json.loads(line))
                    except ValueError:
                        continue

    def _remember(self, trace: dict[str, Any]) -> None:
        self.recent.pop(trace["id"], None)
        self.recent[trace["id"]] = trace
        while len(self.recent) > self.keep:
            self.recent.popitem(last=False)

    def _append(self, prefix: str, record: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.refresh()                                  # take in other writers' lines first
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        path = self.dir / f"{prefix}-{day}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        self._read[path] = path.stat().st_size          # our own line is already in memory

    def save(self, trace: dict[str, Any]) -> None:
        self._remember(trace)
        self._append("traces", trace)

    def get(self, trace_id: str) -> dict[str, Any] | None:
        self.refresh()
        return self.recent.get(trace_id)

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        self.refresh()
        out = []
        for trace in reversed(self.recent.values()):
            out.append({k: trace.get(k) for k in (
                "id", "started_at", "model", "target", "disclosure", "message", "status",
                "wall_ms", "steps", "input_tokens", "output_tokens", "cost_usd", "tools",
                "source", "case")}
                | {"answer_confidence": (trace.get("evals") or {}).get("answer_confidence")})
            if len(out) >= limit:
                break
        return out

    def add_browser(self, record: dict[str, Any]) -> None:
        """A browser measurement: a page load, an API call batch, or one chat turn's stream (that
        one also joins its trace as the ``browser`` span)."""
        record = {**record, "received_at": datetime.now(UTC).isoformat()}
        self.frontend.append(record)
        self._append("browser", record)
        trace = self.get(str(record.get("trace_id"))) if record.get("kind") == "chat" else None
        if trace is not None:
            trace["browser"] = record
            total = record.get("total_ms")
            if isinstance(total, (int, float)):
                trace["spans"].append({
                    "id": "browser", "parent": None, "kind": "browser",
                    "name": "browser (request to last event rendered)", "start_ms": 0.0,
                    "end_ms": round(float(total), 1), "duration_ms": round(float(total), 1),
                    "status": "ok", "attrs": {k: v for k, v in record.items()
                                              if k not in ("kind", "trace_id", "received_at")},
                    "events": []})
            self.save(trace)


class HttpStats:
    """The hub's own request timings per route, for the metrics page and Server-Timing."""

    def __init__(self, keep: int = 2_000) -> None:
        self.samples: dict[str, deque[tuple[float, int]]] = defaultdict(lambda: deque(maxlen=keep))

    def record(self, route: str, ms: float, status: int) -> None:
        self.samples[route].append((ms, status))

    def summary(self) -> list[dict[str, Any]]:
        out = []
        for route, samples in sorted(self.samples.items()):
            ms = [m for m, _ in samples]
            out.append({"route": route, "count": len(ms), "p50_ms": _pct(ms, 50),
                        "p95_ms": _pct(ms, 95), "errors": sum(1 for _, s in samples if s >= 500)})
        return out


# --- roll-up -------------------------------------------------------------------------------------

def _pct(values: list[float], p: float) -> float | None:
    values = sorted(v for v in values if isinstance(v, (int, float)))
    if not values:
        return None
    k = (len(values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return round(values[lo] + (values[hi] - values[lo]) * (k - lo), 1)


def _median(values: list[float]) -> float | None:
    values = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.median(values), 2) if values else None


def _shift_sources(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The elements whose moves made up the layout shift, summed over page loads, worst first."""
    total: dict[str, float] = defaultdict(float)
    for page in pages:
        for source in page.get("cls_sources") or []:
            total[str(source.get("node"))] += float(source.get("value") or 0)
    return [{"node": k, "shift": round(v, 3)} for k, v in sorted(total.items(),
                                                                  key=lambda kv: -kv[1])[:5]]


def compute_metrics(traces: list[dict[str, Any]], http: HttpStats | None = None,
                    browser: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Recent traces rolled up per layer."""
    models: dict[str, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
    tools: dict[str, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
    pantry_steps: dict[str, list[float]] = defaultdict(list)
    observers: dict[str, int] = defaultdict(int)
    checks: dict[str, list[bool]] = defaultdict(list)
    confidence: list[float] = []
    for t in traces:
        m = models[str(t.get("model"))]
        m["turn_ms"].append(t.get("wall_ms"))
        m["steps"].append(t.get("steps"))
        m["cost"].append(t.get("cost_usd") or 0)
        m["in"].append(t.get("input_tokens"))
        m["out"].append(t.get("output_tokens"))
        m["answered"].append(t.get("status") == "answered")
        ev = t.get("evals") or {}
        if ev.get("answer_confidence") is not None:
            confidence.append(ev["answer_confidence"])
            m["confidence"].append(ev["answer_confidence"])
        for c in ev.get("checks") or []:
            checks[c["name"]].append(bool(c["passed"]))
        for s in t.get("spans") or []:
            a = s.get("attrs") or {}
            if s["kind"] == "model":
                m["step_ms"].append(s.get("duration_ms"))
                m["ttft_ms"].append(a.get("first_token_ms"))
                m["read_tok_s"].append(a.get("read_tok_s"))
                m["write_tok_s"].append(a.get("write_tok_s"))
                m["cached_share"].append(a.get("cached_share"))
                m["prompt_tokens"].append(a.get("prompt_tokens"))
            elif s["kind"] == "tool":
                tl = tools[canonical(a.get("tool", ""))]
                tl["ms"].append(s.get("duration_ms"))
                tl["error"].append(s.get("status") == "error")
                tl["result_chars"].append(a.get("result_chars"))
                tl["model_chars"].append(a.get("model_chars"))
                tl["overhead"].append(a.get("gateway_overhead_ms"))
            elif s["kind"] == "pantry.step":
                pantry_steps[s["name"].removeprefix("pantry · ")].append(s.get("duration_ms"))
            for e in s.get("events") or []:
                if e.get("name") == "observation" and e["attrs"].get("value"):
                    observers[f"{e['attrs'].get('observer')}.{e['attrs'].get('condition')}"] += 1
    out_models = []
    for name, m in models.items():
        out_models.append({
            "model": name, "turns": len(m["turn_ms"]),
            "answered_share": round(sum(m["answered"]) / len(m["answered"]), 2),
            "turn_p50_ms": _pct(m["turn_ms"], 50), "turn_p95_ms": _pct(m["turn_ms"], 95),
            "steps_per_turn": _median(m["steps"]),
            "step_p50_ms": _pct(m["step_ms"], 50), "step_p95_ms": _pct(m["step_ms"], 95),
            "ttft_p50_ms": _pct(m["ttft_ms"], 50),
            "read_tok_s": _median(m["read_tok_s"]), "write_tok_s": _median(m["write_tok_s"]),
            "cached_share": _median(m["cached_share"]),
            "prompt_tokens_p50": _pct(m["prompt_tokens"], 50),
            "tokens_in_per_turn": _median(m["in"]), "tokens_out_per_turn": _median(m["out"]),
            "cost_per_turn_usd": _median(m["cost"]),
            "answer_confidence": _median(m["confidence"]),
        })
    out_tools = [{
        "tool": name, "calls": len(t["ms"]), "error_share": round(sum(t["error"]) / len(t["error"]), 2),
        "p50_ms": _pct(t["ms"], 50), "p95_ms": _pct(t["ms"], 95),
        "gateway_overhead_p50_ms": _pct(t["overhead"], 50),
        "result_chars_p50": _pct(t["result_chars"], 50),
        "model_chars_p50": _pct(t["model_chars"], 50)} for name, t in sorted(tools.items())]
    browser = browser or []
    chats = [b for b in browser if b.get("kind") == "chat"]
    pages = [b for b in browser if b.get("kind") == "page"]
    apis = [e for b in browser if b.get("kind") == "api" for e in b.get("entries") or []]
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in apis:
        by_path[str(e.get("path"))].append(e)
    return {
        "traces": len(traces),
        "answer_confidence_mean": round(sum(confidence) / len(confidence), 2) if confidence else None,
        "models": sorted(out_models, key=lambda r: -r["turns"]),
        "tools": out_tools,
        "observers": [{"condition": k, "fired": v} for k, v in sorted(observers.items(),
                                                                       key=lambda kv: -kv[1])],
        "evals": [{"check": k, "runs": len(v), "pass_share": round(sum(v) / len(v), 2)}
                  for k, v in sorted(checks.items())],
        "pantry_steps": [{"step": k, "runs": len(v), "p50_ms": _pct(v, 50), "p95_ms": _pct(v, 95)}
                         for k, v in sorted(pantry_steps.items())],
        "browser": {
            "chat_turns": len(chats),
            "ttfb_p50_ms": _pct([c.get("ttfb_ms") for c in chats], 50),
            "first_event_p50_ms": _pct([c.get("first_event_ms") for c in chats], 50),
            "stream_lag_p95_ms": _pct([c.get("lag_p95_ms") for c in chats], 95),
            "render_p95_ms": _pct([c.get("render_p95_ms") for c in chats], 95),
            "page_loads": len(pages),
            "page_ttfb_p50_ms": _pct([p.get("ttfb_ms") for p in pages], 50),
            "lcp_p50_ms": _pct([p.get("lcp_ms") for p in pages], 50),
            "inp_p95_ms": _pct([p.get("inp_ms") for p in pages], 95),
            "cls_max": max((p.get("cls") or 0 for p in pages), default=None),
            "long_tasks": sum(int(p.get("long_tasks") or 0) for p in pages),
            "cls_sources": _shift_sources(pages),
            "api": [{"path": k, "calls": len(v), "p50_ms": _pct([e.get("ms") for e in v], 50),
                     "p95_ms": _pct([e.get("ms") for e in v], 95),
                     "server_p50_ms": _pct([e.get("server_ms") for e in v], 50),
                     "errors": sum(1 for e in v if int(e.get("status") or 0) >= 500)}
                    for k, v in sorted(by_path.items())],
        },
        "hub_http": http.summary() if http else [],
    }
