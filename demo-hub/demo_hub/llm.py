"""The Assistant's model: one chat client for Gemini and Ollama.

A model is written ``provider:model`` (``gemini:gemini-flash-latest``, ``ollama:granite4.2:8b``).
Messages are kept in the OpenAI shape end to end, so a Gemini turn is replayed exactly as it came
back, including Gemini 3's ``extra_content`` thought signatures, without which Gemini answers 400.

Gemini is called through its OpenAI-compatible ``/chat/completions``. Ollama is called through its
native ``/api/chat`` instead: only that endpoint takes ``num_ctx``, and Ollama's default 4,096-token
window silently drops the start of a longer prompt (the system prompt and most tool definitions).
It also reports where the time went (prompt reading, generation, model loading), which the
Assistant shows and the model bench compares.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from demo_hub.settings import Settings
from demo_hub.timings import CallTiming, Estimate, TimingStore, common_prefix, remaining_s

PROVIDERS = ("gemini", "ollama")
# Offered in the Assistant's model picker. Free-tier quotas are per model per day, so several
# Flash variants are listed. The local models run on this machine's CPU unless Ollama has a GPU.
MODEL_CHOICES: tuple[dict[str, str], ...] = (
    {"id": "gemini:gemini-3-flash-preview", "label": "Gemini 3 Flash (preview)"},
    {"id": "gemini:gemini-flash-latest", "label": "Gemini Flash (latest)"},
    {"id": "gemini:gemini-flash-lite-latest", "label": "Gemini Flash-Lite (latest)"},
    {"id": "gemini:gemini-3.1-flash-lite", "label": "Gemini 3.1 Flash-Lite"},
    {"id": "ollama:granite4.2:8b#think=false",
     "label": "IBM Granite 4.2 8B, thinking off (local Ollama: slow without a GPU)"},
    {"id": "ollama:granite4.2:8b",
     "label": "IBM Granite 4.2 8B, thinking on (local Ollama: slower still)"},
    {"id": "ollama:command-r7b", "label": "Cohere command-r7b (local Ollama: slow without a GPU)"},
)
MAX_ATTEMPTS = 4
MAX_WAIT_S = 60.0  # a per-minute limit clears within a minute; longer means a daily quota

SleepFn = Callable[[float], Awaitable[None]]
ProgressFn = Callable[[dict[str, Any]], None]
PROGRESS_EVERY_S = 1.5


class LLMError(RuntimeError):
    """A model call that failed in a way the user should see as-is."""


class QuotaExhausted(LLMError):
    """A 429 whose retry delay is longer than a per-minute limit: the model's daily free-tier
    quota is spent, so waiting is pointless and another model should be tried."""

    def __init__(self, message: str, retry_after_s: float) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


def split_variant(spec: str) -> tuple[str, dict[str, Any]]:
    """``ollama:granite4.2:8b#think=false`` is the model with per-call options: ``think`` (true or
    false) and ``temperature``. The whole spec stays the model's name in a conversation."""
    model, _, opts = spec.strip().partition("#")
    options: dict[str, Any] = {}
    for part in filter(None, opts.split(",")):
        key, _, value = part.partition("=")
        try:
            if key == "think" and value in ("true", "false"):
                options["think"] = value == "true"
            elif key == "temperature":
                options["temperature"] = float(value)
            else:
                raise ValueError(part)
        except ValueError as exc:
            raise LLMError(f"unknown model option {part!r} in {spec!r} (think=true|false, "
                           f"temperature=<number>)") from exc
    return model, options


class ModelUnavailable(LLMError):
    """A cloud model that kept answering 5xx through every retry (Gemini's "high demand" 503):
    another model may well answer, so the Assistant moves on to its next fallback."""


def parse_model(spec: str) -> tuple[str, str]:
    """The provider and model name of a spec (any ``#options`` are checked and left off)."""
    provider, sep, name = split_variant(spec)[0].partition(":")
    if not sep or provider not in PROVIDERS or not name.strip():
        raise LLMError(f"model must be gemini:<model> or ollama:<model>, got {spec!r}")
    return provider, name.strip()


@dataclass
class ChatTurn:
    """One assistant reply: the raw message (replayed as-is), its text and tool calls."""

    message: dict[str, Any]
    text: str
    tool_calls: list[dict[str, Any]]
    finish_reason: str
    input_tokens: int = 0
    output_tokens: int = 0
    attempts: int = 1
    # Where the time went: ``wall_s`` always; from Ollama also the prompt tokens it actually read
    # (a cached prefix is not re-read), and the seconds spent reading, generating and loading.
    metrics: dict[str, float] = field(default_factory=dict)


def tool_arguments(raw: Any, tool: str) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise LLMError(f"the model sent arguments for {tool} that are not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise LLMError(f"the model sent arguments for {tool} that are not a JSON object")
    return parsed


class _Progress:
    """A call's progress for the UI: the estimate up front, then (Ollama streams its reply) the
    moment writing starts and the tokens written, at most every PROGRESS_EVERY_S."""

    def __init__(self, estimate: Estimate | None, emit: ProgressFn,
                 clock: Callable[[], float] = time.perf_counter) -> None:
        self.estimate, self.emit, self.clock = estimate, emit, clock
        self.started = clock()
        self.writing_since: float | None = None
        self.tokens = 0
        self.last = float("-inf")

    def begin(self, phase: str) -> None:
        public = self.estimate.public() if self.estimate else {"samples": 0}
        self.emit({"phase": phase, "elapsed_s": 0.0, "tokens": 0,
                   "eta_s": round(self.estimate.total_s, 1) if self.estimate else None, **public})

    def token(self) -> None:
        self.tokens += 1
        now = self.clock() - self.started
        if self.writing_since is None:
            self.writing_since = now
        elif now - self.last < PROGRESS_EVERY_S:
            return
        self.last = now
        eta = (remaining_s(self.estimate, now, self.writing_since, self.tokens)
               if self.estimate else None)
        self.emit({"phase": "writing", "elapsed_s": round(now, 1), "tokens": self.tokens,
                   "eta_s": None if eta is None else round(eta, 1)})


@dataclass
class ChatClient:
    settings: Settings
    client: httpx.AsyncClient | None = None
    sleep: SleepFn = asyncio.sleep
    base_delay: float = 2.0
    timings: TimingStore | None = None
    _owned: list[httpx.AsyncClient] = field(default_factory=list)
    _context_limits: dict[str, int | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timings is None:
            self.timings = TimingStore(self.settings.timings_path)

    def _think(self, model: str) -> bool | None:
        return split_variant(model)[1].get("think", self.settings.ollama_think)

    def model_key(self, model: str) -> str:
        """The key timings are kept under: thinking changes a model's reply length a lot."""
        provider, name = parse_model(model)
        think = self._think(model)
        if provider == "ollama" and think is not None:
            return f"{provider}:{name}#think={str(think).lower()}"
        return f"{provider}:{name}"

    async def _context_limit(self, client: httpx.AsyncClient, name: str) -> int | None:
        """The model's own context length from Ollama's ``/api/show`` (cached): asking for more
        than a model was trained on (command-r7b: 8,192) degrades it rather than helping."""
        if name not in self._context_limits:
            limit = None
            try:
                response = await client.post(f"{self.settings.ollama_url}/api/show",
                                             json={"model": name})
                info = response.json().get("model_info") or {} if response.status_code == 200 else {}
                limit = next((int(v) for k, v in info.items() if k.endswith(".context_length")),
                             None)
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                limit = None
            self._context_limits[name] = limit
        return self._context_limits[name]

    def _request(self, provider: str, name: str, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]], num_ctx: int = 0,
                 options_override: dict[str, Any] | None = None,
                 json_schema: dict[str, Any] | None = None,
                 ) -> tuple[str, dict[str, str], float, dict[str, Any]]:
        override = options_override or {}
        if provider == "gemini":
            if not self.settings.gemini_api_key:
                raise LLMError("GEMINI_API_KEY is not set for the hub")
            body: dict[str, Any] = {"model": name, "messages": messages, "max_tokens": 4096,
                                    "reasoning_effort": "low"}
            if tools:
                body["tools"] = tools
            if json_schema:
                body["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "reply", "schema": json_schema}}
            return (f"{self.settings.gemini_base_url}/chat/completions",
                    {"Authorization": f"Bearer {self.settings.gemini_api_key}"}, 120.0, body)
        options: dict[str, Any] = {"num_ctx": num_ctx or self.settings.ollama_num_ctx}
        temperature = override.get("temperature", self.settings.ollama_temperature)
        if temperature is not None:
            options["temperature"] = temperature
        body = {"model": name, "messages": to_ollama_messages(messages), "stream": True,
                "options": options, "keep_alive": "30m"}
        if tools:
            body["tools"] = tools
        think = override.get("think", self.settings.ollama_think)
        if think is not None:
            body["think"] = think
        if json_schema:
            body["format"] = json_schema
        return f"{self.settings.ollama_url}/api/chat", {}, self.settings.ollama_timeout_s, body

    async def complete(
        self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
        on_progress: ProgressFn | None = None, json_schema: dict[str, Any] | None = None,
    ) -> ChatTurn:
        """One model call. ``json_schema`` asks for a JSON reply of that shape (the observer
        judge): Gemini's ``response_format``, Ollama's ``format``."""
        provider, name = parse_model(model)
        key = self.model_key(model)
        assert self.timings is not None
        # What the model must read: a cached prompt prefix from its previous call is not re-read.
        # Serialized in the order the chat template renders it (the system prompt, then the tools,
        # then the conversation), so a prompt that keeps the instructions but changes the tools
        # still shares the instructions with the previous one.
        head = messages[:1] if messages and messages[0].get("role") == "system" else []
        prompt = json.dumps(head) + json.dumps(tools) + json.dumps(messages[len(head):])
        # The cache belongs to the loaded model, whichever thinking setting used it last.
        previous = self.timings.last_prompt(name) if provider == "ollama" else ""
        cached = common_prefix(prompt, previous)
        later = any(m.get("role") == "assistant" for m in messages)
        self.timings.reload()
        progress = (_Progress(self.timings.estimate(key, len(prompt), cached, later), on_progress)
                    if on_progress else None)
        timeout = self.settings.ollama_timeout_s if provider == "ollama" else 120.0
        client = self.client or httpx.AsyncClient(timeout=timeout)
        try:
            num_ctx = 0
            if provider == "ollama":
                limit = await self._context_limit(client, name)
                num_ctx = min(self.settings.ollama_num_ctx, limit or self.settings.ollama_num_ctx)
            url, headers, _, body = self._request(provider, name, messages, tools, num_ctx,
                                                  split_variant(model)[1], json_schema)
            if progress:
                progress.begin("reading" if provider == "ollama" else "waiting")
            started = time.perf_counter()
            turn = await self._post(client, url, headers, body, provider, name, progress)
        finally:
            if self.client is None:
                await client.aclose()
        turn.metrics["wall_s"] = round(time.perf_counter() - started, 3)
        if num_ctx:
            turn.metrics["num_ctx"] = num_ctx
            self.timings.set_last_prompt(name, prompt)
        m = turn.metrics
        prompt_tokens = int(m.get("prompt_tokens", 0))
        new_tokens = round(prompt_tokens * (len(prompt) - cached) / len(prompt))
        if provider == "ollama" and previous:
            m["new_tokens_est"] = new_tokens   # Ollama counts a cached prefix in prompt_tokens
        self.timings.record(CallTiming(
            model=key, wall_s=m["wall_s"], prompt_chars=len(prompt), prompt_tokens=prompt_tokens,
            new_tokens_est=new_tokens,
            prompt_s=float(m.get("prompt_s", 0)), output_tokens=int(m.get("output_tokens", 0)),
            gen_s=float(m.get("gen_s", 0)), later=later,
            cache_known=provider != "ollama" or bool(previous)))
        return turn

    async def _send(self, client: httpx.AsyncClient, url: str, headers: dict[str, str],
                    body: dict[str, Any], provider: str, progress: _Progress | None
                    ) -> tuple[httpx.Response, dict[str, Any] | None]:
        """One request. Ollama's reply streams, so its tokens drive the progress."""
        if provider != "ollama":
            return await client.post(url, json=body, headers=headers), None
        async with client.stream("POST", url, json=body, headers=headers) as response:
            if response.status_code >= 400:
                await response.aread()
                return response, None
            return response, await _read_ollama_stream(response, progress)

    async def _post(self, client: httpx.AsyncClient, url: str, headers: dict[str, str],
                    body: dict[str, Any], provider: str, name: str,
                    progress: _Progress | None = None) -> ChatTurn:
        # A local model that times out has been computing for minutes: do not start it again.
        attempts = MAX_ATTEMPTS if provider == "gemini" else 2
        for attempt in range(1, attempts + 1):
            try:
                response, streamed = await self._send(client, url, headers, body, provider, progress)
            except httpx.TransportError as exc:
                if provider == "ollama" and isinstance(exc, httpx.TimeoutException):
                    raise LLMError(f"ollama {name} did not answer within the time limit "
                                   f"(OLLAMA_TIMEOUT_S)") from exc
                if attempt == attempts:
                    raise LLMError(f"cannot reach {provider} ({type(exc).__name__})") from exc
                await self.sleep(self.base_delay * 2 ** (attempt - 1))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                delay = _retry_delay(response)
                if response.status_code == 429 and delay is not None and delay > MAX_WAIT_S:
                    hours = delay / 3600
                    raise QuotaExhausted(f"{provider} {name} is out of free-tier quota for about "
                                         f"{hours:.1f} h: {_detail(response)}", delay)
                if attempt == attempts:
                    hint = (" (a free-tier limit: wait a minute or pick another model)"
                            if response.status_code == 429 else "")
                    error = (ModelUnavailable if provider == "gemini" and response.status_code >= 500
                             else LLMError)
                    raise error(f"{provider} {name}: HTTP {response.status_code} after "
                                f"{attempt} attempts{hint}: {_detail(response)}")
                await self.sleep(min(delay or self.base_delay * 2 ** (attempt - 1), MAX_WAIT_S))
                continue
            if response.status_code >= 400:
                raise LLMError(f"{provider} {name}: HTTP {response.status_code}: "
                               f"{_detail(response)}")
            try:
                data = streamed if streamed is not None else response.json()
            except ValueError as exc:
                raise LLMError(f"{provider} {name} answered with something that is not JSON") \
                    from exc
            turn = _ollama_turn(data) if provider == "ollama" else _turn(data)
            turn.attempts = attempt
            return turn
        raise AssertionError("unreachable")  # pragma: no cover


def _retry_delay(response: httpx.Response) -> float | None:
    """Seconds the server asked us to wait: a numeric Retry-After header, else Gemini's
    ``retryDelay`` ("32s") in the error's details, which is how its per-minute limits answer."""
    header = response.headers.get("retry-after", "").strip()
    if header.replace(".", "", 1).isdigit():
        return float(header)
    try:
        payload = response.json()
    except ValueError:
        return None
    if isinstance(payload, list) and payload:
        payload = payload[0]
    error = payload.get("error") if isinstance(payload, dict) else None
    details = error.get("details") if isinstance(error, dict) else None
    for detail in details or []:
        delay = str(detail.get("retryDelay") or "") if isinstance(detail, dict) else ""
        if delay.endswith("s") and delay[:-1].replace(".", "", 1).isdigit():
            return float(delay[:-1])
    return None


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, list) and payload:
            payload = payload[0]
        error = payload.get("error") if isinstance(payload, dict) else ""
        message = error.get("message") if isinstance(error, dict) else error   # Ollama: a string
        return " ".join(str(message or response.text).split())[:300]
    except ValueError:
        return " ".join(response.text.split())[:300]


def _turn(data: dict[str, Any]) -> ChatTurn:
    choice = (data.get("choices") or [{}])[0] or {}
    message = dict(choice.get("message") or {})
    message["role"] = "assistant"
    calls = []
    for i, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function") or {}
        name = str(function.get("name", ""))
        call.setdefault("id", f"call_{i}")
        call.setdefault("type", "function")
        calls.append({"id": call["id"], "name": name,
                      "arguments": tool_arguments(function.get("arguments"), name)})
    usage = data.get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    output = max(int(usage.get("total_tokens") or 0) - prompt,
                 int(usage.get("completion_tokens") or 0))
    return ChatTurn(message=message, text=str(message.get("content") or ""), tool_calls=calls,
                    finish_reason=str(choice.get("finish_reason") or ""),
                    input_tokens=prompt, output_tokens=output)


def to_ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The OpenAI-shaped history in Ollama's native shape: tool-call arguments as objects, and a
    tool result named by its tool (Ollama has no call ids)."""
    out: list[dict[str, Any]] = []
    names: dict[str, str] = {}
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            converted: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
            calls = []
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                name = str(function.get("name", ""))
                names[str(call.get("id"))] = name
                calls.append({"function": {"name": name,
                                           "arguments": tool_arguments(function.get("arguments"), name)}})
            if calls:
                converted["tool_calls"] = calls
            out.append(converted)
        elif role == "tool":
            out.append({"role": "tool", "content": message.get("content") or "",
                        "tool_name": names.get(str(message.get("tool_call_id")), "")})
        else:
            out.append({"role": role, "content": message.get("content") or ""})
    return out


async def _read_ollama_stream(response: httpx.Response,
                              progress: _Progress | None) -> dict[str, Any]:
    """Ollama's streamed reply put back together as the one object a non-streamed call returns."""
    content: list[str] = []
    thinking: list[str] = []
    calls: list[dict[str, Any]] = []
    final: dict[str, Any] | None = None
    async for line in response.aiter_lines():
        if not line.strip():
            continue
        try:
            chunk = json.loads(line)
        except ValueError as exc:
            raise LLMError("ollama streamed a line that is not JSON") from exc
        if chunk.get("error"):
            raise LLMError(f"ollama: {chunk['error']}")
        message = chunk.get("message") or {}
        content.append(str(message.get("content") or ""))
        thinking.append(str(message.get("thinking") or ""))
        calls.extend(message.get("tool_calls") or [])
        if chunk.get("done"):
            final = chunk
        elif progress and (message.get("content") or message.get("thinking")
                           or message.get("tool_calls")):
            progress.token()
    if final is None:
        raise LLMError("the ollama stream ended before the reply was complete")
    return {**final, "message": {"role": "assistant", "content": "".join(content),
                                 "thinking": "".join(thinking), "tool_calls": calls}}


def _ollama_turn(data: dict[str, Any]) -> ChatTurn:
    message = data.get("message") or {}
    text = str(message.get("content") or "")
    calls, stored_calls = [], []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        name = str(function.get("name", ""))
        arguments = tool_arguments(function.get("arguments"), name)
        call_id = f"call_{uuid.uuid4().hex[:12]}"
        calls.append({"id": call_id, "name": name, "arguments": arguments})
        stored_calls.append({"id": call_id, "type": "function",
                             "function": {"name": name, "arguments": json.dumps(arguments)}})
    stored: dict[str, Any] = {"role": "assistant", "content": text or None}
    if stored_calls:
        stored["tool_calls"] = stored_calls
    prompt, output = int(data.get("prompt_eval_count") or 0), int(data.get("eval_count") or 0)
    ns = 1e9
    metrics = {"prompt_tokens": prompt, "output_tokens": output,
               "prompt_s": round((data.get("prompt_eval_duration") or 0) / ns, 3),
               "gen_s": round((data.get("eval_duration") or 0) / ns, 3),
               "load_s": round((data.get("load_duration") or 0) / ns, 3),
               "thinking_chars": len(str(message.get("thinking") or ""))}
    return ChatTurn(message=stored, text=text, tool_calls=calls,
                    finish_reason=str(data.get("done_reason") or ""),
                    input_tokens=prompt, output_tokens=output, metrics=metrics)
