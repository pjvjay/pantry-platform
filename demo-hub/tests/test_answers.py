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


def test_lean_tools_drop_indentation_titles_and_null_wrappers() -> None:
    from demo_hub.agent import lean_description, lean_schema, openai_tools
    doc = ("Plan a recipe: matches every ingredient.\n    Get slugs from list_recipes first.\n\n"
           "    Pass the shopper's location\n    to get stores.\n\n    " + "x" * 700)
    assert lean_description(doc) == ("Plan a recipe: matches every ingredient. Get slugs from "
                                      "list_recipes first.\nPass the shopper's location to get stores.")
    schema = {"title": "plan_recipeArguments", "type": "object", "required": ["slug"],
              "properties": {"slug": {"title": "Slug", "type": "string"},
                             "lat": {"anyOf": [{"type": "number", "maximum": 90}, {"type": "null"}],
                                     "default": None, "title": "Lat"}}}
    assert lean_schema(schema) == {"type": "object", "required": ["slug"], "properties": {
        "slug": {"type": "string"}, "lat": {"type": "number", "maximum": 90}}}
    tool = {"name": "t", "description": doc, "inputSchema": schema}
    assert openai_tools([tool])[0]["function"]["parameters"] == schema       # cloud: untouched
    assert len(str(openai_tools([tool], lean=True))) < len(str(openai_tools([tool]))) / 2


def test_the_hub_adds_the_shoppers_location_to_a_plan_call_without_one() -> None:
    agent = Agent(Settings(observer_model=""), FakeTargets(), ScriptedChat())
    assert agent._with_location("pantry-plan-recipe", {"slug": "tomato_penne"}) == {
        "slug": "tomato_penne", "lat": 49.2827, "lon": -123.1207, "max_km": 5.0}
    made_up = {"slug": "s", "lat": -74.08, "lon": -84.22, "max_km": 20}
    assert agent._with_location("pantry-plan-recipe", made_up) == {
        "slug": "s", "lat": 49.2827, "lon": -123.1207, "max_km": 20}        # a distance stands
    plan = {"name": "pantry-plan-recipe", "inputSchema": {"type": "object", "required": ["slug"],
            "properties": {"slug": {}, "lat": {}, "lon": {}, "max_km": {}}}}
    [shown] = agent._plan_tools([plan], "gemini:g")
    assert set(shown["inputSchema"]["properties"]) == {"slug", "max_km"}     # no lat/lon to send
    [local] = agent._plan_tools([plan], "ollama:m")
    assert set(local["inputSchema"]["properties"]) == {"slug"}               # nor a distance
    assert agent._with_location("plan_recipe", {"slug": "s", "max_km": 0})["max_km"] == 5.0
    assert agent._with_location("pantry-find-product", {"query": "x"}) == {"query": "x"}
    off = Agent(Settings(observer_model="", shopper_location=None), FakeTargets(), ScriptedChat())
    assert off._with_location("plan_recipe", {"slug": "s"}) == {"slug": "s"}


def test_shopper_location_parses_from_the_environment() -> None:
    from demo_hub.settings import _location
    assert _location("49.2827,-123.1207,5") == (49.2827, -123.1207, 5.0)
    assert _location("49.3,-123.1") == (49.3, -123.1, 5.0) and _location("") is None
