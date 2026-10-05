"""The Assistant's chat client against httpx.MockTransport: request shapes per provider, replies,
retries and every failure path. No network."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from demo_hub.llm import (
    MODEL_CHOICES,
    ChatClient,
    LLMError,
    ModelUnavailable,
    QuotaExhausted,
    parse_model,
    split_variant,
    tool_arguments,
)
from demo_hub.settings import Settings

SETTINGS = Settings(gemini_api_key="k", gemini_base_url="https://gemini.test/v1",
                    ollama_url="http://ollama.test")
SIGNATURE = {"google": {"thought_signature": "sig"}}


def reply(message: dict[str, Any], finish: str = "stop", **usage: int) -> dict[str, Any]:
    return {"choices": [{"message": message, "finish_reason": finish}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 15}}


def client(handler: Any, settings: Settings = SETTINGS) -> tuple[ChatClient, list[float]]:
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    return ChatClient(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                      sleep=sleep), slept


def test_parse_model() -> None:
    assert parse_model("gemini:gemini-flash-latest") == ("gemini", "gemini-flash-latest")
    assert parse_model("ollama:llama3.2:3b") == ("ollama", "llama3.2:3b")
    for bad in ("gemini-flash", "openai:gpt", "gemini:", ""):
        with pytest.raises(LLMError, match="gemini:<model> or ollama:<model>"):
            parse_model(bad)
    assert all(parse_model(m["id"]) for m in MODEL_CHOICES)


@pytest.mark.parametrize(("raw", "expected"), [({"a": 1}, {"a": 1}), ('{"a": 1}', {"a": 1}),
                                               (None, {}), ("", {}), ("  ", {})])
def test_tool_arguments_accepts(raw: Any, expected: dict[str, Any]) -> None:
    assert tool_arguments(raw, "t") == expected


@pytest.mark.parametrize(("raw", "needle"), [('{"a": ', "not valid JSON"), ("[1]", "not a JSON object"),
                                             (42, "not valid JSON")])
def test_tool_arguments_rejects(raw: Any, needle: str) -> None:
    with pytest.raises(LLMError, match=needle):
        tool_arguments(raw, "t")


def test_gemini_request_and_tool_call_reply() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=reply(
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "extra_content": SIGNATURE,
                 "function": {"name": "pantry-find-product", "arguments": '{"query": "penne"}'}}]},
            finish="tool_calls", prompt_tokens=100, completion_tokens=5, total_tokens=130))

    chat, _ = client(handler)
    tools = [{"type": "function", "function": {"name": "pantry-find-product", "parameters": {}}}]
    turn = asyncio.run(chat.complete("gemini:gemini-flash-latest",
                                     [{"role": "user", "content": "penne?"}], tools))
    request = seen[0]
    assert str(request.url) == "https://gemini.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer k"
    body = json.loads(request.content)
    assert body == {"model": "gemini-flash-latest", "messages": [{"role": "user", "content": "penne?"}],
                    "max_tokens": 4096, "tools": tools, "reasoning_effort": "low"}
    assert turn.tool_calls == [{"id": "c1", "name": "pantry-find-product", "arguments": {"query": "penne"}}]
    # The raw message is kept for replay, thought signature included.
    assert turn.message["tool_calls"][0]["extra_content"] == SIGNATURE
    assert turn.message["role"] == "assistant"
    assert (turn.input_tokens, turn.output_tokens, turn.finish_reason) == (100, 30, "tool_calls")


def ollama_reply(message: dict[str, Any], **extra: Any) -> str:
    """Ollama's streamed reply: the thinking and content a piece at a time, tool calls in their
    own chunk, then the final chunk with the counters."""
    chunks: list[dict[str, Any]] = [{"message": {"role": "assistant", "thinking": t}, "done": False}
                                    for t in str(message.get("thinking") or "")]
    chunks += [{"message": {"role": "assistant", "content": c}, "done": False}
               for c in str(message.get("content") or "")]
    if message.get("tool_calls"):
        chunks.append({"message": {"role": "assistant", "content": "",
                                   "tool_calls": message["tool_calls"]}, "done": False})
    chunks.append({"message": {"role": "assistant", "content": ""}, "done": True,
                   "done_reason": "stop", "prompt_eval_count": 2000,
                   "prompt_eval_duration": 80_000_000_000, "eval_count": 40,
                   "eval_duration": 10_000_000_000, "load_duration": 500_000_000, **extra})
    return "".join(json.dumps(c) + "\n" for c in chunks)


def ollama(handler_chat: Any, settings: Settings, context: int | None = 8192
           ) -> tuple[ChatClient, list[httpx.Request]]:
    """A mock Ollama: ``/api/show`` reports ``context`` (or fails), ``/api/chat`` is the handler."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"model_info": {"cohere2.context_length": context}}) \
                if context else httpx.Response(404, json={"error": "not found"})
        return handler_chat(request)

    chat, _ = client(handler, settings)
    return chat, seen


def test_ollama_uses_the_native_api_with_a_capped_context_and_no_key() -> None:
    chat, seen = ollama(lambda r: httpx.Response(200, text=ollama_reply({"content": "hi"})),
                        Settings(ollama_url="http://ollama.test"))
    turn = asyncio.run(chat.complete("ollama:command-r7b", [], []))
    show, request = seen
    assert json.loads(show.content) == {"model": "command-r7b"}
    assert str(request.url) == "http://ollama.test/api/chat"
    assert "authorization" not in request.headers
    body = json.loads(request.content)
    # The model's own 8,192 caps the hub's 16,384; no tools, sampling or thinking unless asked.
    assert body == {"model": "command-r7b", "messages": [], "stream": True,
                    "options": {"num_ctx": 8192}, "keep_alive": "30m"}
    assert (turn.text, turn.tool_calls, turn.finish_reason) == ("hi", [], "stop")
    assert turn.metrics | {"wall_s": 0} == {
        "prompt_tokens": 2000, "output_tokens": 40, "prompt_s": 80.0, "gen_s": 10.0,
        "load_s": 0.5, "thinking_chars": 0, "num_ctx": 8192, "wall_s": 0}


def test_ollama_history_tools_and_options_in_the_native_shape() -> None:
    history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "penne?"},
               {"role": "assistant", "content": None, "tool_calls": [
                   {"id": "c1", "type": "function",
                    "function": {"name": "find_product", "arguments": '{"query": "penne"}'}}]},
               {"role": "tool", "tool_call_id": "c1", "content": '{"items": []}'}]
    tools = [{"type": "function", "function": {"name": "find_product", "parameters": {}}}]
    answer = ollama_reply({"content": "", "thinking": "hmm", "tool_calls": [
        {"function": {"name": "get_product", "arguments": {"product_id": 51}}}]})
    settings = Settings(ollama_url="http://ollama.test", ollama_temperature=0.0, ollama_think=False)
    chat, seen = ollama(lambda r: httpx.Response(200, text=answer), settings, context=None)
    turn = asyncio.run(chat.complete("ollama:granite4.2:8b", history, tools))
    body = json.loads(seen[1].content)
    assert body["model"] == "granite4.2:8b" and body["tools"] == tools and body["think"] is False
    assert body["options"] == {"num_ctx": 16384, "temperature": 0.0}   # /api/show failed: no cap
    assert body["messages"] == [
        {"role": "system", "content": "sys"}, {"role": "user", "content": "penne?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "find_product", "arguments": {"query": "penne"}}}]},
        {"role": "tool", "content": '{"items": []}', "tool_name": "find_product"}]
    [call] = turn.tool_calls
    assert call["name"] == "get_product" and call["arguments"] == {"product_id": 51}
    assert call["id"].startswith("call_")
    # The stored turn is OpenAI-shaped, so it replays through the same history conversion.
    stored = turn.message["tool_calls"][0]
    assert stored["id"] == call["id"] and json.loads(stored["function"]["arguments"]) == {"product_id": 51}
    assert turn.metrics["thinking_chars"] == 3
    # The context limit is looked up once per model.
    asyncio.run(chat.complete("ollama:granite4.2:8b", [], []))
    assert [r.url.path for r in seen].count("/api/show") == 1


def test_ollama_timeouts_are_not_retried_and_errors_are_retried_once() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    chat, seen = ollama(slow, Settings(ollama_url="http://ollama.test"))
    with pytest.raises(LLMError, match="did not answer within the time limit"):
        asyncio.run(chat.complete("ollama:command-r7b", [], []))
    assert [r.url.path for r in seen] == ["/api/show", "/api/chat"]

    chat, seen = ollama(lambda r: httpx.Response(500, json={"error": "model crashed"}),
                        Settings(ollama_url="http://ollama.test"))
    with pytest.raises(LLMError, match="HTTP 500 after 2 attempts: model crashed"):
        asyncio.run(chat.complete("ollama:command-r7b", [], []))
    assert [r.url.path for r in seen] == ["/api/show", "/api/chat", "/api/chat"]


def test_calls_without_ids_get_ids() -> None:
    chat, _ = client(lambda r: httpx.Response(200, json=reply({"tool_calls": [
        {"function": {"name": "a", "arguments": "{}"}}, {"function": {"name": "b"}}]})))
    turn = asyncio.run(chat.complete("gemini:m", [], []))
    assert [c["id"] for c in turn.tool_calls] == ["call_0", "call_1"]
    assert turn.message["tool_calls"][0]["type"] == "function"


def test_gemini_without_a_key_makes_no_request() -> None:
    chat, _ = client(lambda r: pytest.fail("no request expected"), Settings())
    with pytest.raises(LLMError, match="GEMINI_API_KEY is not set"):
        asyncio.run(chat.complete("gemini:m", [], []))


def test_429_and_503_are_retried_with_retry_after_or_backoff() -> None:
    replies = [httpx.Response(429, headers={"retry-after": "3"}), httpx.Response(503, text="busy"),
               httpx.Response(200, json=reply({"content": "ok"}))]
    chat, slept = client(lambda r: replies.pop(0))
    turn = asyncio.run(chat.complete("gemini:m", [], []))
    assert (turn.text, turn.attempts, slept) == ("ok", 3, [3.0, 4.0])


def test_retries_give_up_with_the_status_and_detail() -> None:
    chat, slept = client(lambda r: httpx.Response(
        429, json=[{"error": {"message": "quota exceeded for today"}}]))
    with pytest.raises(LLMError, match=r"HTTP 429 after 4 attempts \(a free-tier limit.*: quota exceeded for today"):
        asyncio.run(chat.complete("gemini:m", [], []))
    assert len(slept) == 3


def test_retry_after_is_capped() -> None:
    replies = [httpx.Response(503, headers={"retry-after": "900"}),
               httpx.Response(200, json=reply({"content": "ok"}))]
    chat, slept = client(lambda r: replies.pop(0))
    asyncio.run(chat.complete("gemini:m", [], []))
    assert slept == [60.0]


def test_geminis_retry_delay_in_the_body_is_honoured() -> None:
    quota = [{"error": {"code": 429, "message": "Quota exceeded", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": []},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "32.5s"}]}}]
    replies = [httpx.Response(429, json=quota), httpx.Response(429, json={"error": {"details": [{"retryDelay": "soon"}]}}),
               httpx.Response(429, text="not json"), httpx.Response(200, json=reply({"content": "ok"}))]
    chat, slept = client(lambda r: replies.pop(0))
    assert asyncio.run(chat.complete("gemini:m", [], [])).text == "ok"
    assert slept == [32.5, 4.0, 8.0]


@pytest.mark.parametrize(("response", "needle"), [
    (httpx.Response(400, json={"error": {"message": "bad schema"}}), "HTTP 400: bad schema"),
    (httpx.Response(404, text="no such model"), "HTTP 404: no such model"),
    (httpx.Response(200, text="<html>"), "not JSON"),
])
def test_failures_are_named(response: httpx.Response, needle: str) -> None:
    chat, slept = client(lambda r: response)
    with pytest.raises(LLMError, match=needle):
        asyncio.run(chat.complete("gemini:m", [], []))
    assert slept == []


def test_connection_errors_are_retried_then_named() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    chat, slept = client(handler)
    with pytest.raises(LLMError, match=r"cannot reach gemini \(ConnectError\)"):
        asyncio.run(chat.complete("gemini:m", [], []))
    assert slept == [2.0, 4.0, 8.0]


def test_malformed_arguments_in_a_reply_are_an_error() -> None:
    chat, _ = client(lambda r: httpx.Response(200, json=reply(
        {"tool_calls": [{"id": "x", "function": {"name": "t", "arguments": "{oops"}}]})))
    with pytest.raises(LLMError, match="not valid JSON"):
        asyncio.run(chat.complete("gemini:m", [], []))


def test_an_owned_client_is_created_and_closed() -> None:
    chat = ChatClient(Settings(gemini_api_key="k", gemini_base_url="http://127.0.0.1:9"))

    async def no_sleep(s: float) -> None:
        return None

    chat.sleep = no_sleep
    with pytest.raises(LLMError, match="cannot reach gemini"):
        asyncio.run(chat.complete("gemini:m", [], []))


def test_a_daily_quota_fails_at_once_as_quota_exhausted() -> None:
    body = [{"error": {"code": 429, "message": "Quota exceeded", "details": [
        {"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
        {"retryDelay": "18737s"}]}}]
    chat, slept = client(lambda r: httpx.Response(429, json=body))
    with pytest.raises(QuotaExhausted, match="out of free-tier quota for about 5.2 h") as info:
        asyncio.run(chat.complete("gemini:gemini-flash-latest", [], []))
    assert info.value.retry_after_s == 18737.0 and slept == []


def test_a_broken_ollama_stream_is_an_error() -> None:
    for text, needle in (('{"message": {"content": "a"}, "done": false}\n', "ended before the reply"),
                         ("not json\n", "not JSON"),
                         ('{"error": "model crashed"}\n', "ollama: model crashed")):
        chat, _ = ollama(lambda r, t=text: httpx.Response(200, text=t),
                         Settings(ollama_url="http://ollama.test"))
        with pytest.raises(LLMError, match=needle):
            asyncio.run(chat.complete("ollama:command-r7b", [], []))


def test_progress_and_timings_are_reported_and_recorded(tmp_path: Any) -> None:
    from demo_hub.timings import CallTiming, TimingStore

    store = TimingStore(str(tmp_path / "t.jsonl"))
    # History: a cold read at 20 tok/s, writing at 4 tok/s, 40-token replies.
    store.record(CallTiming(model="ollama:granite4.2:8b", wall_s=110, prompt_chars=7200,
                            prompt_tokens=2000, new_tokens_est=2000, prompt_s=100,
                            output_tokens=40, gen_s=10))
    settings = Settings(ollama_url="http://ollama.test")
    chat, _ = ollama(lambda r: httpx.Response(200, text=ollama_reply({"content": "hello"})), settings)
    chat.timings = store
    updates: list[dict[str, Any]] = []
    messages = [{"role": "user", "content": "x" * 3580}]
    asyncio.run(chat.complete("ollama:granite4.2:8b", messages, [], on_progress=updates.append))
    first = updates[0]
    assert first["phase"] == "reading" and first["samples"] == 1
    assert (first["read_tok_s"], first["gen_tok_s"]) == (20.0, 4.0)
    assert first["output_tokens_est"] == 40 and first["gen_s"] == 10.0
    assert first["prompt_tokens_est"] == first["new_tokens_est"] > 900   # no cache evidence yet
    writing = [u for u in updates if u["phase"] == "writing"]
    assert writing and writing[0]["tokens"] == 1
    # The call is recorded (in memory and in the file) with what it actually took.
    last = store.calls["ollama:granite4.2:8b"][-1]
    assert (last.prompt_tokens, last.prompt_s, last.output_tokens, last.later) == (2000, 80.0, 40, False)
    assert len((tmp_path / "t.jsonl").read_text().splitlines()) == 2
    # A second identical call could reuse the whole prompt from the model's cache.
    asyncio.run(chat.complete("ollama:granite4.2:8b", messages, []))
    assert store.calls["ollama:granite4.2:8b"][-1].new_tokens_est == 0


def test_model_keys_separate_thinking() -> None:
    assert ChatClient(Settings()).model_key("ollama:g") == "ollama:g"
    assert ChatClient(Settings(ollama_think=False)).model_key("ollama:g") == "ollama:g#think=false"
    assert ChatClient(Settings(ollama_think=True)).model_key("gemini:m") == "gemini:m"


def test_model_variants_set_thinking_and_temperature_per_call() -> None:
    assert split_variant("ollama:g#think=false,temperature=0.2") == (
        "ollama:g", {"think": False, "temperature": 0.2})
    assert parse_model("ollama:granite4.2:8b#think=false") == ("ollama", "granite4.2:8b")
    for bad in ("ollama:g#think=maybe", "ollama:g#temperature=hot", "ollama:g#seed=1"):
        with pytest.raises(LLMError, match="unknown model option"):
            split_variant(bad)
    chat, seen = ollama(lambda r: httpx.Response(200, text=ollama_reply({"content": "hi"})),
                        Settings(ollama_url="http://ollama.test", ollama_think=True))
    asyncio.run(chat.complete("ollama:granite4.2:8b#think=false,temperature=0", [], []))
    body = json.loads(seen[-1].content)
    assert body["model"] == "granite4.2:8b" and body["think"] is False
    assert body["options"]["temperature"] == 0.0
    assert chat.model_key("ollama:granite4.2:8b#think=false") == "ollama:granite4.2:8b#think=false"
    assert chat.model_key("ollama:granite4.2:8b") == "ollama:granite4.2:8b#think=true"


def test_gemini_overload_after_every_retry_is_model_unavailable() -> None:
    busy = {"error": {"message": "This model is currently experiencing high demand."}}
    chat, slept = client(lambda r: httpx.Response(503, json=busy))
    with pytest.raises(ModelUnavailable, match="HTTP 503 after 4 attempts: This model is currently"):
        asyncio.run(chat.complete("gemini:m", [], []))
    assert len(slept) == 3


def test_the_cached_prefix_follows_the_template_order(tmp_path: Any) -> None:
    """The system prompt comes before the tools: two prompts with the same instructions but
    different tools share the instructions, as Ollama's cache does."""
    from demo_hub.timings import TimingStore

    chat, _ = ollama(lambda r: httpx.Response(200, text=ollama_reply({"content": "hi"})),
                     Settings(ollama_url="http://ollama.test"))
    chat.timings = TimingStore(str(tmp_path / "t.jsonl"))
    system = {"role": "system", "content": "s" * 4000}
    tool = {"type": "function", "function": {"name": "a", "parameters": {}}}
    asyncio.run(chat.complete("ollama:g", [system, {"role": "user", "content": "q"}], [tool]))
    other = {"type": "function", "function": {"name": "b", "parameters": {}}}
    asyncio.run(chat.complete("ollama:g", [system, {"role": "user", "content": "q"}], [other]))
    last = chat.timings.calls["ollama:g"][-1]
    assert last.new_tokens_est < 0.1 * last.prompt_tokens     # the 4,000-char system prompt was shared


def test_a_json_schema_asks_each_provider_for_a_json_reply() -> None:
    schema = {"type": "object", "properties": {"reports": {"type": "array"}}}
    seen: list[httpx.Request] = []

    def gemini(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=reply({"content": '{"reports": []}'}))

    chat, _ = client(gemini)
    asyncio.run(chat.complete("gemini:m", [], [], json_schema=schema))
    assert json.loads(seen[0].content)["response_format"] == {
        "type": "json_schema", "json_schema": {"name": "reply", "schema": schema}}

    chat, sent = ollama(lambda r: httpx.Response(200, text=ollama_reply({"content": "{}"})),
                        Settings(ollama_url="http://ollama.test"))
    asyncio.run(chat.complete("ollama:g", [], [], json_schema=schema))
    assert json.loads(sent[-1].content)["format"] == schema
