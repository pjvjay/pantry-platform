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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from demo_hub import meal_plans
from demo_hub.answers import (
    cart_key,
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
from demo_hub.observers import Condition, Policy, View, normalize
from demo_hub.recipe_import import Importer, ImportFailure, RecipeDoc, import_note
from demo_hub.recipe_import import result as import_result
from demo_hub.recipe_import.youtube import video_id
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
# A meal plan's whole draft (top level) and the ops its card applies (summary.ops) are for the
# browser too.
FOR_BROWSER = frozenset({"llm_calls", "burr_run", "pipeline", "draft"})
SERVER_ONLY = frozenset({"basis"})
FOR_MODEL_NESTED = frozenset({("nutrition", "lines"), ("days", "*", "nutrition", "lines"),
                              ("ops",)})
AGENT_TARGETS = ("gateway-recipes", "pantry", "gateway-sim")
# tools that take the shopper's location: the hub always sends it (models dropped it, typed it
# and made it up: lat -74, lon -84; lon +123.11), and the distance for the two plan tools
LOCATION_TOOLS = {"plan_recipe", "plan_from_text", "plan_from_lines", "plan_week", "find_product",
                  "get_product", "plan_meals"}
PLAN_LOCATION_TOOLS = {"plan_recipe", "plan_from_text", "plan_from_lines", "plan_meals"}
# the observer whose condition has the hub read counted dishes before the model (meal_plans)
MEAL_OBSERVER = "meal_planner"
# plan_from_lines' reviewed recipe: the model names it by doc_key and the hub fills these in from
# the conversation's docs (an imported link, or the console's reviewed doc), so the model never
# retypes a line and cannot change an amount the shopper reviewed
DOC_ARGS = frozenset({"lines", "title", "servings"})
# a link in the shopper's message, as link_reader's pattern finds it, without the punctuation a
# sentence puts after it
LINK = re.compile(r"https?://\S+")
# plan tools that return the plan's basis when asked (basis=true). When the target's schema
# takes it, the hub asks on plan_from_lines always (import_grounded compares that basis with the
# reviewed doc) and on the others while cart alternatives are on; the model never sees it
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
- A message that starts with [import] holds a recipe the hub already read from the shopper's link
  (or the shopper reviewed) as lines: plan it with plan_from_lines(doc_key=...) using the doc_key
  it names; do not fetch the link; do not retype the lines. If it says no lines were read yet,
  say in one sentence that the shopper can choose how to read them in the import card.
- Any other recipe link, or a pasted recipe: follow the recipe-shopper procedure.
- If a plan call fails or times out, call it again with the same arguments; if its error says
  to retry with allow_partial=true, do that instead.
- A line's trip_store and trip_price are where the recommended trip buys it; its store and price
  are only its cheapest offer in range. With no trip, the plan chose no stores: say so.
- Origin: report the plan's own origin_status and coverage; call get_product_origins only with the
  basket's product_ids.
- A message that starts with [meals] lists the dishes the hub read from the shopper's words: call
  plan_meals once with no dishes (the hub fills them in, with the dates and the shopper's plan).
  Then say in two or three sentences how many meals were placed and the trips' total, and ask
  about each dish that "needs your OK" and each unmatched name. Never state how long food keeps,
  and never approve a trip or offer to: the shopper applies the plan and approves trips in the
  Meal plan.
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


DEFS = "#/$defs/"


def _refs(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(DEFS):
            found.add(ref[len(DEFS):])
        for value in node.values():
            _refs(value, found)
    elif isinstance(node, list):
        for value in node:
            _refs(value, found)


def used_defs(schema: dict[str, Any]) -> dict[str, Any]:
    """``schema`` with only the ``$defs`` its properties still reach. A hidden argument's types
    stay behind otherwise: plan_meals' hidden ``current`` and ``my_recipe_docs`` carried a
    RecipeDoc, a meal plan and their parts, about 2,000 tokens a local model read for nothing."""
    defs = schema.get("$defs")
    if not isinstance(defs, dict):
        return schema
    keep: set[str] = set()
    todo: set[str] = set()
    _refs({k: v for k, v in schema.items() if k != "$defs"}, todo)
    while todo:
        name = todo.pop()
        if name not in keep and name in defs:
            keep.add(name)
            _refs(defs[name], todo)
    if keep == set(defs):
        return schema
    out = {k: v for k, v in schema.items() if k != "$defs"}
    if keep:
        out["$defs"] = {k: v for k, v in defs.items() if k in keep}
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
    if keys & (out.keys() - {"summary", "full"}):    # a meal plan's draft, beside its summary
        out = {k: v for k, v in out.items() if k not in keys}
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
# The MCP SDK puts this before a tool's own refusal; the shopper reads pantry's reason alone.
TOOL_ERROR_PREFIX = re.compile(r"^Error executing tool [\w.-]+: ")


class CartError(RuntimeError):
    """A cart route's refusal, with the HTTP status the route answers."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class PendingChange:
    """A swap the model has not heard about yet. One per (recipe, recipe line), not per purchase:
    a swap can merge a line into another line's purchase, and a later swap or undo made on that
    purchase has to reach the merged line's change too. A later swap of a line replaces `now`
    and keeps `was`, the product the model last knew, and every swap updates the cart's figures
    on all of its recipe's changes. ``as_told`` joins the lines that went from one product to the
    same other product into one note, as the cart shows them."""
    recipe: str                     # answers.cart_key of the cart
    recipe_name: str
    line_no: int                    # the recipe line (the first of `lines` once told)
    lines: list[int]                # [line_no]; once told, every line the note covers
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


def as_told(changes: list[PendingChange]) -> list[PendingChange]:
    """The pending changes as the model hears them: the lines of one cart that went from the
    same product to the same product are one note and one event (a garlic purchase covering
    lines 2 and 4 reads "line 2 (garlic + garlic clove)"), in the order they were first
    changed."""
    groups: dict[tuple[Any, ...], list[PendingChange]] = {}
    for c in changes:
        groups.setdefault((c.recipe, c.was.get("id"), c.now.get("id"), c.undone), []).append(c)
    out = []
    for same in groups.values():
        same.sort(key=lambda c: c.line_no)
        names = dict.fromkeys(c.ingredient for c in same if c.ingredient)
        out.append(replace(same[0], lines=[c.line_no for c in same],
                           ingredient=" + ".join(names)))
    return out


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
    # swaps the model has not been told about, (recipe, recipe line) -> change, told before the
    # shopper's next message.
    pins: dict[int, dict[int, int]] = field(default_factory=dict)
    pending: dict[tuple[str, int], PendingChange] = field(default_factory=dict)
    # Recipes the hub read or the shopper reviewed, by doc_key (imp:1, imp:2, ...): what
    # plan_from_lines plans. ``turn_import`` is what the hub settled in code this turn, before
    # the model's first call, for the observers; ``plan_docs`` maps a plan's tool_log index to
    # the doc it planned, and ``turn_first`` is this turn's first tool_log index (the evals).
    docs: dict[str, dict[str, Any]] = field(default_factory=dict)
    imports: int = 0
    turn_import: dict[str, Any] | None = None
    plan_docs: dict[int, str] = field(default_factory=dict)
    turn_first: int = 0
    # The console's Meal plan in brief (ChatBody.meal_plan), as sent with the latest message, in
    # memory only; and what the hub read of this turn's counted dishes (meal_plans.MealTurn).
    meal_plan: dict[str, Any] | None = None
    turn_meals: meal_plans.MealTurn | None = None


def first_link(text: str) -> str | None:
    m = LINK.search(text)
    return m.group(0).rstrip(".,;:!?)]}>'\"") if m else None


class Agent:
    def __init__(self, settings: Settings, targets: Targets, chat: ChatClient,
                 importer: Importer | None = None, meal_parser: Any = None) -> None:
        self.settings = settings
        self.targets = targets
        self.chat = chat
        self.importer = importer or Importer(settings)
        # pantry's Quick add parse, (base url, body) -> result; tests pass their own
        self.meal_parser = meal_parser or meal_plans.parse_selection
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

    async def run(self, conv: Conversation, user_text: str,
                  recipe_doc: RecipeDoc | None = None,
                  meal_plan: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
        """One turn. ``recipe_doc``: a recipe the shopper reviewed in the console's import
        sheet ("Plan this now"), joined to the conversation before the model reads anything.
        ``meal_plan``: the console's Meal plan in brief (meal_plans.MealPlanBody)."""
        if conv.lock.locked():
            yield {"type": "error", "message": "this conversation is already answering"}
            return
        async with conv.lock:
            conv.meal_plan = meal_plan
            async for event in self._run(conv, user_text, recipe_doc):
                yield event

    async def _run(self, conv: Conversation, user_text: str,
                   recipe_doc: RecipeDoc | None = None) -> AsyncIterator[dict[str, Any]]:
        started = time.perf_counter()
        # Swaps since the last turn reach the model as [cart] notes before the shopper's words
        # (appended, so the conversation's earlier messages, and a model's cached prompt, stay
        # as they were); the observers read only the shopper's words.
        changes = as_told(list(conv.pending.values()))
        conv.pending.clear()
        notes = "\n".join(c.note() for c in changes)
        conv.messages.append({"role": "user",
                              "content": f"{notes}\n\n{user_text}" if notes else user_text})
        message = conv.messages[-1]
        conv.user_texts.append(user_text)
        conv.turn_import = None
        conv.turn_meals = None
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
                # A link is read, or the console's reviewed recipe joins, in code before the
                # model's first call: the model then names the doc instead of reading the page.
                note = None
                if recipe_doc is not None and not self._plans_lines(tools):
                    # The note would name a tool this target lacks, and the model would retype
                    # the lines into plan_from_text: the very re-reading the doc is there to stop.
                    yield {"type": "error", "message": f"the {conv.target} target has no "
                           "plan_from_lines, so it cannot plan a reviewed recipe as reviewed; "
                           "choose the pantry target, or refresh the gateway's pantry tools"}
                    yield self._done(conv, steps, "error", started)
                    return
                if recipe_doc is not None:
                    note, event = self._take_doc(conv, recipe_doc)
                    yield event
                elif self._imports(tools):
                    async for item in self._pre_import(conv, user_text):
                        if isinstance(item, str):
                            note = item
                        else:
                            yield item
                if note:
                    message["content"] = "\n\n".join(x for x in (notes, note, user_text) if x)
                async for event in self._observe(conv, "turn"):
                    yield event
                # Counted dishes are read in code too, before the model's first call.
                meals_note = None
                async for item in self._pre_meal_selection(conv, user_text, d):
                    if isinstance(item, str):
                        meals_note = item
                    else:
                        yield item
                if meals_note:
                    message["content"] = "\n\n".join(x for x in (notes, note, meals_note,
                                                                 user_text) if x)
                # Progressive: the skill joins the conversation when an observer enables it.
                system = {"role": "system", "content": PREAMBLE if d.mode == "progressive"
                          else system_prompt(self.settings)}
                first_result = len(conv.tool_log)        # this turn's results start here
                conv.turn_first = first_result
                # a card's ref is what opens the cart's Options: none while cart alternatives
                # are off, though plan_from_lines still brings its basis back for the evals
                refs = first_result if self.settings.cart_alternatives else None
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
                    text = reply = turn.text
                    cards: list[dict[str, Any]] = []
                    if not turn.tool_calls and self._draft_due(conv, first_result):
                        # the model answered without planning the dishes the hub read: the hub
                        # drafts the plan itself (Granite-first, G10)
                        async for event in self._draft_meals(conv, session, steps):
                            yield event
                        text = reply = text or meal_plans.DRAFTED_REPLY
                    if not turn.tool_calls:
                        # the plan's table, built from its result: the shopper sees it under the
                        # model's few sentences; the model's own message stays short in history.
                        # The browser draws the plans themselves (`plans`) under `reply`.
                        results = [r for _, r in conv.tool_log[first_result:]]
                        cards = plan_cards(results, drop=FOR_BROWSER | SERVER_ONLY,
                                           start=refs)
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
                               **({"reply": strip_tables(reply), "plans": cards}
                                  if cards else {})}
                    if not turn.tool_calls:
                        yield self._done(conv, steps, "answered", started)
                        return
                    for call in turn.tool_calls:
                        sent = self._with_hub_args(call["name"], call["arguments"],
                                                   call["name"] in conv.basis_tools, conv.docs,
                                                   conv)
                        filled = sorted(k for k in sent if k not in call["arguments"])
                        call = {**call, "arguments": sent, "asked": call["arguments"]}
                        yield {"type": "tool_call", "id": call["id"], "name": call["name"],
                               "arguments": sent, "step": steps,
                               **({"filled_by_hub": filled} if filled else {}),
                               # what the model itself wrote, where the hub replaces some of it
                               **({"asked": call["asked"]}
                                  if canonical(call["name"]) == "plan_meals" else {})}
                        async for event in self._tool(conv, session, call, steps):
                            yield event
                # out of steps after the work was done (a 3B model planned, then kept calling
                # tools): the shopper still gets the plan, drawn from its result
                results = [r for _, r in conv.tool_log[first_result:]]
                tables = plan_tables(results)
                if tables:
                    reply = "The model did not finish its summary; here is the plan it made."
                    cards = plan_cards(results, drop=FOR_BROWSER | SERVER_ONLY, start=refs)
                    yield {"type": "assistant", "step": steps, "text": with_tables(reply, tables),
                           **({"reply": reply, "plans": cards} if cards else {})}
                yield self._done(conv, steps, "step budget reached", started)
        except (LLMError, McpTargetError) as exc:
            if not told:                # the turn failed before it started: still say so
                for change in changes:
                    yield change.event()
            yield {"type": "error", "message": str(exc)}
            yield self._done(conv, steps, "error", started)

    # --- meal plans: counted dishes read before the model, a draft when it makes none ----------

    def _meal_request(self, conv: Conversation) -> bool:
        """The meal_planner observer's code conditions hold for the shopper's newest message
        (checked here in any disclosure mode: in "all" no observer runs)."""
        view = self._view(conv)
        return any(c.check is not None and normalize(c.check(view))[0] is True
                   for o in self.policy.observers if o.name == MEAL_OBSERVER
                   for c in o.conditions)

    @staticmethod
    def _meal_tool(d: Disclosure) -> str | None:
        """plan_meals' name on this target, when the model is offered it."""
        return next((t["name"] for t in d.catalog if canonical(t["name"]) == "plan_meals"
                     and d.is_offered(t["name"])), None)

    async def _pre_meal_selection(self, conv: Conversation, user_text: str, d: Disclosure
                                  ) -> AsyncIterator[dict[str, Any] | str]:
        """Read the shopper's counted dishes with pantry's Quick add parse before the first
        model call, when meal_planner's condition holds and plan_meals is offered (an
        observer can withdraw it). Yields the meal_selection event, then the [meals] note (a
        str) when the parse matched a dish. A failed parse leaves the turn to the model."""
        if self._meal_tool(d) is None or not self._meal_request(conv):
            return
        started = time.perf_counter()
        body = meal_plans.parse_request(user_text, conv.meal_plan)
        try:
            parse = await self.meal_parser(self.settings.pantry_api_url, body)
        except Exception as exc:  # noqa: BLE001 - a failed parse never fails the turn
            yield {"type": "meal_selection", "status": "failed",
                   "error": f"{type(exc).__name__}: {exc}"[:300], "ms": _ms_since(started)}
            return
        turn = meal_plans.read_parse(parse)
        conv.turn_meals = turn
        yield {"type": "meal_selection", "status": "ok", "result": parse,
               "note": turn.note if turn.found else None, "ms": _ms_since(started)}
        if turn.found:
            yield turn.note

    def _draft_due(self, conv: Conversation, first_result: int) -> bool:
        """The hub drafts the plan itself: the parse matched dishes this turn, plan_meals is
        offered, and no plan_meals call of this turn succeeded."""
        d = conv.disclosure
        return (conv.turn_meals is not None and conv.turn_meals.found and d is not None
                and self._meal_tool(d) is not None
                and not any(tool == "plan_meals" and isinstance(r, dict)
                            for tool, r in conv.tool_log[first_result:]))

    async def _draft_meals(self, conv: Conversation, session: Any, step: int
                           ) -> AsyncIterator[dict[str, Any]]:
        """plan_meals called by the hub with the parsed dishes, after the model's reply. The
        call joins the conversation as the hub's (an assistant tool call with no arguments, then
        its result), so the model knows the draft on the next turn; the card is labelled
        "drafted from your message"."""
        d = conv.disclosure
        assert d is not None
        name = self._meal_tool(d)
        assert name is not None
        call_id = f"hub_{uuid.uuid4().hex[:8]}"
        sent = self._with_hub_args(name, {}, False, conv.docs, conv)
        conv.messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}]})
        yield {"type": "tool_call", "id": call_id, "name": name, "arguments": sent, "step": step,
               "filled_by_hub": sorted(sent), "by_hub": True}
        async for event in self._tool(conv, session, {"id": call_id, "name": name,
                                                      "arguments": sent}, step):
            yield event
        if conv.tool_log and conv.tool_log[-1][0] == "plan_meals":
            tool, structured = conv.tool_log[-1]
            conv.tool_log[-1] = (tool, meal_plans.drafted(structured))

    # --- recipe import: a link read, or a reviewed doc joined, before the model -----------------

    def _imports(self, tools: list[dict[str, Any]]) -> bool:
        """The hub reads links itself when it can (the skill's extractor is there) and the
        target can plan what it reads (plan_from_lines); otherwise links go to fetch as before."""
        return self._plans_lines(tools) and self.importer.available()[0]

    @staticmethod
    def _plans_lines(tools: list[dict[str, Any]]) -> bool:
        return any(canonical(t["name"]) == "plan_from_lines" for t in tools)

    @staticmethod
    def _next_key(conv: Conversation) -> str:
        conv.imports += 1
        return f"imp:{conv.imports}"

    async def _pre_import(self, conv: Conversation, user_text: str
                          ) -> AsyncIterator[dict[str, Any] | str]:
        """Read the first link in the shopper's message (link_reader's pattern) into a doc,
        before the first model call. Yields the recipe_import event, then the [import] note the
        model reads before the shopper's words (a str). On success the doc is conv.docs[imp:N]
        and link_reader offers plan_from_lines instead of fetch; a web page that cannot be read
        leaves the turn as before (fetch and plan_from_text). A YouTube link is never fetched:
        without lines the shopper chooses how to read it in the import card."""
        url = first_link(user_text)
        if not url:
            return
        started = time.perf_counter()
        youtube = video_id(url) is not None
        key = f"imp:{conv.imports + 1}"
        try:
            res = await self.importer.import_url(url, key)
        except Exception as exc:  # noqa: BLE001 - a failed import never fails the turn
            failure = exc if isinstance(exc, ImportFailure) else ImportFailure(
                502, "import_error", f"The hub could not read the link ({type(exc).__name__}).")
            conv.turn_import = {"ok": False, "fallback": not youtube, "code": failure.code}
            yield {"type": "recipe_import", "status": "failed", "url": url,
                   "error": {"status": failure.status, **failure.body()},
                   "fallback": not youtube, "ms": _ms_since(started)}
            if youtube:
                yield (f"[import] The hub could not read the YouTube link: {failure.message} "
                       "Do not fetch the video.")
            return
        doc = res.get("doc")
        if doc and doc.get("lines"):
            self._next_key(conv)
            conv.docs[key] = doc
            note = import_note(key, doc)
            conv.turn_import = {"ok": True, "doc_key": key, "lines": len(doc["lines"]),
                                "needs": res["needs"]}
        else:
            video = res.get("video") or {}
            why = (res.get("warnings") or ["no ingredient lines were found"])[0].rstrip(".")
            note = (f"[import] {str(video.get('title') or 'A YouTube video')[:120]}, a YouTube "
                    f"video from {str(video.get('channel') or 'an unknown channel')[:80]}: no "
                    f"ingredient lines were read ({why}). The shopper chooses how to read them "
                    "in the import card. Do not fetch the video.")
            conv.turn_import = {"ok": True, "lines": 0, "needs": res["needs"]}
        yield {"type": "recipe_import", "status": "ok", "url": url,
               "doc_key": key if key in conv.docs else None, "result": res, "note": note,
               "ms": _ms_since(started)}
        yield note

    def _take_doc(self, conv: Conversation, doc: RecipeDoc) -> tuple[str, dict[str, Any]]:
        """The console's reviewed recipe ("Plan this now") as the conversation's next imp:N:
        (the [import] note, the recipe_import event)."""
        key = self._next_key(conv)
        stored = doc.model_copy(update={"key": key})
        conv.docs[key] = stored.model_dump()
        note = import_note(key, conv.docs[key])
        conv.turn_import = {"ok": True, "doc_key": key, "lines": len(stored.lines),
                            "needs": "none", "via": "console"}
        return note, {"type": "recipe_import", "status": "ok", "via": "console",
                      "url": stored.source.url, "doc_key": key,
                      "result": import_result(stored), "note": note, "ms": 0.0}

    def turn_plans(self, conv: Conversation) -> list[dict[str, Any]]:
        """This turn's plans as the online evals need them, from the hub's own tool_log: the
        tool, the doc it planned (plan_from_lines) with that doc's reviewed lines, the basis
        lines pantry planned, and the ingredients the plan names as left out (``left_out``:
        water and ice, lines past pantry's 40-line cap, and what was not stocked or in range
        are on those lists, not among the basis lines). The basis never reaches the browser or
        the trace, so the evals get it here."""
        def project(lines: Any) -> list[dict[str, Any]]:
            return [{k: ln.get(k) for k in ("line_no", "name", "quantity", "unit")}
                    for ln in lines or []]

        def left_out(source: dict[str, Any]) -> list[str]:
            return [str(d.get("ingredient") or "") for k in ("not_stocked", "out_of_range",
                                                             "skipped")
                    for d in source.get(k) or [] if isinstance(d, dict)]

        out = []
        for i, (tool, structured) in enumerate(conv.tool_log[conv.turn_first:],
                                               start=conv.turn_first):
            if not is_plan(structured):
                continue
            summary = structured["summary"]
            basis = summary.get("basis")
            key = conv.plan_docs.get(i)
            out.append({"tool": tool, "doc_key": key,
                        "reviewed": project(conv.docs[key]["lines"]) if key in conv.docs
                        else None,
                        "lines": project(basis.get("lines")) if isinstance(basis, dict)
                        else None,
                        "left_out": left_out(basis if isinstance(basis, dict) else summary)})
        return out

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
        """A later plan, or a later swap, of the same recipe (answers.cart_key): the cart at
        ref is not the shopper's latest."""
        key = cart_key(summary)
        return any(is_plan(r) and cart_key(r["summary"]) == key
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
            reason = TOOL_ERROR_PREFIX.sub("", str(result.get("text") or ""), count=1)
            raise CartError(reason or f"{tool} failed", 422)
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
            changes = self._queue(conv, summary, new, new_ref, basis, lines)
        card = plan_card("plan", new, new_ref, FOR_BROWSER | SERVER_ONLY)
        return {"card": card, "note": "\n".join(c.note() for c in changes)}

    @staticmethod
    def _queue(conv: Conversation, old: dict[str, Any], new: dict[str, Any], new_ref: int,
               basis: dict[str, Any], lines: list[int]) -> list[PendingChange]:
        """Record the swap for the model's next turn, coalesced per (recipe, recipe line): the
        model hears once per line, from the product it last knew to the product the cart buys
        for that line now, whichever purchase buys it. A line put back as the model last knew
        it is not mentioned at all. Returns what is left to tell for the swap's lines, as the
        model will read it (``as_told``; [] when nothing is)."""
        recipe = cart_key(old)
        names = {int(b["line_no"]): str(b.get("name") or "") for b in basis.get("lines") or []}
        pinned = {int(p["line_no"]) for p in (new.get("basis") or {}).get("pins") or []}
        same_recipe = [c for (r, _), c in conv.pending.items() if r == recipe]
        before = same_recipe[0].total_before if same_recipe else cart_total(old)
        for n in lines:
            earlier = conv.pending.get((recipe, n))
            was = earlier.was if earlier else _product(_purchase(old, n))
            now = _product(_purchase(new, n))
            if was.get("id") == now.get("id"):
                conv.pending.pop((recipe, n), None)
                continue
            conv.pending[(recipe, n)] = PendingChange(
                recipe=recipe, recipe_name=str(new.get("recipe_name") or recipe_key(old)),
                line_no=n, lines=[n],
                ingredient=names.get(n) or str((_purchase(new, n) or {}).get("ingredient") or ""),
                was=was, now=now, total_before=before, undone=n not in pinned)
        cleaned = {k: v for k, v in new.items() if k not in FOR_BROWSER | SERVER_ONLY}
        for (r, _), c in conv.pending.items():     # the cart's figures are the latest swap's
            if r == recipe:
                c.ref, c.summary = new_ref, cleaned
                c.total_after, c.stores_after = cart_total(new), cart_stores(new)
        return as_told([c for (r, n), c in conv.pending.items() if r == recipe and n in lines])

    def _lean(self, model: str) -> bool:
        return self.settings.local_lean_tools and model.startswith("ollama:")

    def _with_hub_args(self, name: str, arguments: dict[str, Any],
                       takes_basis: bool = False,
                       docs: dict[str, dict[str, Any]] | None = None,
                       conv: Conversation | None = None) -> dict[str, Any]:
        """The arguments the hub fills in, whatever the model sent.

        A location-taking tool (LOCATION_TOOLS) gets the shopper's location
        (DEMO_SHOPPER_LOCATION) from the hub: the models never see lat/lon (``_plan_tools``), so
        none drops it (pantry would choose no stores), types it (about 30 tokens) or makes one up
        (a 3B model sent -74, -84; the 8B sent lon +123.11). A plan's valid max_km stands;
        without one, or outside 0.5-100 km, the shopper's.

        A plan tool whose schema takes it (``takes_basis``) gets basis=true while cart
        alternatives are on (DEMO_CART_ALTERNATIVES): the plan's basis comes back for the hub to
        keep, so the cart can rank and re-price a line without the model. plan_from_lines gets
        it whatever that setting says: the import_grounded eval compares its basis with the
        reviewed doc, and without one every import turn would go unchecked. The model never
        sees the argument, and one it sends anyway is not passed on.

        plan_from_lines gets the reviewed recipe the model named by ``doc_key`` from ``docs``
        (the conversation's): its lines, title and servings, exactly as reviewed. Lines the
        model wrote itself are never passed on; an unknown doc_key is refused in ``_tool``.

        plan_meals gets the dishes the hub read from the shopper's words, the proposals, the
        period, the console's Meal plan and tomorrow's date (``meal_plans.fill``)."""
        tool = canonical(name)
        out = dict(arguments)
        if tool == "plan_meals":
            out = meal_plans.fill(out, conv.turn_meals if conv else None,
                                  conv.meal_plan if conv else None)
        if tool == "plan_from_lines":
            for k in DOC_ARGS:
                out.pop(k, None)
            doc = (docs or {}).get(str(arguments.get("doc_key") or ""))
            if doc:
                out["lines"] = [{"name": ln["name"], "quantity": ln.get("quantity"),
                                 "unit": ln.get("unit") or "", "note": ln.get("note") or "",
                                 "text": ln.get("text") or "",
                                 "confirmed": bool(ln.get("confirmed", True)),
                                 "amount_basis": ln["amount_basis"]} for ln in doc["lines"]]
                out["title"] = doc["title"]
                if doc.get("servings"):
                    out["servings"] = doc["servings"]
        if tool in BASIS_TOOLS:
            out.pop("basis", None)
            if takes_basis and (self.settings.cart_alternatives or tool == "plan_from_lines"):
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
            if tool == "plan_from_lines":
                hidden |= DOC_ARGS
            if tool == "plan_meals":
                hidden |= meal_plans.HIDDEN
            locating = located and tool in LOCATION_TOOLS
            if locating:
                hidden |= {"lat", "lon"} | ({"max_km", "verbose"}
                                            if lean and tool in PLAN_LOCATION_TOOLS else set())
            properties = schema.get("properties")
            if isinstance(properties, dict) and (locating or hidden & properties.keys()):
                schema = used_defs({
                    **schema,
                    "properties": {k: ({**v, "description": COUNTRY_ARGS[k]}
                                       if locating and k in COUNTRY_ARGS
                                       and isinstance(v, dict) else v)
                                   for k, v in properties.items() if k not in hidden},
                    **({"required": [r for r in schema["required"] if r not in hidden]}
                       if "required" in schema else {})})
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
        elif canonical(name) == "plan_from_lines" and d.is_offered(name) \
                and str(call["arguments"].get("doc_key")) not in conv.docs:
            known = ", ".join(conv.docs) or "none yet"
            result = {"name": name, "is_error": True, "structured": None, "ms": 0.0,
                      "truncated": False,
                      "text": f"Unknown doc_key {call['arguments'].get('doc_key')!r}: "
                              f"plan_from_lines plans a recipe the hub holds, named in an "
                              f"[import] note. Known doc_keys: {known}."}
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
            if canonical(name) == "plan_meals":
                # the dishes the model sent, set against what the shopper wrote
                result = meal_plans.with_differences(result, meal_plans.differences(
                    call.get("asked") or {}, conv.turn_meals))
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
            if canonical(name) == "plan_from_lines" and not result.get("is_error"):
                conv.plan_docs[len(conv.tool_log) - 1] = str(call["arguments"].get("doc_key"))
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
                    transcript=transcript,
                    hub={"import": conv.turn_import} if conv.turn_import else {})

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


def _ms_since(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)
