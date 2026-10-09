"""Compare local models on the pantry tasks: performance, consistency and correctness.

Every case is a shopper's message answered by the Assistant's own agent loop: the same system
prompt, the same MCP tools on the direct pantry server, the same step budget. Each case runs
``--repeat`` times per model, interleaved (repetition, then case, then model) so a stopped bench
still compares every model on what it finished. Grading is deterministic and grounded in the tool
results the run itself received; there is no LLM judge.

    python -m demo_hub.bench --model ollama:granite4.2:8b --model ollama:command-r7b --repeat 3

Runs stream to ``<out>/runs.jsonl`` (a rerun with the same ``--out`` skips finished runs) and
``<out>/report.md`` and ``<out>/summary.json`` are rewritten after every run. Put pantry in demo
mode first (System tab) so its own planner is deterministic and the only model that varies is the
one under test.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import re
import statistics
import subprocess
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from demo_hub.agent import AGENT_TARGETS, Agent
from demo_hub.llm import ChatClient, parse_model, split_variant
from demo_hub.mcp_targets import Targets
from demo_hub.settings import Settings
from demo_hub.telemetry import TraceRecorder, TraceStore, add_gateway_spans

# The read-only tools a shopper's questions need, plus near misses to choose between. The full
# profile offers all 15, exactly as the Assistant does.
CORE_TOOLS = frozenset(
    {
        "list_recipes",
        "get_recipe",
        "find_product",
        "get_product",
        "plan_recipe",
        "plan_from_text",
        "get_product_origins",
    }
)
MONEY_RE = re.compile(r"\$\s?(\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?)")
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
DECLINE_WORDS = ("no ", "not ", "n't", "unable", "cannot", "couldn", "isn't", "none")


# --- one run, as the grader sees it ---------------------------------------------------------------


def canonical_tool(name: str) -> str:
    """A tool's name as the cases write it, whichever server answered: ContextForge's
    ``pantry-find-product`` is pantry's ``find_product``."""
    return str(name).removeprefix("pantry-").replace("-", "_")


@dataclass
class ToolUse:
    name: str
    arguments: dict[str, Any]
    is_error: bool
    result: Any  # the structured result, else the text
    ms: float


@dataclass
class Run:
    model: str
    case: str
    rep: int
    answer: str = ""
    tools_offered: list[str] = field(default_factory=list)
    tool_uses: list[ToolUse] = field(default_factory=list)
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    stop: str = ""
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0
    trace_id: str = ""                       # the run's Assistant trace, when one was kept
    answer_confidence: float | None = None   # its online evals' share of checks passed

    @staticmethod
    def from_events(model: str, case: str, rep: int, events: Iterable[dict[str, Any]]) -> Run:
        run = Run(model, case, rep)
        pending: dict[str, dict[str, Any]] = {}
        for e in events:
            kind = e.get("type")
            if kind == "start":
                run.tools_offered = [canonical_tool(t) for t in e.get("tools") or []]
            elif kind in ("observation", "tools_offered"):
                # progressive disclosure: an observer (or discover_tools) changed the toolset
                added = [canonical_tool(t) for t in e.get("added") or []]
                removed = {canonical_tool(t) for t in e.get("removed") or []}
                run.tools_offered = [t for t in run.tools_offered if t not in removed] + [
                    t for t in added if t not in run.tools_offered]
            elif kind == "llm_call":
                run.llm_calls.append({k: v for k, v in e.items() if k != "type"})
            elif kind == "assistant":
                run.answer = str(e.get("text") or "")  # the last text is the answer
            elif kind == "tool_call":
                pending[str(e.get("id"))] = e
            elif kind == "tool_result":
                call = pending.pop(str(e.get("id")), {})
                result = e.get("structured") if e.get("structured") is not None else e.get("text")
                run.tool_uses.append(
                    ToolUse(
                        canonical_tool(str(e.get("name") or call.get("name"))),
                        dict(call.get("arguments") or {}),
                        bool(e.get("is_error")),
                        result,
                        float(e.get("ms") or 0),
                    )
                )
            elif kind == "error":
                run.errors.append(str(e.get("message")))
            elif kind == "done":
                run.stop, run.seconds = str(e.get("stop")), float(e.get("seconds") or 0)
        return run

    def used(self, *names: str) -> list[ToolUse]:
        return [u for u in self.tool_uses if u.name in names]

    def results_text(self) -> str:
        return " ".join(
            json.dumps(u.result) if not isinstance(u.result, str) else u.result
            for u in self.tool_uses
        )


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


# --- grading ---------------------------------------------------------------------------------------


def _numbers(text: str) -> set[float]:
    return {round(float(n), 2) for n in NUMBER_RE.findall(text.replace(",", ""))}


def money_in(text: str) -> list[float]:
    return [round(float(m.replace(",", "")), 2) for m in MONEY_RE.findall(text)]


def check_finished(run: Run) -> Check:
    return Check(
        "finished", run.stop == "answered" and not run.errors, "; ".join(run.errors) or run.stop
    )


def check_known_tools(run: Run) -> Check:
    unknown = sorted({u.name for u in run.tool_uses if u.name not in run.tools_offered})
    return Check(
        "known_tools", not unknown, f"called tools it was not offered: {unknown}" if unknown else ""
    )


def check_valid_calls(run: Run) -> Check:
    bad = [f"{u.name}: {str(u.result)[:120]}" for u in run.tool_uses if u.is_error]
    return Check("valid_calls", not bad, "; ".join(bad))


def check_grounded_money(run: Run) -> Check:
    """Every dollar amount in the answer appears in a tool result this run received."""
    seen = _numbers(run.results_text())
    invented = sorted({m for m in money_in(run.answer) if m not in seen})
    return Check(
        "grounded_money",
        not invented,
        f"amounts not in any tool result: {invented}" if invented else "",
    )


def check_used_tools(run: Run) -> Check:
    return Check("used_tools", bool(run.tool_uses), "" if run.tool_uses else "answered from memory")


COMMON: tuple[Callable[[Run], Check], ...] = (
    check_finished,
    check_known_tools,
    check_valid_calls,
    check_grounded_money,
)


def _mentions(answer: str, needle: str) -> bool:
    return needle.lower() in answer.lower()


def _price_text(price: float) -> str:
    return f"{price:.2f}"


def cheapest_penne(run: Run) -> list[Check]:
    finds = [
        u for u in run.used("find_product") if "penne" in str(u.arguments.get("query", "")).lower()
    ]
    checks = [check_used_tools(run), Check("find_product(penne)", bool(finds))]
    items = [i for u in finds if isinstance(u.result, dict) for i in u.result.get("items", [])]
    if items:
        best = min(items, key=lambda i: i["price"])
        for label, needle in (
            ("names the product", best["name"]),
            ("names the store", best["store"]),
            ("states the price", _price_text(best["price"])),
        ):
            checks.append(Check(label, _mentions(run.answer, needle), needle))
    else:
        checks.append(Check("names the product", False, "no find_product result to compare with"))
    return checks


def _plans(run: Run, tool: str) -> list[dict[str, Any]]:
    return [
        u.result["summary"]
        for u in run.used(tool)
        if not u.is_error
        and isinstance(u.result, dict)
        and isinstance(u.result.get("summary"), dict)
    ]


def stir_fry(run: Run) -> list[Check]:
    plans = _plans(run, "plan_from_text") + _plans(run, "plan_recipe")
    checks = [
        check_used_tools(run),
        Check("planned a basket", bool(plans), "plan_from_text or plan_recipe returned a plan"),
    ]
    if plans:
        plan = plans[-1]
        lines = plan.get("lines") or []
        named = [ln["product"] for ln in lines if _mentions(run.answer, ln["product"])]
        checks.append(
            Check(
                "lists the products",
                len(named) >= min(3, len(lines)),
                f"{len(named)} of {len(lines)} planned products named",
            )
        )
        total = plan.get("total_cost")
        checks.append(
            Check(
                "states the total",
                total is not None and _price_text(total) in run.answer,
                f"total_cost {total}",
            )
        )
        stores = {ln.get("store") for ln in lines if ln.get("store")}
        checks.append(
            Check(
                "names a store",
                any(_mentions(run.answer, s) for s in stores),
                ", ".join(sorted(stores)) or "the plan chose no stores (plan_recipe)",
            )
        )
    return checks


def tomato_penne_no_us(run: Run) -> list[Check]:
    def excludes_us(u: ToolUse) -> bool:
        return any(
            "united states" in str(c).lower() for c in u.arguments.get("exclude_origin") or []
        )

    calls = run.used("plan_recipe", "plan_from_text")
    checks = [
        check_used_tools(run),
        Check(
            "plans tomato penne",
            any(
                str(u.arguments.get("slug", "")) == "tomato_penne"
                or "penne" in str(u.arguments.get("recipe_text", "")).lower()
                for u in calls
            ),
        ),
        Check("excludes the United States", any(excludes_us(u) for u in calls)),
    ]
    plans = [
        p for p in _plans(run, "plan_recipe") + _plans(run, "plan_from_text") if p.get("coverage")
    ]
    if plans:
        fraction = plans[-1]["coverage"]["spend_fraction"]
        said = _numbers(run.answer.replace("%", " "))
        ok = any(abs(n - fraction * 100) <= 1 or abs(n - fraction) <= 0.01 for n in said)
        checks.append(Check("reports the verified share", ok, f"spend_fraction {fraction}"))
    else:
        checks.append(Check("reports the verified share", False, "no plan with a coverage figure"))
    return checks


def recipe_list(run: Run) -> list[Check]:
    lists = [u.result for u in run.used("list_recipes") if isinstance(u.result, dict)]
    names = [r["name"] for res in lists for r in res.get("result", [])]
    named = [n for n in names if _mentions(run.answer, n)]
    return [
        check_used_tools(run),
        Check("list_recipes", bool(lists)),
        Check(
            "names the recipes",
            len(named) >= min(3, len(names)),
            f"{len(named)} of {len(names)} named",
        ),
    ]


def unknown_product(run: Run) -> list[Check]:
    finds = run.used("find_product", "list_products")
    empty = [
        u
        for u in finds
        if isinstance(u.result, dict) and not u.result.get("total") and not u.result.get("items")
    ]
    return [
        check_used_tools(run),
        Check("looked it up", bool(finds)),
        Check(
            "says it is not stocked",
            bool(empty)
            and not money_in(run.answer)
            and any(w in run.answer.lower() for w in DECLINE_WORDS),
            "the lookup found nothing; the answer must say so and quote no price",
        ),
    ]


def out_of_scope(run: Run) -> list[Check]:
    invented = re.search(r"-?\d+\s?(°|degrees|celsius|fahrenheit)", run.answer, re.IGNORECASE)
    return [
        Check("no tool calls", not run.tool_uses, ", ".join(u.name for u in run.tool_uses)),
        Check("no invented forecast", not invented, invented.group(0) if invented else ""),
    ]


@dataclass(frozen=True)
class Case:
    id: str
    message: str
    grade: Callable[[Run], list[Check]]
    kind: str = "positive"


CASES: tuple[Case, ...] = (
    Case("cheapest-penne", "What is the cheapest penne, and where can I buy it?", cheapest_penne),
    Case(
        "stir-fry-3-meals",
        "I would like to make some simple stir fry veggies for 3 meals",
        stir_fry,
    ),
    Case(
        "tomato-penne-no-us",
        "Plan tomato penne with nothing from the United States, and tell me "
        "how much of the basket's origin is verified.",
        tomato_penne_no_us,
    ),
    Case("recipe-list", "Which recipes can you plan for me?", recipe_list),
    Case("unknown-product", "How much does dragon fruit jam cost?", unknown_product, "negative"),
    Case(
        "out-of-scope",
        "What's the weather going to be in Vancouver tomorrow?",
        out_of_scope,
        "negative",
    ),
)


def grade(run: Run, case: Case) -> list[Check]:
    return [fn(run) for fn in COMMON] + case.grade(run)


# --- running ---------------------------------------------------------------------------------------


def parse_variant(spec: str) -> tuple[str, dict[str, Any]]:
    """``ollama:granite4.2:8b#think=false`` is the model with thinking off; ``#temperature=0`` fixes
    the sampling. The whole spec labels the variant in the report."""
    return split_variant(spec)


def variant_settings(settings: Settings, options: dict[str, Any]) -> Settings:
    return replace(
        settings,
        ollama_think=options.get("think", settings.ollama_think),
        ollama_temperature=options.get("temperature", settings.ollama_temperature),
    )


async def run_case(
    agent: Agent,
    spec: str,
    case: Case,
    rep: int,
    tools: frozenset[str] | None,
    disclosure: str = "all",
    target: str = "pantry",
    store: TraceStore | None = None,
    stores: Iterable[str] = (),
) -> Run:
    """One case, once. With a ``store`` the run is also kept as an Assistant trace (the same
    spans, online evals and, through ContextForge, gateway spans as a turn in the browser), tagged
    with its case, so the Metrics page and the report read bench runs like any other turn."""
    from demo_hub.evals import evaluate  # evals grades with this module's checks

    conv = agent.conversation(None, parse_variant(spec)[0], target, disclosure)
    # through ContextForge the same tools are named pantry-<tool-with-dashes>
    conv.tools = tools if tools is None or target == "pantry" else frozenset(
        "pantry-" + t.replace("_", "-") for t in tools)
    # the trace names the full spec (e.g. "#think=false"): the agent's model has the variant
    # applied through its settings, not in its name
    recorder = TraceRecorder(conversation_id=conv.id, model=spec, target=target,
                             message=case.message, disclosure=disclosure)
    events = []
    async for e in agent.run(conv, case.message):
        events.append(e)
        recorder.on(e)
    agent.conversations.pop(conv.id, None)
    run = Run.from_events(spec, case.id, rep, events)
    if store is not None:
        recorder.evals = evaluate(events, stores, spec)
        trace = recorder.to_dict() | {"source": "bench", "case": case.id, "rep": rep}
        store.save(trace)
        settings = agent.settings
        if target != "pantry" and await add_gateway_spans(trace, settings.contextforge_url,
                                                          settings.contextforge_jwt):
            store.save(trace)
        run.trace_id = trace["id"]
        run.answer_confidence = recorder.evals["answer_confidence"]
    return run


def record(run: Run, checks: list[Check]) -> dict[str, Any]:
    return {
        "model": run.model,
        "case": run.case,
        "rep": run.rep,
        "passed": all(c.passed for c in checks),
        "checks": [asdict(c) for c in checks],
        "answer": run.answer,
        "stop": run.stop,
        "errors": run.errors,
        "seconds": run.seconds,
        "trace_id": run.trace_id,
        "answer_confidence": run.answer_confidence,
        "tools_offered": len(run.tools_offered),
        "tool_uses": [
            {"name": u.name, "arguments": u.arguments, "is_error": u.is_error, "ms": u.ms}
            for u in run.tool_uses
        ],
        "llm_calls": run.llm_calls,
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


async def ollama_model_info(settings: Settings, model: str) -> dict[str, Any]:
    """What Ollama says about the model, and how it is loaded right now (size, VRAM, context)."""
    _, name = parse_model(model)
    info: dict[str, Any] = {"model": model}
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            show = (
                await client.post(f"{settings.ollama_url}/api/show", json={"model": name})
            ).json()
            details = show.get("details") or {}
            info |= {
                "family": details.get("family"),
                "parameters": details.get("parameter_size"),
                "quantization": details.get("quantization_level"),
                "capabilities": show.get("capabilities"),
                "trained_context": next(
                    (
                        v
                        for k, v in (show.get("model_info") or {}).items()
                        if k.endswith(".context_length")
                    ),
                    None,
                ),
            }
            loaded = (await client.get(f"{settings.ollama_url}/api/ps")).json().get("models", [])
            for m in loaded:
                if m.get("name") in (name, f"{name}:latest"):
                    info |= {
                        "digest": str(m.get("digest", ""))[:12],
                        "loaded_gb": round(m["size"] / 1e9, 2),
                        "vram_gb": round(m.get("size_vram", 0) / 1e9, 2),
                        "context_length": m.get("context_length"),
                    }
        except (httpx.HTTPError, ValueError):
            pass
    return info


def machine() -> dict[str, Any]:
    def sysctl(key: str) -> str:
        try:
            return subprocess.run(
                ["sysctl", "-n", key], capture_output=True, text=True, check=False
            ).stdout.strip()
        except OSError:
            return ""

    mem = sysctl("hw.memsize")
    return {
        "cpu": sysctl("machdep.cpu.brand_string") or platform.processor(),
        "arch": platform.machine(),
        "ram_gb": round(int(mem) / 2**30) if mem.isdigit() else None,
        "os": platform.platform(),
    }


# --- the report ------------------------------------------------------------------------------------


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def _p90(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, round(0.9 * (len(ordered) - 1)))], 2)


def _rate(tokens: list[float], seconds: list[float]) -> float | None:
    total = sum(seconds)
    return round(sum(tokens) / total, 1) if total else None


def _modal_share(values: list[str]) -> tuple[str, float]:
    if not values:
        return "", 0.0
    value, count = Counter(values).most_common(1)[0]
    return value, round(count / len(values), 2)


def summarise(records: list[dict[str, Any]], models: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"models": {}, "cases": {}}
    for model in models:
        rows = [r for r in records if r["model"] == model]
        calls = [c for r in rows for c in r["llm_calls"]]
        first = [r["llm_calls"][0] for r in rows if r["llm_calls"]]
        later = [c for r in rows for c in r["llm_calls"][1:]]
        checks: dict[str, list[bool]] = {}
        for r in rows:
            for c in r["checks"]:
                checks.setdefault(c["name"], []).append(c["passed"])
        out["models"][model] = {
            "runs": len(rows),
            "passed": sum(r["passed"] for r in rows),
            "pass_rate": round(sum(r["passed"] for r in rows) / len(rows), 2) if rows else None,
            "runs_with_tool_calls": sum(bool(r["tool_uses"]) for r in rows),
            "tool_calls": sum(len(r["tool_uses"]) for r in rows),
            "tool_errors": sum(u["is_error"] for r in rows for u in r["tool_uses"]),
            "invented_amounts": sum(
                not c["passed"] for r in rows for c in r["checks"] if c["name"] == "grounded_money"
            ),
            "checks": {k: round(sum(v) / len(v), 2) for k, v in checks.items()},
            "model_calls": len(calls),
            "call_s_median": _median([c.get("wall_s", 0) for c in calls]),
            "call_s_p90": _p90([c.get("wall_s", 0) for c in calls]),
            "run_s_median": _median([r["seconds"] for r in rows]),
            "first_call_prompt_tokens": _median([c.get("prompt_tokens", 0) for c in first]),
            "later_call_prompt_tokens": _median([c.get("prompt_tokens", 0) for c in later]),
            # Ollama counts a cached prefix in prompt_tokens without re-reading it, so this is
            # the effective intake, not the raw reading speed.
            "prompt_tok_s": _rate(
                [c.get("prompt_tokens", 0) for c in calls], [c.get("prompt_s", 0) for c in calls]
            ),
            "first_call_prompt_s": _median([c.get("prompt_s", 0) for c in first]),
            "later_call_prompt_s": _median([c.get("prompt_s", 0) for c in later]),
            "gen_s_median": _median([c.get("gen_s", 0) for c in calls]),
            "gen_tok_s": _rate(
                [c.get("output_tokens", 0) for c in calls], [c.get("gen_s", 0) for c in calls]
            ),
            "output_tokens_median": _median([c.get("output_tokens", 0) for c in calls]),
            "thinking_chars": sum(c.get("thinking_chars", 0) for c in calls),
            "load_s_max": max((c.get("load_s", 0) for c in calls), default=None),
        }
    for case in {r["case"] for r in records}:
        for model in models:
            rows = [r for r in records if r["case"] == case and r["model"] == model]
            if not rows:
                continue
            sequences = [
                " > ".join(u["name"] for u in r["tool_uses"]) or "(no tools)" for r in rows
            ]
            firsts = [
                json.dumps(r["tool_uses"][0] | {"ms": None, "is_error": None}, sort_keys=True)
                if r["tool_uses"]
                else "(no tools)"
                for r in rows
            ]
            amounts = [json.dumps(sorted(set(money_in(r["answer"])))) for r in rows]
            seq, seq_share = _modal_share(sequences)
            _, first_share = _modal_share(firsts)
            _, amount_share = _modal_share(amounts)
            out["cases"].setdefault(case, {})[model] = {
                "runs": len(rows),
                "passed": sum(r["passed"] for r in rows),
                "sequences": dict(Counter(sequences)),
                "modal_sequence": seq,
                "sequence_agreement": seq_share,
                "first_call_agreement": first_share,
                "answer_amounts_agreement": amount_share,
                "run_s_median": _median([r["seconds"] for r in rows]),
                "failed_checks": dict(
                    Counter(c["name"] for r in rows for c in r["checks"] if not c["passed"])
                ),
            }
    return out


def _fmt(value: Any, suffix: str = "") -> str:
    return "—" if value is None else f"{value}{suffix}"


def report(summary: dict[str, Any], meta: dict[str, Any], records: list[dict[str, Any]]) -> str:
    models = list(summary["models"])
    head = "| | " + " | ".join(f"`{m}`" for m in models) + " |\n|---|" + "---|" * len(models) + "\n"

    def row(label: str, key: str, suffix: str = "") -> str:
        return (
            f"| {label} | "
            + " | ".join(_fmt(summary["models"][m].get(key), suffix) for m in models)
            + " |\n"
        )

    lines = [
        f"# Local model bench: {' vs '.join(models)}\n",
        (
            f"{meta['started']} → {meta['updated']} · {meta['machine']['cpu']}, "
            f"{meta['machine']['ram_gb']} GB RAM · profile **{meta['profile']}** ({meta['tools']} tools)"
            f" · repeat {meta['repeat']} · temperature "
            f"{meta['temperature'] if meta['temperature'] is not None else 'model default'} · num_ctx "
            f"{meta['num_ctx']} (capped at each model's own) · pantry demo mode: {meta['demo_mode']}\n"
        ),
        "\n## Correctness\n\n",
        head,
        row("runs passed (all checks)", "passed"),
        row("pass rate", "pass_rate"),
        row("runs that called any tool", "runs_with_tool_calls"),
        row("tool calls", "tool_calls"),
        row("tool calls the server rejected", "tool_errors"),
        row("runs quoting a $ amount no tool returned", "invented_amounts"),
    ]
    names = sorted({k for m in models for k in summary["models"][m]["checks"]})
    lines += [
        f"| check: {n} | "
        + " | ".join(_fmt(summary["models"][m]["checks"].get(n)) for m in models)
        + " |\n"
        for n in names
    ]
    lines += [
        "\n## Performance\n\n",
        head,
        row("model calls", "model_calls"),
        row("median seconds per model call", "call_s_median", " s"),
        row("p90 seconds per model call", "call_s_p90", " s"),
        row("median seconds per case", "run_s_median", " s"),
        row("prompt size on a run's first call (median tokens)", "first_call_prompt_tokens"),
        row("prompt size on later calls (median tokens)", "later_call_prompt_tokens"),
        row("seconds reading the prompt, first call (median)", "first_call_prompt_s", " s"),
        row("seconds reading the prompt, later calls (median)", "later_call_prompt_s", " s"),
        row("effective prompt intake (a cached prefix is not re-read)", "prompt_tok_s", " tok/s"),
        row("seconds generating per call (median)", "gen_s_median", " s"),
        row("generation speed", "gen_tok_s", " tok/s"),
        row("output tokens per call (median)", "output_tokens_median"),
        row("thinking characters (all calls)", "thinking_chars"),
        row("longest model load", "load_s_max", " s"),
    ]
    for m in models:
        info = meta["model_info"].get(m, {})
        lines.append(
            f"\n`{m}`: {info.get('family')} {info.get('parameters')} {info.get('quantization')}, "
            f"trained context {info.get('trained_context')}, loaded {info.get('loaded_gb')} GB "
            f"({info.get('vram_gb')} GB on GPU), capabilities {info.get('capabilities')}, "
            f"digest {info.get('digest')}\n"
        )
    lines += [
        "\n## Consistency and per-case results\n\n",
        (
            "| case | model | passed | tool sequence (most common) | sequence agreement "
            "| first-call agreement | $-amount agreement | median s | failed checks |\n"
            "|---|---|---|---|---|---|---|---|---|\n"
        ),
    ]
    for case in [c.id for c in CASES if c.id in summary["cases"]]:
        for m in models:
            c = summary["cases"][case].get(m)
            if c:
                failed = ", ".join(f"{k} ×{v}" for k, v in c["failed_checks"].items()) or "—"
                lines.append(
                    f"| {case} | `{m}` | {c['passed']}/{c['runs']} | {c['modal_sequence']} | "
                    f"{c['sequence_agreement']} | {c['first_call_agreement']} | "
                    f"{c['answer_amounts_agreement']} | {_fmt(c['run_s_median'])} | {failed} |\n"
                )
    lines.append("\n## Failed checks, run by run\n\n")
    for r in records:
        bad = [c for c in r["checks"] if not c["passed"]]
        if bad:
            lines.append(
                f"- **{r['case']}** · `{r['model']}` · rep {r['rep']}: "
                + "; ".join(
                    f"{c['name']} ({c['detail']})" if c["detail"] else c["name"] for c in bad
                )
                + "\n"
            )
    lines.append(
        "\nThe checks are deterministic: tool names and arguments, and facts in the answer compared "
        "with the tool results that run received. A $ amount counts as invented when no tool "
        "result in the run contains that number.\n"
    )
    return "".join(lines)


def reported(models: list[str], records: list[dict[str, Any]]) -> list[str]:
    """This invocation's models first, then any earlier phase's that have runs in the same --out."""
    return list(dict.fromkeys([*models, *(r["model"] for r in records)]))


def write_outputs(
    out: Path, records: list[dict[str, Any]], meta: dict[str, Any], models: list[str]
) -> None:
    meta["updated"] = datetime.now(UTC).isoformat(timespec="seconds")
    summary = summarise(records, models)
    (out / "summary.json").write_text(
        json.dumps({"meta": meta, **summary}, indent=2), encoding="utf-8"
    )
    (out / "report.md").write_text(report(summary, meta, records), encoding="utf-8")


async def main(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--model", action="append", required=True, help="ollama:<model>; repeat for each"
    )
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--case", action="append", help="only these case ids")
    parser.add_argument("--profile", choices=("core", "full"), default="core")
    parser.add_argument("--temperature", type=float, default=None, help="default: each model's own")
    parser.add_argument("--num-ctx", type=int, default=16384)
    parser.add_argument("--max-steps", type=int, default=6)
    parser.add_argument(
        "--disclosure",
        choices=("all", "progressive"),
        default="all",
        help="all: every profile tool from the first call (as the first runs); "
        "progressive: the Assistant's observers enable them",
    )
    parser.add_argument("--target", choices=AGENT_TARGETS, default="pantry",
                        help="pantry: the MCP server directly; gateway-recipes: through ContextForge")
    parser.add_argument("--no-traces", action="store_true",
                        help="do not keep each run as an Assistant trace")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args(argv)

    out = args.out or Path.home() / ".pantry-demo" / "bench" / datetime.now(
        UTC
    ).astimezone().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    settings = replace(
        Settings.from_env(),
        ollama_num_ctx=args.num_ctx,
        ollama_temperature=args.temperature,
        agent_max_steps=args.max_steps,
        ollama_timeout_s=1800.0,
    )
    cases = [c for c in CASES if not args.case or c.id in args.case]
    tools = CORE_TOOLS if args.profile == "core" else None
    runs_path = out / "runs.jsonl"
    meta_path = out / "meta.json"
    meta = (
        json.loads(meta_path.read_text())
        if meta_path.exists()
        else {
            "started": datetime.now(UTC).isoformat(timespec="seconds"),
            "machine": machine(),
            "profile": args.profile,
            "tools": len(tools) if tools else 15,
            "repeat": args.repeat,
            "temperature": args.temperature,
            "num_ctx": args.num_ctx,
            "models": args.model,
            "disclosure": args.disclosure,
            "target": args.target,
            "model_info": {},
            "demo_mode": None,
        }
    )
    records = load_records(runs_path)
    if not args.report_only:
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                runtime = (await client.get(f"{settings.pantry_api_url}/settings/runtime")).json()
                meta["demo_mode"] = runtime.get("demo_mode")
            except (httpx.HTTPError, ValueError):
                meta["demo_mode"] = None
        targets = Targets(settings)
        store = None if args.no_traces else TraceStore(settings.traces_dir
                                                       or out / "traces")
        stores: list[str] = []
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                stores = [s["name"] for s in
                          (await client.get(f"{settings.pantry_api_url}/stores")).json()]
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                stores = []
        agents = {}
        for spec in args.model:
            variant = variant_settings(settings, parse_variant(spec)[1])
            agents[spec] = Agent(variant, targets, ChatClient(variant))
        done = {(r["model"], r["case"], r["rep"]) for r in records}
        for rep in range(1, args.repeat + 1):
            for case in cases:
                for model in args.model:
                    if (model, case.id, rep) in done:
                        continue
                    started = time.perf_counter()
                    run = await run_case(agents[model], model, case, rep, tools, args.disclosure,
                                         args.target, store, stores)
                    checks = grade(run, case)
                    rec = record(run, checks)
                    with runs_path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(rec) + "\n")
                    records.append(rec)
                    if model not in meta["model_info"]:
                        meta["model_info"][model] = (await ollama_model_info(
                            settings, parse_variant(model)[0]
                        ) if model.startswith("ollama:") else {"model": model}) | {
                            "options": parse_variant(model)[1]}
                    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                    write_outputs(out, records, meta, reported(args.model, records))
                    print(
                        f"{datetime.now(UTC).astimezone():%H:%M:%S} rep {rep} {case.id:20s} {model:24s} "
                        f"{'PASS' if rec['passed'] else 'fail'} {time.perf_counter() - started:6.0f}s "
                        f"tools={[u['name'] for u in rec['tool_uses']]}",
                        flush=True,
                    )
    write_outputs(out, records, meta, reported(args.model, records))
    print(f"report: {out / 'report.md'}")
    return out


if __name__ == "__main__":
    asyncio.run(main())
