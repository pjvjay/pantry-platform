"""Cart alternatives in the hub: the plan's basis kept server-side and hidden from the model,
the cart's follow-up tools hidden from it too."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from demo_hub import agent as agent_module
from demo_hub.agent import (
    Agent,
    model_copy,
    result_for_model,
)
from demo_hub.answers import plan_for_model
from demo_hub.assistant_policy import POLICY
from demo_hub.disclosure import Disclosure, visible_tools
from demo_hub.settings import Settings
from tests.test_agent import FakeTargets, ScriptedChat, turn


def _tool(name: str, *props: str) -> dict[str, Any]:
    return {"name": name, "description": f"{name}.",
            "inputSchema": {"type": "object", "properties": {p: {} for p in props}}}


PLAN_TOOL = _tool("plan_recipe", "slug", "lat", "lon", "max_km", "verbose", "exclude_origin",
                  "preference", "basis")
CATALOG = [_tool("list_recipes"), _tool("find_product", "query", "lat", "lon"), PLAN_TOOL,
           _tool("plan_from_text", "recipe_text", "lat", "lon", "max_km", "basis"),
           _tool("rank_alternatives", "basis", "line_no", "limit"),
           _tool("reprice_plan", "basis", "pins")]


def line(line_no: int, ingredient: str, pid: int, product: str, price: float,
         also: list[int] | None = None) -> dict[str, Any]:
    return {"line_no": line_no, "ingredient": ingredient, "product_id": pid, "product": product,
            "brand": "Demo", "size": "500g", "store": "Pantry Mart Downtown", "price": price,
            "confidence": 0.9, "origin_country": "", "origin_status": "none", "match": "exact",
            "also_lines": also or [], "packs": 1, "trip_store": "Pantry Mart Downtown",
            "trip_price": price}


def plan_result(slug: str = "spaghetti_bolognese", name: str = "Spaghetti Bolognese"
                ) -> dict[str, Any]:
    lines = [line(1, "ground beef", 11, "Lean Ground Beef 500g", 7.99),
             line(2, "garlic + garlic clove", 21, "Garlic Bulb 3-pack", 2.49, also=[4]),
             line(3, "spaghetti", 31, "Spaghetti 500g", 1.97)]
    basket = round(sum(ln["price"] for ln in lines), 2)
    return {"summary": {
        "recipe_slug": slug, "recipe_name": name, "total_cost": basket,
        "origin_status": "not_requested", "coverage": None, "lines": lines,
        "trip": {"stores": ["Pantry Mart Downtown"], "basket_cost": basket, "travel_cost": 1.5,
                 "total_cost": round(basket + 1.5, 2), "stops": 1, "items": []},
        "notes": [], "not_stocked": [], "out_of_range": [], "skipped": [],
        "llm_cost_usd": 0.0, "latency_ms": 12, "llm_calls": [], "burr_run": "run-1",
        "pipeline": {"build_plan": 3.0},
        "basis": {"v": 1, "path": "library", "recipe_slug": slug, "recipe_name": name,
                  "lines": [{"line_no": 1, "name": "ground beef", "product_id": 11},
                            {"line_no": 2, "name": "garlic", "product_id": 21},
                            {"line_no": 3, "name": "spaghetti", "product_id": 31},
                            {"line_no": 4, "name": "garlic clove", "product_id": 21}],
                  "lat": 49.2827, "lon": -123.1207, "max_km": 5.0, "pins": []}},
        "full": None}


class PantrySession:
    """pantry's MCP server as the agent and the hub's cart routes see it."""

    def __init__(self, catalog: list[dict[str, Any]] | None = None) -> None:
        self.catalog = catalog or CATALOG
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> Any:
        return SimpleNamespace(tools=[SimpleNamespace(name=t["name"],
                                                      model_dump=lambda t=t, **_: t)
                                      for t in self.catalog])


@pytest.fixture
def pantry(monkeypatch: pytest.MonkeyPatch) -> PantrySession:
    fake = PantrySession()

    @asynccontextmanager
    async def fake_open(target: Any) -> AsyncIterator[PantrySession]:
        yield fake

    async def fake_call(session: PantrySession, name: str, arguments: dict[str, Any]
                        ) -> dict[str, Any]:
        session.calls.append((name, copy.deepcopy(arguments)))
        structured = plan_result() if name in ("plan_recipe", "plan_from_text") else None
        return {"name": name, "is_error": False, "structured": structured, "text": "",
                "ms": 1.0, "truncated": False}

    monkeypatch.setattr(agent_module, "open_session", fake_open)
    monkeypatch.setattr(agent_module, "call_tool", fake_call)
    return fake


def chat_turn(agent: Agent, message: str, model: str = "gemini:m", disclosure: str = "all",
              conv: Any = None) -> tuple[list[dict[str, Any]], Any]:
    conv = conv or agent.conversation(None, model, "pantry", disclosure)

    async def collect() -> list[dict[str, Any]]:
        return [e async for e in agent.run(conv, message)]

    return asyncio.run(collect()), conv


PLAN_CALL = {"id": "c1", "name": "plan_recipe", "arguments": {"slug": "spaghetti_bolognese"}}


@pytest.mark.parametrize("model", ["gemini:m", "ollama:m"])
def test_the_hub_asks_for_the_basis_and_only_it_keeps_it(pantry: PantrySession,
                                                         model: str) -> None:
    chat = ScriptedChat(turn(calls=[PLAN_CALL]), turn("The trip is $13.95 at Pantry Mart."))
    events, conv = chat_turn(Agent(Settings(observer_model=""), FakeTargets(), chat), "plan it",
                             model=model)
    # pantry is asked for the basis; the event says the hub filled it in
    [(name, sent)] = pantry.calls
    assert name == "plan_recipe" and sent["basis"] is True
    call = next(e for e in events if e["type"] == "tool_call")
    assert "basis" in call["filled_by_hub"]
    # the model never sees the argument, in either schema shape
    offered = {f["function"]["name"]: f["function"]["parameters"] for f in chat.requests[0]["tools"]}
    assert "basis" not in offered["plan_recipe"]["properties"]
    assert "basis" not in offered["plan_from_text"]["properties"]
    # nor the cart's follow-up tools
    assert not {"rank_alternatives", "reprice_plan"} & offered.keys()
    # nor the basis in what it reads, as JSON or as a local model's lines
    tool_message = next(m for m in conv.messages if m["role"] == "tool")["content"]
    assert "basis" not in tool_message and "Lean Ground Beef 500g" in tool_message
    # the browser's event, its trace and the cards go without it too; tool_log keeps it
    result = next(e for e in events if e["type"] == "tool_result")
    assert "basis" not in result["structured"]["summary"]
    assert result["structured"]["summary"]["burr_run"] == "run-1"     # trace views still get it
    [card] = next(e for e in events if e["type"] == "assistant")["plans"]
    assert "basis" not in card["summary"] and "llm_calls" not in card["summary"]
    assert card["ref"] == 0 and card["pinned_lines"] == []       # its index in tool_log
    assert conv.tool_log[0][1]["summary"]["basis"]["recipe_slug"] == "spaghetti_bolognese"


def test_a_gateway_with_an_older_schema_is_never_sent_the_flag(pantry: PantrySession) -> None:
    pantry.catalog = [_tool("plan_recipe", "slug", "lat", "lon", "max_km")]
    model_sent = {**PLAN_CALL, "arguments": {"slug": "s", "basis": True}}
    chat = ScriptedChat(turn(calls=[model_sent]), turn("Done."))
    chat_turn(Agent(Settings(observer_model=""), FakeTargets(), chat), "plan it")
    assert "basis" not in pantry.calls[0][1]


def test_cart_alternatives_off_sends_no_basis(pantry: PantrySession) -> None:
    model_sent = {**PLAN_CALL, "arguments": {"slug": "s", "basis": True}}
    chat = ScriptedChat(turn(calls=[model_sent]), turn("Done."))
    agent = Agent(Settings(observer_model="", cart_alternatives=False), FakeTargets(), chat)
    chat_turn(agent, "plan it")
    assert "basis" not in pantry.calls[0][1]       # the model's own value is not passed on


def test_with_hub_args_sets_basis_on_plan_tools_only() -> None:
    agent = Agent(Settings(observer_model="", shopper_location=None), FakeTargets(),
                  ScriptedChat())
    assert agent._with_hub_args("pantry-plan-from-text", {"recipe_text": "x"}, True) == {
        "recipe_text": "x", "basis": True}
    assert agent._with_hub_args("plan_recipe", {"slug": "s", "basis": False}, True)["basis"]
    assert agent._with_hub_args("plan_recipe", {"slug": "s", "basis": True}, False) == {
        "slug": "s"}
    assert agent._with_hub_args("find_product", {"query": "x"}, True) == {"query": "x"}
    # with no location set, basis is still hidden from the plan tools
    [shown] = agent._plan_tools([PLAN_TOOL], "gemini:g")
    assert "basis" not in shown["inputSchema"]["properties"]
    assert {"lat", "lon"} <= shown["inputSchema"]["properties"].keys()


def test_hidden_tools_are_never_offered_or_discovered(pantry: PantrySession) -> None:
    assert [t["name"] for t in visible_tools(POLICY, CATALOG)] == [
        "list_recipes", "find_product", "plan_recipe", "plan_from_text"]
    for mode in ("progressive", "all"):
        d = Disclosure.start(POLICY, CATALOG, mode)
        assert not {"rank_alternatives", "reprice_plan"} & set(d.offered)
        if d.discoverable:
            _, added = d.discover("rank alternatives and reprice a plan")
            assert not {"rank_alternatives", "reprice_plan"} & set(added)
    # a model that calls one anyway is refused, as for any tool it is not allowed
    call = {"id": "c1", "name": "reprice_plan", "arguments": {"pins": []}}
    chat = ScriptedChat(turn(calls=[call]), turn("Sorry."))
    events, _ = chat_turn(Agent(Settings(observer_model=""), FakeTargets(), chat), "reprice")
    assert pantry.calls == []
    assert any(e["type"] == "notice" and e["text"] == "scope violation: reprice_plan (not allowed)"
               for e in events)


def test_the_model_copy_leaves_out_server_and_browser_fields() -> None:
    result = plan_result()
    result["full"] = {"line_items": [], "basis": {"v": 1}}
    result["summary"]["nutrition"] = {"per_portion": {"kcal": 610}, "lines": [{"kcal": 1}]}
    result["summary"]["days"] = [{"nutrition": {"lines": [1], "kcal": 2}}, {"name": "x"}]
    copy_ = model_copy(result)
    assert "basis" not in copy_["summary"] and "basis" not in copy_["full"]
    assert not {"llm_calls", "burr_run", "pipeline"} & copy_["summary"].keys()
    assert copy_["summary"]["nutrition"] == {"per_portion": {"kcal": 610}}
    assert copy_["summary"]["days"] == [{"nutrition": {"kcal": 2}}, {"name": "x"}]
    assert "basis" in result["summary"] and result["summary"]["nutrition"]["lines"]   # untouched
    text = result_for_model({"structured": result})
    assert "basis" not in json.loads(text)["summary"]
    assert "basis" not in (plan_for_model(result) or "")
    assert model_copy("plain text") == "plain text" and model_copy(None) is None
