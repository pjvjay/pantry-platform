"""A plan's answer (and the recipe library's list), built by code rather than typed by the model.

``plan_tables`` renders the shopping table a plan's answer ends with, straight from the tool's
result: every product, store and price is exact, and a slow local model no longer spends minutes
writing it (286 tokens at 2 tokens/s was 2 min 25 s of a 7-minute answer). The model writes two
or three sentences; the hub appends the table.

``plan_for_model`` is what a local model reads instead of a plan's JSON: the same facts as short
lines, about a fifth of the tokens (reading ran at 10-15 tokens/s on the demo laptop).

``plan_cards`` gives the browser the same plans as data, to draw as carts. A plan card carries
``ref``, the plan's place in the conversation's tool_log, when the hub holds its basis: the
cart's Options dialog and a swap name the plan by it. A week card links to the Meal plan.
``cart_note`` is the line the model reads, before the shopper's next message, about a swap the
shopper made in the cart.

A meal plan (pantry's plan_meals) is a draft the shopper applies in the Meal plan: its card
carries the ops to apply, the plan's base rev and "Open in Meal plan"; ``mealplan_table`` is
its Markdown, and a local model reads it as at most 12 lines (``mealplan_for_model``).
"""

from __future__ import annotations

import re
from typing import Any

PLAN_TOOLS = {"plan_recipe", "plan_from_text"}
WEEK_TOOLS = {"plan_week"}
# A week card has no basis (each day's plan would make it large), so it offers no Options; the
# Meal plan opens it as a draft whose every trip line has them.
WEEK_LINKS = ({"label": "Open in Meal plan", "href": "#/mealplan?from=week"},)
MEALPLAN_TOOLS = {"plan_meals"}
MEALPLAN_LINKS = ({"label": "Open in Meal plan", "href": "#/mealplan?from=assistant"},)
MEALPLAN_MODEL_LINES = 12
MEALPLAN_MODEL_CHARS = 1500
CART_PREFIX = "[cart]"
CART_NOTE_CHARS = 300
TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")


def _money(v: Any) -> str:
    return "–" if v is None else f"${float(v):.2f}"


def _summary(structured: Any) -> dict[str, Any] | None:
    summary = structured.get("summary") if isinstance(structured, dict) else None
    return summary if isinstance(summary, dict) else None


def is_plan(structured: Any) -> bool:
    s = _summary(structured)
    return s is not None and isinstance(s.get("lines"), list) and "recipe_name" in s


def is_week(structured: Any) -> bool:
    s = _summary(structured)
    return s is not None and isinstance(s.get("days"), list) and "shopping_list" in s


def is_mealplan(structured: Any) -> bool:
    """A plan_meals result: a meal-plan draft's summary."""
    s = _summary(structured)
    return s is not None and s.get("kind") == "mealplan" and isinstance(s.get("meals"), list)


SWAP = "still available, not a direct match: "
EXCLUDED_PRODUCT = re.compile(r"^(?P<name>.*) \((?P<price>\$[\d.]+)\) — (?P<country>.+?) via \S+$")


def options(d: dict[str, Any]) -> tuple[list[str], list[str]]:
    """What pantry lists for a left-out ingredient: (swaps the plan still allows, the excluded
    products the shopper could allow back, as "name ($price, country)"). Other suggestions (the
    nearest offer out of range) are already in the reason."""
    swaps, back = [], []
    for item in d.get("suggestions") or []:
        item = str(item)
        if item.startswith(SWAP):
            swaps.append(item[len(SWAP):])
        elif m := EXCLUDED_PRODUCT.match(item):
            back.append(f"{m['name']} ({m['price']}, {m['country']})")
    return swaps, back


def _left_out(s: dict[str, Any]) -> list[str]:
    out = []
    for key in ("not_stocked", "out_of_range", "skipped"):
        for d in s.get(key) or []:
            reason = d.get("reason") or key.replace("_", " ")
            swaps, back = options(d)
            out.append(f"{d.get('ingredient')} ({reason}"
                       + (f"; swap: {', '.join(swaps)}" if swaps else "")
                       + (f"; or allow: {', '.join(back)}" if back else "") + ")")
    return out


def _line_store_price(line: dict[str, Any]) -> tuple[str, Any]:
    """Where the recommended trip buys the line, or its cheapest offer when there is no trip."""
    if line.get("trip_store"):
        return str(line["trip_store"]), line.get("trip_price")
    return str(line.get("store") or "–"), line.get("price")


def _total(s: dict[str, Any]) -> str:
    trip = s.get("trip")
    if trip:
        return (f"{_money(trip.get('total_cost'))} for the trip to {', '.join(trip.get('stores') or [])}"
                f" ({_money(trip.get('basket_cost'))} basket + {_money(trip.get('travel_cost'))} travel)")
    return (f"{_money(s.get('total_cost'))}, each line at its cheapest store in range "
            "(the plan chose no trip)")


def _origin(s: dict[str, Any]) -> str | None:
    cov = s.get("coverage")
    if not cov:
        return None
    share = round(float(cov.get("spend_fraction") or 0) * 100)
    status = s.get("origin_status")
    return (f"{share}% of the spend verified ({cov.get('lines_known')} of {cov.get('lines_total')} "
            f"lines)" + ("" if status == "verified" else f"; status {status}"))


def plan_table(s: dict[str, Any]) -> str:
    rows = ["| Item | Product | Store | Price | Origin |", "|---|---|---|---|---|"]
    for line in s.get("lines") or []:
        store, price = _line_store_price(line)
        packs = int(line.get("packs") or 1)
        product = str(line.get("product")) + (f" ×{packs}" if packs > 1 else "")
        origin = line.get("origin_country") or line.get("origin_status") or "–"
        rows.append(f"| {line.get('ingredient')} | {product} | {store} | {_money(price)} | {origin} |")
    out = [f"### {s.get('recipe_name') or s.get('recipe_slug')}", "", *rows, "",
           f"**Total:** {_total(s)}"]
    if (origin := _origin(s)) is not None:
        out.append(f"**Origin:** {origin}")
    out.append(f"**Left out:** {'; '.join(_left_out(s)) or 'nothing'}")
    return "\n".join(out)


def week_table(s: dict[str, Any]) -> str:
    out = ["### Week plan", "", "| Day | Recipe | Cost |", "|---|---|---|"]
    out += [f"| {i} | {d.get('recipe_name')} | {_money(d.get('day_cost'))} |"
            for i, d in enumerate(s.get("days") or [], 1)]
    out += ["", "| Product | Store | Price | For |", "|---|---|---|---|"]
    out += [f"| {item.get('product')} | {item.get('store')} | {_money(item.get('price'))} | "
            f"{', '.join(item.get('used_by') or [])} |" for item in s.get("shopping_list") or []]
    out += ["", f"**Total:** {_total(s)}"]
    if s.get("overlap_savings"):
        out.append(f"**Shared ingredients saved:** {_money(s.get('overlap_savings'))}")
    if (origin := _origin(s)) is not None:
        out.append(f"**Origin:** {origin}")
    return "\n".join(out)


def is_recipe_list(structured: Any) -> bool:
    items = structured.get("result") if isinstance(structured, dict) else None
    return isinstance(items, list) and bool(items) and all(
        isinstance(i, dict) and "slug" in i and "name" in i for i in items)


def recipe_table(structured: dict[str, Any]) -> str:
    rows = [f"| {r.get('name')} | {r.get('servings', '–')} | {r.get('ingredient_count', '–')} |"
            for r in structured["result"]]
    return "\n".join(["### Recipes I can plan", "", "| Recipe | Serves | Ingredients |",
                      "|---|---|---|", *rows])


def _latest(results: list[Any]) -> tuple[list[tuple[str, dict[str, Any], int]], Any]:
    """This turn's plans to show, as (kind, summary, index in ``results``): the latest plan of
    each recipe (or week) once. And the recipe library's list, shown only when the turn planned
    nothing (a listing was a step on the way)."""
    latest: dict[str, tuple[str, dict[str, Any], int]] = {}
    listing = None
    for i, structured in enumerate(results):
        if is_plan(structured):
            s = _summary(structured) or {}
            latest[f"plan:{recipe_key(s)}"] = ("plan", s, i)
        elif is_week(structured):
            latest["week"] = ("week", _summary(structured) or {}, i)
        elif is_mealplan(structured):
            latest["mealplan"] = ("mealplan", _summary(structured) or {}, i)
        elif is_recipe_list(structured):
            listing = structured
    return list(latest.values()), listing


def recipe_key(summary: dict[str, Any]) -> str:
    """Which recipe a plan is for: two plans with the same key are the same cart."""
    return str(summary.get("recipe_slug") or summary.get("recipe_name") or "")


def cart_key(summary: dict[str, Any]) -> str:
    """Which cart a plan is, for the cart's swaps: its recipe_key and, for a plan with no slug
    (a pasted or written recipe, whose name is only a title two recipes can share), the
    ingredient names in the basis the hub keeps. A swap re-prices the same lines and a re-plan
    of the same recipe reads the same names, so both keep the key; another recipe that happens
    to have the same title does not."""
    key = recipe_key(summary)
    basis = summary.get("basis")
    if summary.get("recipe_slug") or not isinstance(basis, dict):
        return key
    names = sorted(str(ln.get("name") or "") for ln in basis.get("lines") or []
                   if isinstance(ln, dict))
    return "\n".join([key, *names])


def plan_tables(results: list[Any]) -> list[str]:
    """The tables for this turn's results (see _latest)."""
    plans, listing = _latest(results)
    if plans:
        return [plan_table(s) if kind == "plan" else mealplan_table(s) if kind == "mealplan"
                else week_table(s) for kind, s, _ in plans]
    return [recipe_table(listing)] if listing else []


def pinned_lines(summary: dict[str, Any]) -> list[int]:
    """The lines whose product the shopper chose (the basis's pins)."""
    basis = summary.get("basis")
    pins = basis.get("pins") if isinstance(basis, dict) else None
    return sorted({int(p["line_no"]) for p in pins or [] if isinstance(p, dict)})


def plan_card(kind: str, summary: dict[str, Any], ref: int | None = None,
              drop: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]:
    """One card: {kind, summary} without the fields in `drop`. A plan whose summary has its
    basis also gets `ref` (when given) and `pinned_lines`; a week gets its links."""
    card: dict[str, Any] = {"kind": kind,
                            "summary": {k: v for k, v in summary.items() if k not in drop}}
    if kind == "plan" and ref is not None and isinstance(summary.get("basis"), dict):
        card.update(ref=ref, pinned_lines=pinned_lines(summary))
    elif kind == "week":
        card["links"] = [dict(link) for link in WEEK_LINKS]
    elif kind == "mealplan":
        card["links"] = [dict(link) for link in MEALPLAN_LINKS]
        card["base_rev"] = summary.get("base_rev")
        if summary.get("drafted_from_message"):
            card["label"] = "drafted from your message"
    return card


def plan_cards(results: list[Any], drop: set[str] | frozenset[str] = frozenset(),
               start: int | None = None) -> list[dict[str, Any]]:
    """The same plans as data, for the browser to draw as a cart (see plan_card); [] when the
    turn planned nothing. `start` is the tool_log index of ``results[0]``: a plan's ref is its
    own index there."""
    plans, _ = _latest(results)
    return [plan_card(kind, s, None if start is None else start + i, drop)
            for kind, s, i in plans]


def cart_total(summary: dict[str, Any]) -> float | None:
    """What the cart costs: the recommended trip's total, or the lines' when there is no trip."""
    trip = summary.get("trip")
    value = trip.get("total_cost") if isinstance(trip, dict) else summary.get("total_cost")
    return None if value is None else round(float(value), 2)


def cart_stores(summary: dict[str, Any]) -> list[str]:
    trip = summary.get("trip")
    return [str(x) for x in (trip.get("stores") or [])] if isinstance(trip, dict) else []


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def cart_note(*, recipe: str, line_no: int, ingredient: str, was: str, now: str,
              before: float | None, after: float | None, stores: list[str],
              undone: bool = False) -> str:
    """The model's line about a swap the shopper made in the cart, at most CART_NOTE_CHARS:
    the line, the product before and after, and the cart's total and stores now (all from
    pantry's re-price). Long names are shortened, never the figures."""
    def render(name_chars: int, store_count: int) -> str:
        if stores:
            shown = ", ".join(_clip(x, name_chars) for x in stores[:store_count])
            more = len(stores) - store_count
            money = f"Trip now {_money(after)} at {shown}" + (f" and {more} more" if more > 0
                                                               else "")
        else:
            money = f"Total now {_money(after)} (the plan chose no trip)"
        if before is not None and before != after:
            money += f", was {_money(before)}"
        change = (f"back to the planner's pick, {_clip(now, name_chars)} "
                  f"(was {_clip(was, name_chars)})" if undone
                  else f"{_clip(was, name_chars)} -> {_clip(now, name_chars)}")
        return (f"{CART_PREFIX} The shopper changed line {line_no} "
                f"({_clip(ingredient, name_chars)}) of {_clip(recipe, name_chars)} in the cart: "
                f"{change}. {money}.")

    text = ""
    for name_chars in (80, 40, 24, 16):
        for store_count in sorted({len(stores), 2, 1}, reverse=True):
            text = render(name_chars, max(store_count, 1))
            if len(text) <= CART_NOTE_CHARS:
                return text
    return text[:CART_NOTE_CHARS - 1] + "…"


def strip_tables(text: str) -> str:
    """The model's text without any Markdown table it wrote anyway: the code's table replaces it."""
    kept = [line for line in text.splitlines() if not TABLE_LINE.match(line)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def with_tables(text: str, tables: list[str]) -> str:
    if not tables:
        return text
    return "\n\n".join([t for t in [strip_tables(text)] if t] + tables)


def plan_for_model(structured: Any) -> str | None:
    """A plan or week result as short lines for a local model; None for any other result."""
    if is_mealplan(structured):
        return mealplan_for_model(_summary(structured) or {})
    if is_plan(structured):
        s = _summary(structured) or {}
        out = [f"plan: {s.get('recipe_name')} ({s.get('recipe_slug')})"]
        if (origin := _origin(s)) is not None:
            out.append(f"origin: {origin}")
        out.append(f"total: {_total(s)}")
        out.append("lines (ingredient: product [product_id], store and price, origin):")
        for line in s.get("lines") or []:
            store, price = _line_store_price(line)
            origin = line.get("origin_country") or line.get("origin_status") or "unknown"
            out.append(f"- {line.get('ingredient')}: {line.get('product')} [{line.get('product_id')}],"
                       f" {store} {_money(price)}, {origin}")
        if s.get("trip"):
            out.append(f"lines at their cheapest stores instead (not the trip): "
                       f"{_money(s.get('total_cost'))}")
        out.append(f"left out: {'; '.join(_left_out(s)) or 'nothing'}")
        out += [f"note: {n}" for n in s.get("notes") or []]
    elif is_week(structured):
        s = _summary(structured) or {}
        out = ["week plan:"]
        out += [f"- day {i}: {d.get('recipe_name')} {_money(d.get('day_cost'))}"
                for i, d in enumerate(s.get("days") or [], 1)]
        out.append(f"total: {_total(s)}")
        if (origin := _origin(s)) is not None:
            out.append(f"origin: {origin}")
        out += [f"note: {n}" for n in s.get("notes") or []]
    else:
        return None
    out.append("(the shopper sees these lines as a table under your answer)")
    return "\n".join(out)


# --- meal plans (plan_meals) -------------------------------------------------------------------------

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _day(iso: Any) -> str:
    """'2026-10-10' -> 'Sat 10 Oct'."""
    import datetime as dt

    try:
        d = dt.date.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    return f"{_DAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]}"


def _trip_cost(t: dict[str, Any]) -> str:
    if t.get("total_cost") is None:
        return "price unknown"
    return ("at least " if t.get("total_is_floor") else "") + _money(t.get("total_cost"))


def _dishes(items: list[dict[str, Any]]) -> str:
    return ", ".join(f"{a.get('count')} {a.get('title')}"
                     + (f" ({a.get('slot')})" if a.get("slot") not in (None, "dinner") else "")
                     for a in items)


def _proposals(s: dict[str, Any]) -> str:
    return "; ".join(f"{p.get('count')} {p.get('title')} (you wrote \"{p.get('input')}\")"
                     for p in s.get("proposals") or [])


def _unmatched(s: dict[str, Any]) -> str:
    out = []
    for u in s.get("unmatched") or []:
        could = ", ".join(str(c.get("title")) for c in u.get("candidates") or [])
        out.append(f"{u.get('input')}" + (f" (could be {could})" if could else ""))
    return "; ".join(out)


def mealplan_table(s: dict[str, Any]) -> str:
    """A drafted meal plan as Markdown: the meals by day, the trips, the dishes waiting for the
    shopper's OK and the names not found. The console draws the card instead."""
    by_day: dict[str, list[str]] = {}
    for m in s.get("meals") or []:
        by_day.setdefault(str(m.get("date")), []).append(
            f"{m.get('title')}" + (f" ({m.get('slot')})" if m.get("slot") != "dinner" else "")
            + ("" if m.get("new") else " (already planned)"))
    out = [f"### Meal plan draft: {s.get('days')} days from {_day(s.get('start_date'))}", "",
           "| Day | Meals |", "|---|---|"]
    out += [f"| {_day(d)} | {', '.join(meals)} |" for d, meals in sorted(by_day.items())]
    trips = s.get("trips") or []
    out += ["", f"**Trips ({s.get('strategy')}, suggested):** "
            + ("; ".join(f"{_day(t.get('date'))} {_trip_cost(t)} at "
                         f"{', '.join(t.get('stores') or []) or 'no store'}" for t in trips)
               or "none")]
    if trips:
        out.append(f"**Total:** {_trip_cost(s)} (demo prices)")
    if s.get("proposals"):
        out.append(f"**Needs your OK:** {_proposals(s)}")
    if s.get("unmatched"):
        out.append(f"**Not found:** {_unmatched(s)}")
    if s.get("nutrition"):
        out.append(f"**Nutrition:** {s['nutrition']}")
    return "\n".join(out)


def mealplan_for_model(s: dict[str, Any]) -> str:
    """A drafted meal plan as at most MEALPLAN_MODEL_LINES short lines (and under
    MEALPLAN_MODEL_CHARS) for a local model: what was placed, what waits for the shopper's OK,
    what was not found, the trips and their total, the nutrition line as pantry wrote it."""
    new = [m for m in s.get("meals") or [] if m.get("new")]
    added = s.get("added") or []
    lines = [(f"meal plan draft (not applied yet): {len(new)} new meal(s) over {s.get('days')} "
              f"days from {_day(s.get('start_date'))}, each serving "
              f"{s.get('household_servings')}")]
    lines.append(f"placed: {_dishes(added) or 'nothing'}")
    lines.append(f"needs the shopper's OK, not placed: {_proposals(s) or 'none'}")
    lines.append(f"not found: {_unmatched(s) or 'none'}")
    if s.get("unplaced"):
        lines.append("no free slot: " + ", ".join(f"{u.get('count')} {u.get('title')}"
                                                  for u in s["unplaced"]))
    trips = s.get("trips") or []
    shown = "; ".join(f"{_day(t.get('date'))} {_trip_cost(t)}" for t in trips[:4])
    more = f" and {len(trips) - 4} more" if len(trips) > 4 else ""
    total = s.get("total_cost")
    lines.append(f"trips ({s.get('strategy')}, suggested): {len(trips)}: {shown or 'none'}{more}"
                 + ("" if total is None else f"; total {_trip_cost(s)} (demo prices)"))
    other = s.get("other_strategy")
    if isinstance(other, dict):
        lines.append(f"other strategy ({other.get('name')}): {other.get('trips')} trip(s), "
                     f"{_trip_cost(other)}")
    if s.get("nutrition"):
        lines.append(str(s["nutrition"]))
    lines += [f"warning: {w}" for w in (s.get("warnings") or [])[:2]]
    lines.append("(the shopper sees the plan as a card under your answer; only they apply it "
                 "and approve trips)")
    lines = lines[:MEALPLAN_MODEL_LINES - 1] + lines[-1:] if len(lines) > MEALPLAN_MODEL_LINES \
        else lines
    text = "\n".join(_clip(line, 400) for line in lines)
    return text if len(text) < MEALPLAN_MODEL_CHARS else text[:MEALPLAN_MODEL_CHARS - 2] + "…"
