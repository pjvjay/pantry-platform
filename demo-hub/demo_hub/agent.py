"""The Assistant: a grocery agent that plans by calling the pantry MCP tools.

Each user message runs a tool-use loop: the model sees the target's MCP catalog as functions,
asks for calls, the hub runs them on the MCP session and feeds the results back, until the model
answers without asking for a tool (or the step budget runs out). Every step is yielded as an
event, so the browser shows the tool traffic as it happens.

The system prompt is a short preamble plus pantry-api's recipe-shopper skill (its frontmatter
stripped), the same SOP the simulations grade the agent against.

Tools are disclosed progressively by default (``disclosure.py``, the policy in
``assistant_policy.py`` written with the ``observers`` SDK): a conversation starts with a few
tools, and observers watching the shopper's messages and the tool traffic enable more, and the
skill, when they see what calls for them; the agent can also ask for tools with
``discover_tools``. ``disclosure="all"`` offers every tool and the skill from the first call and
runs no observers, as before.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from demo_hub.answers import plan_for_model, plan_tables, with_tables
from demo_hub.disclosure import (
    DISCOVER,
    DISCOVER_FUNCTION,
    JUDGE_SYSTEM,
    REPORT_SCHEMA,
    Disclosure,
    Judge,
    canonical,
    judge_prompt,
    parse_reports,
)
from demo_hub.llm import (
    ChatClient,
    ChatTurn,
    LLMError,
    ModelUnavailable,
    QuotaExhausted,
    parse_model,
)
from demo_hub.mcp_targets import McpTargetError, Targets, call_tool, open_session
from demo_hub.observers import Condition, Policy, View
from demo_hub.settings import Settings

MAX_CONVERSATIONS = 50
GOAL_PREFIX = "Goal enabled by observation"
RESULT_CHARS_FOR_MODEL = 16_000
REASONING_CHARS = 8_000            # a step's reasoning sent to the browser and kept in its trace
# Plan-summary fields for the browser's trace views, never sent to the model.
FOR_BROWSER = {"llm_calls", "burr_run", "pipeline"}
AGENT_TARGETS = ("gateway-recipes", "pantry", "gateway-sim")
# plan tools that choose stores only with a location: the hub fills the shopper's when it is left out
PLAN_LOCATION_TOOLS = {"plan_recipe", "plan_from_text"}
# what the plan tools' country lists take (a 3B model sent preference ["local", "organic"])
COUNTRY_ARGS = {
    "exclude_origin": 'country names to leave out, e.g. ["United States"]',
    "preference": 'country names to favour, e.g. ["Canada"]; nothing else',
}

PREAMBLE = """\
You are a grocery-planning assistant for shoppers in Vancouver, BC, connected to the pantry MCP
server through tools. Every product, price, store, distance, origin and total you mention must
come from a tool result in this conversation; never invent or estimate one, and say plainly when
the tools cannot answer. Tool names may carry a `pantry-` prefix with dashes (`pantry-plan-from-text`
is `plan_from_text`). If a tool you need is not in your list, ask for it with discover_tools.

Plans:
- A library recipe: call plan_recipe with its slug from list_recipes (tomato_penne, not
  tomato-penne). The shopper's location and distance are added for you.
- To see which recipes can be planned, call list_recipes.
- A dish the shopper names that is not in list_recipes and comes without a recipe or link: write
  a short recipe for it (a title with the servings, then one "- ingredient" line each) and plan
  it with plan_from_text, allow_partial true.
- A recipe link or a pasted recipe: follow the recipe-shopper procedure.
- If a plan call fails or times out, call it again with the same arguments.
- A line's trip_store and trip_price are where the recommended trip buys it; its store and price
  are only its cheapest offer in range. With no trip, the plan chose no stores: say so.
- Origin: report the plan's own origin_status and coverage; call get_product_origins only with the
  basket's product_ids.

After a plan or a week plan, answer in two or three sentences: the trip's total and its store(s),
the verified origin share when origin was asked about, and anything not found. The shopper sees
the plan's table under your answer, added automatically: do not write a table or list the lines.
For other questions (a product's price, where it comes from), call the matching tool and report
its result briefly.

You cannot run scripts or open a shell. Read a recipe page with the fetch tool (markdown,
max_length 20000; call again with start_index if it is cut before the ingredient list).
"""


def load_skill(path: str) -> str:
    """The SKILL.md body without its YAML frontmatter; "" when the file is missing."""
    if not path:
        return ""
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


def system_prompt(settings: Settings) -> str:
    skill = load_skill(settings.recipe_shopper_skill)
    return PREAMBLE + (f"\n# Recipe-shopper procedure\n\n{skill}\n" if skill else "")


def load_policy(spec: str) -> Policy:
    """``package.module:NAME`` -> the Policy object it names (DEMO_AGENT_POLICY)."""
    module, _, attr = spec.partition(":")
    policy = getattr(importlib.import_module(module), attr or "POLICY")
    if not isinstance(policy, Policy):
        raise TypeError(f"{spec} is not an observers.Policy")
    return policy


def openai_tools(tools: list[dict[str, Any]], lean: bool = False) -> list[dict[str, Any]]:
    """The tools as function definitions. ``lean`` (local models) drops what costs reading time
    and tells the model nothing: docstring indentation, a schema's titles, ``anyOf [X, null]``
    around optional fields and their null defaults; and keeps a description to its first
    paragraphs, up to LEAN_DESCRIPTION characters (plan_recipe: 1,997 characters to about 900)."""
    out = []
    for t in tools:
        description = t.get("description") or ""
        parameters = t.get("inputSchema") or {"type": "object", "properties": {}}
        if lean:
            description = lean_description(description)
            parameters = lean_schema(parameters)
        out.append({"type": "function", "function": {
            "name": t["name"], "description": description[:1024], "parameters": parameters}})
    return out


LEAN_DESCRIPTION = 600


def lean_description(text: str, limit: int = LEAN_DESCRIPTION) -> str:
    """Paragraphs with their lines joined and indentation gone, as many whole ones as fit in
    ``limit`` (the first always, cut at a sentence if it is longer)."""
    paragraphs = [" ".join(line.strip() for line in p.splitlines() if line.strip())
                  for p in re.split(r"\n\s*\n", text.strip())]
    paragraphs = [p for p in paragraphs if p]
    if not paragraphs:
        return ""
    kept = [paragraphs[0]]
    for p in paragraphs[1:]:
        if len("\n".join([*kept, p])) > limit:
            break
        kept.append(p)
    out = "\n".join(kept)
    if len(out) > limit:
        cut = out[:limit].rfind(". ")
        out = out[:cut + 1] if cut > limit // 2 else out[:limit]
    return out


def lean_schema(node: Any) -> Any:
    if isinstance(node, list):
        return [lean_schema(x) for x in node]
    if not isinstance(node, dict):
        return node
    options = node.get("anyOf")
    if isinstance(options, list) and len(options) == 2 and {"type": "null"} in options:
        merged = {k: v for k, v in node.items() if k != "anyOf"}
        merged.update(next(o for o in options if o != {"type": "null"}))
        node = merged
    return {k: lean_schema(v) for k, v in node.items()
            if k != "title" and not (k == "default" and v is None)}


TOOLS_PREFIX = "More tools are now available; call them like the others"
EMPTY_REPLY_NUDGE = ("Your last reply was empty or could not be read as a tool call. Call one tool "
                     "with valid JSON arguments, or answer the shopper.")


def announce_tools(tools: list[dict[str, Any]], lean: bool = False) -> str:
    """Tools offered after the first step, as a message: name, description and parameters."""
    lines = [f"{TOOLS_PREFIX}:"]
    for t in openai_tools(tools, lean):
        fn = t["function"]
        lines.append(f"- {fn['name']}: {fn['description']}\n  parameters: "
                     f"{json.dumps(fn['parameters'], ensure_ascii=False)}")
    return "\n".join(lines)


def result_for_model(result: dict[str, Any], limit: int = RESULT_CHARS_FOR_MODEL) -> str:
    """The tool result as the model reads it: the structured content as JSON (or the text), at
    most `limit` characters. A plan summary's `llm_calls` and `burr_run` (where pantry's LLM time
    went and its Burr trace, for the browser) are left out: the model has no use for them and pays
    for every token. JSON over
    the limit is shrunk by shortening its longest lists, so it stays valid and says what was left
    out; only what still does not fit is cut."""
    body = result.get("structured")
    summary = body.get("summary") if isinstance(body, dict) else None
    if isinstance(summary, dict) and FOR_BROWSER & summary.keys():
        body = {**body, "summary": {k: v for k, v in summary.items() if k not in FOR_BROWSER}}
    if body is not None:
        text = json.dumps(body, ensure_ascii=False)
        if len(text) > limit:
            text = json.dumps(shrink_json(body, limit), ensure_ascii=False)
    else:
        text = result.get("text", "")
    if result.get("is_error"):
        text = f"ERROR: {text}"
    return text[:limit]


def shrink_json(body: Any, limit: int) -> Any:
    """`body` with its longest lists halved, longest first, until its JSON fits in `limit`
    characters. Each shortened list ends with a note of how many items it left out, so the model
    knows the answer is partial and can ask for less (ids, a search, a smaller page)."""
    body = json.loads(json.dumps(body))            # a copy the browser's result never sees
    for _ in range(40):
        if len(json.dumps(body, ensure_ascii=False)) <= limit:
            break
        lists = _lists(body)
        if not lists:
            break
        _, longest = max(lists, key=lambda sl: sl[0])
        note = longest[-1] if isinstance(longest[-1], str) and longest[-1].startswith("... ") \
            else None
        items = longest[:-1] if note else longest
        hidden = int(note.split()[1]) if note else 0
        keep = max(1, len(items) // 2)
        hidden += len(items) - keep
        longest[:] = [*items[:keep], SHRUNK_NOTE.format(hidden)]
    return body


SHRUNK_NOTE = "... {} more not shown: ask for fewer (ids, a search, a smaller limit)"


def _lists(node: Any) -> list[tuple[int, list[Any]]]:
    """Every list in `node` with more than one item, with the size of its JSON."""
    if isinstance(node, list):
        found = [(len(json.dumps(node, ensure_ascii=False)), node)] if len(node) > 1 else []
        return found + [x for item in node for x in _lists(item)]
    if isinstance(node, dict):
        return [x for value in node.values() for x in _lists(value)]
    return []


@dataclass
class Conversation:
    id: str
    model: str
    target: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tried: set[str] = field(default_factory=set)
    input_tokens: int = 0
    output_tokens: int = 0
    # Offer only these tools (the model bench's smaller tool sets); None offers every tool.
    tools: frozenset[str] | None = None
    # "progressive" (observers enable tools as they see the need) or "all".
    disclosure_mode: str = "progressive"
    disclosure: Disclosure | None = None
    user_texts: list[str] = field(default_factory=list)       # what the observers read
    tool_log: list[tuple[str, Any]] = field(default_factory=list)   # (tool, structured result)
    observer_calls: int = 0                                     # observer-model calls so far
    # local_stable_tools: the tools sent with the conversation's first step, kept for every
    # later step; tools offered after it are announced in a message instead
    fixed_tools: list[str] | None = None
    announced: set[str] = field(default_factory=set)


class Agent:
    def __init__(self, settings: Settings, targets: Targets, chat: ChatClient) -> None:
        self.settings = settings
        self.targets = targets
        self.chat = chat
        self.conversations: OrderedDict[str, Conversation] = OrderedDict()
        self.policy = load_policy(settings.disclosure_policy)

    def conversation(self, conversation_id: str | None, model: str, target: str,
                     disclosure: str | None = None) -> Conversation:
        parse_model(model)
        if target not in AGENT_TARGETS:
            raise LLMError(f"the assistant can use {', '.join(AGENT_TARGETS)}, not {target!r}")
        mode = disclosure or self.settings.assistant_disclosure
        if mode not in ("progressive", "all"):
            raise LLMError(f"disclosure must be progressive or all, not {mode!r}")
        existing = self.conversations.get(conversation_id or "")
        if existing is not None:
            existing.model, existing.target, existing.disclosure_mode = model, target, mode
            self.conversations.move_to_end(existing.id)
            return existing
        conv = Conversation(id=uuid.uuid4().hex[:12], model=model, target=target,
                            disclosure_mode=mode)
        self.conversations[conv.id] = conv
        while len(self.conversations) > MAX_CONVERSATIONS:
            self.conversations.popitem(last=False)
        return conv

    async def warm(self, model: str, target: str, disclosure: str | None = None
                   ) -> dict[str, Any]:
        """Have a local model read what every conversation starts with (the instructions,
        discover_tools and the first tools, in the order a first step sends them) before the
        shopper asks: the first step then reads only the message and any tools observers add."""
        mode = disclosure or self.settings.assistant_disclosure
        resolved = await self.targets.resolve(target)
        async with open_session(resolved) as session:
            listed = await session.list_tools()
        tools = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in listed.tools]
        d = Disclosure.start(self.policy, tools, mode,
                             {"recipe-shopper": load_skill(self.settings.recipe_shopper_skill)})
        system = {"role": "system", "content": PREAMBLE if d.mode == "progressive"
                  else system_prompt(self.settings)}
        functions = ([DISCOVER_FUNCTION] if d.discoverable else []) + openai_tools(
            self._plan_tools(d.offered_tools(), model), lean=self._lean(model))
        return await self.chat.warm(model, [system], functions)

    async def run(self, conv: Conversation, user_text: str) -> AsyncIterator[dict[str, Any]]:
        if conv.lock.locked():
            yield {"type": "error", "message": "this conversation is already answering"}
            return
        async with conv.lock:
            async for event in self._run(conv, user_text):
                yield event

    async def _run(self, conv: Conversation, user_text: str) -> AsyncIterator[dict[str, Any]]:
        started = time.perf_counter()
        conv.messages.append({"role": "user", "content": user_text})
        conv.user_texts.append(user_text)
        steps = 0
        try:
            target = await self.targets.resolve(conv.target)
            async with open_session(target) as session:
                listed = await session.list_tools()
                tools = [t.model_dump(mode="json", by_alias=True, exclude_none=True)
                         for t in listed.tools if conv.tools is None or t.name in conv.tools]
                d = conv.disclosure
                if (d is None or d.mode != conv.disclosure_mode
                        or [t["name"] for t in d.catalog] != [t["name"] for t in tools]):
                    d = conv.disclosure = Disclosure.start(
                        self.policy, tools, conv.disclosure_mode,
                        {"recipe-shopper": load_skill(self.settings.recipe_shopper_skill)})
                yield {"type": "start", "conversation_id": conv.id, "model": conv.model,
                       "target": conv.target, "tools": list(d.offered),
                       "available": len(tools), "disclosure": d.mode,
                       "discoverable": d.discoverable}
                async for event in self._observe(conv, "turn"):
                    yield event
                # Progressive: the skill joins the conversation when an observer enables it.
                system = {"role": "system", "content": PREAMBLE if d.mode == "progressive"
                          else system_prompt(self.settings)}
                first_result = len(conv.tool_log)        # this turn's results start here
                nudged = False
                for steps in range(1, self.settings.agent_max_steps + 1):
                    # discover_tools first and the rest in the order offered: a tool an observer
                    # adds goes last, so a local model's cached prompt holds up to it
                    offered = self._plan_tools(d.offered_tools(), conv.model)
                    if self._stable_tools(conv):
                        offered, later = self._split_offered(conv, offered)
                        if later:
                            conv.messages.append({"role": "user", "content": announce_tools(
                                later, lean=self._lean(conv.model))})
                            conv.announced.update(t["name"] for t in later)
                            yield {"type": "notice", "text": "tools announced in the conversation: "
                                   + ", ".join(t["name"] for t in later)}
                    functions = ([DISCOVER_FUNCTION] if d.discoverable else []) + openai_tools(
                        offered, lean=self._lean(conv.model))
                    while True:
                        # A model call can take minutes on a local CPU model: say what is awaited.
                        yield {"type": "thinking", "step": steps, "model": conv.model}
                        try:
                            async for item in self._call_model(conv, system, functions, steps):
                                if isinstance(item, ChatTurn):
                                    turn = item
                                else:
                                    yield item
                            break
                        except (QuotaExhausted, ModelUnavailable) as exc:
                            fallback = self._fallback(conv)
                            if fallback is None:
                                raise
                            yield {"type": "notice", "text": f"{exc} Switching to {fallback}."}
                            conv.tried.add(conv.model)
                            conv.model = fallback
                    conv.input_tokens += turn.input_tokens
                    conv.output_tokens += turn.output_tokens
                    # tokens from the provider's usage (Gemini), or Ollama's own counts in metrics
                    yield {"type": "llm_call", "step": steps, "model": conv.model,
                           "tool_calls": len(turn.tool_calls),
                           "prompt_tokens": turn.input_tokens,
                           "output_tokens": turn.output_tokens, **turn.metrics,
                           **({"reasoning": turn.reasoning[:REASONING_CHARS]}
                              if turn.reasoning else {})}
                    conv.messages.append(turn.message)
                    text = turn.text
                    if not turn.tool_calls:
                        # the plan's table, built from its result: the shopper sees it under the
                        # model's few sentences; the model's own message stays short in history
                        text = with_tables(text, plan_tables(
                            [r for _, r in conv.tool_log[first_result:]]))
                    if not turn.tool_calls and not text.strip() and turn.output_tokens \
                            and not nudged:
                        # the model wrote something that is neither text nor a readable tool
                        # call (a small model's malformed call): ask once, then go on
                        nudged = True
                        conv.messages.append({"role": "user", "content": EMPTY_REPLY_NUDGE})
                        yield {"type": "notice", "text": "the model's reply was empty or not a "
                               "readable tool call; asked it once more"}
                        continue
                    if text:
                        yield {"type": "assistant", "text": text, "step": steps}
                    if not turn.tool_calls:
                        yield self._done(conv, steps, "answered", started)
                        return
                    for call in turn.tool_calls:
                        sent = self._with_location(call["name"], call["arguments"])
                        filled = sorted(k for k in sent if k not in call["arguments"])
                        call = {**call, "arguments": sent}
                        yield {"type": "tool_call", "id": call["id"], "name": call["name"],
                               "arguments": sent, "step": steps,
                               **({"filled_by_hub": filled} if filled else {})}
                        async for event in self._tool(conv, session, call, steps):
                            yield event
                yield self._done(conv, steps, "step budget reached", started)
        except (LLMError, McpTargetError) as exc:
            yield {"type": "error", "message": str(exc)}
            yield self._done(conv, steps, "error", started)

    def _lean(self, model: str) -> bool:
        return self.settings.local_lean_tools and model.startswith("ollama:")

    def _with_location(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """A plan call gets the shopper's location (DEMO_SHOPPER_LOCATION) from the hub: the
        models never see lat/lon (``_plan_tools``), so none drops it (pantry would choose no
        stores), types it (about 30 tokens) or makes one up (a 3B model sent -74, -84). The
        model's max_km stands; without one, the shopper's."""
        loc = self.settings.shopper_location
        if not loc or canonical(name) not in PLAN_LOCATION_TOOLS:
            return arguments
        km = arguments.get("max_km")
        valid = isinstance(km, (int, float)) and 0.5 <= km <= 100     # H-Tiny sent max_km 0
        return {**arguments, "lat": loc[0], "lon": loc[1], "max_km": km if valid else loc[2]}

    def _plan_tools(self, tools: list[dict[str, Any]], model: str) -> list[dict[str, Any]]:
        """The tools with lat/lon taken out of the plan tools' parameters when the hub supplies
        the shopper's location (pantry's stores are all in Vancouver); for a local model also
        max_km (the shopper's distance stands) and verbose (the full plan is for the browser):
        two arguments a small model got wrong (max_km 0, verbose true)."""
        if not self.settings.shopper_location:
            return tools
        hidden = {"lat", "lon"} | ({"max_km", "verbose"} if self._lean(model) else set())
        out = []
        for t in tools:
            schema = t.get("inputSchema") or {}
            if canonical(t["name"]) in PLAN_LOCATION_TOOLS and "properties" in schema:
                schema = {**schema,
                          "properties": {k: ({**v, "description": COUNTRY_ARGS[k]}
                                             if k in COUNTRY_ARGS and isinstance(v, dict) else v)
                                         for k, v in schema["properties"].items()
                                         if k not in hidden},
                          **({"required": [r for r in schema["required"] if r not in hidden]}
                             if "required" in schema else {})}
                t = {**t, "inputSchema": schema}
            out.append(t)
        return out

    def _stable_tools(self, conv: Conversation) -> bool:
        return (self.settings.local_stable_tools and conv.model.startswith("ollama:")
                and conv.disclosure is not None and conv.disclosure.mode == "progressive")

    @staticmethod
    def _split_offered(conv: Conversation, offered: list[dict[str, Any]]
                       ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """(the tools block, the tools to announce now). The block is fixed at the first step:
        a tool offered later is described in a message at the end of the conversation, so the
        prompt only grows and a local model's cache (a hybrid model's checkpoints too) holds."""
        if conv.fixed_tools is None:
            conv.fixed_tools = [t["name"] for t in offered]
        fixed = set(conv.fixed_tools)
        block = [t for t in offered if t["name"] in fixed]
        later = [t for t in offered if t["name"] not in fixed and t["name"] not in conv.announced]
        return block, later

    async def _tool(self, conv: Conversation, session: Any, call: dict[str, Any],
                    step: int) -> AsyncIterator[dict[str, Any]]:
        """One tool call: discover_tools answered by the hub, a tool that is not offered refused
        (a scope violation, never sent to the server), anything else run on the MCP session."""
        d = conv.disclosure
        assert d is not None
        name, added, scope = call["name"], [], None
        if name == DISCOVER and d.discoverable:
            query = str(call["arguments"].get("query", ""))
            text, added = d.discover(query)
            result = {"name": name, "is_error": False, "structured": None, "text": text,
                      "ms": 0.0, "truncated": False}
        elif not d.is_offered(name):
            scope = "not disclosed" if any(t["name"] == name for t in d.catalog) else "not allowed"
            hint = " Ask for it with discover_tools." if d.discoverable and scope == "not disclosed" else ""
            result = {"name": name, "is_error": True, "structured": None, "ms": 0.0,
                      "truncated": False,
                      "text": f"tool {name} is not available in this conversation.{hint}"}
        else:
            try:
                result = await call_tool(session, name, call["arguments"])
            except Exception as exc:  # noqa: BLE001 - the model sees the failure
                result = {"name": name, "is_error": True, "structured": None,
                          "text": f"{type(exc).__name__}: {exc}", "ms": 0, "truncated": False}
        local = conv.model.startswith("ollama:")
        limit = self.settings.local_result_chars if local else RESULT_CHARS_FOR_MODEL
        compact = (plan_for_model(result.get("structured"))
                   if local and self.settings.local_compact_plans and not result.get("is_error")
                   else None)
        content = compact or result_for_model(result, limit)
        # model_chars: how much of the result the model reads (shrunk or cut to its limit)
        yield {"type": "tool_result", "id": call["id"], **result, "step": step,
               "model_chars": len(content)}
        conv.messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})
        if scope:
            yield {"type": "notice", "text": f"scope violation: {name} ({scope})"}
        elif added:
            yield {"type": "tools_offered", "added": added, "removed": [],
                   "reason": f"discover_tools:{query}"}
        elif name != DISCOVER:
            conv.tool_log.append((canonical(name), result.get("structured")))
            async for event in self._observe(conv, "tool_result"):
                yield event

    async def _observe(self, conv: Conversation, trigger: str) -> AsyncIterator[dict[str, Any]]:
        """The observers' reports for ``trigger``. LLM conditions are judged in one call to the
        observer model (announced first: the browser shows the observers reading); an enabled
        goal or skill joins the conversation as a message after the cached prefix, so the
        model's prompt cache holds. ``all`` runs no observers."""
        d = conv.disclosure
        assert d is not None
        if d.mode == "all":
            return
        view = self._view(conv)
        due = d.due_llm(trigger)
        judge: Judge | None = None
        if due and self.settings.observer_model \
                and conv.observer_calls < self.settings.observer_max_calls:
            yield {"type": "observing", "trigger": trigger, "model": self.settings.observer_model,
                   "observers": sorted({c.observer.name for c in due})}
            judge = self._judge(conv)
        events = await d.observe(trigger, view, judge)
        failed = next((ev for key, (value, ev) in getattr(judge, "last", {}).items()
                       if ev.startswith("observer model failed")), None)
        if failed:
            yield {"type": "notice", "text": f"Observers could not judge this turn: {failed}"}
        for event in events:
            if event["type"] == "goal_enabled":
                where = event["reason"].removeprefix("observer:")
                body = (f"follow the {event['skill']} procedure:\n\n{event['text']}"
                        if event.get("skill") else event["text"])
                conv.messages.append({"role": "user", "content": f"{GOAL_PREFIX} ({where}): {body}"})
                if event.get("skill"):       # the browser gets the name, not 2,000 tokens
                    event["text"] = f"the {event['skill']} procedure"
            yield event

    def _judge(self, conv: Conversation) -> Judge:
        """The observer model, judging every llm condition due in one JSON-schema call. A
        failure makes them unknown (nothing fires): observers never block the answer."""
        async def judge(conditions: list[Condition], view: View
                        ) -> dict[str, tuple[bool | None, str]]:
            conv.observer_calls += 1
            messages = [{"role": "system", "content": JUDGE_SYSTEM},
                        {"role": "user", "content": judge_prompt(conditions, view)}]
            try:
                turn = await self.chat.complete(self.settings.observer_model, messages, [],
                                                json_schema=REPORT_SCHEMA)
                out = parse_reports(turn.text, conditions)
            except LLMError as exc:
                out = {c.key: (None, f"observer model failed: {exc}") for c in conditions}
            judge.last = out  # type: ignore[attr-defined]
            return out

        return judge

    @staticmethod
    def _view(conv: Conversation) -> View:
        """What the observers see: the shopper's messages, the tools called and their last
        results, and a numbered transcript (text, tool calls, results cut short) for the LLMs."""
        transcript: list[str] = []
        names: dict[str, str] = {}
        for message in conv.messages:
            role, content = message.get("role"), str(message.get("content") or "")
            if role == "user" and not content.startswith((GOAL_PREFIX, TOOLS_PREFIX,
                                                          EMPTY_REPLY_NUDGE)):
                transcript.append(f"[{len(transcript) + 1}] shopper: {content[:1500]}")
            elif role == "assistant":
                if content:
                    transcript.append(f"[{len(transcript) + 1}] assistant: {content[:1500]}")
                for call in message.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    names[str(call.get("id"))] = str(fn.get("name"))
                    transcript.append(f"[{len(transcript) + 1}] assistant calls {fn.get('name')}"
                                      f"({str(fn.get('arguments'))[:300]})")
            elif role == "tool":
                transcript.append(f"[{len(transcript) + 1}] "
                                  f"{names.get(str(message.get('tool_call_id')), 'tool')} "
                                  f"returned: {content[:300]}")
        return View(user_messages=list(conv.user_texts), tool_calls=[n for n, _ in conv.tool_log],
                    results={n: r for n, r in conv.tool_log if isinstance(r, dict)},
                    transcript=transcript)

    async def _call_model(self, conv: Conversation, system: dict[str, Any],
                          functions: list[dict[str, Any]], step: int
                          ) -> AsyncIterator[dict[str, Any] | ChatTurn]:
        """The model call, yielding its progress events while it runs and its turn at the end. If
        the listener goes away (the browser closed the stream), the call is cancelled."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        def on_progress(update: dict[str, Any]) -> None:
            queue.put_nowait({"type": "progress", "step": step, "model": conv.model, **update})

        call = asyncio.ensure_future(self.chat.complete(
            conv.model, [system, *conv.messages], functions, on_progress=on_progress))
        try:
            while not call.done() or not queue.empty():
                if queue.empty():
                    waiter = asyncio.ensure_future(queue.get())
                    await asyncio.wait({call, waiter}, return_when=asyncio.FIRST_COMPLETED)
                    if not waiter.done():
                        waiter.cancel()
                        continue
                    yield waiter.result()
                else:
                    yield queue.get_nowait()
            yield call.result()
        finally:
            if not call.done():
                call.cancel()

    def _fallback(self, conv: Conversation) -> str | None:
        """The next model to try when ``conv.model`` is out of quota or overloaded: the configured
        fallbacks, in order, skipping the current one and any already tried. A local model is
        never swapped for a cloud one behind the shopper's back."""
        if parse_model(conv.model)[0] != "gemini":
            return None
        for spec in self.settings.agent_fallbacks:
            if spec != conv.model and spec not in conv.tried:
                return spec
        return None

    @staticmethod
    def _done(conv: Conversation, steps: int, stop: str, started: float) -> dict[str, Any]:
        return {"type": "done", "steps": steps, "stop": stop,
                "seconds": round(time.perf_counter() - started, 1),
                "input_tokens": conv.input_tokens, "output_tokens": conv.output_tokens}
