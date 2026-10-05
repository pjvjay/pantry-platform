"""The tables of the local-models report, computed from bench runs and their traces.

    python -m demo_hub.report RUN_DIR [RUN_DIR ...] [--traces DIR] [--out FILE]

Each RUN_DIR is a bench output (``runs.jsonl`` and ``meta.json``, e.g. from
``scripts/report-bench.sh``); every run that kept a trace is looked up in the trace store
(``--traces``, default ``~/.pantry-demo/traces``) for its per-step numbers: tokens read (cached
and new) and written, read and write rates, time to first token, time queued for the model
server, the gateway's overhead per tool call and pantry's own steps. Costs are what the tokens
would cost at Google's paid list prices (``pricing.py``); local models cost $0 per call, and
Gemini's free tier bills nothing.
Writes Markdown tables to stdout or ``--out``.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from demo_hub.pricing import PRICES, PRICES_SOURCE, call_cost_usd
from demo_hub.telemetry import TraceStore, step_rates


def _median(xs: list[float]) -> float | None:
    xs = [x for x in xs if isinstance(x, (int, float))]
    return statistics.median(xs) if xs else None


def _mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if isinstance(x, (int, float))]
    return statistics.fmean(xs) if xs else None


def _s(ms: float | None) -> str:
    if ms is None:
        return "–"
    s = ms / 1000
    return f"{s / 60:.1f} min" if s >= 120 else f"{s:.1f} s"


def _n(v: float | None, digits: int = 0) -> str:
    return "–" if v is None else f"{v:,.{digits}f}"


def _usd(v: float | None) -> str:
    if v is None:
        return "–"
    return "$0" if v == 0 else f"${v:.4f}" if v < 0.1 else f"${v:.2f}"


def load_runs(dirs: list[Path]) -> list[dict[str, Any]]:
    """Every run of every dir, with its configuration (model, toolset, target) attached."""
    runs = []
    for d in dirs:
        meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
        for line in (d / "runs.jsonl").read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                r["disclosure"] = meta.get("disclosure", "all")
                r["target"] = meta.get("target", "pantry")
                r["config"] = f"{r['model']} · {r['disclosure']}"
                runs.append(r)
    return runs


def run_numbers(run: dict[str, Any], store: TraceStore) -> dict[str, Any]:
    trace = store.get(run.get("trace_id") or "") or {}
    steps = [s for s in trace.get("spans", []) if s["kind"] == "model"]
    tools = [s for s in trace.get("spans", []) if s["kind"] == "tool"]
    a0 = steps[0]["attrs"] if steps else {}
    prompt = [s["attrs"].get("prompt_tokens") or 0 for s in steps]
    new = [s["attrs"].get("new_tokens_est") for s in steps]
    out = [s["attrs"].get("output_tokens") or 0 for s in steps]
    if not any(prompt) and trace.get("input_tokens"):
        # steps recorded without token counts (Gemini before the agent sent them): the turn's
        # totals, which the provider's usage reported
        prompt, out = [int(trace["input_tokens"])], [int(trace.get("output_tokens") or 0)]
    cost = sum(call_cost_usd(run["model"], p, o) or 0 for p, o in zip(prompt, out, strict=True)) \
        if call_cost_usd(run["model"], 1, 1) is not None else None
    return {
        "wall_ms": (run.get("seconds") or 0) * 1000,
        "steps": len(steps),
        "first_prompt": a0.get("prompt_tokens"),
        "prompt_total": sum(prompt),
        "new_total": sum(n for n in new if isinstance(n, (int, float))) if any(
            isinstance(n, (int, float)) for n in new) else None,
        "output_total": sum(out),
        "read_tok_s": _median([s["attrs"].get("read_tok_s") for s in steps]),
        "write_tok_s": _median([s["attrs"].get("write_tok_s") for s in steps]),
        "ttft_ms": _median([s["attrs"].get("first_token_ms") for s in steps]),
        # waiting for the model server behind another request (traces before queued_ms was
        # recorded have what it is computed from)
        "queued_ms": sum(step_rates(s["attrs"]).get("queued_ms") or 0 for s in steps)
        if any(s["attrs"].get("prompt_s") for s in steps) else None,
        "step_ms": _median([s.get("duration_ms") for s in steps]),
        "cost": cost,
        "confidence": (trace.get("evals") or {}).get("answer_confidence", run.get("answer_confidence")),
        "tool_ms": [t["attrs"].get("mcp_ms") or t.get("duration_ms") for t in tools],
        "overhead_ms": [t["attrs"].get("gateway_overhead_ms") for t in tools
                        if t["attrs"].get("gateway_overhead_ms") is not None],
        "model_chars": [t["attrs"].get("model_chars") for t in tools],
        "pantry": [(s["name"].removeprefix("pantry · "), s.get("duration_ms"))
                   for s in trace.get("spans", []) if s["kind"] == "pantry.step"],
        "tools": [t["attrs"].get("tool") for t in tools],
    }


def tables(runs: list[dict[str, Any]], store: TraceStore) -> str:
    by_config: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for r in runs:
        by_config[r["config"]].append((r, run_numbers(r, store)))
    lines = ["### Per configuration", "",
             ("| Configuration | Runs | Passed (bench) | Answer confidence (mean) | Median turn | "
              "Queued / turn | Steps | Step-1 prompt | Prompt tokens / turn | New (uncached) / turn | "
              "Output / turn | Read tok/s | Write tok/s | First token | Cost / turn (paid list) |"),
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for config, items in by_config.items():
        nums = [n for _, n in items]
        lines.append(
            f"| `{config}` | {len(items)} | {sum(r['passed'] for r, _ in items)}/{len(items)} | "
            f"{_n(_mean([n['confidence'] for n in nums]), 2)} | "
            f"{_s(_median([n['wall_ms'] for n in nums]))} | "
            f"{_s(_median([n['queued_ms'] for n in nums]))} | "
            f"{_n(_median([n['steps'] for n in nums]))} | "
            f"{_n(_median([n['first_prompt'] for n in nums]))} | "
            f"{_n(_median([n['prompt_total'] for n in nums]))} | "
            f"{_n(_median([n['new_total'] for n in nums]))} | "
            f"{_n(_median([n['output_total'] for n in nums]))} | "
            f"{_n(_median([n['read_tok_s'] for n in nums]), 1)} | "
            f"{_n(_median([n['write_tok_s'] for n in nums]), 1)} | "
            f"{_s(_median([n['ttft_ms'] for n in nums]))} | "
            f"{_usd(_median([n['cost'] for n in nums]))} |")
    lines += ["", "### Per case", "",
              ("| Case | Configuration | Passed | Confidence | Turn | Queued | Steps | "
               "Step-1 prompt | Tools called |"), "|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(runs, key=lambda r: (r["case"], r["config"])):
        n = run_numbers(r, store)
        lines.append(f"| {r['case']} | `{r['config']}` | {'✓' if r['passed'] else '✗'} | "
                     f"{_n(n['confidence'], 2)} | {_s(n['wall_ms'])} | {_s(n['queued_ms'])} | "
                     f"{n['steps']} | "
                     f"{_n(n['first_prompt'])} | {', '.join(n['tools']) or '–'} |")
    tool_ms: dict[str, list[float]] = defaultdict(list)
    overhead: dict[str, list[float]] = defaultdict(list)
    pantry: dict[str, list[float]] = defaultdict(list)
    for r in runs:
        n = run_numbers(r, store)
        for name, ms in zip(n["tools"], n["tool_ms"], strict=True):
            tool_ms[str(name)].append(ms)
        trace = store.get(r.get("trace_id") or "") or {}
        for t in [s for s in trace.get("spans", []) if s["kind"] == "tool"]:
            if t["attrs"].get("gateway_overhead_ms") is not None:
                overhead[str(t["attrs"].get("tool"))].append(t["attrs"]["gateway_overhead_ms"])
        for name, ms in n["pantry"]:
            pantry[name].append(ms)
    lines += ["", "### MCP tool calls through ContextForge", "",
              "| Tool | Calls | Median call (hub to result) | Median gateway overhead |",
              "|---|---|---|---|"]
    for name in sorted(tool_ms):
        lines.append(f"| `{name}` | {len(tool_ms[name])} | {_n(_median(tool_ms[name]))} ms | "
                     f"{_n(_median(overhead.get(name, [])), 1)} ms |")
    if pantry:
        lines += ["", "### pantry's own steps (demo mode: no model)", "",
                  "| Step | Runs | Median |", "|---|---|---|"]
        for name in sorted(pantry, key=lambda k: -(_median(pantry[k]) or 0)):
            lines.append(f"| {name} | {len(pantry[name])} | {_n(_median(pantry[name]), 1)} ms |")
    lines += ["", "### Gemini list prices used (USD per 1M tokens)", "",
              "| Model | Input | Output |", "|---|---|---|"]
    lines += [f"| `{k}` | ${v[0]:.2f} | ${v[1]:.2f} |" for k, v in PRICES.items()]
    lines += ["", f"Source: {PRICES_SOURCE}."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--traces", type=Path, default=Path.home() / ".pantry-demo" / "traces")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    text = tables(load_runs(args.runs), TraceStore(args.traces, keep=5_000))
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":  # pragma: no cover
    main()
