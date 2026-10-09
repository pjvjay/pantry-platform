"""A meal plan's card, Markdown and model lines (answers.py), what the model and the browser get
of a plan_meals result (agent.py), and the two meal-plan evals: no_invented_shelf_life and
grounded_nutrition (evals.py)."""

from __future__ import annotations

import copy
from typing import Any

from demo_hub.agent import FOR_BROWSER, SERVER_ONLY, for_browser, model_copy
from demo_hub.answers import (
    is_mealplan,
    mealplan_for_model,
    mealplan_table,
    plan_cards,
    plan_for_model,
    plan_tables,
)
from demo_hub.bench import Run
from demo_hub.evals import (
    check_grounded_nutrition,
    check_no_invented_shelf_life,
    evaluate,
    reply_of,
)
from tests.test_pre_meal_selection import PARSED_DISHES, PARSED_PROPOSALS, drafted


def result(**summary: Any) -> dict[str, Any]:
    out = drafted({"dishes": PARSED_DISHES, "proposed": PARSED_PROPOSALS, "days": 14,
                   "start_date": "2026-10-09", "current": {"rev": 7}})
    out["summary"].update(summary)
    return out


def test_a_plan_meals_result_is_a_mealplan_card_with_its_ops() -> None:
    r = result()
    assert is_mealplan(r)
    [card] = plan_cards([{"result": []}, r], drop=FOR_BROWSER | SERVER_ONLY, start=3)
    assert card["kind"] == "mealplan" and card["base_rev"] == 7
    assert card["links"] == [{"label": "Open in Meal plan", "href": "#/mealplan?from=assistant"}]
    assert len(card["summary"]["ops"]) == 12 and "label" not in card
    drafted_card = plan_cards([result(drafted_from_message=True)], drop=FOR_BROWSER)[0]
    assert drafted_card["label"] == "drafted from your message"


def test_the_model_never_reads_the_ops_or_the_draft_and_the_browser_does() -> None:
    r = result()
    seen = model_copy(r)
    assert "draft" not in seen and "ops" not in seen["summary"]
    assert seen["summary"]["proposals"] == r["summary"]["proposals"]
    shown = for_browser({"structured": r, "text": ""})
    assert shown["structured"]["draft"] == r["draft"]
    assert shown["structured"]["summary"]["ops"] == r["summary"]["ops"]
    assert r["summary"]["ops"] and r["draft"]                          # the original untouched


def test_a_local_model_reads_at_most_twelve_short_lines() -> None:
    many = result(trips=[{"date": f"2026-10-{9 + i:02d}", "stores": ["A"], "items": 3,
                          "total_cost": 10.0 + i, "total_is_floor": i == 2, "reason": "x"}
                         for i in range(9)],
                  warnings=[f"warning {i} " + "w" * 300 for i in range(8)],
                  unplaced=[{"title": "Mango Milkshake", "count": 1, "reason": "no slot"}])
    for r in (result(), many):
        text = plan_for_model(r)
        assert text == mealplan_for_model(r["summary"])
        assert len(text.splitlines()) <= 12 and len(text) < 1500
    lines = plan_for_model(result()).splitlines()
    assert lines[0] == ("meal plan draft (not applied yet): 12 new meal(s) over 14 days from "
                        "Fri 9 Oct, each serving 2")
    assert lines[1] == "placed: 3 Pepperoni Pizza, 2 Chicken Fried Rice, 7 Mango Milkshake"
    assert lines[2] == ("needs the shopper's OK, not placed: 3 Chicken Biryani (you wrote "
                        "\"chicken briyani\")")
    assert "(demo amounts)" in plan_for_model(result())
    assert "and 5 more" in plan_for_model(many)


def test_the_meal_plan_markdown() -> None:
    [table] = plan_tables([result()])
    assert table == mealplan_table(result()["summary"])
    assert table.startswith("### Meal plan draft: 14 days from Fri 9 Oct\n\n| Day | Meals |")
    assert "| Fri 9 Oct | Pepperoni Pizza, Chicken Fried Rice, Mango Milkshake |" in table
    assert "**Trips (fresh, suggested):** Fri 9 Oct $63.76 at Pantry Mart Downtown" in table
    assert "**Total:** $63.76 (demo prices)" in table
    assert "**Needs your OK:** 3 Chicken Biryani (you wrote \"chicken briyani\")" in table
    assert "**Nutrition:** nutrition per person (demo amounts)" in table


# --- evals ----------------------------------------------------------------------------------------------

def run_with(answer: str, structured: Any, reply: str | None = None) -> tuple[Run, list[dict]]:
    events = [{"type": "start", "tools": ["plan_meals"]},
              {"type": "tool_call", "id": "c1", "name": "plan_meals", "arguments": {}},
              {"type": "tool_result", "id": "c1", "name": "plan_meals", "is_error": False,
               "structured": structured, "ms": 1},
              {"type": "assistant", "text": answer,
               **({"reply": reply} if reply is not None else {})},
              {"type": "done", "stop": "answered", "seconds": 1}]
    return Run.from_events("m", "online", 0, events), events


def test_grounded_nutrition_fails_kcal_for_demo_amounts_without_demo() -> None:
    r = result()
    run, events = run_with("Over the two weeks the meals add up to at least 5,456 kcal per "
                           "person.", r)
    check = check_grounded_nutrition(run, reply_of(events))
    assert check is not None and not check.passed and "demo" in check.detail
    run, events = run_with("They add up to at least 5,456 kcal per person (demo amounts).", r)
    check = check_grounded_nutrition(run, reply_of(events))
    assert check is not None and check.passed, check
    # an invented figure fails, labelled or not
    run, events = run_with("About 2,100 kcal a day (demo amounts).", r)
    check = check_grounded_nutrition(run, reply_of(events))
    assert check is not None and not check.passed and "2,100 kcal" in check.detail
    # the hub's own table says "demo amounts": the model's reply must say it itself
    run, events = run_with("The plan has 5,456 kcal.\n\n**Nutrition:** (demo amounts) 5,456 kcal",
                           r, reply="The plan has 5,456 kcal.")
    check = check_grounded_nutrition(run, reply_of(events))
    assert check is not None and not check.passed
    # no figure quoted: nothing to check
    run, events = run_with("Twelve meals are placed.", r)
    assert check_grounded_nutrition(run, reply_of(events)) is None


def test_grounded_nutrition_needs_no_badge_for_amounts_from_a_source() -> None:
    r = result(nutrition="nutrition per person: 4 of 14 days complete; 1,840 kcal and 96 g "
                         "protein on average over the complete days")
    run, events = run_with("You average 1,840 kcal and 96 g protein a day.", r)
    check = check_grounded_nutrition(run, reply_of(events))
    assert check is not None and check.passed, check


def test_no_invented_shelf_life() -> None:
    r = result()      # its trip reason cites "keeps 1 to 2 days in the fridge"
    cases = [
        ("Chicken keeps 1 to 2 days in the fridge, so the trip is Friday.", True),
        ("Chicken keeps 1-2 days in the fridge.", True),
        ("Chicken keeps 5 days in the fridge, so one trip covers it.", False),
        ("The milk lasts 2 weeks.", False),
        ("I placed 12 meals over 14 days; 3 Chicken Biryani need your OK.", True),
    ]
    for answer, ok in cases:
        run, events = run_with(answer, r)
        check = check_no_invented_shelf_life(run, reply_of(events))
        assert check is not None and check.passed is ok, (answer, check)
    # a turn with no meal plan and no storage claim: not applicable
    run = Run.from_events("m", "online", 0, [{"type": "assistant", "text": "Penne is $1.97."}])
    assert check_no_invented_shelf_life(run, "Penne is $1.97.") is None


def test_evaluate_runs_both_checks_on_a_meal_plan_turn() -> None:
    _, events = run_with("Twelve meals at least 5,456 kcal (demo amounts); chicken keeps 1 to 2 "
                         "days in the fridge.", copy.deepcopy(result()))
    checks = {c["name"]: c for c in evaluate(events)["checks"]}
    assert checks["grounded_nutrition"]["passed"] and checks["no_invented_shelf_life"]["passed"]
