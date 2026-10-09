"""Meal plans in the Assistant (P8, G10): the shopper's counted dishes read by pantry's Quick add
parse in code before the first model call (``_pre_meal_selection``), the [meals] note,
plan_meals' hidden arguments, the hub's own draft when the model makes no tool call, the
meal_planner observer and compliance, and ChatBody.meal_plan."""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from demo_hub import agent as agent_module
from demo_hub import app as app_module
from demo_hub import meal_plans
from demo_hub.agent import Agent
from demo_hub.assistant_policy import POLICY, compliance_officer, wants_meal_plan
from demo_hub.disclosure import Disclosure
from demo_hub.observers import View, normalize
from demo_hub.settings import Settings
from tests.conftest import console_client
from tests.test_agent import FakeTargets, ScriptedChat, turn

SENTENCE = ("3 Pepperoni Pizza + 2 Chicken Fried Rice + 3 chicken briyani + 7 mango milkshakes "
            "in 2 weeks")
TODAY = dt.date(2026, 10, 8)
PIZZA, RICE, BIRYANI, SHAKE = (f"starter:{k}" for k in ("pepperoni_pizza", "chicken_fried_rice",
                                                         "chicken_biryani", "mango_milkshake"))


def selection(text: str, count: int, key: str | None, title: str, how: str | None,
              slot: str = "dinner") -> dict[str, Any]:
    """One selection as pantry's /mealplan/selection/parse returns it."""
    match = None if key is None else {"recipe_key": key, "title": title, "kind": "starter",
                                      "label": "demo starter", "slot": slot, "how": how,
                                      "distance": 0}
    return {"input": f"{count} {text}", "name": text, "count": count, "count_stated": True,
            "slot_hint": None, "status": "matched" if match else "unmatched",
            "matched_as": match,
            "needs_confirmation": match is None or how in ("alias", "fuzzy"),
            "candidates": [], "meaning": None}


# pantry's parse of the user's sentence (seeds/mealplan_starters.json), as test_selection_parse
# in pantry-api has it: pizza and fried rice exact, briyani an alias, milkshakes a plural
PARSE = {"selections": [
    selection("Pepperoni Pizza", 3, PIZZA, "Pepperoni Pizza", "exact"),
    selection("Chicken Fried Rice", 2, RICE, "Chicken Fried Rice", "exact"),
    selection("chicken briyani", 3, BIRYANI, "Chicken Biryani", "alias"),
    selection("mango milkshakes", 7, SHAKE, "Mango Milkshake", "plural", "snack")],
    "unmatched": [], "period_days": 14, "warnings": []}


def _tool(name: str, *props: str) -> dict[str, Any]:
    return {"name": name, "description": f"{name}.",
            "inputSchema": {"type": "object", "properties": {p: {} for p in props}}}


CATALOG = [_tool("list_recipes"), _tool("find_product", "query", "lat", "lon"),
           _tool("plan_recipe", "slug", "lat", "lon", "max_km", "basis"),
           _tool("plan_week", "days", "max_total_budget", "lat", "lon"),
           _tool("plan_meals", "dishes", "days", "household_servings", "proposed", "start_date",
                 "current", "my_recipe_docs", "lat", "lon", "max_km", "verbose")]
TITLES = {PIZZA: "Pepperoni Pizza", RICE: "Chicken Fried Rice", BIRYANI: "Chicken Biryani",
          SHAKE: "Mango Milkshake"}


def drafted(args: dict[str, Any]) -> dict[str, Any]:
    """pantry's plan_meals as far as these tests need it: every dish placed (one meal a day
    from the start), every proposal returned and never placed, a trip, the nutrition line."""
    start = dt.date.fromisoformat(args["start_date"])
    meals, ops = [], []
    for d in args.get("dishes") or []:
        key = d["recipe"]
        for i in range(int(d["count"])):
            day = (start + dt.timedelta(days=i)).isoformat()
            meals.append({"date": day, "slot": "dinner", "title": TITLES.get(key, key),
                          "recipe_key": key, "new": True})
            ops.append({"op": "place", "recipe_key": key, "date": day, "slot": "dinner"})
    summary = {
        "kind": "mealplan", "start_date": start.isoformat(), "days": args.get("days") or 7,
        "base_rev": (args.get("current") or {}).get("rev"), "household_servings": 2,
        "meals": meals,
        "added": [{"recipe_key": d["recipe"], "title": TITLES.get(d["recipe"], d["recipe"]),
                   "label": "demo starter", "how": "key", "count": d["count"],
                   "placed": d["count"], "slot": "dinner"} for d in args.get("dishes") or []],
        "proposals": [{"input": p["input"], "recipe_key": p["recipe_key"],
                       "title": TITLES[p["recipe_key"]], "label": "demo starter",
                       "how": p["how"], "count": p["count"], "slot": "dinner",
                       "question": f"{p['input']} → {TITLES[p['recipe_key']]} (demo starter)?",
                       "op": {"op": "add_recipe", "recipe_key": p["recipe_key"],
                              "count": p["count"], "spread": True}}
                      for p in args.get("proposed") or []],
        "unmatched": [], "unplaced": [], "strategy": "fresh",
        "trips": [{"date": start.isoformat(), "stores": ["Pantry Mart Downtown"], "items": 9,
                   "total_cost": 63.76, "total_is_floor": False,
                   "reason": "Chicken Fried Rice needs chicken, which keeps 1 to 2 days in the "
                             "fridge."}],
        "other_strategy": {"name": "fewest_trips", "trips": 1, "total_cost": 66.1,
                           "total_is_floor": False},
        "total_cost": 63.76, "total_is_floor": False,
        "nutrition": "nutrition per person (demo amounts): no day is complete, so there is no "
                     "daily average; the period's meals add up to at least 5,456 kcal",
        "warnings": [], "notes": ["Trips are suggestions: only the shopper approves them."],
        "ops": ops}
    return {"summary": summary, "draft": {"v": 1, "start_date": start.isoformat()},
            "full": None}


@pytest.fixture
def pantry(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    class Session:
        async def list_tools(self) -> Any:
            return SimpleNamespace(tools=[SimpleNamespace(name=t["name"],
                                                          model_dump=lambda t=t, **_: t)
                                          for t in CATALOG])

    @asynccontextmanager
    async def fake_open(target: Any) -> AsyncIterator[Session]:
        yield Session()

    async def fake_call(session: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, copy.deepcopy(arguments)))
        if name == "plan_meals":
            if not arguments.get("dishes") and not arguments.get("proposed"):
                return {"name": name, "is_error": True, "structured": None, "ms": 1.0,
                        "truncated": False, "text": "No dishes"}
            structured: Any = drafted(arguments)
        else:
            structured = {"result": []}
        return {"name": name, "is_error": False, "structured": structured,
                "text": json.dumps(structured), "ms": 1.0, "truncated": False}

    monkeypatch.setattr(agent_module, "open_session", fake_open)
    monkeypatch.setattr(agent_module, "call_tool", fake_call)
    monkeypatch.setattr(meal_plans, "today", lambda: TODAY)
    return calls


class Ordered(ScriptedChat):
    """A scripted model that notes when it is called, beside the parser's own note."""

    def __init__(self, order: list[str], *turns: Any) -> None:
        super().__init__(*turns)
        self.order = order

    async def complete(self, *args: Any, **kwargs: Any) -> Any:
        self.order.append("model")
        return await super().complete(*args, **kwargs)


def make_agent(*turns: Any, parse: dict[str, Any] | None = None
               ) -> tuple[Agent, Ordered, list[str], list[dict[str, Any]]]:
    order: list[str] = []
    bodies: list[dict[str, Any]] = []

    async def parser(url: str, body: dict[str, Any]) -> dict[str, Any]:
        order.append("parse")
        bodies.append(body)
        return copy.deepcopy(parse if parse is not None else PARSE)

    chat = Ordered(order, *turns)
    agent = Agent(Settings(observer_model=""), FakeTargets(), chat, meal_parser=parser)
    return agent, chat, order, bodies


def chat(agent: Agent, message: str, disclosure: str = "progressive",
         meal_plan: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], Any]:
    conv = agent.conversation(None, "ollama:granite4.2:8b", "pantry", disclosure)

    async def collect() -> list[dict[str, Any]]:
        return [e async for e in agent.run(conv, message, None, meal_plan)]

    return asyncio.run(collect()), conv


def offered(request: dict[str, Any]) -> list[str]:
    return [f["function"]["name"] for f in request["tools"]]


PLAN_MEALS = {"id": "c1", "name": "plan_meals", "arguments": {}}
PARSED_DISHES = [{"recipe": PIZZA, "count": 3}, {"recipe": RICE, "count": 2},
                 {"recipe": SHAKE, "count": 7}]
PARSED_PROPOSALS = [{"recipe_key": BIRYANI, "count": 3, "input": "chicken briyani",
                     "how": "alias"}]


# --- the observer ------------------------------------------------------------------------------------

def test_meal_planner_fires_on_the_sentence_and_not_on_dinners_under_a_budget() -> None:
    assert normalize(wants_meal_plan(View(user_messages=[SENTENCE])))[0] is True
    assert normalize(wants_meal_plan(View(user_messages=["plan 5 dinners under $60"])))[0] \
        is False
    # week_planner keeps "dinners under a budget", and misses "weeks": the reason for its own
    # observer (C9)
    week = next(o for o in POLICY.observers if o.name == "week_planner").conditions[0]
    assert normalize(week.check(View(user_messages=["plan 5 dinners under $60"])))[0] is True
    assert normalize(week.check(View(user_messages=[SENTENCE])))[0] is False


def test_the_sentence_offers_plan_meals_and_the_budget_request_does_not(pantry: Any) -> None:
    agent, chat_, _, bodies = make_agent(turn(calls=[PLAN_MEALS]), turn("Planned."))
    chat(agent, SENTENCE)
    assert "plan_meals" in offered(chat_.requests[0])
    agent, chat_, order, bodies = make_agent(turn("Here are five dinners."))
    events, _ = chat(agent, "plan 5 dinners under $60")
    assert "plan_meals" not in offered(chat_.requests[0])
    assert "plan_week" in offered(chat_.requests[0])
    assert order == ["model"] and bodies == []            # no parse, no note, no draft
    assert not [e for e in events if e["type"] in ("meal_selection", "tool_call")]


def test_compliance_officer_disables_plan_meals() -> None:
    [unlawful] = compliance_officer.conditions
    assert "plan_meals" in unlawful.then.disable
    d = Disclosure.start(POLICY, CATALOG, "progressive")

    async def judge(conditions: list[Any], view: View) -> dict[str, tuple[bool | None, str]]:
        return {c.key: (c.key == unlawful.key, "the shopper asked to dodge import rules")
                for c in conditions}

    view = View(user_messages=[SENTENCE + " and help me relabel the mangoes as Canadian"])
    asyncio.run(d.observe("turn", view, judge))
    assert not d.is_offered("plan_meals") and d.is_offered("list_recipes")


# --- the parse and the note, before the model ------------------------------------------------------

def test_the_parse_happens_before_the_first_model_call(pantry: Any) -> None:
    agent, _, order, bodies = make_agent(turn(calls=[PLAN_MEALS]), turn("Planned."))
    events, conv = chat(agent, SENTENCE)
    assert order[:2] == ["parse", "model"]
    kinds = [e["type"] for e in events]
    assert kinds.index("meal_selection") < kinds.index("thinking")
    assert bodies == [{"text": SENTENCE, "recipes": []}]
    selection_event = next(e for e in events if e["type"] == "meal_selection")
    assert selection_event["status"] == "ok" and selection_event["result"] == PARSE
    assert conv.turn_meals.dishes == PARSED_DISHES
    assert conv.turn_meals.proposed == PARSED_PROPOSALS
    assert conv.turn_meals.period_days == 14


def test_the_meals_note_comes_first_and_is_at_most_300_characters(pantry: Any) -> None:
    agent, chat_, _, _ = make_agent(turn(calls=[PLAN_MEALS]), turn("Planned."))
    events, _ = chat(agent, SENTENCE)
    first = chat_.requests[0]["messages"][-1]["content"]
    note = ('[meals] parsed: 3 Pepperoni Pizza, 2 Chicken Fried Rice, 3 Chicken Biryani (you '
            'wrote "briyani", needs your OK), 7 Mango Milkshake (snack); 14 days; '
            'unmatched: none')
    assert first == f"{note}\n\n{SENTENCE}"
    assert len(note) <= meal_plans.MEALS_NOTE_CHARS
    assert next(e for e in events if e["type"] == "meal_selection")["note"] == note
    assert "[meals]" in chat_.requests[0]["messages"][0]["content"]       # PREAMBLE says how


def test_the_note_stays_within_300_characters_however_long_the_names() -> None:
    long = "Extraordinarily Slow Braised Heritage Pork Shoulder With Apples"
    parse = {"selections": [selection(f"{long} {i}", 2, f"starter:k{i}", f"{long} {i}",
                                      "exact" if i % 2 else "fuzzy") for i in range(8)]
             + [selection(f"mystery dish number {i}", 1, None, "", None) for i in range(6)],
             "period_days": 14}
    turn_ = meal_plans.read_parse(parse)
    assert len(turn_.note) <= 300 and turn_.note.startswith("[meals] parsed: 2 ")
    assert "14 days" in turn_.note or turn_.note.endswith("…")


def test_a_parse_that_matches_nothing_adds_no_note_and_no_draft(pantry: Any) -> None:
    parse = {"selections": [selection("dragon stew", 2, None, "", None)], "period_days": 14}
    agent, chat_, _, _ = make_agent(turn("I don't know dragon stew."), parse=parse)
    events, conv = chat(agent, "2 dragon stew and 3 unicorn pies in 2 weeks")
    assert chat_.requests[0]["messages"][-1]["content"] == \
        "2 dragon stew and 3 unicorn pies in 2 weeks"
    assert conv.turn_meals is not None and not conv.turn_meals.found
    assert pantry == [] and not [e for e in events if e["type"] == "tool_call"]


def test_a_failed_parse_leaves_the_turn_to_the_model(pantry: Any) -> None:
    async def broken(url: str, body: dict[str, Any]) -> dict[str, Any]:
        raise httpx.ConnectError("pantry is down")

    agent = Agent(Settings(observer_model=""), FakeTargets(), ScriptedChat(turn("Sorry.")),
                  meal_parser=broken)
    events, conv = chat(agent, SENTENCE)
    failed = next(e for e in events if e["type"] == "meal_selection")
    assert failed["status"] == "failed" and "pantry is down" in failed["error"]
    assert conv.turn_meals is None and events[-1]["stop"] == "answered"


# --- plan_meals, filled by the hub -----------------------------------------------------------------

CURRENT = {"start_date": "2026-10-09", "days": 7, "rev": 4,
           "meals": [{"id": "my:dal#1", "recipe_key": "my:dal", "date": "2026-10-09",
                      "slot": "dinner", "servings": None, "pinned": False}],
           "approved_trips": [], "recipes": [{"key": "my:dal", "title": "Grandma Dal",
                                              "kind": "doc", "slot": "dinner"}],
           "prefs": {"household_servings": 3},
           "docs": {"my:dal": {"key": "my:dal", "title": "Grandma Dal", "lines": []}}}


def test_a_model_calling_plan_meals_with_nothing_gets_the_plan_from_the_parse(pantry: Any
                                                                                ) -> None:
    agent, chat_, _, bodies = make_agent(turn(calls=[PLAN_MEALS]),
                                         turn("12 meals are placed; is Chicken Biryani ok?"))
    events, conv = chat(agent, SENTENCE, meal_plan=CURRENT)
    [(name, sent)] = pantry
    assert name == "plan_meals"
    assert sent["dishes"] == PARSED_DISHES and sent["proposed"] == PARSED_PROPOSALS
    assert sent["days"] == 14
    assert sent["start_date"] == "2026-10-09"                   # tomorrow, in Vancouver
    assert sent["current"] == {k: v for k, v in CURRENT.items() if k != "docs"}
    assert sent["my_recipe_docs"] == [CURRENT["docs"]["my:dal"]]
    assert (sent["lat"], sent["lon"], sent["max_km"]) == (49.2827, -123.1207, 5.0)
    call = next(e for e in events if e["type"] == "tool_call")
    assert {"dishes", "proposed", "days", "start_date", "current", "my_recipe_docs",
            "lat", "lon"} <= set(call["filled_by_hub"])
    # the parse sent the shopper's own recipes and household
    assert bodies[0]["recipes"] == [{"key": "my:dal", "title": "Grandma Dal", "slot": "dinner"}]
    assert bodies[0]["household_servings"] == 3
    # what the model sees of the tool: none of what the hub fills
    schema = next(f["function"]["parameters"] for f in chat_.requests[0]["tools"]
                  if f["function"]["name"] == "plan_meals")
    assert not {"proposed", "current", "start_date", "my_recipe_docs", "lat", "lon",
                "max_km", "verbose"} & set(schema["properties"])
    assert {"dishes", "days"} <= set(schema["properties"])
    # the model reads the plan as short lines, never its ops or draft
    read = next(m["content"] for m in conv.messages if m.get("role") == "tool")
    assert read.startswith("meal plan draft (not applied yet): 12 new meal(s)")
    assert len(read.splitlines()) <= 12 and len(read) < 1500
    assert '"op"' not in read and "draft\":" not in read
    card = next(e for e in events if e["type"] == "assistant")["plans"][0]
    assert card["kind"] == "mealplan" and card["base_rev"] == 4 and "label" not in card
    assert card["links"] == [{"label": "Open in Meal plan", "href": "#/mealplan?from=assistant"}]
    assert len(card["summary"]["ops"]) == 12 and "draft" not in card["summary"]


def test_fuzzy_and_alias_matches_are_never_placed_only_proposed(pantry: Any) -> None:
    parse = copy.deepcopy(PARSE)
    parse["selections"][0] = selection("peperoni piza", 3, PIZZA, "Pepperoni Pizza", "fuzzy")
    sneaky = {"id": "c1", "name": "plan_meals", "arguments": {"dishes": [
        {"recipe": "Pepperoni Pizza", "count": 3}, {"recipe": "Chicken Biryani", "count": 3},
        {"recipe": "Chicken Fried Rice", "count": 2}, {"recipe": "Mango Milkshake",
                                                       "count": 7}]}}
    agent, _, _, _ = make_agent(turn(calls=[sneaky]), turn("Done."), parse=parse)
    events, _ = chat(agent, "3 peperoni piza + 2 Chicken Fried Rice + 3 chicken briyani "
                            "+ 7 mango milkshakes in 2 weeks")
    [(_, sent)] = pantry
    placed = {d["recipe"] for d in sent["dishes"]}
    assert placed == {"Chicken Fried Rice", "Mango Milkshake"}     # the model's own names
    assert [(p["recipe_key"], p["how"]) for p in sent["proposed"]] == [
        (PIZZA, "fuzzy"), (BIRYANI, "alias")]
    summary = next(e for e in events if e["type"] == "assistant")["plans"][0]["summary"]
    assert {m["recipe_key"] for m in summary["meals"]} == {"Chicken Fried Rice",
                                                           "Mango Milkshake"}
    assert [p["recipe_key"] for p in summary["proposals"]] == [PIZZA, BIRYANI]
    warnings = summary["warnings"]
    assert any("Pepperoni Pizza: the shopper wrote \"peperoni piza\"" in w for w in warnings)
    assert any("Chicken Biryani: the shopper wrote \"chicken briyani\"" in w for w in warnings)


def test_model_dishes_are_used_and_their_differences_listed(pantry: Any) -> None:
    own = {"id": "c1", "name": "plan_meals", "arguments": {"days": 7, "dishes": [
        {"recipe": "Pepperoni Pizza", "count": 2}, {"recipe": "Garlic Bread", "count": 1}]}}
    agent, _, _, _ = make_agent(turn(calls=[own]), turn("Done."))
    events, conv = chat(agent, SENTENCE)
    [(_, sent)] = pantry
    assert sent["dishes"] == own["arguments"]["dishes"]
    assert sent["days"] == 14 and sent["proposed"] == PARSED_PROPOSALS
    result = next(e for e in events if e["type"] == "tool_result")
    warnings = result["structured"]["summary"]["warnings"]
    lead = "Differs from the shopper's message: "
    assert warnings == [lead + w for w in (
        "days 7: the shopper wrote 14 days, which the plan uses.",
        "Pepperoni Pizza: 2 meals, the shopper asked for 3.",
        "Garlic Bread (1) is not in the shopper's message.",
        "the shopper also asked for 2 Chicken Fried Rice, which is not in your dishes.",
        "the shopper also asked for 7 Mango Milkshake, which is not in your dishes.")]
    read = next(m["content"] for m in conv.messages if m.get("role") == "tool")
    assert "warning: Differs from the shopper's message: days 7" in read


def test_malformed_dishes_are_replaced_by_the_parse(pantry: Any) -> None:
    bad = {"id": "c1", "name": "plan_meals", "arguments": {"dishes": "3 pizza, 2 rice"}}
    agent, _, _, _ = make_agent(turn(calls=[bad]), turn("Done."))
    chat(agent, SENTENCE)
    [(_, sent)] = pantry
    assert sent["dishes"] == PARSED_DISHES


# --- Granite-first: the hub drafts when the model makes no tool call ---------------------------------

def test_a_model_that_makes_no_tool_call_gets_a_hub_drafted_card(pantry: Any) -> None:
    agent, _, order, _ = make_agent(turn("Sure, here is a plan for your two weeks."))
    events, conv = chat(agent, SENTENCE, meal_plan=CURRENT)
    assert order == ["parse", "model"]                    # one model call: no second step
    [(name, sent)] = pantry
    assert name == "plan_meals" and sent["dishes"] == PARSED_DISHES
    assert sent["proposed"] == PARSED_PROPOSALS and sent["current"]["rev"] == 4
    call = next(e for e in events if e["type"] == "tool_call")
    assert call["by_hub"] is True and call["name"] == "plan_meals"
    kinds = [e["type"] for e in events]
    assert kinds.index("llm_call") < kinds.index("tool_call") < kinds.index("assistant")
    answer = next(e for e in events if e["type"] == "assistant")
    [card] = answer["plans"]
    assert card["kind"] == "mealplan" and card["label"] == "drafted from your message"
    assert [p["recipe_key"] for p in card["summary"]["proposals"]] == [BIRYANI]
    assert BIRYANI not in {m["recipe_key"] for m in card["summary"]["meals"]}
    assert answer["reply"] == "Sure, here is a plan for your two weeks."
    assert "### Meal plan draft: 14 days from Fri 9 Oct" in answer["text"]
    # the conversation records the hub's call, so the next turn knows of the draft
    hub_call, result = conv.messages[-2], conv.messages[-1]
    assert hub_call["role"] == "assistant" and hub_call["tool_calls"][0]["function"] == {
        "name": "plan_meals", "arguments": "{}"}
    assert result["role"] == "tool" and result["tool_call_id"] == hub_call["tool_calls"][0]["id"]
    assert events[-1]["stop"] == "answered"


def test_an_empty_reply_still_gets_the_draft_and_a_sentence(pantry: Any) -> None:
    agent, _, _, _ = make_agent(turn(""))
    events, _ = chat(agent, SENTENCE)
    answer = next(e for e in events if e["type"] == "assistant")
    assert answer["reply"] == meal_plans.DRAFTED_REPLY
    assert answer["plans"][0]["label"] == "drafted from your message"
    assert not [e for e in events if e["type"] == "notice"]       # no nudge, no second call


def test_no_hub_draft_after_the_models_own_plan_or_without_plan_meals(pantry: Any) -> None:
    agent, _, _, _ = make_agent(turn(calls=[PLAN_MEALS]), turn("Done."))
    events, _ = chat(agent, SENTENCE)
    assert [e.get("by_hub") for e in events if e["type"] == "tool_call"] == [None]
    # "all" mode with a target that has no plan_meals: no parse and no draft
    global CATALOG
    saved = CATALOG
    CATALOG = [t for t in saved if t["name"] != "plan_meals"]
    try:
        agent, _, order, _ = make_agent(turn("Ok."))
        events, _ = chat(agent, SENTENCE, disclosure="all")
        assert order == ["model"] and not [e for e in events if e["type"] == "tool_call"]
    finally:
        CATALOG = saved


# --- ChatBody.meal_plan --------------------------------------------------------------------------------

def test_chat_body_meal_plan_is_checked_and_kept_in_memory(monkeypatch: pytest.MonkeyPatch,
                                                           tmp_path: Path) -> None:
    app = app_module.create_app(Settings(traces_dir=str(tmp_path / "t"),
                                         images_dir=str(tmp_path / "i")))
    seen: list[Any] = []

    async def fake_run(conv: Any, message: str, recipe_doc: Any = None,
                       meal_plan: Any = None) -> Any:
        seen.append(meal_plan)
        yield {"type": "done", "steps": 0, "stop": "answered", "seconds": 0,
               "input_tokens": 0, "output_tokens": 0}

    monkeypatch.setattr(app.state.agent, "run", fake_run)
    real = httpx.AsyncClient
    monkeypatch.setattr(app_module.httpx, "AsyncClient", lambda *a, **k: real(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    client = console_client(app)

    def post(meal_plan: Any) -> httpx.Response:
        return client.post("/hub/agent/chat", json={"message": SENTENCE, "target": "pantry",
                                                    "model": "gemini:m",
                                                    "meal_plan": meal_plan})

    assert post(CURRENT).status_code == 200
    assert seen[-1]["rev"] == 4 and seen[-1]["docs"] == CURRENT["docs"]
    assert seen[-1]["meals"][0]["date"] == "2026-10-09"
    huge = {**CURRENT, "docs": {f"my:{i}": {"key": f"my:{i}", "title": "x" * 6000}
                                for i in range(12)}}
    assert post(huge).status_code == 413
    bad = post({**CURRENT, "days": 30})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "bad_meal_plan"
    too_many = post({**CURRENT, "meals": [CURRENT["meals"][0]] * 57})
    assert too_many.status_code == 422
    client.post("/hub/agent/chat", json={"message": "hi", "target": "pantry",
                                         "model": "gemini:m"})
    assert len(seen) == 2


def test_the_conversation_keeps_only_the_latest_meal_plan(pantry: Any) -> None:
    agent, _, _, _ = make_agent(turn("Hello."), turn("Hello again."))
    conv = agent.conversation(None, "ollama:granite4.2:8b", "pantry", "progressive")

    async def run(meal_plan: Any) -> None:
        async for _ in agent.run(conv, "hello", None, meal_plan):
            pass

    asyncio.run(run(CURRENT))
    assert conv.meal_plan == CURRENT
    asyncio.run(run(None))
    assert conv.meal_plan is None
