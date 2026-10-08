"""A plan's answer (and the recipe library's list), built by code rather than typed by the model.

``plan_tables`` renders the shopping table a plan's answer ends with, straight from the tool's
result: every product, store and price is exact, and a slow local model no longer spends minutes
writing it (286 tokens at 2 tokens/s was 2 min 25 s of a 7-minute answer). The model writes two
or three sentences; the hub appends the table.

``plan_for_model`` is what a local model reads instead of a plan's JSON: the same facts as short
lines, about a fifth of the tokens (reading ran at 10-15 tokens/s on the demo laptop).
"""

from __future__ import annotations

import re
from typing import Any

PLAN_TOOLS = {"plan_recipe", "plan_from_text"}
WEEK_TOOLS = {"plan_week"}
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


def _latest(results: list[Any]) -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    """This turn's plans to show, as (kind, summary): the latest plan of each recipe (or week)
    once. And the recipe library's list, shown only when the turn planned nothing (a listing was
    a step on the way)."""
    latest: dict[str, tuple[str, dict[str, Any]]] = {}
    listing = None
    for structured in results:
        if is_plan(structured):
            s = _summary(structured) or {}
            latest[f"plan:{s.get('recipe_slug') or s.get('recipe_name')}"] = ("plan", s)
        elif is_week(structured):
            latest["week"] = ("week", _summary(structured) or {})
        elif is_recipe_list(structured):
            listing = structured
    return list(latest.values()), listing


def plan_tables(results: list[Any]) -> list[str]:
    """The tables for this turn's results (see _latest)."""
    plans, listing = _latest(results)
    if plans:
        return [plan_table(s) if kind == "plan" else week_table(s) for kind, s in plans]
    return [recipe_table(listing)] if listing else []


def plan_cards(results: list[Any], drop: set[str] | frozenset[str] = frozenset()
               ) -> list[dict[str, Any]]:
    """The same plans as data, for the browser to draw as a cart: {kind, summary} each, without
    the summary fields in `drop`; [] when the turn planned nothing."""
    plans, _ = _latest(results)
    return [{"kind": kind, "summary": {k: v for k, v in s.items() if k not in drop}}
            for kind, s in plans]


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
