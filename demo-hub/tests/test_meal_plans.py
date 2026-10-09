"""A meal plan's card, Markdown and model lines (answers.py), and what the model and the browser
get of a plan_meals result (agent.py)."""

from __future__ import annotations

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
