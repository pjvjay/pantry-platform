"""The observer SDK, the disclosure engine that runs a policy, the observer-model judge, and the
Assistant's progressive disclosure end to end with a scripted model and a fake MCP session."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from demo_hub import agent as agent_module
from demo_hub.agent import Agent, load_policy
from demo_hub.assistant_policy import POLICY
from demo_hub.disclosure import (
    DISCOVER,
    JUDGE_SYSTEM,
    REPORT_SCHEMA,
    Disclosure,
    canonical,
    judge_prompt,
    parse_reports,
)
from demo_hub.llm import ChatTurn, LLMError
from demo_hub.mcp_targets import Target
from demo_hub.observers import (
    Effects,
    Observer,
    Policy,
    View,
    gte,
    normalize,
    one_of,
    tool_called,
    tool_result,
    user_says,
)
from demo_hub.settings import Settings

GATEWAY = ["pantry-list-recipes", "pantry-find-product", "pantry-get-product", "pantry-get-recipe",
           "pantry-plan-recipe", "pantry-plan-from-text", "pantry-plan-week",
           "pantry-get-product-origins", "pantry-rank-products-by-origin", "fetch-fetch"]
CATALOG = [{"name": n, "description": f"{canonical(n).replace('_', ' ')} tool. More detail.",
            "inputSchema": {"type": "object", "properties": {}}} for n in GATEWAY]


def run_async(coro: Any) -> Any:
    return asyncio.run(coro)


# --- the SDK ----------------------------------------------------------------------------------------

def test_when_chains_effects_and_decorates_effect_functions() -> None:
    desk = Observer("desk", "A desk.")
    cond = desk.when("the shopper asks where food comes from", check=user_says(r"origin")) \
        .enable_tools("get_product_origins").enable_goal("Mind the origin.")
    cond.otherwise.enable_goal("No origin talk.")
    assert (cond.key, cond.kind, cond.on) == ("desk.the_shopper_asks_where_food_comes", "code",
                                              ("turn",))

    @desk.when("a product was found", check=tool_called("find_product"), on="tool_result",
               id="found")
    def _(ctx: Effects) -> None:
        ctx.enable_tools("get_product")
        if "cheap" in ctx.view.last_message:
            ctx.enable_goal("Lead with the price.")

    view = View(user_messages=["something cheap"])
    fired = desk.conditions[1].effects(True, "find_product was called", view)
    assert (fired.enable, fired.goals, fired.evidence) == (["get_product"], ["Lead with the price."],
                                                           "find_product was called")
    assert cond.effects(False, "", view).goals == ["No origin talk."]
    assert desk.when("the shopper is upset").kind == "llm"          # no check: the model reads it


def test_bad_declarations_are_refused() -> None:
    with pytest.raises(ValueError, match="lowercase"):
        Observer("Bad Name")
    with pytest.raises(ValueError, match="unknown trigger"):
        Observer("o", on="end")
    o = Observer("o")
    o.when("x", id="same")
    with pytest.raises(ValueError, match="two conditions named"):
        o.when("y", id="same")


def test_ready_made_checks() -> None:
    view = View(user_messages=["I love penne", "Where is it made?"], tool_calls=["find_product"],
                results={"find_product": {"total": 2, "match": "direct", "summary": {"n": 3}}})
    assert normalize(user_says(r"made in|made\?")(view)) == (True, "matched 'made?'")
    assert normalize(user_says(r"penne")(view))[0] is False              # newest message only
    assert normalize(user_says(r"penne", anywhere=True)(view))[0] is True
    assert normalize(user_says(r"x")(View()))[0] is None
    assert normalize(tool_called("find_*")(view)) == (True, "find_product was called")
    assert normalize(tool_called("plan_*")(view))[0] is False
    found = tool_result("find_product", total=gte(1), match=one_of("direct", "generic"),
                        summary__n=3)
    expected = ("find_product.total = 2, find_product.match = 'direct', "
                "find_product.summary.n = 3")
    assert normalize(found(view)) == (True, expected)
    assert normalize(tool_result("find_product", total=gte(5))(view))[0] is False
    assert normalize(tool_result("plan_recipe")(view)) == (None, "plan_recipe has not answered yet")
    assert normalize(True) == (True, "") and normalize(None) == (None, "")


def test_the_assistant_policy_loads_by_name() -> None:
    assert load_policy("demo_hub.assistant_policy:POLICY") is POLICY
    kinds = {c.kind for c in POLICY.conditions()}
    assert kinds == {"code", "llm"} and POLICY.initial == ["list_recipes", "find_product"]
    with pytest.raises(TypeError, match="not an observers.Policy"):
        load_policy("demo_hub.settings:Settings")


# --- the engine ------------------------------------------------------------------------------------

def make_policy() -> Policy:
    menu = Observer("menu", "Hears dishes.")
    menu.when("a dish", check=user_says(r"\bplan\b"), id="dish") \
        .enable_tools("plan_*").enable_skill("recipe-shopper")
    shelf = Observer("shelf", "Reads results.", on="tool_result")
    shelf.when("found", check=tool_result("find_product", total=gte(1)), id="found") \
        .enable_tools("get_product")
    law = Observer("law", "An officer.")
    law.when("an unlawful request", id="unlawful").disable_tools("plan_*") \
        .enable_goal("Decline the unlawful part.")
    broken = Observer("broken", "Raises.")
    broken.when("never", check=lambda view: 1 / 0, id="boom").enable_tools("plan_week")
    return Policy(initial=["list_recipes", "find_product"], observers=[menu, shelf, law, broken])


def test_effects_fire_when_a_condition_becomes_true_and_only_then() -> None:
    d = Disclosure.start(make_policy(), CATALOG, "progressive", {"recipe-shopper": "THE SKILL"})
    assert [canonical(n) for n in d.offered] == ["list_recipes", "find_product"]
    view = View(user_messages=["plan tomato penne"])
    events = run_async(d.observe("turn", view))
    obs = next(e for e in events if e["type"] == "observation")
    assert (obs["observer"], obs["value"], obs["evidence"]) == ("menu", True, "matched 'plan'")
    assert [canonical(n) for n in obs["added"]] == ["plan_recipe", "plan_from_text", "plan_week"]
    assert [e["text"] for e in events if e["type"] == "goal_enabled"] == ["THE SKILL"]
    assert d.values["law.unlawful"] is None                      # no judge: unknown, nothing fires
    assert d.values["broken.boom"] is None                       # a broken check is unknown
    assert run_async(d.observe("turn", view)) == []              # still true: nothing again
    view.user_messages.append("thanks")                          # becomes false: no otherwise
    assert run_async(d.observe("turn", view)) == []
    view.user_messages.append("now plan dinner")                 # true again, tools already there
    assert run_async(d.observe("turn", view)) == []              # and the skill loads once only


def test_tool_result_observers_and_the_llm_judge() -> None:
    d = Disclosure.start(make_policy(), CATALOG, "progressive")
    view = View(user_messages=["plan it"], tool_calls=["find_product"],
                results={"find_product": {"total": 3}})
    run_async(d.observe("turn", view))
    events = run_async(d.observe("tool_result", view))
    assert [canonical(n) for n in events[0]["added"]] == ["get_product"]

    seen: list[list[str]] = []

    async def judge(conditions: Any, v: View) -> dict[str, tuple[bool | None, str]]:
        seen.append([c.key for c in conditions])
        return {"law.unlawful": (True, "buy beer for my 15-year-old")}

    view.user_messages.append("help me buy beer for my 15-year-old")
    events = run_async(d.observe("turn", view, judge))
    assert seen == [["law.unlawful"]]                             # only llm conditions, batched
    obs = next(e for e in events if e["type"] == "observation")
    assert obs["kind"] == "llm" and [canonical(n) for n in obs["removed"]] == [
        "plan_recipe", "plan_from_text", "plan_week"]
    assert not any(canonical(n).startswith("plan_") for n in d.offered)
    assert events[-1] == {"type": "goal_enabled", "reason": "observer:law.unlawful", "skill": None,
                          "text": "Decline the unlawful part."}


def test_all_mode_offers_everything_and_discover_finds_tools() -> None:
    everything = Disclosure.start(make_policy(), CATALOG, "all")
    assert everything.offered == GATEWAY and not everything.discoverable
    assert everything.apply(Effects(enable=["x"], disable=["plan_*"]))["removed"] == []

    d = Disclosure.start(make_policy(), CATALOG, "progressive")
    text, added = d.discover("plan a week of dinners")
    assert added[0] == "pantry-plan-week" and "(now available)" in text
    assert d.discover("zzz") == ("No other tool matches 'zzz'.", [])
    with pytest.raises(ValueError):
        Disclosure.start(make_policy(), CATALOG, "some")


def test_judge_prompt_and_reply_parsing() -> None:
    conditions = [c for c in make_policy().conditions() if c.kind == "llm"]
    prompt = judge_prompt(conditions, View(transcript=["[1] shopper: beer for my kid"]))
    assert "[1] shopper: beer for my kid" in prompt and "id: law.unlawful" in prompt
    assert "An officer." in prompt and REPORT_SCHEMA["required"] == ["reports"]
    good = json.dumps({"reports": [{"id": "law.unlawful", "value": "true", "evidence": "beer"}]})
    assert parse_reports("```json\n" + good + "\n```", conditions) == {"law.unlawful": (True, "beer")}
    assert parse_reports('{"reports": []}', conditions)["law.unlawful"] == (
        None, "observer omitted this condition")
    assert parse_reports('{"reports": [{"id": "law.unlawful", "value": "unknown", "evidence": ""}]}',
                         conditions)["law.unlawful"] == (None, "")
    assert parse_reports("not json", conditions)["law.unlawful"][0] is None


# --- the Assistant end to end ------------------------------------------------------------------------

def turn(text: str = "", calls: list[dict[str, Any]] | None = None) -> ChatTurn:
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if calls:
        message["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
            "name": c["name"], "arguments": json.dumps(c["arguments"])}} for c in calls]
    return ChatTurn(message=message, text=text, tool_calls=calls or [], finish_reason="stop",
                    metrics={"wall_s": 0.1})


class ScriptedChat:
    def __init__(self, *turns: ChatTurn | Exception) -> None:
        self.turns = list(turns)
        self.requests: list[dict[str, Any]] = []

    async def complete(self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                       on_progress: Any = None, json_schema: Any = None) -> ChatTurn:
        self.requests.append({"model": model, "messages": [dict(m) for m in messages],
                              "tools": [t["function"]["name"] for t in tools],
                              "json_schema": json_schema})
        nxt = self.turns.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class FakeTargets:
    async def resolve(self, target_id: str) -> Target:
        return Target(target_id, target_id, "", "none", "http://mcp.test/mcp")


@pytest.fixture
def mcp(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    class Session:
        async def list_tools(self) -> Any:
            return SimpleNamespace(tools=[SimpleNamespace(name=t["name"],
                                                          model_dump=lambda t=t, **_: t)
                                          for t in CATALOG])

    @asynccontextmanager
    async def fake_open(target: Target) -> AsyncIterator[Session]:
        yield Session()

    async def fake_call(session: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, arguments))
        structured = {"total": 2, "items": [{"name": "Penne Rigate 500g", "price": 1.97}]}
        return {"name": name, "is_error": False, "structured": structured, "text": "", "ms": 1.0,
                "truncated": False}

    monkeypatch.setattr(agent_module, "open_session", fake_open)
    monkeypatch.setattr(agent_module, "call_tool", fake_call)
    return calls


def chat_run(agent: Agent, message: str, disclosure: str | None = None) -> tuple[list[dict[str, Any]], Any]:
    conv = agent.conversation(None, "ollama:m", "gateway-recipes", disclosure)

    async def collect() -> list[dict[str, Any]]:
        return [e async for e in agent.run(conv, message)]

    return asyncio.run(collect()), conv


def test_a_conversation_starts_small_and_observers_grow_it(mcp: Any) -> None:
    chat = ScriptedChat(
        turn(calls=[{"id": "c1", "name": "pantry-find-product", "arguments": {"query": "penne"}}]),
        turn(calls=[{"id": "c2", "name": "pantry-plan-week", "arguments": {}}]),   # not offered
        turn(calls=[{"id": "c3", "name": DISCOVER, "arguments": {"query": "plan a week of dinners"}}]),
        turn("Here is the plan."))
    agent = Agent(Settings(observer_model=""), FakeTargets(), chat)   # type: ignore[arg-type]
    events, _ = chat_run(agent, "Plan tomato penne with nothing from the United States")
    start = events[0]
    assert start["disclosure"] == "progressive" and start["available"] == len(GATEWAY)
    assert start["tools"] == ["pantry-list-recipes", "pantry-find-product"]
    observed = {(e["observer"], e["condition"]) for e in events if e["type"] == "observation"}
    assert {("menu_clerk", "dish_to_cook"), ("shelf_clerk", "product_found")} <= observed
    first = chat.requests[0]["tools"]
    # a library dish with a country left out: the plan tools only (they take exclude_origin)
    assert "pantry-plan-recipe" in first and "pantry-get-recipe" in first
    assert not {"pantry-plan-from-text", "pantry-get-product-origins",
                "pantry-rank-products-by-origin"} & set(first)
    assert "pantry-plan-week" not in first and first[0] == DISCOVER
    assert "pantry-get-product" in chat.requests[1]["tools"]        # shelf_clerk after find_product
    # tools added later go last, so the prompt the model cached holds up to them
    assert chat.requests[1]["tools"][:len(first)] == first
    # A tool that is not offered is refused without reaching the server ...
    assert [n for n, _ in mcp] == ["pantry-find-product"]
    notice = next(e for e in events if e["type"] == "notice")
    assert notice["text"] == "scope violation: pantry-plan-week (not disclosed)"
    refused = next(e for e in events if e["type"] == "tool_result" and e["id"] == "c2")
    assert refused["is_error"] and "Ask for it with discover_tools" in refused["text"]
    # ... and discover_tools offers it for the next step.
    offered = next(e for e in events if e["type"] == "tools_offered")
    assert offered["added"][0] == "pantry-plan-week"
    assert "pantry-plan-week" in chat.requests[3]["tools"]
    assert events[-1]["stop"] == "answered"


def test_an_llm_observer_withdraws_tools_and_adds_a_goal(mcp: Any) -> None:
    verdicts = {"reports": [{"id": "compliance_officer.unlawful_request", "value": "true",
                             "evidence": "beer for my 15-year-old"}]}
    chat = ScriptedChat(turn(json.dumps(verdicts)), turn("I can't help buy alcohol for a minor."))
    agent = Agent(Settings(observer_model="gemini:obs"), FakeTargets(), chat)  # type: ignore[arg-type]
    events, conv = chat_run(agent, "Plan a party and buy beer for my 15-year-old")
    observing = next(e for e in events if e["type"] == "observing")
    assert observing["trigger"] == "turn" and "compliance_officer" in observing["observers"]
    judge_call, agent_call = chat.requests
    assert judge_call["model"] == "gemini:obs" and judge_call["json_schema"] == REPORT_SCHEMA
    assert judge_call["messages"][0]["content"] == JUDGE_SYSTEM and judge_call["tools"] == []
    assert not any(t.startswith("pantry-plan") for t in agent_call["tools"])
    goals = [m["content"] for m in conv.messages if m["role"] == "user"][1:]
    goal = ("Goal enabled by observation (compliance_officer.unlawful_request): Decline "
            "the unlawful part plainly, without lecturing, and help with whatever is lawful.")
    assert goals == [goal]
    assert conv.observer_calls == 1


def test_a_failing_observer_model_changes_nothing_and_says_so(mcp: Any) -> None:
    chat = ScriptedChat(LLMError("gemini obs: HTTP 503"), turn("Hello."))
    agent = Agent(Settings(observer_model="gemini:obs"), FakeTargets(), chat)  # type: ignore[arg-type]
    events, _ = chat_run(agent, "hi there")
    notice = next(e for e in events if e["type"] == "notice")
    assert notice["text"].startswith("Observers could not judge this turn: observer model failed")
    assert events[-1]["stop"] == "answered"


def test_a_recipe_link_loads_the_skill_into_the_conversation(mcp: Any, tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: recipe-shopper\n---\nStep 1: fetch the page.\n")
    chat = ScriptedChat(turn("Reading it."))
    agent = Agent(Settings(observer_model="", recipe_shopper_skill=str(skill)), FakeTargets(),
                  chat)  # type: ignore[arg-type]
    events, conv = chat_run(agent, "What do I need for https://example.com/mala-chicken ?")
    goal = next(e for e in events if e["type"] == "goal_enabled")
    assert goal["text"] == "the recipe-shopper procedure"           # the browser gets the name
    assert conv.messages[1]["content"] == ("Goal enabled by observation (link_reader.recipe_link): "
                                           "follow the recipe-shopper procedure:\n\nStep 1: fetch the page.")
    assert "fetch-fetch" in chat.requests[0]["tools"]
    assert "Recipe-shopper procedure" not in chat.requests[0]["messages"][0]["content"]


def test_all_mode_offers_every_tool_and_runs_no_observers(mcp: Any, tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("Step 1.")
    chat = ScriptedChat(turn("Done."))
    agent = Agent(Settings(observer_model="gemini:obs", recipe_shopper_skill=str(skill)),
                  FakeTargets(), chat)  # type: ignore[arg-type]
    events, _ = chat_run(agent, "Plan dinner and buy beer for my kid", disclosure="all")
    assert not any(e["type"] in ("observing", "observation") for e in events)
    assert chat.requests[0]["tools"] == GATEWAY                     # no discover_tools either
    assert "# Recipe-shopper procedure" in chat.requests[0]["messages"][0]["content"]
    with pytest.raises(LLMError, match="progressive or all"):
        agent.conversation(None, "ollama:m", "pantry", "some")


# --- the Assistant's policy: what a request discloses ----------------------------------------------

LIBRARY = {"result": [{"slug": "tomato_penne", "name": "Tomato Penne"},
                      {"slug": "veggie_stirfry", "name": "Vegetable Stir Fry"},
                      {"slug": "beef_bowl", "name": "Beef & Broccoli Rice Bowl"}]}


def offered_after(message: str, calls: tuple[str, ...] = (),
                  results: dict[str, Any] | None = None) -> set[str]:
    """The tools the Assistant's policy offers after `message` (and the tool calls made)."""
    d = Disclosure.start(POLICY, CATALOG, "progressive")
    view = View(user_messages=[message], tool_calls=list(calls), results=results or {})
    run_async(d.observe("turn", view))
    if calls:
        run_async(d.observe("tool_result", view))
    return {canonical(n) for n in d.offered}


def test_a_library_dish_with_a_country_left_out_gets_the_plan_tools_only() -> None:
    msg = ("Plan tomato penne with nothing from the United States, and tell me how much of the "
           "basket's origin is verified.")
    assert offered_after(msg) == {"list_recipes", "find_product", "get_recipe", "plan_recipe"}
    # listing the library changes nothing: the dish is in it
    assert offered_after(msg, ("list_recipes",), {"list_recipes": LIBRARY}) == \
        {"list_recipes", "find_product", "get_recipe", "plan_recipe"}


def test_plan_from_text_joins_only_when_the_library_cannot_serve_the_dish() -> None:
    risotto = "I'd like to cook a mushroom risotto for 4"
    assert "plan_from_text" not in offered_after(risotto)
    assert "plan_from_text" in offered_after(risotto, ("list_recipes",),
                                             {"list_recipes": LIBRARY})
    # a lookup that failed (unknown slug): no structured result
    assert "plan_from_text" in offered_after("plan mushroom risotto", ("plan_recipe",))
    # just browsing the library is not cooking
    assert "plan_from_text" not in offered_after("what recipes do you have?", ("list_recipes",),
                                                 {"list_recipes": LIBRARY})


@pytest.mark.parametrize("message,added", [
    ("Where does the garlic come from?", {"get_product_origins"}),
    ("What's the origin of the penne?", {"get_product_origins"}),
    ("Is the olive oil made in Italy?", {"get_product_origins"}),
    ("I'd prefer Canadian olive oil", {"rank_products_by_origin"}),
    ("Rank the cheeses by origin", {"rank_products_by_origin"}),
    ("Which products are from Mexico?", {"rank_products_by_origin"}),
    ("find penne with nothing from the United States", set()),
])
def test_origin_questions_get_the_one_tool_they_need(message: str, added: set[str]) -> None:
    origin_tools = {"get_product_origins", "rank_products_by_origin"}
    assert offered_after(message) & origin_tools == added


def test_with_stable_tools_a_local_model_keeps_its_tools_block(mcp: Any) -> None:
    chat = ScriptedChat(
        turn(calls=[{"id": "c1", "name": "pantry-find-product", "arguments": {"query": "penne"}}]),
        turn(calls=[{"id": "c2", "name": "pantry-get-product", "arguments": {"product_id": 1}}]),
        turn("Penne Rigate 500g is $1.97."))
    agent = Agent(Settings(observer_model="", local_stable_tools=True), FakeTargets(), chat)  # type: ignore[arg-type]
    events, conv = chat_run(agent, "cheapest penne?")
    # the shelf clerk offers get_product after find_product: announced, the block unchanged
    assert chat.requests[1]["tools"] == chat.requests[0]["tools"] == chat.requests[2]["tools"]
    announcement = chat.requests[1]["messages"][-1]
    assert announcement["role"] == "user" and announcement["content"].startswith(
        "More tools are now available") and "pantry-get-product" in announcement["content"]
    # it can call the announced tool, and the prompt only ever grew
    assert [n for n, _ in mcp] == ["pantry-find-product", "pantry-get-product"]
    first, second = chat.requests[1]["messages"], chat.requests[2]["messages"]
    assert second[:len(first)] == first
    assert any(e["type"] == "notice" and "pantry-get-product" in e["text"] for e in events)
    assert "pantry-get-product" in conv.announced


def test_asking_what_the_library_holds_is_not_asking_to_cook() -> None:
    from demo_hub.assistant_policy import dish_not_in_library, wants_a_dish
    listing = View(user_messages=["Which recipes can you plan for me?"], tool_calls=["list_recipes"],
                   results={"list_recipes": {"result": [{"name": "Tomato Penne"}]}}, transcript=[])
    assert wants_a_dish(listing)[0] is False and dish_not_in_library(listing)[0] is False
    cooking = View(user_messages=["Plan a stir-fry for three dinners"], tool_calls=["list_recipes"],
                   results=listing.results, transcript=[])
    assert wants_a_dish(cooking)[0] is True and dish_not_in_library(cooking)[0] is True


def test_a_new_dish_after_a_library_one_is_not_in_the_library() -> None:
    from demo_hub.assistant_policy import dish_not_in_library
    listed = {"list_recipes": {"result": [{"name": "Tomato Penne"}]}}
    follow_up = View(user_messages=["Plan tomato penne with nothing from the United States",
                                    "Can you create a similar recipe with fish?"],
                     tool_calls=["list_recipes"], results=listed, transcript=[])
    assert dish_not_in_library(follow_up)[0] is True        # the newest message names no recipe
    same = View(user_messages=["Plan tomato penne"], tool_calls=["list_recipes"],
                results=listed, transcript=[])
    assert dish_not_in_library(same)[0] is False
