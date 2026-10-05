"""A plan's answer built by code: the table under the model's sentences, and the compact plan a
local model reads instead of the JSON."""

from __future__ import annotations

import json
from typing import Any

from demo_hub.agent import Agent, result_for_model
from demo_hub.answers import plan_for_model, plan_tables, strip_tables, with_tables
from demo_hub.settings import Settings
from tests.test_agent import (  # noqa: F401
    FakeSession,
    FakeTargets,
    ScriptedChat,
    run,
    session,
    turn,
)


def line(n: int, ingredient: str, product: str, pid: int, price: float, trip: float,
         origin: str) -> dict[str, Any]:
    return {"line_no": n, "ingredient": ingredient, "product_id": pid, "product": product,
            "brand": "B", "size": "", "store": "GreenLeaf Grocers Kitsilano", "price": price,
            "confidence": 0.9, "origin_country": origin, "origin_status": "resolved",
            "match": "exact", "also_lines": [], "packs": 1,
            "trip_store": "Pantry Mart Downtown", "trip_price": trip}


PLAN: dict[str, Any] = {"summary": {
    "recipe_slug": "tomato_penne", "recipe_name": "Tomato Penne", "total_cost": 19.32,
    "origin_status": "verified",
    "coverage": {"spend_fraction": 0.9648, "count_fraction": 0.8, "meets_floor": True,
                 "lines_known": 4, "lines_total": 5, "lines_excluded_origin": 7},
    "lines": [line(1, "Penne", "Penne Rigate 500g", 51, 1.97, 2.51, "Italy"),
              line(2, "Olive Oil", "Extra Virgin Olive Oil 500ml", 16, 9.29, 9.84, "Spain")],
    "trip": {"stores": ["Pantry Mart Downtown"], "basket_cost": 20.05, "travel_cost": 0.21,
             "total_cost": 20.26, "savings_vs_one_stop": 0.0, "items": []},
    "notes": [], "not_stocked": [{"ingredient": "Basil", "reason": "not stocked",
                                  "suggestions": []}],
    "out_of_range": [], "skipped": [], "llm_cost_usd": 0.0, "latency_ms": 0,
    "llm_calls": [], "burr_run": "run-x", "pipeline": {"load_recipe": 2.5}}}


def test_the_table_comes_from_the_plan() -> None:
    [table] = plan_tables([PLAN])
    assert table.startswith("### Tomato Penne")
    assert "| Penne | Penne Rigate 500g | Pantry Mart Downtown | $2.51 | Italy |" in table
    assert "**Total:** $20.26 for the trip to Pantry Mart Downtown ($20.05 basket + $0.21 travel)" \
        in table
    assert "**Origin:** 96% of the spend verified (4 of 5 lines)" in table
    assert "**Not found:** Basil (not stocked)" in table


def test_without_a_trip_the_table_says_no_stores_were_chosen() -> None:
    summary = {**PLAN["summary"], "trip": None, "coverage": None,
               "lines": [{**line(1, "Penne", "Penne Rigate 500g", 51, 1.97, 0, "Italy"),
                          "trip_store": "", "trip_price": None}], "not_stocked": []}
    [table] = plan_tables([{"summary": summary}])
    assert "| Penne | Penne Rigate 500g | GreenLeaf Grocers Kitsilano | $1.97 | Italy |" in table
    assert "(the plan chose no trip)" in table and "**Origin:**" not in table
    assert "**Not found:** nothing" in table


def test_a_week_gets_its_days_and_shopping_list() -> None:
    week = {"summary": {"days": [{"recipe_name": "Tomato Penne", "day_cost": 9.5, "lines": []}],
                        "shopping_list": [{"product": "Penne Rigate 500g", "store": "S",
                                           "price": 1.97, "used_by": ["Tomato Penne"]}],
                        "total_cost": 9.5, "trip": None, "coverage": None, "overlap_savings": 0}}
    [table] = plan_tables([week])
    assert "| 1 | Tomato Penne | $9.50 |" in table
    assert "| Penne Rigate 500g | S | $1.97 | Tomato Penne |" in table


def test_a_table_the_model_wrote_anyway_is_replaced() -> None:
    text = "Here is your plan.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nEnjoy."
    assert strip_tables(text) == "Here is your plan.\n\nEnjoy."
    assert with_tables("Sum.", []) == "Sum."
    assert with_tables(text, ["T"]) == "Here is your plan.\n\nEnjoy.\n\nT"


def test_a_local_model_reads_short_lines_instead_of_the_json() -> None:
    text = plan_for_model(PLAN)
    assert text is not None
    assert "- Penne: Penne Rigate 500g [51], Pantry Mart Downtown $2.51, Italy" in text
    assert "total: $20.26 for the trip to Pantry Mart Downtown" in text
    assert "origin: 96% of the spend verified" in text and "not found: Basil" in text
    assert len(text) < len(result_for_model({"structured": PLAN})) / 2
    assert plan_for_model({"items": []}) is None and plan_for_model(None) is None


def test_the_answer_ends_with_the_table_and_the_history_stays_short(
        session: FakeSession) -> None:  # noqa: F811
    session.results["pantry-find-product"] = PLAN
    chat = ScriptedChat(
        turn(calls=[{"id": "c1", "name": "pantry-find-product", "arguments": {"query": "x"}}]),
        turn("Your basket is $20.26 at Pantry Mart Downtown; basil is not stocked."))
    events, conv = run(Agent(Settings(observer_model=""), FakeTargets(), chat), "plan it",
                       model="ollama:m")
    answer = next(e for e in events if e["type"] == "assistant")["text"]
    assert answer.startswith("Your basket is $20.26") and "| Penne | Penne Rigate 500g |" in answer
    assert conv.messages[-1]["content"] == "Your basket is $20.26 at Pantry Mart Downtown; " \
        "basil is not stocked."                         # the table is not replayed to the model
    tool_message = next(m for m in chat.requests[1]["messages"] if m["role"] == "tool")
    assert tool_message["content"].startswith("plan: Tomato Penne")       # compact, not JSON
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["model_chars"] == len(tool_message["content"])


def test_a_cloud_model_still_reads_the_json(session: FakeSession) -> None:  # noqa: F811
    session.results["pantry-find-product"] = PLAN
    chat = ScriptedChat(
        turn(calls=[{"id": "c1", "name": "pantry-find-product", "arguments": {"query": "x"}}]),
        turn("Done."))
    run(Agent(Settings(observer_model=""), FakeTargets(), chat), "plan it")
    tool_message = next(m for m in chat.requests[1]["messages"] if m["role"] == "tool")
    assert json.loads(tool_message["content"])["summary"]["recipe_name"] == "Tomato Penne"
