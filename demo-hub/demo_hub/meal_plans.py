"""Meal plans in the Assistant: the shopper's dishes read in code before the model (G10, P8).

"3 Pepperoni Pizza + 2 Chicken Fried Rice + 3 chicken briyani + 7 mango milkshakes in 2 weeks"
is a lot for a local model to turn into tool arguments. When the meal_planner observer's
condition holds, the hub sends the shopper's words to pantry's Quick add parser
(``POST /mealplan/selection/parse``, no LLM) before the model's first call, keeps what it read
for the turn (``MealTurn``), and tells the model in one ``[meals]`` note (at most 300
characters). When the model calls ``plan_meals``, the hub fills in what the model would get
wrong (``fill``):

- ``dishes``: the parse's exact and plural matches, with the counts the shopper wrote. When the
  parse matched anything, the model's own dishes never decide what is placed: a recipe it adds
  or swaps in (Granite once replaced the "briyani" proposal with Simple Chicken Curry) is not
  placed, and every difference from the parse is listed on the result (``with_differences``)
  for the model to ask the shopper about. The model's dishes are used only when the parse
  matched nothing;
- ``proposed``: the parse's alias and fuzzy matches, which pantry never places: the shopper
  says Use or Not this on the card;
- ``days``: the period the shopper wrote ("in 2 weeks"), over the model's;
- ``current``: the console's Meal plan in brief (``ChatBody.meal_plan``, at most 64 KB, kept in
  memory only), and ``my_recipe_docs``, its own recipes' lines;
- ``start_date``: tomorrow in Vancouver, for a plan that does not exist yet.

A model that answers without calling a tool on such a turn still gets the plan: the hub calls
``plan_meals`` itself after the reply, and the card says "drafted from your message".
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

MEALS_PREFIX = "[meals]"
MEALS_NOTE_CHARS = 300
MAX_MEAL_PLAN = 64_000           # ChatBody.meal_plan, as UTF-8 JSON bytes
TIMEZONE = "America/Vancouver"
DRAFTED_LABEL = "drafted from your message"
DRAFTED_REPLY = "I drafted this plan from your message."
# plan_meals' arguments the hub always sets: the model never sees them (agent._plan_tools)
HIDDEN = frozenset({"proposed", "current", "start_date", "my_recipe_docs"})
SLOT = Literal["breakfast", "lunch", "dinner", "snack"]


# --- ChatBody.meal_plan: the console's plan in brief ------------------------------------------------

class ContextMeal(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    recipe_key: str = Field(min_length=1, max_length=100)
    date: dt.date | None = None
    slot: SLOT | None = None
    servings: int | None = Field(default=None, ge=1, le=20)
    pinned: bool = False


class ContextRecipe(BaseModel):
    key: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    kind: str = Field(default="doc", max_length=20)
    slot: SLOT = "dinner"


class ContextTrip(BaseModel):
    date: dt.date
    strategy: str | None = Field(default=None, max_length=20)


class MealPlanBody(BaseModel):
    """What the console sends with each chat message: its Meal plan's window, meals, recipes,
    the dates of approved trips (approvals stay in the console), household settings, and the
    lines of the shopper's own recipes in it (``docs``, RecipeDoc JSON by key)."""
    start_date: dt.date
    days: int = Field(ge=1, le=14)
    rev: int = Field(default=0, ge=0)
    meals: list[ContextMeal] = Field(default_factory=list, max_length=56)
    approved_trips: list[ContextTrip] = Field(default_factory=list, max_length=14)
    recipes: list[ContextRecipe] = Field(default_factory=list, max_length=12)
    prefs: dict[str, Any] | None = None
    docs: dict[str, dict[str, Any]] = Field(default_factory=dict, max_length=12)


class MealPlanRefused(ValueError):
    def __init__(self, status: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status, self.detail = status, detail


def meal_plan_body(raw: dict[str, Any]) -> dict[str, Any]:
    """ChatBody.meal_plan checked: MealPlanRefused 413 over 64 KB, 422 when it is not a plan."""
    if len(json.dumps(raw, ensure_ascii=False, default=str).encode("utf-8")) > MAX_MEAL_PLAN:
        raise MealPlanRefused(413, f"meal_plan is over {MAX_MEAL_PLAN // 1000} KB")
    try:
        return MealPlanBody.model_validate(raw).model_dump(mode="json")
    except ValidationError as exc:
        first = exc.errors(include_url=False, include_context=False)[0]
        where = ".".join(str(x) for x in first.get("loc", ()))
        raise MealPlanRefused(422, {"code": "bad_meal_plan",
                                    "message": f"meal_plan.{where}: {first.get('msg')}"}) from exc


def today() -> dt.date:
    """The shopper's today, in Vancouver (the demo's stores and shopper)."""
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime.now(ZoneInfo(TIMEZONE)).date()
    except Exception:  # noqa: BLE001 - no tz database: the machine's own date
        return dt.datetime.now(dt.UTC).astimezone().date()


# --- the parse --------------------------------------------------------------------------------------

def parse_request(text: str, context: dict[str, Any] | None) -> dict[str, Any]:
    """The body for pantry's /mealplan/selection/parse: the shopper's words, the shopper's own
    recipes in the plan (so "grandma's dal" can match one), and the household's servings."""
    recipes = []
    for r in (context or {}).get("recipes") or []:
        if r.get("kind") not in ("library", "starter") and r.get("key") in (
                (context or {}).get("docs") or {}):
            recipes.append({"key": r["key"], "title": r["title"], "slot": r.get("slot")})
    servings = ((context or {}).get("prefs") or {}).get("household_servings")
    body: dict[str, Any] = {"text": text[:8000], "recipes": recipes[:50]}
    if isinstance(servings, int) and 1 <= servings <= 20:
        body["household_servings"] = servings
    return body


async def parse_selection(pantry_api_url: str, body: dict[str, Any]) -> dict[str, Any]:
    """pantry's Quick add parse (no LLM), on the same base URL the /pantry/api proxy uses."""
    async with httpx.AsyncClient(base_url=pantry_api_url, timeout=15.0, trust_env=False) as c:
        r = await c.post("/mealplan/selection/parse", json=body)
        r.raise_for_status()
        return r.json()


@dataclass
class MealTurn:
    """What the parse read, kept for the turn: dishes to place (exact and plural matches, by
    recipe key), proposals (alias and fuzzy matches), names no single recipe fits, the period.
    ``titles`` maps each matched key to its title, for the note and the differences."""
    parse: dict[str, Any]
    dishes: list[dict[str, Any]] = field(default_factory=list)
    proposed: list[dict[str, Any]] = field(default_factory=list)
    unmatched: list[dict[str, Any]] = field(default_factory=list)
    period_days: int | None = None
    titles: dict[str, str] = field(default_factory=dict)
    # the matched items in the shopper's order, for the note: (count, title, slot, wrote|None)
    said: list[tuple[int, str, str, str | None]] = field(default_factory=list)
    note: str = ""

    @property
    def found(self) -> bool:
        return bool(self.dishes or self.proposed)


def read_parse(parse: dict[str, Any]) -> MealTurn:
    turn = MealTurn(parse=parse)
    period = parse.get("period_days")
    turn.period_days = period if isinstance(period, int) and 1 <= period <= 14 else None
    for sel in parse.get("selections") or []:
        match = sel.get("matched_as") if sel.get("status") == "matched" else None
        count = int(sel.get("count") or 1)
        if match:
            key = str(match["recipe_key"])
            turn.titles[key] = str(match.get("title") or key)
            sure = match.get("how") in ("exact", "plural") and not sel.get("needs_confirmation")
            turn.said.append((count, turn.titles[key],
                              str(sel.get("slot_hint") or match.get("slot") or "dinner"),
                              None if sure else str(sel.get("name") or sel.get("input") or "")))
            if sure:
                turn.dishes.append({"recipe": key, "count": count,
                                    **({"slot": sel["slot_hint"]} if sel.get("slot_hint")
                                       else {})})
            else:
                turn.proposed.append({"recipe_key": key, "count": count,
                                      "input": str(sel.get("name") or sel.get("input") or ""),
                                      "how": "alias" if match.get("how") == "alias"
                                      else "fuzzy",
                                      **({"slot": sel["slot_hint"]} if sel.get("slot_hint")
                                         else {})})
        else:
            turn.unmatched.append({"name": str(sel.get("name") or sel.get("input") or ""),
                                   "count": count,
                                   "candidates": [str(c.get("title"))
                                                  for c in sel.get("candidates") or []]})
    turn.note = meals_note(turn)
    return turn


def _wrote(name: str, title: str) -> str:
    """The shopper's words that are not in the recipe's title: "briyani" of "chicken
    briyani" for Chicken Biryani; the whole name when every word differs or none does."""
    title_words = set(re.findall(r"[a-z0-9]+", title.casefold()))
    words = re.findall(r"[a-z0-9]+", name.casefold())
    odd = [w for w in words if w not in title_words]
    return " ".join(odd) if odd and len(odd) < len(words) else name.strip()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def meals_note(turn: MealTurn) -> str:
    """The model's line about what the hub read, at most MEALS_NOTE_CHARS: each dish with its
    count, the ones that need the shopper's OK with the word they wrote, the period and the
    names nothing matched. Long names are shortened, then the details, never the counts."""
    def render(chars: int, detail: bool, unmatched_shown: int) -> str:
        items = []
        for count, full_title, slot, wrote in turn.said:
            title = _clip(full_title, chars)
            if wrote is None:
                items.append(f"{count} {title}" + (f" ({slot})" if slot != "dinner" else ""))
            else:
                said = _clip(_wrote(wrote, full_title), chars)
                items.append(f"{count} {title} " + (f'(you wrote "{said}", needs your OK)'
                                                    if detail else "(needs your OK)"))
        names = [_clip(u["name"], chars) for u in turn.unmatched]
        shown = names[:unmatched_shown]
        more = len(names) - len(shown)
        unmatched = (", ".join(shown) + (f" and {more} more" if more else "")) if names \
            else "none"
        period = f"{turn.period_days} days" if turn.period_days else "period not said"
        return (f"{MEALS_PREFIX} parsed: {', '.join(items) or 'nothing'}; {period}; "
                f"unmatched: {unmatched}")

    text = ""
    for chars, detail, shown in ((60, True, 4), (32, True, 3), (24, False, 2), (16, False, 1),
                                 (12, False, 0)):
        text = render(chars, detail, shown)
        if len(text) <= MEALS_NOTE_CHARS:
            return text
    return text[:MEALS_NOTE_CHARS - 1] + "…"


# --- plan_meals' arguments --------------------------------------------------------------------------

def _dish_list(value: Any) -> list[dict[str, Any]] | None:
    """The model's dishes when they are a usable list of {recipe, count}; None otherwise
    (missing, empty, or malformed: a string, a number, items without a recipe)."""
    if not isinstance(value, list) or not value:
        return None
    out = []
    for d in value:
        if not isinstance(d, dict) or not str(d.get("recipe") or "").strip():
            return None
        out.append(d)
    return out


def _same(name: str, key: str, turn: MealTurn) -> bool:
    """Whether the model's dish name is the parse's recipe ``key`` (its key, slug or title)."""
    plain = name.strip().casefold()
    title = turn.titles.get(key, "").casefold()
    words = set(re.findall(r"[a-z0-9]+", plain))
    return plain in {key.casefold(), key.split(":", 1)[-1].casefold(), title} or (
        bool(words) and words == set(re.findall(r"[a-z0-9]+", title)))


def fill(arguments: dict[str, Any], turn: MealTurn | None, context: dict[str, Any] | None,
         on: dt.date | None = None) -> dict[str, Any]:
    """plan_meals' arguments as sent to pantry (see the module docstring)."""
    out = {k: v for k, v in arguments.items() if k not in HIDDEN}
    if turn is not None:
        given = _dish_list(arguments.get("dishes"))
        # The shopper's words decide what is placed (G10): with a parse that matched anything,
        # a dish the model adds, swaps in or recounts is reported back (``differences``), never
        # placed without the shopper.
        out["dishes"] = ([dict(d) for d in turn.dishes] if given is None or turn.found
                         else given)
        out["proposed"] = [dict(p) for p in turn.proposed]
        if turn.period_days:
            out["days"] = turn.period_days
    if context:
        out["current"] = {k: v for k, v in context.items() if k != "docs"}
        docs = list((context.get("docs") or {}).values())
        if docs:
            out["my_recipe_docs"] = docs
    out["start_date"] = ((on or today()) + dt.timedelta(days=1)).isoformat()
    return out


def differences(arguments: dict[str, Any], turn: MealTurn | None) -> list[str]:
    """How the arguments the model sent differ from what the shopper wrote (the parse), and what
    the plan does instead (``fill``): another number of days or other counts (the shopper's are
    used), a dish the model left out (placed anyway), one it added (not placed: ask the shopper
    first), and one that needs the shopper's OK (a proposal). Only the days when the model sent
    no dishes (the hub's are the parse's); [] with no parse."""
    if turn is None:
        return []
    out = []
    days = arguments.get("days")
    if turn.period_days and days is not None and days != turn.period_days:
        out.append(f"days {days}: the shopper wrote {turn.period_days} days, which the plan "
                   "uses")
    given = _dish_list(arguments.get("dishes"))
    if given is None:
        return [f"Differs from the shopper's message: {d}." for d in out]
    matched: set[str] = set()
    for d in given:
        name, count = str(d["recipe"]), d.get("count")
        proposal = next((p for p in turn.proposed if _same(name, p["recipe_key"], turn)), None)
        if proposal is not None:
            matched.add(proposal["recipe_key"])
            out.append(f"{name}: the shopper wrote \"{proposal['input']}\", which needs their "
                       "OK, so it is proposed, not placed")
            continue
        dish = next((x for x in turn.dishes if _same(name, x["recipe"], turn)), None)
        if dish is None:
            out.append(f"{name} ({count}) is not in the shopper's message, so it is not "
                       "placed: ask the shopper before adding it")
            continue
        matched.add(dish["recipe"])
        if count != dish["count"]:
            out.append(f"{name}: {count} meals, the shopper asked for {dish['count']}, which "
                       "the plan uses")
    for x in turn.dishes:
        if x["recipe"] not in matched:
            out.append(f"the shopper also asked for {x['count']} "
                       f"{turn.titles.get(x['recipe'], x['recipe'])}, which the plan includes")
    return [f"Differs from the shopper's message: {d}." for d in out]


def with_differences(result: dict[str, Any], notes: list[str]) -> dict[str, Any]:
    """A plan_meals result with the differences first among its summary's warnings."""
    structured = result.get("structured")
    summary = structured.get("summary") if isinstance(structured, dict) else None
    if not notes or not isinstance(summary, dict) or result.get("is_error"):
        return result
    summary = {**summary, "warnings": [*notes, *(summary.get("warnings") or [])]}
    structured = {**structured, "summary": summary}
    return {**result, "structured": structured,
            "text": json.dumps(structured, ensure_ascii=False, indent=2)}


def drafted(structured: Any) -> Any:
    """A plan_meals result the hub drafted itself, marked for the card's label."""
    summary = structured.get("summary") if isinstance(structured, dict) else None
    if not isinstance(summary, dict):
        return structured
    return {**structured, "summary": {**summary, "drafted_from_message": True}}
