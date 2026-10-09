"""The model bench: parsing a run's events, every grader (passing and failing), the consistency
and performance summary, the report, and a resumable end-to-end run with the agent stubbed."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from demo_hub import bench
from demo_hub.bench import CASES, Run, check_known_tools, grade, money_in, summarise
from demo_hub.llm import LLMError
from demo_hub.settings import Settings

PENNE = {"query": "penne", "match": "direct", "total": 2, "items": [
    {"name": "Penne Rigate 500g", "store": "GreenLeaf Grocers Kitsilano", "price": 1.97},
    {"name": "Gluten-Free Penne 340g", "store": "Pantry Mart Downtown", "price": 4.04}]}
PLAN = {"summary": {"total_cost": 19.28, "coverage": None, "lines": [
    {"product": "Broccoli Crown", "store": "ValueFoods East Van", "price": 2.27},
    {"product": "Carrots 1kg", "store": "GreenLeaf Grocers Kitsilano", "price": 2.15},
    {"product": "Green Bell Pepper", "store": "ValueFoods East Van", "price": 1.49}]}}
TOMATO = {"summary": {"total_cost": 20.35, "coverage": {"spend_fraction": 0.9612}, "lines": []}}
RECIPES = {"result": [{"name": "Tomato Penne"}, {"name": "Vegetable Stir Fry"}, {"name": "Chicken Curry"}]}
CASE = {c.id: c for c in CASES}


def events(answer: str, *uses: tuple[str, dict[str, Any], Any], error: bool = False,
           stop: str = "answered") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"type": "start", "tools": sorted(bench.CORE_TOOLS)}]
    for i, (name, args, result) in enumerate(uses):
        out += [{"type": "llm_call", "step": i + 1, "wall_s": 10.0, "prompt_tokens": 2000 if i == 0 else 50,
                 "prompt_s": 8.0, "output_tokens": 20, "gen_s": 5.0, "load_s": 1.0},
                {"type": "tool_call", "id": f"c{i}", "name": name, "arguments": args},
                {"type": "tool_result", "id": f"c{i}", "name": name, "is_error": error,
                 "structured": result if isinstance(result, dict) else None,
                 "text": result if isinstance(result, str) else "", "ms": 30.0}]
    out += [{"type": "llm_call", "step": len(uses) + 1, "wall_s": 6.0, "prompt_tokens": 100,
             "prompt_s": 4.0, "output_tokens": 60, "gen_s": 15.0},
            {"type": "assistant", "text": answer}, {"type": "done", "stop": stop, "seconds": 42.0}]
    return out


def graded(case: str, evs: list[dict[str, Any]]) -> dict[str, bool]:
    run = Run.from_events("ollama:m", case, 1, evs)
    return {c.name: c.passed for c in grade(run, CASE[case])}


def test_run_from_events_pairs_calls_with_results() -> None:
    run = Run.from_events("ollama:m", "cheapest-penne", 2, events(
        "Penne Rigate 500g is $1.97.", ("find_product", {"query": "penne"}, PENNE)))
    assert (run.model, run.case, run.rep, run.stop, run.seconds) == ("ollama:m", "cheapest-penne", 2, "answered", 42.0)
    [use] = run.tool_uses
    assert (use.name, use.arguments, use.is_error, use.result["total"], use.ms) == (
        "find_product", {"query": "penne"}, False, 2, 30.0)
    assert len(run.llm_calls) == 2 and run.llm_calls[0]["prompt_tokens"] == 2000
    assert "Penne Rigate" in run.results_text()


def test_money_extraction() -> None:
    assert money_in("Costs $1.97, or $ 12 or $1,204.50; 3 meals, 5 km") == [1.97, 12.0, 1204.5]


def test_cheapest_penne_passes_when_the_answer_matches_the_result() -> None:
    ok = graded("cheapest-penne", events(
        "The cheapest is Penne Rigate 500g at $1.97 from GreenLeaf Grocers Kitsilano.",
        ("find_product", {"query": "Penne"}, PENNE)))
    assert all(ok.values()), ok


def test_cheapest_penne_from_memory_fails_and_invented_prices_are_caught() -> None:
    ok = graded("cheapest-penne", events("Barilla penne is about $2.49 at Safeway."))
    assert not ok["used_tools"] and not ok["find_product(penne)"] and not ok["names the product"]
    assert not ok["grounded_money"]                  # $2.49 is in no tool result
    ok = graded("cheapest-penne", events(
        "Penne Rigate 500g, $1.79, GreenLeaf Grocers Kitsilano.",   # a misquoted price
        ("find_product", {"query": "penne"}, PENNE)))
    assert not ok["states the price"] and not ok["grounded_money"] and ok["names the store"]


def test_server_rejections_and_unoffered_tools_fail() -> None:
    evs = events("Sorry.", ("plan_week", {}, "Error executing tool: unknown"), error=True)
    ok = graded("cheapest-penne", evs)
    assert not ok["valid_calls"] and not ok["known_tools"]


def test_an_unfinished_run_fails_finished() -> None:
    evs = events("", stop="error")
    evs.insert(-1, {"type": "error", "message": "ollama timed out"})
    assert not graded("recipe-list", evs)["finished"]


def test_stir_fry_needs_products_total_and_a_store() -> None:
    answer = ("Buy Broccoli Crown ($2.27, ValueFoods East Van), Carrots 1kg ($2.15) and Green Bell "
              "Pepper ($1.49). Total $19.28.")
    ok = graded("stir-fry-3-meals", events(answer, ("plan_from_text", {"recipe_text": "- broccoli"}, PLAN)))
    assert all(ok.values()), ok
    no_store = {"summary": {**PLAN["summary"], "lines": [ln | {"store": ""} for ln in PLAN["summary"]["lines"]]}}
    ok = graded("stir-fry-3-meals", events(answer, ("plan_recipe", {"slug": "veggie_stirfry"}, no_store)))
    assert not ok["names a store"] and ok["states the total"]
    ok = graded("stir-fry-3-meals", events("Here is a stir-fry recipe: 2 cups broccoli, soy sauce."))
    assert not ok["planned a basket"] and not ok["used_tools"]


def test_tomato_penne_checks_the_exclusion_and_the_verified_share() -> None:
    args = {"slug": "tomato_penne", "exclude_origin": ["United States"]}
    ok = graded("tomato-penne-no-us", events("Total $20.35; 96% of the spend is verified.",
                                             ("plan_recipe", args, TOMATO)))
    assert all(ok.values()), ok
    ok = graded("tomato-penne-no-us", events("Total $20.35; all of it is verified.",
                                             ("plan_recipe", {"slug": "tomato_penne"}, TOMATO)))
    assert not ok["excludes the United States"] and not ok["reports the verified share"]


def test_recipe_list_unknown_product_and_out_of_scope() -> None:
    ok = graded("recipe-list", events("I can plan Tomato Penne, Vegetable Stir Fry and Chicken Curry.",
                                      ("list_recipes", {}, RECIPES)))
    assert all(ok.values()), ok
    assert not graded("recipe-list", events("Tomato Penne.", ("list_recipes", {}, RECIPES)))["names the recipes"]

    none = {"query": "dragon fruit jam", "match": "none", "total": 0, "items": []}
    ok = graded("unknown-product", events("Sorry, dragon fruit jam is not in the catalog.",
                                          ("find_product", {"query": "dragon fruit jam"}, none)))
    assert all(ok.values()), ok
    ok = graded("unknown-product", events("Dragon fruit jam costs $6.99.",
                                          ("find_product", {"query": "dragon fruit jam"}, none)))
    assert not ok["says it is not stocked"] and not ok["grounded_money"]

    ok = graded("out-of-scope", events("I can only help with groceries."))
    assert all(ok.values()), ok
    ok = graded("out-of-scope", events("Expect 14°C and rain.", ("find_product", {"query": "weather"}, PENNE)))
    assert not ok["no tool calls"] and not ok["no invented forecast"]


def record(model: str, case: str, rep: int, answer: str, *uses: tuple[str, dict[str, Any], Any]) -> dict[str, Any]:
    run = Run.from_events(model, case, rep, events(answer, *uses))
    return bench.record(run, grade(run, CASE[case]))


def test_summary_measures_consistency_and_speed() -> None:
    good = "The cheapest is Penne Rigate 500g at $1.97 from GreenLeaf Grocers Kitsilano."
    use = ("find_product", {"query": "penne"}, PENNE)
    records = [record("ollama:a", "cheapest-penne", 1, good, use),
               record("ollama:a", "cheapest-penne", 2, good, use),
               record("ollama:a", "cheapest-penne", 3, "About $3.00."),
               record("ollama:b", "cheapest-penne", 1, good, use)]
    s = summarise(records, ["ollama:a", "ollama:b"])
    a = s["models"]["ollama:a"]
    assert (a["runs"], a["passed"], a["pass_rate"], a["runs_with_tool_calls"], a["invented_amounts"]) == (3, 2, 0.67, 2, 1)
    assert a["first_call_prompt_tokens"] == 2000 and a["later_call_prompt_tokens"] == 100
    # 2 calls of (2000 tokens / 8 s) and 3 calls of (100 / 4 s); generation (20/5 s) and (60/15 s).
    assert a["prompt_tok_s"] == round((2000 * 2 + 100 * 3) / (8 * 2 + 4 * 3), 1)
    assert a["gen_tok_s"] == 4.0 and a["load_s_max"] == 1.0
    case = s["cases"]["cheapest-penne"]["ollama:a"]
    assert case["sequences"] == {"find_product": 2, "(no tools)": 1}
    assert (case["sequence_agreement"], case["first_call_agreement"], case["answer_amounts_agreement"]) == (0.67, 0.67, 0.67)
    assert case["failed_checks"]["used_tools"] == 1
    meta = {"started": "t0", "updated": "t1", "machine": {"cpu": "cpu", "ram_gb": 32}, "profile": "core",
            "tools": 7, "repeat": 3, "temperature": None, "num_ctx": 16384, "demo_mode": True,
            "model_info": {"ollama:a": {"family": "granite"}}}
    text = bench.report(s, meta, records)
    assert "| runs passed (all checks) | 2 | 1 |" in text
    assert "| cheapest-penne | `ollama:a` | 2/3 | find_product | 0.67 |" in text
    [line] = [ln for ln in text.splitlines() if ln.startswith("- **cheapest-penne** · `ollama:a` · rep 3:")]
    assert "grounded_money (amounts not in any tool result: [3.0])" in line
    assert "used_tools (answered from memory)" in line


def test_main_runs_interleaved_resumes_and_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[tuple[str, str, int]] = []

    async def fake_run_case(agent: Any, model: str, case: Any, rep: int, tools: Any,
                            disclosure: str = "all", *rest: Any) -> Run:
        order.append((model, case.id, rep))
        assert tools == bench.CORE_TOOLS
        return Run.from_events(model, case.id, rep, events("I can only help with groceries."))

    async def fake_info(settings: Any, model: str) -> dict[str, Any]:
        return {"model": model, "family": "test"}

    monkeypatch.setattr(bench, "run_case", fake_run_case)
    monkeypatch.setattr(bench, "ollama_model_info", fake_info)
    monkeypatch.setenv("PANTRY_API_URL", "http://127.0.0.1:9")     # unreachable: demo_mode unknown
    argv = ["--model", "ollama:a", "--model", "ollama:b", "--repeat", "2", "--case", "out-of-scope",
            "--case", "recipe-list", "--out", str(tmp_path)]
    asyncio.run(bench.main(argv))
    # Repetition, then case (in CASES order), then model.
    assert order[:4] == [("ollama:a", "recipe-list", 1), ("ollama:b", "recipe-list", 1),
                         ("ollama:a", "out-of-scope", 1), ("ollama:b", "out-of-scope", 1)]
    assert len(order) == 8
    lines = (tmp_path / "runs.jsonl").read_text().splitlines()
    passed = {(r["case"], r["passed"]) for r in map(json.loads, lines)}
    assert len(lines) == 8 and passed == {("out-of-scope", True), ("recipe-list", False)}
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["meta"]["demo_mode"] is None and summary["models"]["ollama:a"]["runs"] == 4
    assert "# Local model bench: ollama:a vs ollama:b" in (tmp_path / "report.md").read_text()
    # A rerun with the same --out skips every finished run; --report-only runs nothing.
    order.clear()
    asyncio.run(bench.main([*argv, "--repeat", "3"]))
    assert len(order) == 4 and {r for _, _, r in order} == {3}
    order.clear()
    asyncio.run(bench.main([*argv, "--report-only"]))
    assert order == []
    # A later phase with fewer models still reports the earlier phase's.
    asyncio.run(bench.main(["--model", "ollama:b", "--report-only", "--out", str(tmp_path)]))
    assert list(json.loads((tmp_path / "summary.json").read_text())["models"]) == ["ollama:b", "ollama:a"]


def test_model_variants() -> None:
    assert bench.parse_variant("ollama:granite4.2:8b") == ("ollama:granite4.2:8b", {})
    assert bench.parse_variant("ollama:granite4.2:8b#think=false,temperature=0") == (
        "ollama:granite4.2:8b", {"think": False, "temperature": 0.0})
    with pytest.raises(LLMError, match="unknown model option"):
        bench.parse_variant("ollama:m#think=maybe")
    settings = bench.variant_settings(bench.Settings(ollama_temperature=0.7), {"think": True})
    assert (settings.ollama_think, settings.ollama_temperature) == (True, 0.7)


def test_main_gives_each_variant_its_own_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_run_case(agent: Any, spec: str, case: Any, rep: int, tools: Any,
                            disclosure: str = "all", *rest: Any) -> Run:
        seen[spec] = agent.chat.settings.ollama_think
        return Run.from_events(spec, case.id, rep, events("I can only help with groceries."))

    async def fake_info(settings: Any, model: str) -> dict[str, Any]:
        return {"model": model}

    monkeypatch.setattr(bench, "run_case", fake_run_case)
    monkeypatch.setattr(bench, "ollama_model_info", fake_info)
    monkeypatch.setenv("PANTRY_API_URL", "http://127.0.0.1:9")
    asyncio.run(bench.main(["--model", "ollama:g", "--model", "ollama:g#think=false", "--repeat", "1",
                            "--case", "out-of-scope", "--out", str(tmp_path)]))
    assert seen == {"ollama:g": None, "ollama:g#think=false": False}
    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["model_info"]["ollama:g#think=false"] == {"model": "ollama:g", "options": {"think": False}}


def test_a_bench_run_is_kept_as_a_tagged_trace_with_its_online_evals(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from demo_hub.telemetry import TraceStore

    class FakeAgent:
        def __init__(self) -> None:
            self.settings = Settings()
            self.conversations: dict[str, Any] = {}

        def conversation(self, cid: Any, model: str, target: str, disclosure: str) -> Any:
            return SimpleNamespace(id="c1", model=model, tools=None)

        async def run(self, conv: Any, message: str) -> Any:
            for e in events("I can only help with groceries."):
                yield e

    store = TraceStore(tmp_path)
    case = next(c for c in bench.CASES if c.id == "out-of-scope")
    run = asyncio.run(bench.run_case(FakeAgent(), "ollama:m#think=false", case, 2, None, "progressive",
                                     "pantry", store, ["Pantry Mart Downtown"]))
    trace = store.get(run.trace_id)
    assert trace is not None and (trace["source"], trace["case"], trace["rep"]) == \
        ("bench", "out-of-scope", 2)
    assert trace["evals"]["checks"] and run.answer_confidence == trace["evals"]["answer_confidence"]
    assert trace["model"] == "ollama:m#think=false"          # the full spec, variant included
    assert bench.record(run, [])["trace_id"] == run.trace_id


def test_discover_tools_counts_as_offered_when_the_toolset_is_progressive() -> None:
    events = [{"type": "start", "tools": ["pantry-list-recipes"], "discoverable": True},
              {"type": "tool_call", "id": "c1", "name": "discover_tools", "arguments": {}},
              {"type": "tool_result", "id": "c1", "name": "discover_tools", "is_error": False,
               "text": "offered"},
              {"type": "done", "stop": "answered", "seconds": 1}]
    run = Run.from_events("m", "case", 1, events)
    assert check_known_tools(run).passed
    run = Run.from_events("m", "case", 1, [{**events[0], "discoverable": False}, *events[1:]])
    assert not check_known_tools(run).passed


def test_excluding_the_united_states_by_its_alias_counts() -> None:
    case = next(c for c in CASES if c.id == "tomato-penne-no-us")
    for name in ("United States", "US", "U.S.A.", "usa"):
        events = [{"type": "start", "tools": ["plan_recipe"]},
                  {"type": "tool_call", "id": "c", "name": "plan_recipe",
                   "arguments": {"slug": "tomato_penne", "exclude_origin": [name]}},
                  {"type": "tool_result", "id": "c", "name": "plan_recipe", "is_error": False,
                   "structured": {"summary": {}}}]
        checks = {c.name: c.passed for c in case.grade(Run.from_events("m", case.id, 1, events))}
        assert checks["excludes the United States"], name
