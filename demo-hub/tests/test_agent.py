"""The Assistant's tool-use loop with a scripted model and a fake MCP session."""

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
from demo_hub.agent import Agent, load_skill, openai_tools, result_for_model, system_prompt
from demo_hub.llm import ChatTurn, LLMError, ModelUnavailable, QuotaExhausted
from demo_hub.mcp_targets import McpTargetError, Target
from demo_hub.settings import Settings

TOOL = {"name": "pantry-find-product", "description": "Find a product.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}}


def turn(text: str = "", calls: list[dict[str, Any]] | None = None) -> ChatTurn:
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if calls:
        message["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
            "name": c["name"], "arguments": json.dumps(c["arguments"])}} for c in calls]
    return ChatTurn(message=message, text=text, tool_calls=calls or [], finish_reason="stop",
                    input_tokens=10, output_tokens=2, metrics={"wall_s": 0.5})


class ScriptedChat:
    def __init__(self, *turns: ChatTurn | Exception) -> None:
        self.turns = list(turns)
        self.requests: list[dict[str, Any]] = []

    async def complete(self, model: str, messages: list[dict[str, Any]],
                       tools: list[dict[str, Any]], on_progress: Any = None) -> ChatTurn:
        self.requests.append({"model": model, "messages": [dict(m) for m in messages], "tools": tools})
        if on_progress:
            on_progress({"phase": "reading", "eta_s": 2.0})
            await asyncio.sleep(0)
            on_progress({"phase": "writing", "tokens": 3, "eta_s": 1.0})
        nxt = self.turns.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class FakeTargets:
    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail

    async def resolve(self, target_id: str) -> Target:
        if self.fail:
            raise self.fail
        return Target(target_id, target_id, "", "none", "http://mcp.test/mcp")


class FakeSession:
    def __init__(self, results: dict[str, Any]) -> None:
        self.results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> Any:
        return SimpleNamespace(tools=[SimpleNamespace(name=TOOL["name"], model_dump=lambda **_: TOOL)])


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    fake = FakeSession({"pantry-find-product": {"items": [{"name": "Penne Rigate 500g", "price": 1.97}]}})

    @asynccontextmanager
    async def fake_open(target: Target) -> AsyncIterator[FakeSession]:
        yield fake

    async def fake_call(session: FakeSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        session.calls.append((name, arguments))
        if arguments.get("explode"):
            raise RuntimeError("server exploded")
        return {"name": name, "is_error": False, "structured": session.results.get(name),
                "text": "", "ms": 1.0, "truncated": False}

    monkeypatch.setattr(agent_module, "open_session", fake_open)
    monkeypatch.setattr(agent_module, "call_tool", fake_call)
    return fake


def run(agent: Agent, message: str, model: str = "gemini:m",
        target: str = "gateway-recipes") -> tuple[list[dict[str, Any]], Any]:
    conv = agent.conversation(None, model, target)

    async def collect() -> list[dict[str, Any]]:
        return [e async for e in agent.run(conv, message)]

    return asyncio.run(collect()), conv


def test_a_tool_call_then_an_answer(session: FakeSession) -> None:
    chat = ScriptedChat(
        turn(calls=[{"id": "c1", "name": "pantry-find-product", "arguments": {"query": "penne"}}]),
        turn("Penne Rigate 500g costs $1.97."))
    events, conv = run(Agent(Settings(observer_model=""), FakeTargets(), chat), "cheapest penne?")
    assert [e["type"] for e in events] == [
        "start", "thinking", "progress", "progress", "llm_call", "tool_call", "tool_result",
        "thinking", "progress", "progress", "llm_call", "assistant", "done"]
    assert events[0]["tools"] == ["pantry-find-product"]
    assert events[1] == {"type": "thinking", "step": 1, "model": "gemini:m"}
    assert events[2] == {"type": "progress", "step": 1, "model": "gemini:m", "phase": "reading",
                         "eta_s": 2.0}
    assert events[3]["phase"] == "writing" and events[3]["tokens"] == 3
    assert events[4] == {"type": "llm_call", "step": 1, "model": "gemini:m", "tool_calls": 1,
                         "prompt_tokens": 10, "output_tokens": 2, "wall_s": 0.5}
    assert events[5]["arguments"] == {"query": "penne"}
    assert events[6]["structured"]["items"][0]["price"] == 1.97
    assert events[-1] | {"seconds": 0} == {"type": "done", "steps": 2, "stop": "answered",
                                         "seconds": 0, "input_tokens": 20, "output_tokens": 4}
    assert session.calls == [("pantry-find-product", {"query": "penne"})]
    # The second model call sees the system prompt, the user, the assistant's call and the result.
    second = chat.requests[1]["messages"]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "tool"]
    assert second[3]["tool_call_id"] == "c1" and "1.97" in second[3]["content"]
    # discover_tools first, then the tools in the order they were offered
    assert [f["function"]["name"] for f in chat.requests[0]["tools"]][:2] == [
        "discover_tools", "pantry-find-product"]
    # The conversation keeps its history for the next turn.
    assert [m["role"] for m in conv.messages] == ["user", "assistant", "tool", "assistant"]


def test_a_local_model_reads_a_shorter_result(session: FakeSession) -> None:
    session.results["pantry-find-product"] = {
        "items": [{"name": f"Penne {i}", "price": 1.97, "about": "x" * 40} for i in range(40)]}
    sizes = {}
    for model in ("ollama:m", "gemini:m"):
        chat = ScriptedChat(
            turn(calls=[{"id": "c1", "name": "pantry-find-product", "arguments": {"query": "p"}}]),
            turn("done"))
        settings = Settings(observer_model="", local_result_chars=600)
        events, _ = run(Agent(settings, FakeTargets(), chat), "penne?", model=model)
        content = chat.requests[1]["messages"][3]["content"]
        sizes[model] = len(content)
        assert json.loads(content)["items"][0]["name"] == "Penne 0"
        # the browser still gets the whole result
        assert len(next(e for e in events if e["type"] == "tool_result")["structured"]["items"]) == 40
    assert sizes["ollama:m"] <= 600 < sizes["gemini:m"]


def test_a_failing_tool_is_reported_to_the_model_not_raised(session: FakeSession) -> None:
    chat = ScriptedChat(turn(calls=[{"id": "c1", "name": "pantry-find-product",
                                     "arguments": {"explode": True}}]), turn("Sorry."))
    events, conv = run(Agent(Settings(observer_model=""), FakeTargets(), chat), "go")
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["is_error"] and "server exploded" in result["text"]
    assert conv.messages[2]["content"].startswith("ERROR: RuntimeError: server exploded")


def test_the_step_budget_ends_a_loop(session: FakeSession) -> None:
    call = {"id": "c", "name": "pantry-find-product", "arguments": {"query": "x"}}
    chat = ScriptedChat(*[turn(calls=[call]) for _ in range(3)])
    events, _ = run(Agent(Settings(observer_model="", agent_max_steps=3), FakeTargets(), chat), "loop")
    assert events[-1]["stop"] == "step budget reached" and events[-1]["steps"] == 3


def test_a_model_error_is_an_error_event(session: FakeSession) -> None:
    chat = ScriptedChat(LLMError("gemini m: HTTP 429 after 4 attempts"))
    events, _ = run(Agent(Settings(observer_model=""), FakeTargets(), chat), "hi")
    assert [e["type"] for e in events] == ["start", "thinking", "progress", "progress", "error",
                                         "done"]
    assert "429" in events[4]["message"] and events[5]["stop"] == "error"


def test_an_unreachable_target_is_an_error_event(session: FakeSession) -> None:
    events, _ = run(Agent(Settings(observer_model=""), FakeTargets(McpTargetError("HTTP 401", 401)), ScriptedChat()), "hi")
    assert [e["type"] for e in events] == ["error", "done"]


def test_a_busy_conversation_refuses_a_second_turn(session: FakeSession) -> None:
    agent = Agent(Settings(observer_model=""), FakeTargets(), ScriptedChat())
    conv = agent.conversation(None, "gemini:m", "pantry")

    async def go() -> list[dict[str, Any]]:
        await conv.lock.acquire()
        return [e async for e in agent.run(conv, "again")]

    assert asyncio.run(go()) == [{"type": "error", "message": "this conversation is already answering"}]


def test_conversations_are_reused_and_bounded() -> None:
    agent = Agent(Settings(observer_model=""), FakeTargets(), ScriptedChat())
    first = agent.conversation(None, "gemini:a", "pantry")
    again = agent.conversation(first.id, "gemini:b", "gateway-sim")
    assert again is first and (again.model, again.target) == ("gemini:b", "gateway-sim")
    for _ in range(agent_module.MAX_CONVERSATIONS + 5):
        agent.conversation(None, "gemini:a", "pantry")
    assert len(agent.conversations) == agent_module.MAX_CONVERSATIONS
    assert first.id not in agent.conversations


@pytest.mark.parametrize(("model", "target", "needle"), [
    ("gpt-4", "pantry", "gemini:<model>"), ("gemini:m", "pantry-anon", "the assistant can use")])
def test_bad_model_or_target(model: str, target: str, needle: str) -> None:
    with pytest.raises(LLMError, match=needle):
        Agent(Settings(observer_model=""), FakeTargets(), ScriptedChat()).conversation(None, model, target)


def test_skill_loading(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: recipe-shopper\n---\n# Procedure\nCall plan_from_text.\n")
    assert load_skill(str(skill)) == "# Procedure\nCall plan_from_text."
    assert load_skill("") == "" and load_skill(str(tmp_path / "missing.md")) == ""
    prompt = system_prompt(Settings(observer_model="", recipe_shopper_skill=str(skill)))
    assert "never invent" in prompt and "# Recipe-shopper procedure" in prompt
    assert "# Recipe-shopper procedure" not in system_prompt(Settings(observer_model=""))


def test_tool_and_result_shaping() -> None:
    [fn] = openai_tools([{"name": "t", "description": "d" * 2000}])
    assert fn["function"]["parameters"] == {"type": "object", "properties": {}}
    assert len(fn["function"]["description"]) == 1024
    assert result_for_model({"structured": {"a": 1}, "text": "x"}) == '{"a": 1}'
    assert result_for_model({"structured": None, "text": "plain", "is_error": True}) == "ERROR: plain"
    assert len(result_for_model({"structured": None, "text": "x" * 50_000})) == agent_module.RESULT_CHARS_FOR_MODEL
    # pantry's per-call LLM trace is for the browser, not the model
    plan = {"summary": {"total_cost": 9.5, "llm_calls": [{"step": "select_products"}],
                        "burr_run": "run-tomato_penne-20261004-231902-ab12cd"}, "full": None}
    assert json.loads(result_for_model({"structured": plan})) == \
        {"summary": {"total_cost": 9.5}, "full": None}
    assert plan["summary"]["llm_calls"]          # the browser's copy is untouched


def test_an_oversized_result_is_shrunk_to_valid_json_that_says_what_it_left_out() -> None:
    # get_product_origins over the whole catalog: 18,000+ characters
    page = {"by_status": {"resolved": 60, "unknown": 101}, "total": 60,
            "items": [{"product_id": i, "name": f"Product {i}", "status": "resolved",
                       "country": "Italy", "verbatim": "Product of Italy " * 10}
                      for i in range(60)]}
    full = json.dumps(page)
    assert len(full) > 16_000
    text = result_for_model({"structured": page}, 4_000)
    shrunk = json.loads(text)                     # still valid JSON
    assert len(text) <= 4_000
    assert shrunk["by_status"] == page["by_status"] and shrunk["total"] == 60
    kept = [i for i in shrunk["items"] if isinstance(i, dict)]
    assert kept == page["items"][:len(kept)]
    assert shrunk["items"][-1] == (f"... {60 - len(kept)} more not shown: ask for fewer "
                                   "(ids, a search, a smaller limit)")
    assert len(page["items"]) == 60              # the browser's copy is untouched
    # small results and plain text are as before
    assert result_for_model({"structured": {"a": [1, 2, 3]}}, 4_000) == '{"a": [1, 2, 3]}'
    assert result_for_model({"structured": None, "text": "x" * 9_000}, 4_000) == "x" * 4_000


def test_an_exhausted_model_falls_back_to_the_next(session: FakeSession) -> None:
    settings = Settings(observer_model="", agent_fallbacks=("gemini:a", "gemini:b", "gemini:c"))
    chat = ScriptedChat(QuotaExhausted("gemini a is out of free-tier quota", 9000),
                        QuotaExhausted("gemini b is out of free-tier quota", 9000), turn("Hello."))
    events, conv = run(Agent(settings, FakeTargets(), chat), "hi", model="gemini:a")
    notices = [e["text"] for e in events if e["type"] == "notice"]
    assert notices == ["gemini a is out of free-tier quota Switching to gemini:b.",
                       "gemini b is out of free-tier quota Switching to gemini:c."]
    assert [r["model"] for r in chat.requests] == ["gemini:a", "gemini:b", "gemini:c"]
    assert [e["model"] for e in events if e["type"] == "thinking"] == ["gemini:a", "gemini:b", "gemini:c"]
    assert conv.model == "gemini:c" and events[-1]["stop"] == "answered"


def test_when_every_fallback_is_exhausted_the_error_is_reported(session: FakeSession) -> None:
    settings = Settings(observer_model="", agent_fallbacks=("gemini:a",))
    chat = ScriptedChat(QuotaExhausted("gemini a is out of free-tier quota", 9000))
    events, _ = run(Agent(settings, FakeTargets(), chat), "hi", model="gemini:a")
    assert [e["type"] for e in events if e["type"] != "progress"] == ["start", "thinking", "error",
                                                                    "done"]
    assert "out of free-tier quota" in events[-2]["message"]


def test_a_conversation_can_offer_a_subset_of_tools(session: FakeSession) -> None:
    chat = ScriptedChat(turn("No tools for that."))
    agent = Agent(Settings(observer_model=""), FakeTargets(), chat)
    conv = agent.conversation(None, "gemini:m", "pantry")
    conv.tools = frozenset({"list_recipes"})

    async def collect() -> list[dict[str, Any]]:
        return [e async for e in agent.run(conv, "hi")]

    events = asyncio.run(collect())
    assert events[0]["tools"] == []
    assert [f["function"]["name"] for f in chat.requests[0]["tools"]] == ["discover_tools"]


def test_closing_the_stream_cancels_the_model_call(session: FakeSession) -> None:
    started, cancelled = asyncio.Event(), []

    class SlowChat:
        async def complete(self, model: str, messages: Any, tools: Any, on_progress: Any = None) -> ChatTurn:
            on_progress({"phase": "reading", "eta_s": 600.0})
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            raise AssertionError("unreachable")

    agent = Agent(Settings(observer_model=""), FakeTargets(), SlowChat())  # type: ignore[arg-type]
    conv = agent.conversation(None, "ollama:m", "pantry")

    async def go() -> list[str]:
        stream = agent.run(conv, "hi")
        seen = [(await stream.__anext__())["type"] for _ in range(3)]   # start, thinking, progress
        await stream.aclose()                                         # the browser went away
        await asyncio.sleep(0)
        return seen

    assert asyncio.run(go()) == ["start", "thinking", "progress"] and cancelled == [True]


def test_an_overloaded_gemini_model_falls_back_but_a_local_one_does_not(session: FakeSession) -> None:
    settings = Settings(observer_model="", agent_fallbacks=("gemini:a", "gemini:b"))
    chat = ScriptedChat(ModelUnavailable("gemini a: HTTP 503 after 4 attempts: high demand"),
                        turn("Hello."))
    events, conv = run(Agent(settings, FakeTargets(), chat), "hi", model="gemini:a")
    assert [e["text"] for e in events if e["type"] == "notice"] == [
        "gemini a: HTTP 503 after 4 attempts: high demand Switching to gemini:b."]
    assert conv.model == "gemini:b" and events[-1]["stop"] == "answered"

    chat = ScriptedChat(ModelUnavailable("ollama m: HTTP 500"))
    events, conv = run(Agent(settings, FakeTargets(), chat), "hi", model="ollama:m")
    assert conv.model == "ollama:m" and events[-1]["stop"] == "error"


def test_an_empty_reply_gets_one_nudge(session: FakeSession) -> None:
    empty = turn("")
    empty.output_tokens = 50                       # it wrote something nobody could read
    chat = ScriptedChat(empty, turn("Penne is $1.97."))
    events, _ = run(Agent(Settings(observer_model=""), FakeTargets(), chat), "penne?")
    assert any(e["type"] == "notice" and "asked it once more" in e["text"] for e in events)
    assert chat.requests[1]["messages"][-1]["content"].startswith("Your last reply was empty")
    assert next(e for e in events if e["type"] == "assistant")["text"] == "Penne is $1.97."
    # a second empty reply is the answer: no loop
    chat = ScriptedChat(empty, empty)
    events, _ = run(Agent(Settings(observer_model=""), FakeTargets(), chat), "penne?")
    assert events[-1]["stop"] == "answered" and len(chat.requests) == 2
