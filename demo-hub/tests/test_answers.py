"""A plan's answer built by code: the table under the model's sentences, and the compact plan a
local model reads instead of the JSON."""

from __future__ import annotations

import json
from typing import Any

from demo_hub.agent import Agent, result_for_model
from demo_hub.answers import (
    options,
    plan_cards,
    plan_for_model,
    plan_tables,
    strip_tables,
    with_tables,
)
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
    assert "**Left out:** Basil (not stocked)" in table


def test_without_a_trip_the_table_says_no_stores_were_chosen() -> None:
    summary = {**PLAN["summary"], "trip": None, "coverage": None,
               "lines": [{**line(1, "Penne", "Penne Rigate 500g", 51, 1.97, 0, "Italy"),
                          "trip_store": "", "trip_price": None}], "not_stocked": []}
    [table] = plan_tables([{"summary": summary}])
    assert "| Penne | Penne Rigate 500g | GreenLeaf Grocers Kitsilano | $1.97 | Italy |" in table
    assert "(the plan chose no trip)" in table and "**Origin:**" not in table
    assert "**Left out:** nothing" in table


ONION = {"ingredient": "Yellow Onion",
         "reason": "all 1 candidate(s) are evidenced as coming from United States, which this "
                   "plan excludes",
         "suggestions": ["Yellow Onion ($0.87) — United States via ingredient_origin",
                         "still available, not a direct match: Red Onion ($1.34, Mexico)",
                         "still available, not a direct match: Green Onions ($1.39)"]}


def test_an_excluded_ingredient_lists_its_swaps_and_what_could_be_allowed_back() -> None:
    assert options(ONION) == (["Red Onion ($1.34, Mexico)", "Green Onions ($1.39)"],
                              ["Yellow Onion ($0.87, United States)"])
    summary = {**PLAN["summary"], "not_stocked": [], "out_of_range": [ONION]}
    [table] = plan_tables([{"summary": summary}])
    assert ("**Left out:** Yellow Onion (all 1 candidate(s) are evidenced as coming from United "
            "States, which this plan excludes; swap: Red Onion ($1.34, Mexico), Green Onions "
            "($1.39); or allow: Yellow Onion ($0.87, United States))") in table
    assert "swap: Red Onion ($1.34, Mexico)" in (plan_for_model({"summary": summary}) or "")


def test_plan_cards_carry_the_latest_plan_without_the_browser_only_fields() -> None:
    older = {"summary": {**PLAN["summary"], "total_cost": 1.0}}
    [card] = plan_cards([older, PLAN], drop={"llm_calls", "burr_run", "pipeline"})
    assert card["kind"] == "plan" and card["summary"]["total_cost"] == PLAN["summary"]["total_cost"]
    assert "llm_calls" not in card["summary"] and "burr_run" not in card["summary"]
    assert plan_cards([{"result": [{"slug": "a", "name": "A"}]}]) == []


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
    assert "origin: 96% of the spend verified" in text and "left out: Basil" in text
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
    event = next(e for e in events if e["type"] == "assistant")
    answer = event["text"]
    assert answer.startswith("Your basket is $20.26") and "| Penne | Penne Rigate 500g |" in answer
    # the browser draws the plan itself under the model's own sentences
    assert event["reply"] == "Your basket is $20.26 at Pantry Mart Downtown; basil is not stocked."
    assert [c["kind"] for c in event["plans"]] == ["plan"]
    assert "llm_calls" not in event["plans"][0]["summary"]
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
    assert agent._with_hub_args("pantry-plan-recipe", {"slug": "tomato_penne"}) == {
        "slug": "tomato_penne", "lat": 49.2827, "lon": -123.1207, "max_km": 5.0}
    made_up = {"slug": "s", "lat": -74.08, "lon": -84.22, "max_km": 20}
    assert agent._with_hub_args("pantry-plan-recipe", made_up) == {
        "slug": "s", "lat": 49.2827, "lon": -123.1207, "max_km": 20}        # a distance stands
    plan = {"name": "pantry-plan-recipe", "inputSchema": {"type": "object", "required": ["slug"],
            "properties": {"slug": {}, "lat": {}, "lon": {}, "max_km": {}}}}
    [shown] = agent._plan_tools([plan], "gemini:g")
    assert set(shown["inputSchema"]["properties"]) == {"slug", "max_km"}     # no lat/lon to send
    [local] = agent._plan_tools([plan], "ollama:m")
    assert set(local["inputSchema"]["properties"]) == {"slug"}               # nor a distance
    plan["inputSchema"]["properties"]["preference"] = {"type": "array"}
    [described] = agent._plan_tools([plan], "ollama:m")
    assert "country names" in described["inputSchema"]["properties"]["preference"]["description"]
    assert agent._with_hub_args("plan_recipe", {"slug": "s", "max_km": 0})["max_km"] == 5.0
    # a product search gets the location too (the 8B sent lon +123.11), but no distance
    assert agent._with_hub_args("pantry-find-product", {"query": "x", "lon": 123.11}) == {
        "query": "x", "lat": 49.2827, "lon": -123.1207}
    assert agent._with_hub_args("pantry-list-recipes", {}) == {}
    off = Agent(Settings(observer_model="", shopper_location=None), FakeTargets(), ScriptedChat())
    assert off._with_hub_args("plan_recipe", {"slug": "s"}) == {"slug": "s"}


def test_shopper_location_parses_from_the_environment() -> None:
    from demo_hub.settings import _location
    assert _location("49.2827,-123.1207,5") == (49.2827, -123.1207, 5.0)
    assert _location("49.3,-123.1") == (49.3, -123.1, 5.0) and _location("") is None


def test_the_recipe_list_is_drawn_only_when_the_turn_planned_nothing() -> None:
    listed = {"result": [{"slug": "tomato_penne", "name": "Tomato Penne", "servings": 2,
                          "ingredient_count": 5}]}
    [table] = plan_tables([listed])
    assert "| Tomato Penne | 2 | 5 |" in table
    assert plan_tables([listed, PLAN])[0].startswith("### Tomato Penne")   # the plan, not the list
    assert len(plan_tables([listed, PLAN])) == 1


def test_out_of_steps_after_a_plan_the_shopper_still_gets_it(session: FakeSession) -> None:  # noqa: F811
    session.results["pantry-find-product"] = PLAN
    call = turn(calls=[{"id": "c", "name": "pantry-find-product", "arguments": {"query": "x"}}])
    chat = ScriptedChat(call, call, call)
    events, _ = run(Agent(Settings(observer_model="", agent_max_steps=3), FakeTargets(), chat), "go")
    answer = next(e for e in events if e["type"] == "assistant")["text"]
    assert answer.startswith("The model did not finish its summary") and "| Penne |" in answer
    assert events[-1]["stop"] == "step budget reached"


WEEK: dict[str, Any] = {"summary": {
    "days": [{"recipe_slug": "tomato_penne", "recipe_name": "Tomato Penne", "day_cost": 9.5,
              "lines": []}],
    "shopping_list": [{"product": "Penne Rigate 500g", "store": "S", "price": 1.97,
                       "used_by": ["Tomato Penne"]}],
    "total_cost": 9.5, "trip": None, "coverage": None, "overlap_savings": 0}}


def test_every_week_card_links_to_the_meal_plan() -> None:
    [card] = plan_cards([WEEK], start=4)
    assert card["links"] == [{"label": "Open in Meal plan", "href": "#/mealplan?from=week"}]
    assert "ref" not in card                     # no basis: no Options on a week card
    cards = plan_cards([PLAN, WEEK, {"summary": {**WEEK["summary"], "total_cost": 8.0}}])
    assert [c["kind"] for c in cards] == ["plan", "week"]
    assert all(c["links"] for c in cards if c["kind"] == "week")
    assert "links" not in cards[0]


def test_a_plan_card_names_its_plan_only_when_the_hub_holds_its_basis() -> None:
    based = {"summary": {**PLAN["summary"], "basis": {"v": 1, "pins": [
        {"line_no": 4, "product_id": 9}, {"line_no": 2, "product_id": 9}]}}}
    older = {"summary": {**based["summary"], "total_cost": 1.0}}
    [card] = plan_cards([older, {"result": []}, based], drop={"basis"}, start=7)
    assert card["ref"] == 9 and card["pinned_lines"] == [2, 4] and "basis" not in card["summary"]
    [plain] = plan_cards([PLAN], start=7)          # a gateway that sent no basis back
    assert "ref" not in plain and "pinned_lines" not in plain
    assert "ref" not in plan_cards([based])[0]     # no start: the caller holds no tool_log


def test_the_week_card_link_reaches_the_browser(session: FakeSession) -> None:  # noqa: F811
    session.results["pantry-find-product"] = WEEK
    chat = ScriptedChat(
        turn(calls=[{"id": "c", "name": "pantry-find-product", "arguments": {"query": "x"}}]),
        turn("Here is your week."))
    events, _ = run(Agent(Settings(observer_model=""), FakeTargets(), chat), "a week of dinners")
    [card] = next(e for e in events if e["type"] == "assistant")["plans"]
    assert card["kind"] == "week" and card["links"][0]["href"] == "#/mealplan?from=week"


def test_a_cart_note_fits_and_keeps_its_figures() -> None:
    from demo_hub.answers import CART_NOTE_CHARS, cart_note
    note = cart_note(recipe="Spaghetti Bolognese", line_no=3, ingredient="garlic",
                     was="Garlic Bulb 3-pack", now="Fraser Farms Garlic 200g", before=41.7,
                     after=41.2, stores=["Pantry Mart Downtown", "GreenLeaf Grocers Kitsilano"])
    assert note == ("[cart] The shopper changed line 3 (garlic) of Spaghetti Bolognese in the "
                    "cart: Garlic Bulb 3-pack -> Fraser Farms Garlic 200g. Trip now $41.20 at "
                    "Pantry Mart Downtown, GreenLeaf Grocers Kitsilano, was $41.70.")
    long = cart_note(recipe="R" * 200, line_no=12, ingredient="i" * 200, was="W" * 200,
                     now="N" * 200, before=123.45, after=99.99,
                     stores=[f"Store number {i} with a long name" for i in range(5)])
    assert len(long) <= CART_NOTE_CHARS and "$99.99" in long and "$123.45" in long
    undo = cart_note(recipe="R", line_no=1, ingredient="beef", was="B", now="A", before=10.0,
                     after=10.0, stores=[], undone=True)
    assert "back to the planner's pick, A (was B)" in undo
    assert undo.endswith("Total now $10.00 (the plan chose no trip).")
