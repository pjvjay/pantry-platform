"""Online evals: every Assistant answer graded the moment it finishes, deterministically.

The bench (bench.py) grades fixed cases against what each case expects; these checks need no
expectations, only the conversation's own tool results, so they run on every answer:

- the bench's common checks: finished, called only offered tools, no failed calls, every dollar
  amount in the answer appears in a tool result (grounded_money);
- grounded_stores: every store the answer names is a real store (pantry's /stores) that a tool
  result of this turn mentioned;
- plan_table: an answer to a plan has the shopping table and names every planned product;
- kept_location: a plan retried after a failure kept the shopping location (the agent once
  dropped it to get an answer, then described stores it never had);
- no_scope_violation: the agent never called a tool it was not offered;
- import_grounded: a recipe the hub imported (or the shopper reviewed) this turn was planned
  exactly as reviewed: plan_from_lines, and every planned line's name, quantity and unit equal
  to the doc's (never plan_from_text, which re-reads the recipe and can change an amount); a
  line pantry names as left out (water, a line past its cap) is not a dropped line;
- no_invented_shelf_life: every storage time the model's reply states ("keeps 3 days in the
  fridge") is a figure a tool result of this turn holds; checked on every meal-plan turn;
- grounded_nutrition: every kcal or protein figure in the model's reply is in a tool result,
  and a reply quoting one for meals whose amounts are demo house amounts says "demo".

``answer_confidence`` is the share of applicable checks that passed. ``plan`` summarises the
plan's own confidence: the selector's per-line confidence, how many lines matched the ingredient
exactly, and the origin coverage.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

from demo_hub.bench import COMMON, Check, Run, _numbers

PLAN_TOOLS = ("plan_recipe", "plan_from_text", "plan_from_lines", "plan_week")
TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
# a sentence about how long food keeps, and the durations in it ("2 to 3 days", "3-4 days")
STORAGE_WORDS = re.compile(r"\b(keeps?|kept|lasts?|fresh|fridge|refrigerat\w*|freez\w*|frozen"
                           r"|thaw\w*|shelf|spoil\w*|storage|stored?|good for|use (it |them )?"
                           r"within|use[- ]by)\b", re.IGNORECASE)
DURATION = re.compile(r"(\d+(?:\.\d+)?)(?:\s*(?:-|–|to)\s*(\d+(?:\.\d+)?))?\s*"
                      r"(hours?|days?|weeks?|months?|years?)\b", re.IGNORECASE)
KCAL = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:kcal|k?cal(?:orie)?s?\b)", re.IGNORECASE)
PROTEIN = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*g(?:rams?)?\s+(?:of\s+)?protein", re.IGNORECASE)
MEAL_TOOLS = ("plan_meals",)


def canonical(name: str) -> str:
    return name.removeprefix("pantry-").replace("-", "_")


def _plans(run: Run) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(arguments, summary) of every successful plan call, in order."""
    out = []
    for use in run.tool_uses:
        if canonical(use.name) in PLAN_TOOLS and not use.is_error and isinstance(use.result, dict):
            summary = use.result.get("summary")
            if isinstance(summary, dict):
                out.append((use.arguments, summary))
    return out


def check_grounded_stores(run: Run, stores: Iterable[str]) -> Check:
    named = [s for s in stores if s and s.lower() in run.answer.lower()]
    seen = run.results_text().lower()
    invented = [s for s in named if s.lower() not in seen]
    return Check("grounded_stores", not invented,
                 f"named stores no tool result mentioned: {invented}" if invented
                 else f"{len(named)} store(s) named, all from tool results")


def check_plan_table(run: Run) -> Check | None:
    plans = _plans(run)
    if not plans or "plan_week" in {canonical(u.name) for u in run.tool_uses}:
        return None
    lines = plans[-1][1].get("lines") or []
    rows = TABLE_ROW.findall(run.answer)
    missing = [str(line.get("product")) for line in lines
               if str(line.get("product", "")).lower() not in run.answer.lower()]
    ok = len(rows) >= len(lines) + 2 and not missing     # header, separator, one row a line
    detail = (f"{max(len(rows) - 2, 0)} table row(s) for {len(lines)} line(s)"
              + (f"; products not named: {missing}" if missing else ""))
    return Check("plan_table", ok, detail)


def check_kept_location(run: Run) -> Check | None:
    calls = [u for u in run.tool_uses if canonical(u.name) in ("plan_recipe", "plan_from_text")]
    located = [("lat" in u.arguments or "max_km" in u.arguments) for u in calls]
    if True not in located:
        return None
    dropped = not all(located[located.index(True):])      # a plan after the first located one
    return Check("kept_location", not dropped,
                 "a later plan call dropped the shopping location" if dropped else "")


def check_no_scope_violation(events: list[dict[str, Any]]) -> Check:
    hits = [e["text"] for e in events
            if e.get("type") == "notice" and str(e.get("text", "")).startswith("scope violation")]
    return Check("no_scope_violation", not hits, "; ".join(hits))


def _imported_docs(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The docs this turn imported, by doc_key, from its recipe_import events."""
    out = {}
    for e in events:
        doc = ((e.get("result") or {}).get("doc") or {}) if e.get("type") == "recipe_import" \
            else {}
        if e.get("doc_key") and doc.get("lines"):
            out[str(e["doc_key"])] = doc
    return out


def _line(ln: dict[str, Any]) -> tuple[str, float | None, str]:
    q = ln.get("quantity")
    return (str(ln.get("name") or ""), None if q is None else float(q),
            str(ln.get("unit") or ""))


def _show(t: tuple[str, float | None, str]) -> str:
    name, q, unit = t
    return " ".join(x for x in (f"{q:g}" if q is not None else "", unit, name) if x)


def _named(name: str) -> str:
    return " ".join(name.split()).casefold()


def check_import_grounded(events: list[dict[str, Any]],
                          plans: list[dict[str, Any]] | None) -> Check | None:
    """Every reviewed recipe planned this turn was planned as reviewed. ``plans`` are the hub's
    own records of this turn's plans (Agent.turn_plans: tool, doc_key, the doc's reviewed lines,
    the basis lines and the names the plan left out), since the basis never reaches the
    events. Fails on plan_from_text in a turn that imported a recipe, a plan of a doc the hub
    does not hold, a changed name, quantity or unit, or an added or dropped line.

    A reviewed line missing from the basis is not a dropped line when the plan names it as
    left out (``left_out``: pantry's not_stocked, out_of_range and skipped). pantry never plans
    water or ice, nor a line past its 40-ingredient cap, and lists each one there by name
    instead; each name accounts for one line.

    Not applicable to a turn with neither an import nor a plan_from_lines call, nor to a plan
    whose basis did not come back (a target whose schema has no ``basis``): there is nothing
    to compare, which is not the same as a plan that differs."""
    docs = _imported_docs(events)
    relevant = [p for p in plans or [] if p["tool"] == "plan_from_lines"
                or (docs and p["tool"] == "plan_from_text")]
    if not relevant:
        return None
    problems: list[str] = []
    planned = 0
    named = 0
    for plan in relevant:
        if plan["tool"] == "plan_from_text":
            problems.append("plan_from_text re-read an imported recipe instead of "
                            "plan_from_lines")
            reviewed = next(reversed(docs.values()))["lines"]
        else:
            reviewed = plan.get("reviewed") or (docs.get(str(plan.get("doc_key"))) or {}).get(
                "lines")
        if not reviewed:
            problems.append(f"planned {plan.get('doc_key')}, a doc the hub does not hold")
            continue
        if plan.get("lines") is None:
            continue
        planned += 1
        want = {int(ln["line_no"]): _line(ln) for ln in reviewed}
        got = {int(ln["line_no"]): _line(ln) for ln in plan["lines"]}
        left_out = Counter(_named(str(name)) for name in plan.get("left_out") or [])
        for n in sorted(want.keys() | got.keys()):
            if n not in got:
                name = _named(want[n][0])
                if left_out[name] > 0:
                    left_out[name] -= 1
                    named += 1
                else:
                    problems.append(f"line {n} ({_show(want[n])}) was not planned")
            elif n not in want:
                problems.append(f"line {n} ({_show(got[n])}) was added")
            elif got[n] != want[n]:
                problems.append(f"line {n}: planned {_show(got[n])}, reviewed {_show(want[n])}")
    if not problems and not planned:
        return None
    detail = f"{planned} reviewed recipe(s) planned exactly as reviewed" + (
        f"; {named} line(s) the plan names as left out" if named else "")
    return Check("import_grounded", not problems, "; ".join(problems[:6]) if problems
                 else detail)


def reply_of(events: list[dict[str, Any]]) -> str:
    """The model's own words in the turn's answer: its reply when the hub added tables (or a
    card) after it, else the answer's text."""
    for e in reversed(events):
        if e.get("type") == "assistant":
            return str(e.get("reply") if e.get("reply") is not None else e.get("text") or "")
    return ""


def _durations(text: str) -> list[tuple[str, str]]:
    """(as written, normalised) for every duration in a sentence about keeping food."""
    out = []
    for sentence in SENTENCE.split(text):
        if not STORAGE_WORDS.search(sentence):
            continue
        for m in DURATION.finditer(sentence):
            unit = m.group(3).lower().rstrip("s")
            span = f"{m.group(1)} to {m.group(2)}" if m.group(2) else m.group(1)
            out.append((m.group(0), f"{span} {unit}"))
    return out


def _normalised_durations(text: str) -> set[str]:
    return {f"{(f'{m.group(1)} to {m.group(2)}' if m.group(2) else m.group(1))} "
            f"{m.group(3).lower().rstrip('s')}" for m in DURATION.finditer(text)}


def check_no_invented_shelf_life(run: Run, reply: str | None = None) -> Check | None:
    """Every storage time the reply states is one a tool result of this turn holds (pantry
    cites them on trip lines and reasons): a "3 days in the fridge" no result has is invented.
    Applies to a turn that planned meals, or whose reply states a storage time."""
    said = _durations(run.answer if reply is None else reply)
    meals = any(canonical(u.name) in MEAL_TOOLS for u in run.tool_uses)
    if not said and not meals:
        return None
    seen = _normalised_durations(run.results_text().replace("\\u2013", "-"))
    invented = sorted({raw for raw, norm in said if norm not in seen})
    return Check("no_invented_shelf_life", not invented,
                 f"storage times no tool result states: {invented}" if invented
                 else f"{len(said)} storage time(s) stated, all from tool results" if said
                 else "no storage time stated")


def _figures(pattern: re.Pattern[str], text: str) -> list[tuple[str, float]]:
    return [(m.group(0), float(m.group(1).replace(",", ""))) for m in pattern.finditer(text)]


def check_grounded_nutrition(run: Run, reply: str | None = None) -> Check | None:
    """Every kcal or protein figure in the reply is in a tool result of this turn (to the
    nearest whole number), and a reply quoting one for meals whose amounts are demo house
    amounts says "demo". Not applicable when the reply quotes no such figure."""
    text = run.answer if reply is None else reply
    quoted = _figures(KCAL, text) + _figures(PROTEIN, text)
    if not quoted:
        return None
    seen_text = run.results_text()
    seen = {round(n) for n in _numbers(seen_text)}
    invented = [raw for raw, n in quoted if round(n) not in seen]
    demo = '"demo_amounts": true' in seen_text or "demo amounts" in seen_text.lower()
    problems = []
    if invented:
        problems.append(f"figures not in any tool result: {invented}")
    if demo and "demo" not in text.lower():
        problems.append("quotes nutrition for meals with demo house amounts without saying "
                        "\"demo\"")
    return Check("grounded_nutrition", not problems, "; ".join(problems) if problems
                 else f"{len(quoted)} figure(s), all from tool results"
                 + (", labelled demo" if demo else ""))


def plan_confidence(run: Run) -> dict[str, Any] | None:
    """The last plan's own confidence: the selector's line confidence (min and mean), the share
    of lines matched exactly, and the origin coverage, when the plan has one."""
    plans = _plans(run)
    if not plans:
        return None
    summary = plans[-1][1]
    lines = summary.get("lines") or []
    conf = [float(line.get("confidence", 0)) for line in lines if line.get("confidence") is not None]
    exact = sum(1 for line in lines if line.get("match") == "exact")
    coverage = summary.get("coverage") or {}
    return {
        "lines": len(lines),
        "min_line_confidence": round(min(conf), 2) if conf else None,
        "mean_line_confidence": round(sum(conf) / len(conf), 2) if conf else None,
        "exact_share": round(exact / len(lines), 2) if lines else None,
        "origin_status": summary.get("origin_status"),
        "origin_spend_verified": coverage.get("spend_fraction"),
        "left_out": sum(len(summary.get(k) or []) for k in ("not_stocked", "out_of_range",
                                                             "skipped")),
    }


def evaluate(events: list[dict[str, Any]], stores: Iterable[str] = (),
             model: str = "", plans: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Grade one answered turn from its events (and ``plans``, the hub's own record of this
    turn's plans with their basis lines, for import_grounded)."""
    run = Run.from_events(model, "online", 0, events)
    reply = reply_of(events)
    checks: list[Check] = [check(run) for check in COMMON]
    checks.append(check_grounded_stores(run, stores))
    for optional in (check_plan_table(run), check_kept_location(run),
                     check_import_grounded(events, plans),
                     check_no_invented_shelf_life(run, reply),
                     check_grounded_nutrition(run, reply)):
        if optional is not None:
            checks.append(optional)
    checks.append(check_no_scope_violation(events))
    passed = sum(c.passed for c in checks)
    return {
        "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in checks],
        "passed": passed,
        "total": len(checks),
        "answer_confidence": round(passed / len(checks), 2) if checks else None,
        "plan": plan_confidence(run),
    }
