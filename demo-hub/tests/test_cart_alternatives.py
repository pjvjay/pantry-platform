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

import httpx
import pytest

from demo_hub import agent as agent_module
from demo_hub import app as app_module
from demo_hub.agent import (
    Agent,
    model_copy,
    result_for_model,
)
from demo_hub.answers import plan_for_model
from demo_hub.assistant_policy import POLICY
from demo_hub.disclosure import Disclosure, visible_tools
from demo_hub.mcp_targets import McpTargetError
from demo_hub.settings import Settings
from tests.conftest import console_client
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
        self.targets: list[str] = []

    async def list_tools(self) -> Any:
        return SimpleNamespace(tools=[SimpleNamespace(name=t["name"],
                                                      model_dump=lambda t=t, **_: t)
                                      for t in self.catalog])


# products a fake re-price can pin: id -> (name, price, store)
PRODUCTS = {11: ("Lean Ground Beef 500g", 7.99, "Pantry Mart Downtown"),
            12: ("Extra Lean Ground Beef 450g", 8.49, "GreenLeaf Grocers Kitsilano"),
            13: ("Ground Beef Family Pack 1kg", 9.99, "Pantry Mart Downtown"),
            21: ("Garlic Bulb 3-pack", 2.49, "Pantry Mart Downtown"),
            22: ("Fraser Farms Garlic 200g", 1.99, "Pantry Mart Downtown"),
            31: ("Spaghetti 500g", 1.97, "Pantry Mart Downtown")}


def repriced(basis: dict[str, Any], pins: list[dict[str, Any]]) -> dict[str, Any]:
    """pantry's reprice_plan in miniature: the basis's pins merged with `pins`, a pin equal to
    the planner's pick dropped, each pinned purchase's product, price and store swapped, the
    trip re-totalled ($1.50 travel per store)."""
    planner = {b["line_no"]: b["product_id"] for b in basis["lines"]}
    merged = {p["line_no"]: p["product_id"] for p in [*basis["pins"], *pins]}
    merged = {n: pid for n, pid in merged.items() if pid != planner[n]}
    result = plan_result(basis["recipe_slug"], basis["recipe_name"])
    summary = result["summary"]
    for ln in summary["lines"]:
        pid = merged.get(ln["line_no"])
        if pid is not None:
            name, price, store = PRODUCTS[pid]
            ln.update(product_id=pid, product=name, price=price, store=store, trip_store=store,
                      trip_price=price, confidence=1.0)
    stores = sorted({ln["trip_store"] for ln in summary["lines"]})
    basket = round(sum(ln["price"] for ln in summary["lines"]), 2)
    summary.update(total_cost=basket, notes=[f"line {n}: chosen by the shopper"
                                             for n in sorted(merged)])
    summary["trip"].update(stores=stores, basket_cost=basket, travel_cost=1.5 * len(stores),
                           total_cost=round(basket + 1.5 * len(stores), 2))
    summary["basis"]["pins"] = [{"line_no": n, "product_id": p} for n, p in sorted(merged.items())]
    return result


@pytest.fixture
def pantry(monkeypatch: pytest.MonkeyPatch) -> PantrySession:
    fake = PantrySession()

    @asynccontextmanager
    async def fake_open(target: Any) -> AsyncIterator[PantrySession]:
        fake.targets.append(target.id)
        yield fake

    async def fake_call(session: PantrySession, name: str, arguments: dict[str, Any]
                        ) -> dict[str, Any]:
        session.calls.append((name, copy.deepcopy(arguments)))
        error, structured = "", None
        if name in ("plan_recipe", "plan_from_text"):
            structured = plan_result()
        elif name == "rank_alternatives":
            if arguments["line_no"] not in (1, 2, 3, 4):
                error = f"line {arguments['line_no']} is not a planned line; planned: 1, 2, 3, 4."
            else:
                structured = {"line_no": arguments["line_no"], "items": [], "total": 0}
        elif name == "reprice_plan":
            unknown = [p["product_id"] for p in arguments["pins"]
                       if p["product_id"] not in PRODUCTS]
            if unknown:
                error = f"unknown product id {unknown[0]} in pins."
            else:
                structured = repriced(arguments["basis"], arguments["pins"])
        return {"name": name, "is_error": bool(error), "structured": structured, "text": error,
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


# --- the cart: alternatives, swaps and the next turn -------------------------------------------

def planned(pantry: PantrySession, settings: Settings | None = None,
            target: str = "pantry") -> tuple[Agent, Any]:
    """A conversation whose first turn planned Spaghetti Bolognese (tool_log[0])."""
    agent = Agent(settings or Settings(observer_model=""), FakeTargets(),
                  ScriptedChat(turn(calls=[PLAN_CALL]), turn("Planned.")))
    conv = agent.conversation(None, "gemini:m", target, "all")
    chat_turn(agent, "plan spaghetti bolognese", conv=conv)
    pantry.calls.clear()
    return agent, conv


def call(coro: Any) -> Any:
    return asyncio.run(coro)


def test_alternatives_are_ranked_from_the_hubs_own_basis(pantry: PantrySession) -> None:
    agent, conv = planned(pantry)
    before = json.dumps(conv.messages)
    ranking = call(agent.alternatives(conv.id, 0, 2, limit=5))
    assert ranking == {"line_no": 2, "items": [], "total": 0}
    [(name, sent)] = pantry.calls
    assert name == "rank_alternatives" and pantry.targets[-1] == "pantry"    # direct, always
    assert sent["basis"] == conv.tool_log[0][1]["summary"]["basis"] and sent["limit"] == 5
    # read-only: nothing in the conversation changes
    assert json.dumps(conv.messages) == before and len(conv.tool_log) == 1 and not conv.pending

    def refused(*args: Any) -> tuple[int, str]:
        with pytest.raises(agent_module.CartError) as err:
            call(agent.alternatives(*args))
        return err.value.status, str(err.value)

    assert refused("gone", 0, 1)[0] == 404
    assert refused(conv.id, 7, 1)[0] == 404
    conv.tool_log.append(("find_product", {"items": []}))
    assert refused(conv.id, 1, 1) == (422, "result 1 is not a recipe plan")
    assert refused(conv.id, 0, 9) == (422, "line 9 is not a planned line; planned: 1, 2, 3, 4.")
    off = Agent(Settings(observer_model="", cart_alternatives=False), FakeTargets(),
                ScriptedChat())
    off.conversations[conv.id] = conv
    with pytest.raises(agent_module.CartError) as err:
        call(off.alternatives(conv.id, 0, 1))
    assert err.value.status == 404 and "DEMO_CART_ALTERNATIVES" in str(err.value)


def test_a_plan_without_its_basis_has_no_options(pantry: PantrySession) -> None:
    agent, conv = planned(pantry)
    summary = {k: v for k, v in conv.tool_log[0][1]["summary"].items() if k != "basis"}
    conv.tool_log[0] = ("plan_recipe", {"summary": summary, "full": None})
    with pytest.raises(agent_module.CartError) as err:
        call(agent.alternatives(conv.id, 0, 1))
    assert err.value.status == 422 and "without its basis" in str(err.value)


def test_a_swap_reprices_the_cart_and_waits_for_the_next_turn(pantry: PantrySession) -> None:
    agent, conv = planned(pantry)
    before = json.dumps(conv.messages)
    # line 4 is bought with line 2 (one garlic purchase): the choice is for both lines
    out = call(agent.swap(conv.id, 0, 4, 22))
    [(name, sent)] = pantry.calls
    assert name == "reprice_plan" and sent["basis"]["pins"] == []
    assert sent["pins"] == [{"line_no": 2, "product_id": 22}, {"line_no": 4, "product_id": 22}]
    card = out["card"]
    assert card["kind"] == "plan" and card["ref"] == 1 and card["pinned_lines"] == [2, 4]
    assert "basis" not in card["summary"] and "llm_calls" not in card["summary"]
    assert card["summary"]["lines"][1]["product"] == "Fraser Farms Garlic 200g"
    assert out["note"] == ("[cart] The shopper changed line 2 (garlic + garlic clove) of "
                           "Spaghetti Bolognese in the cart: Garlic Bulb 3-pack -> Fraser Farms "
                           "Garlic 200g. Trip now $13.45 at Pantry Mart Downtown, was $13.95.")
    # one more tool_log entry, the pins it carries, one pending change; history untouched
    assert [n for n, _ in conv.tool_log] == ["plan_recipe", "reprice_plan"]
    assert conv.pins[1] == {2: 22, 4: 22}
    assert list(conv.pending) == [("spaghetti_bolognese", 2)]
    assert json.dumps(conv.messages) == before

    # the next turn: the change is told after "start", before the model reads the shopper
    agent.chat = ScriptedChat(turn("Your trip is now $13.45 at Pantry Mart Downtown."))
    events, _ = chat_turn(agent, "what is my total now?", conv=conv)
    kinds = [e["type"] for e in events]
    assert kinds[:2] == ["start", "cart_change"]
    change = events[1]
    assert change["ref"] == 1 and change["lines"] == [2, 4] and change["line_no"] == 2
    assert change["from"] == {"id": 21, "name": "Garlic Bulb 3-pack"}
    assert change["to"] == {"id": 22, "name": "Fraser Farms Garlic 200g"}
    assert (change["total_before"], change["total_after"]) == (13.95, 13.45)
    assert change["stores_after"] == ["Pantry Mart Downtown"] and change["undone"] is False
    assert change["note"] == out["note"]
    assert "basis" not in change["structured"]["summary"]
    asked = agent.chat.requests[0]["messages"][-1]
    assert asked == {"role": "user", "content": f"{out['note']}\n\nwhat is my total now?"}
    assert conv.user_texts[-1] == "what is my total now?" and not conv.pending
    # the turn's cards are its own plans: the swap's cart is not drawn again
    assert "plans" not in next(e for e in events if e["type"] == "assistant")


def test_swaps_of_one_line_coalesce_and_a_line_put_back_is_not_told(
        pantry: PantrySession) -> None:
    agent, conv = planned(pantry)
    call(agent.swap(conv.id, 0, 1, 12))
    out = call(agent.swap(conv.id, 1, 1, 13))
    assert len(conv.pending) == 1
    [change] = conv.pending.values()
    assert (change.was["id"], change.now["id"]) == (11, 13)             # first to last
    assert change.total_before == 13.95 and change.total_after == 15.95
    assert out["card"]["ref"] == 2 and "Lean Ground Beef 500g -> Ground Beef Family" in out["note"]
    # another line of the same cart: both changes carry the cart's latest figures
    call(agent.swap(conv.id, 2, 3, 31))           # the planner's own pick: nothing to tell
    assert list(conv.pending) == [("spaghetti_bolognese", 1)]
    call(agent.swap(conv.id, 3, 2, 22))
    assert [c.total_after for c in conv.pending.values()] == [15.45, 15.45]
    assert {c.ref for c in conv.pending.values()} == {4}
    # line 1 back to what the model last knew: no longer worth a word
    out = call(agent.swap(conv.id, 4, 1, None))
    assert list(conv.pending) == [("spaghetti_bolognese", 2)] and conv.pins[5] == {2: 22, 4: 22}
    assert out["note"] == ""
    assert out["card"]["pinned_lines"] == [2, 4]


def test_undoing_a_swap_the_model_knows_about_is_told_as_undone(pantry: PantrySession) -> None:
    agent, conv = planned(pantry)
    call(agent.swap(conv.id, 0, 1, 12))
    agent.chat = ScriptedChat(turn("Noted."))
    chat_turn(agent, "ok", conv=conv)            # the model has heard of it
    out = call(agent.swap(conv.id, 1, 1, None))
    [change] = conv.pending.values()
    assert change.undone and (change.was["id"], change.now["id"]) == (12, 11)
    assert "back to the planner's pick, Lean Ground Beef 500g (was Extra Lean" in out["note"]
    assert pantry.calls[-1][1]["pins"] == [] and out["card"]["pinned_lines"] == []


def test_a_swap_is_refused_while_answering_on_a_stale_cart_or_on_the_sim_gateway(
        pantry: PantrySession) -> None:
    agent, conv = planned(pantry)

    def refused(*args: Any) -> tuple[int, str]:
        with pytest.raises(agent_module.CartError) as err:
            call(agent.swap(*args))
        return err.value.status, str(err.value)

    asyncio.run(conv.lock.acquire())
    try:
        assert refused(conv.id, 0, 1, 12) == (
            409, "The assistant is answering; choose again when it finishes.")
    finally:
        conv.lock.release()
    call(agent.swap(conv.id, 0, 1, 12))
    assert refused(conv.id, 0, 1, 13) == (
        409, "This cart is older than the latest plan for this recipe.")
    # pantry's refusal passes through, and nothing changes
    log, pending = len(conv.tool_log), dict(conv.pending)
    assert refused(conv.id, 1, 1, 99) == (422, "unknown product id 99 in pins.")
    assert len(conv.tool_log) == log and conv.pending == pending
    assert refused(conv.id, 1, 8, 12) == (422, "line 8 is not in this cart")
    conv.target = "gateway-sim"
    status, text = refused(conv.id, 1, 1, 13)
    assert status == 409 and "pantry server" in text


def test_the_cart_routes_end_to_end(pantry: PantrySession, tmp_path: Any,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    real = httpx.AsyncClient

    def stores(request: httpx.Request) -> httpx.Response:      # pantry's /stores, for the evals
        return httpx.Response(200, json=[{"id": 1, "name": "Pantry Mart Downtown"},
                                         {"id": 2, "name": "GreenLeaf Grocers Kitsilano"}])

    monkeypatch.setattr(app_module.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(stores)}))
    client = console_client(app_module.create_app(Settings(
        observer_model="", pantry_api_url="http://pantry.test",
        traces_dir=str(tmp_path / "traces"), images_dir=str(tmp_path / "i"))))
    agent = client.app.state.agent  # type: ignore[attr-defined]
    agent.chat = ScriptedChat(turn(calls=[PLAN_CALL]), turn("Planned: $13.95."))

    def stream(message: str, cid: str | None = None) -> list[dict[str, Any]]:
        r = client.post("/hub/agent/chat", json={"message": message, "target": "pantry",
                                                  "disclosure": "all", "conversation_id": cid})
        return [json.loads(x[6:]) for x in r.text.split("\n\n") if x.startswith("data: ")]

    events = stream("plan spaghetti bolognese")
    cid = events[0]["conversation_id"]
    [card] = next(e for e in events if e["type"] == "assistant")["plans"]
    assert card["ref"] == 0 and card["pinned_lines"] == []
    base = f"/hub/agent/conversations/{cid}"

    # Options: a basis in the body is ignored; the hub's own is sent
    r = client.post(f"{base}/alternatives", json={"ref": 0, "line_no": 2, "basis": {"v": 9}})
    assert r.status_code == 200 and r.json()["line_no"] == 2
    assert pantry.calls[-1][1]["basis"]["recipe_slug"] == "spaghetti_bolognese"
    assert pantry.calls[-1][1]["limit"] == 12
    assert client.post(f"{base}/alternatives", json={"ref": 3, "line_no": 1}).status_code == 404
    gone = client.post("/hub/agent/conversations/nope/alternatives", json={"ref": 0, "line_no": 1})
    assert gone.status_code == 404 and "ask again to re-plan" in gone.json()["detail"]
    r = client.post(f"{base}/alternatives", json={"ref": 0, "line_no": 9})
    assert r.status_code == 422 and r.json()["detail"].startswith("line 9 is not a planned line")
    assert client.post(f"{base}/alternatives", json={"ref": 0, "line_no": 1,
                                                     "limit": 26}).status_code == 422

    # Use this
    r = client.post(f"{base}/swap", json={"ref": 0, "line_no": 2, "product_id": 22})
    assert r.status_code == 200 and r.json()["card"]["ref"] == 1
    assert r.json()["note"].startswith("[cart] The shopper changed line 2")
    assert client.post(f"{base}/swap", json={"ref": 0, "line_no": 2,
                                             "product_id": 21}).status_code == 409
    assert client.post(f"{base}/swap", json={"ref": 1, "line_no": 2}).status_code == 422
    r = client.post(f"{base}/swap", json={"ref": 1, "line_no": 1, "product_id": 99})
    assert r.status_code == 422 and r.json()["detail"] == "unknown product id 99 in pins."

    # the next turn tells the model, and its trace keeps the change
    agent.chat = ScriptedChat(turn("Your trip is now $13.45 at Pantry Mart Downtown."))
    events = stream("what is my total now?", cid)
    assert [e["type"] for e in events][:2] == ["start", "cart_change"]
    # the answer quotes the re-priced total and store: grounded by the change it was told
    evals = next(e for e in events if e["type"] == "evals")
    checks = {c["name"]: c["passed"] for c in evals["checks"]}
    assert checks["grounded_money"] and checks["grounded_stores"] and checks["known_tools"]
    trace = client.get(f"/hub/traces/{events[0]['trace_id']}").json()
    [told] = [e for e in trace["spans"][0]["events"] if e["name"] == "cart_change"]
    assert told["attrs"]["total_after"] == 13.45 and "structured" not in told["attrs"]
    metrics = client.get("/hub/metrics").json()
    assert metrics["cart"] == {"changes": 1, "undone": 0, "turns": 1}
    routes = {r["route"] for r in metrics["hub_http"]}
    assert {"POST /hub/agent/conversations/{conversation_id}/swap",
            "POST /hub/agent/conversations/{conversation_id}/alternatives"} <= routes

    # pantry down
    class Down:
        async def __aenter__(self) -> None:
            raise McpTargetError("cannot reach http://127.0.0.1:8000/mcp")

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(agent_module, "open_session", lambda target: Down())
    r = client.post(f"{base}/alternatives", json={"ref": 1, "line_no": 1})
    assert r.status_code == 502 and "pantry is not reachable" in r.json()["detail"]
