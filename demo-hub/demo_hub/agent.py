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

from demo_hub.answers import (
    cart_note,
    cart_stores,
    cart_total,
    is_plan,
    plan_card,
    plan_cards,
    plan_for_model,
    plan_tables,
    recipe_key,
    strip_tables,
    with_tables,
)
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
    visible_tools,
)
from demo_hub.llm import (
    ChatClient,
    ChatTurn,
    LLMError,
    ModelUnavailable,
    QuotaExhausted,
    parse_model,
)
from demo_hub.mcp_targets import TEXT_LIMIT, McpTargetError, Targets, call_tool, open_session
from demo_hub.observers import Condition, Policy, View
from demo_hub.settings import Settings

MAX_CONVERSATIONS = 50
GOAL_PREFIX = "Goal enabled by observation"
RESULT_CHARS_FOR_MODEL = 16_000
REASONING_CHARS = 8_000            # a step's reasoning sent to the browser and kept in its trace
# What of a plan result the model reads (C10 in the plan). FOR_BROWSER: summary fields for the
# browser's trace views, never sent to the model. SERVER_ONLY: kept by the hub alone (in
# tool_log), never sent to the model nor the browser: the plan's basis, which the cart's
# Options dialog and a swap are built from, and which no client is trusted to send back.
# FOR_MODEL_NESTED: paths inside the summary for the browser only ("*" is every item of a list).
FOR_BROWSER = frozenset({"llm_calls", "burr_run", "pipeline"})
SERVER_ONLY = frozenset({"basis"})
FOR_MODEL_NESTED = frozenset({("nutrition", "lines"), ("days", "*", "nutrition", "lines")})
AGENT_TARGETS = ("gateway-recipes", "pantry", "gateway-sim")
# tools that take the shopper's location: the hub always sends it (models dropped it, typed it
# and made it up: lat -74, lon -84; lon +123.11), and the distance for the two plan tools
LOCATION_TOOLS = {"plan_recipe", "plan_from_text", "plan_week", "find_product", "get_product"}
PLAN_LOCATION_TOOLS = {"plan_recipe", "plan_from_text"}
# plan tools that return the plan's basis when asked (basis=true): the hub always asks, when the
# target's schema takes it, and the model never sees the argument
BASIS_TOOLS = {"plan_recipe", "plan_from_text", "plan_from_lines"}
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
- A library recipe: call plan_recipe with its slug from list_recipes (beef_rice_bowl, not
  beef-rice-bowl). The shopper's location and distance are added for you.
- To see which recipes can be planned, call list_recipes.
- A dish the shopper names that is not in list_recipes and comes without a recipe or link: write
  a short recipe for it (a title with the servings, then one "- ingredient" line each) and plan
  it with plan_from_text, allow_partial true.
- A recipe link or a pasted recipe: follow the recipe-shopper procedure.
- If a plan call fails or times out, call it again with the same arguments; if its error says
  to retry with allow_partial=true, do that instead.
- A line's trip_store and trip_price are where the recommended trip buys it; its store and price
  are only its cheapest offer in range. With no trip, the plan chose no stores: say so.
- Origin: report the plan's own origin_status and coverage; call get_product_origins only with the
  basket's product_ids.
- A message that starts with [cart] reports a product the shopper swapped in the cart themselves:
  use its figures for that cart from then on. Do not plan the recipe again unless asked; if you
  do, say that a new plan drops the shopper's swaps.

After a plan or a week plan, answer in two or three sentences: the trip's total and its store(s),
the verified origin share when origin was asked about, and anything left out. For an ingredient
left out because of an exclusion, name the swap the plan lists for it and ask whether to add it
or relax the exclusion. After
list_recipes alone, answer in one sentence. The shopper sees the plan's table, or the recipe
list, under your answer, added automatically: do not write a table or list the lines or recipes.
For other questions (a product's price, where it comes from), call the matching tool and report
its result briefly.

You cannot run scripts or open a shell. Read a recipe page with the fetch tool (markdown,
max_length 20000; call again with start_index if it is cut before the ingredient list).
"""


def takes_basis(tool: dict[str, Any]) -> bool:
    """A plan tool (BASIS_TOOLS) whose listed schema has the `basis` flag."""
    properties = (tool.get("inputSchema") or {}).get("properties")
    return canonical(tool["name"]) in BASIS_TOOLS and isinstance(properties, dict) \
        and "basis" in properties


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
REPEATED_CALL = ("You already called {name} with these arguments in this turn; its result is "
                 "above. Answer the shopper from it, or call a different tool.")
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


def _without_path(node: Any, path: tuple[str, ...]) -> Any:
    """``node`` without the value at ``path`` ("*" walks every item of a list); a copy only
    where something is taken out, and unchanged where the path is absent."""
    if not path:
        return node
    head, rest = path[0], path[1:]
    if head == "*":
        return [_without_path(x, rest) for x in node] if isinstance(node, list) else node
    if not isinstance(node, dict) or head not in node:
        return node
    if not rest:
        return {k: v for k, v in node.items() if k != head}
    return {**node, head: _without_path(node[head], rest)}


def without(structured: Any, keys: frozenset[str] | set[str],
            nested: frozenset[tuple[str, ...]] = frozenset()) -> Any:
    """A plan or week result with ``keys`` taken out of its summary and its full plan, and the
    ``nested`` paths out of its summary. Anything else comes back as it is."""
    if not isinstance(structured, dict):
        return structured
    out = structured
    for part in ("summary", "full"):
        body = out.get(part)
        if isinstance(body, dict) and keys & body.keys():
            out = {**out, part: {k: v for k, v in body.items() if k not in keys}}
    summary = out.get("summary")
    if isinstance(summary, dict):
        for path in nested:
            summary = _without_path(summary, path)
        if summary is not out["summary"]:
            out = {**out, "summary": summary}
    return out


def model_copy(structured: Any) -> Any:
    """What the model reads of a result: no basis, nothing for the browser's views alone."""
    return without(structured, SERVER_ONLY | FOR_BROWSER, FOR_MODEL_NESTED)


def for_browser(result: dict[str, Any]) -> dict[str, Any]:
    """A tool result as the browser (and the turn's trace) gets it: everything but the basis.
    The MCP result's text is the same JSON as its structured content, basis included, so it is
    written again from the stripped copy."""
    body = result.get("structured")
    stripped = without(body, SERVER_ONLY)
    if stripped is body:
        return result
    text = json.dumps(stripped, ensure_ascii=False, indent=2)
    return {**result, "structured": stripped, "text": text[:TEXT_LIMIT],
            "truncated": len(text) > TEXT_LIMIT}


def result_for_model(result: dict[str, Any], limit: int = RESULT_CHARS_FOR_MODEL) -> str:
    """The tool result as the model reads it: the structured content as JSON (or the text), at
    most `limit` characters. A plan summary's `llm_calls` and `burr_run` (where pantry's LLM time
    went and its Burr trace, for the browser) and its basis (the hub's, for the cart) are left
    out (``model_copy``): the model has no use for them and pays for every token. JSON over
    the limit is shrunk by shortening its longest lists, so it stays valid and says what was left
    out; only what still does not fit is cut."""
    body = model_copy(result.get("structured"))
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


# The cart's follow-ups (rank_alternatives, reprice_plan) always go to pantry directly: the
# hub's own token, no gateway schema in the way, and the same database the plan used.
CART_TARGET = "pantry"
CART_GONE = ("This conversation is gone (the hub restarted or forgot it): ask again to "
             "re-plan.")


class CartError(RuntimeError):
    """A cart route's refusal, with the HTTP status the route answers."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class PendingChange:
    """A swap the model has not heard about yet. One per (recipe, purchase line): a later swap of
    the same line replaces `now` and keeps `was`, the product the model last knew, and every
    swap updates the cart's figures on all of its recipe's changes."""
    recipe: str                     # answers.recipe_key of the cart
    recipe_name: str
    line_no: int                    # the purchase's own line
    lines: list[int]                # every recipe line the purchase covers
    ingredient: str
    was: dict[str, Any]             # {id, name}: the product the model last knew
    now: dict[str, Any]             # {id, name}: the product in the cart now
    total_before: float | None      # the cart's total when the model last knew it
    ref: int = 0                    # the cart's tool_log index now
    total_after: float | None = None
    stores_after: list[str] = field(default_factory=list)
    undone: bool = False            # back to the planner's own pick
    summary: dict[str, Any] = field(default_factory=dict)

    def note(self) -> str:
        return cart_note(recipe=self.recipe_name, line_no=self.line_no,
                         ingredient=self.ingredient, was=str(self.was.get("name")),
                         now=str(self.now.get("name")), before=self.total_before,
                         after=self.total_after, stores=self.stores_after, undone=self.undone)

    def event(self) -> dict[str, Any]:
        """The cart_change event: what changed, the cart's figures before and after, the note
        the model read, and the re-priced plan (``structured``, as a plan tool returns it) so
        the turn's grounding evals know the new figures."""
        return {"type": "cart_change", "ref": self.ref, "line_no": self.line_no,
                "lines": list(self.lines), "recipe_name": self.recipe_name,
                "ingredient": self.ingredient, "from": dict(self.was), "to": dict(self.now),
                "total_before": self.total_before, "total_after": self.total_after,
                "stores_after": list(self.stores_after), "undone": self.undone,
                "note": self.note(), "structured": {"summary": self.summary, "full": None}}


def _purchase(summary: dict[str, Any], line_no: int) -> dict[str, Any] | None:
    """The cart line buying recipe line ``line_no`` (its own line or one of its also_lines)."""
    for ln in summary.get("lines") or []:
        if ln.get("line_no") == line_no or line_no in (ln.get("also_lines") or []):
            return ln
    return None


def _product(ln: dict[str, Any] | None) -> dict[str, Any]:
    return {"id": ln.get("product_id"), "name": ln.get("product")} if ln else \
        {"id": None, "name": "nothing"}


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
    turn_calls: set[tuple[str, str]] = field(default_factory=set)   # (tool, arguments) this turn
    # the target's tools whose schema takes `basis` (BASIS_TOOLS), as listed this turn: only
    # those are sent basis=true, so a gateway with an older schema is never sent an unknown
    # argument (its plans then carry no basis, and the cart offers no Options)
    basis_tools: set[str] = field(default_factory=set)
    # The cart: the shopper's pins per plan (tool_log index -> line_no -> product_id), and the
    # swaps the model has not been told about, (recipe, line) -> change, told before the
    # shopper's next message.
    pins: dict[int, dict[int, int]] = field(default_factory=dict)
    pending: dict[tuple[str, int], PendingChange] = field(default_factory=dict)


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
        tools = visible_tools(self.policy, [t.model_dump(mode="json", by_alias=True,
                                                         exclude_none=True) for t in listed.tools])
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
        # Swaps since the last turn reach the model as [cart] notes before the shopper's words
        # (appended, so the conversation's earlier messages, and a model's cached prompt, stay
        # as they were); the observers read only the shopper's words.
        changes = list(conv.pending.values())
        conv.pending.clear()
        notes = "\n".join(c.note() for c in changes)
        conv.messages.append({"role": "user",
                              "content": f"{notes}\n\n{user_text}" if notes else user_text})
        conv.user_texts.append(user_text)
        steps = 0
        told = False
        try:
            target = await self.targets.resolve(conv.target)
            async with open_session(target) as session:
                listed = await session.list_tools()
                tools = [t.model_dump(mode="json", by_alias=True, exclude_none=True)
                         for t in listed.tools if conv.tools is None or t.name in conv.tools]
                tools = visible_tools(self.policy, tools)
                conv.basis_tools = {t["name"] for t in tools if takes_basis(t)}
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
                told = True
                for change in changes:
                    yield change.event()
                async for event in self._observe(conv, "turn"):
                    yield event
                # Progressive: the skill joins the conversation when an observer enables it.
                system = {"role": "system", "content": PREAMBLE if d.mode == "progressive"
                          else system_prompt(self.settings)}
                first_result = len(conv.tool_log)        # this turn's results start here
                nudged = False
                conv.turn_calls.clear()
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
                    cards: list[dict[str, Any]] = []
                    if not turn.tool_calls:
                        # the plan's table, built from its result: the shopper sees it under the
                        # model's few sentences; the model's own message stays short in history.
                        # The browser draws the plans themselves (`plans`) under `reply`.
                        results = [r for _, r in conv.tool_log[first_result:]]
                        cards = plan_cards(results, drop=FOR_BROWSER | SERVER_ONLY,
                                           start=first_result)
                        text = with_tables(text, plan_tables(results))
                    if not turn.tool_calls and not text.strip() and turn.output_tokens \
                            and not nudged:
                        # the model wrote something that is neither text nor a readable tool
                        # call (a small model's malformed call): ask once, then go on
                        nudged = True
                        conv.messages.append({"role": "user", "content": EMPTY_REPLY_NUDGE})
                        yield {"type": "notice", "text": "the model's reply was empty or not a "
                               "readable tool call; asked it once more"}
                        continue
                    if turn.finish_reason == "length" and not turn.tool_calls:
                        yield {"type": "notice", "text": f"the model's reply was cut at "
                               f"{turn.output_tokens} tokens (DEMO_LOCAL_MAX_TOKENS)"}
                    if text:
                        yield {"type": "assistant", "text": text, "step": steps,
                               **({"reply": strip_tables(turn.text), "plans": cards}
                                  if cards else {})}
                    if not turn.tool_calls:
                        yield self._done(conv, steps, "answered", started)
                        return
                    for call in turn.tool_calls:
                        sent = self._with_hub_args(call["name"], call["arguments"],
                                                   call["name"] in conv.basis_tools)
                        filled = sorted(k for k in sent if k not in call["arguments"])
                        call = {**call, "arguments": sent}
                        yield {"type": "tool_call", "id": call["id"], "name": call["name"],
                               "arguments": sent, "step": steps,
                               **({"filled_by_hub": filled} if filled else {})}
                        async for event in self._tool(conv, session, call, steps):
                            yield event
                # out of steps after the work was done (a 3B model planned, then kept calling
                # tools): the shopper still gets the plan, drawn from its result
                results = [r for _, r in conv.tool_log[first_result:]]
                tables = plan_tables(results)
                if tables:
                    reply = "The model did not finish its summary; here is the plan it made."
                    cards = plan_cards(results, drop=FOR_BROWSER | SERVER_ONLY,
                                       start=first_result)
                    yield {"type": "assistant", "step": steps, "text": with_tables(reply, tables),
                           **({"reply": reply, "plans": cards} if cards else {})}
                yield self._done(conv, steps, "step budget reached", started)
        except (LLMError, McpTargetError) as exc:
            if not told:                # the turn failed before it started: still say so
                for change in changes:
                    yield change.event()
            yield {"type": "error", "message": str(exc)}
            yield self._done(conv, steps, "error", started)

    # --- the cart: alternatives and swaps, no model involved ------------------------------------

    def _cart(self, conversation_id: str) -> Conversation:
        if not self.settings.cart_alternatives:
            raise CartError("cart alternatives are off (DEMO_CART_ALTERNATIVES=0)", 404)
        conv = self.conversations.get(conversation_id)
        if conv is None:
            raise CartError(CART_GONE, 404)
        return conv

    @staticmethod
    def _plan_at(conv: Conversation, ref: int) -> tuple[dict[str, Any], dict[str, Any]]:
        """(summary, basis) of the plan at tool_log[ref]."""
        if not 0 <= ref < len(conv.tool_log):
            raise CartError(f"this conversation has no result {ref}", 404)
        structured = conv.tool_log[ref][1]
        if not is_plan(structured):
            raise CartError(f"result {ref} is not a recipe plan", 422)
        summary = structured["summary"]
        basis = summary.get("basis")
        if not isinstance(basis, dict):
            raise CartError("this plan came back without its basis (an older gateway schema?), "
                            "so its lines have no options; ask again to re-plan", 422)
        return summary, basis

    @staticmethod
    def _newer(conv: Conversation, ref: int, summary: dict[str, Any]) -> bool:
        """A later plan, or a later swap, of the same recipe: the cart at ref is not the
        shopper's latest."""
        key = recipe_key(summary)
        return any(is_plan(r) and recipe_key(r["summary"]) == key
                   for _, r in conv.tool_log[ref + 1:])

    async def _pantry(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """One call to pantry's own MCP server; its ToolError text is the 422's detail."""
        try:
            target = await self.targets.resolve(CART_TARGET)
            async with open_session(target) as session:
                result = await call_tool(session, tool, arguments)
        except McpTargetError as exc:
            raise CartError(f"pantry is not reachable: {exc}", 502) from exc
        if result.get("is_error"):
            raise CartError(str(result.get("text") or f"{tool} failed"), 422)
        structured = result.get("structured")
        if not isinstance(structured, dict):
            raise CartError(f"pantry's {tool} answered without data", 502)
        return structured

    @staticmethod
    def _basis_with_pins(basis: dict[str, Any], pins: dict[int, int]) -> dict[str, Any]:
        return {**basis, "pins": [{"line_no": n, "product_id": p} for n, p in sorted(pins.items())]}

    async def alternatives(self, conversation_id: str, ref: int, line_no: int,
                           limit: int = 12) -> dict[str, Any]:
        """pantry's ranking of the products that could fill one line of the cart at ``ref``,
        from the plan's basis and the shopper's pins there. Read-only: no lock, no change to the
        conversation or its trace, no model call."""
        conv = self._cart(conversation_id)
        _, basis = self._plan_at(conv, ref)
        return await self._pantry("rank_alternatives", {
            "basis": self._basis_with_pins(basis, conv.pins.get(ref, {})),
            "line_no": line_no, "limit": limit})

    async def swap(self, conversation_id: str, ref: int, line_no: int,
                   product_id: int | None) -> dict[str, Any]:
        """The shopper chose ``product_id`` for a line of the cart at ``ref`` (None: back to the
        planner's pick). pantry re-prices the plan with the shopper's pins (no LLM); the result
        joins tool_log as the cart's new ref, and the change waits for the model's next turn
        (``pending``). Returns {card, note}: the redrawn cart and the note the model will read.
        """
        conv = self._cart(conversation_id)
        if conv.target == "gateway-sim":
            raise CartError("cart changes need the pantry server; this conversation plans "
                            "through the pantry-sim gateway", 409)
        if conv.lock.locked():
            raise CartError("The assistant is answering; choose again when it finishes.", 409)
        summary, basis = self._plan_at(conv, ref)
        if self._newer(conv, ref, summary):
            raise CartError("This cart is older than the latest plan for this recipe.", 409)
        purchase = _purchase(summary, line_no)
        if purchase is None:
            raise CartError(f"line {line_no} is not in this cart", 422)
        lines = [int(purchase["line_no"]), *(int(n) for n in purchase.get("also_lines") or [])]
        planner = {int(b["line_no"]): b.get("product_id") for b in basis.get("lines") or []}
        async with conv.lock:
            # a purchase covering several recipe lines is chosen for all of them
            pins = dict(conv.pins.get(ref, {}))
            for n in lines:
                if product_id is None or product_id == planner.get(n):
                    pins.pop(n, None)
                else:
                    pins[n] = product_id
            # the whole pin set, on a basis without pins: an undone pin is simply left out
            structured = await self._pantry("reprice_plan", {
                "basis": self._basis_with_pins(basis, {}),
                "pins": [{"line_no": n, "product_id": p} for n, p in sorted(pins.items())]})
            if not is_plan(structured):
                raise CartError("pantry's reprice_plan answered without a plan", 502)
            conv.tool_log.append(("reprice_plan", structured))
            new_ref = len(conv.tool_log) - 1
            new = structured["summary"]
            new_basis = new.get("basis") if isinstance(new.get("basis"), dict) else {}
            conv.pins[new_ref] = {int(p["line_no"]): int(p["product_id"])
                                  for p in new_basis.get("pins") or []}
            change = self._queue(conv, summary, new, new_ref, purchase, lines)
        card = plan_card("plan", new, new_ref, FOR_BROWSER | SERVER_ONLY)
        return {"card": card, "note": change.note() if change else ""}

    @staticmethod
    def _queue(conv: Conversation, old: dict[str, Any], new: dict[str, Any], new_ref: int,
               purchase: dict[str, Any], lines: list[int]) -> PendingChange | None:
        """Record the swap for the model's next turn, coalesced per (recipe, line): the model
        hears once, from what it last knew to what the cart is now. A line put back as the model
        last knew it is not mentioned at all. Returns the change (None when nothing is left to
        tell for this line)."""
        recipe = recipe_key(old)
        line_no = int(purchase["line_no"])
        same_recipe = [c for (r, _), c in conv.pending.items() if r == recipe]
        before = same_recipe[0].total_before if same_recipe else cart_total(old)
        earlier = conv.pending.get((recipe, line_no))
        now = _product(_purchase(new, line_no))
        pinned = {int(p["line_no"]) for p in (new.get("basis") or {}).get("pins") or []}
        change = PendingChange(
            recipe=recipe, recipe_name=str(new.get("recipe_name") or recipe), line_no=line_no,
            lines=lines, ingredient=str(purchase.get("ingredient") or ""),
            was=earlier.was if earlier else _product(purchase), now=now, total_before=before,
            undone=not pinned & set(lines))
        conv.pending[(recipe, line_no)] = change
        if change.was.get("id") == now.get("id"):
            del conv.pending[(recipe, line_no)]
        cleaned = {k: v for k, v in new.items() if k not in FOR_BROWSER | SERVER_ONLY}
        for c in [*same_recipe, change]:       # the cart's figures are the latest swap's
            c.ref, c.summary = new_ref, cleaned
            c.total_after, c.stores_after = cart_total(new), cart_stores(new)
        return conv.pending.get((recipe, line_no))

    def _lean(self, model: str) -> bool:
        return self.settings.local_lean_tools and model.startswith("ollama:")

    def _with_hub_args(self, name: str, arguments: dict[str, Any],
                       takes_basis: bool = False) -> dict[str, Any]:
        """The arguments the hub fills in, whatever the model sent.

        A location-taking tool (LOCATION_TOOLS) gets the shopper's location
        (DEMO_SHOPPER_LOCATION) from the hub: the models never see lat/lon (``_plan_tools``), so
        none drops it (pantry would choose no stores), types it (about 30 tokens) or makes one up
        (a 3B model sent -74, -84; the 8B sent lon +123.11). A plan's valid max_km stands;
        without one, or outside 0.5-100 km, the shopper's.

        A plan tool whose schema takes it (``takes_basis``) gets basis=true while cart
        alternatives are on (DEMO_CART_ALTERNATIVES): the plan's basis comes back for the hub to
        keep, so the cart can rank and re-price a line without the model. The model never sees
        the argument, and one it sends anyway is not passed on."""
        tool = canonical(name)
        out = dict(arguments)
        if tool in BASIS_TOOLS:
            out.pop("basis", None)
            if takes_basis and self.settings.cart_alternatives:
                out["basis"] = True
        loc = self.settings.shopper_location
        if not loc or tool not in LOCATION_TOOLS:
            return out
        out.update(lat=loc[0], lon=loc[1])
        if tool in PLAN_LOCATION_TOOLS:
            km = arguments.get("max_km")
            valid = isinstance(km, (int, float)) and 0.5 <= km <= 100     # H-Tiny sent max_km 0
            out["max_km"] = km if valid else loc[2]
        return out

    def _plan_tools(self, tools: list[dict[str, Any]], model: str) -> list[dict[str, Any]]:
        """The tools as the model sees them. A plan tool never shows `basis` (the hub sets it).
        When the hub supplies the shopper's location (pantry's stores are all in Vancouver),
        lat/lon are taken out of every location-taking tool's parameters, and for a local model
        also the plan tools' max_km (the shopper's distance stands) and verbose (the full plan
        is for the browser): two arguments a small model got wrong (max_km 0, verbose true).
        The country lists say what they take."""
        located = bool(self.settings.shopper_location)
        lean = self._lean(model)
        out = []
        for t in tools:
            schema = t.get("inputSchema") or {}
            tool = canonical(t["name"])
            hidden = {"basis"} if tool in BASIS_TOOLS else set()
            locating = located and tool in LOCATION_TOOLS
            if locating:
                hidden |= {"lat", "lon"} | ({"max_km", "verbose"}
                                            if lean and tool in PLAN_LOCATION_TOOLS else set())
            properties = schema.get("properties")
            if isinstance(properties, dict) and (locating or hidden & properties.keys()):
                schema = {**schema,
                          "properties": {k: ({**v, "description": COUNTRY_ARGS[k]}
                                             if locating and k in COUNTRY_ARGS
                                             and isinstance(v, dict) else v)
                                         for k, v in properties.items() if k not in hidden},
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
        key = (name, json.dumps(call["arguments"], sort_keys=True))
        repeated = key in conv.turn_calls
        conv.turn_calls.add(key)
        if repeated:
            # the same call again in this turn (the 8B sent one plan_recipe eight times): not run
            # again; the model is pointed at the result it already has
            result = {"name": name, "is_error": False, "structured": None, "ms": 0.0,
                      "truncated": False, "text": REPEATED_CALL.format(name=name)}
        elif name == DISCOVER and d.discoverable:
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
        # model_chars: how much of the result the model reads (shrunk or cut to its limit). The
        # browser and the trace get the result without its basis, which only tool_log keeps.
        yield {"type": "tool_result", "id": call["id"], **for_browser(result), "step": step,
               "model_chars": len(content)}
        conv.messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})
        if repeated:
            yield {"type": "notice", "text": f"repeated call: {name} with the same arguments was "
                   "not run again"}
        elif scope:
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
