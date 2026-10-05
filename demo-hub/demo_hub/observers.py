"""A small SDK for observers that disclose tools: ``observer.when(condition)`` and its effects.

The Sierra pattern mcp-sim borrows: an observer is an informant with an identity. It watches the
conversation and reports whether a condition holds; when the condition BECOMES true, its effects
change what the agent may do (offer or withdraw tools, add a goal). In code::

    origin_desk = Observer("origin_desk", "Hears any question about where products come from.")

    # A plain-English condition, judged by the observer model (no check: an LLM reads it).
    origin_desk.when("the shopper wants to know or control where products come from") \\
        .enable_tools("get_product_origins", "rank_products_by_origin")

    # A deterministic condition (a check decides) with the effect block as Python.
    shelf = Observer("shelf_clerk", "Reads find_product's results and nothing else.")

    @shelf.when("find_product returned at least one product",
                check=tool_result("find_product", total=gte(1)), on="tool_result")
    def _(ctx: Effects) -> None:
        ctx.enable_tools("get_product")
        if ctx.view.results["find_product"]["total"] > 20:
            ctx.enable_goal("Many products match: ask which one the shopper means.")

    policy = Policy(initial=["list_recipes", "find_product"], observers=[origin_desk, shelf])

``when`` returns the condition: chain ``enable_tools`` / ``disable_tools`` / ``enable_goal`` /
``enable_skill`` on it, use it as a decorator for an effect function, and ``.otherwise`` for the
effects when the condition becomes false. A check is any function ``(view) -> bool | None`` or
``(view) -> (bool | None, evidence)``; ``user_says``, ``tool_called`` and ``tool_result`` are the
ready-made ones. Tool names are globs over the target's tools with the gateway's ``pantry-``
prefix dropped and dashes as underscores (``pantry-plan-recipe`` is ``plan_recipe``).

The vocabulary is mcp-sim's (docs/DESIGN.md §2 "Tool scoping and disclosure", §2b "Observers"):
``on`` triggers (``turn``: the shopper's new message; ``tool_result``: after a tool answers),
``kind`` (``code`` when a check decides, ``llm`` when the observer model reads the condition),
effects that fire on a change of value only, never on a repeat, and never on unknown.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

TRIGGERS = ("turn", "tool_result")

CheckResult = bool | None | tuple[bool | None, str]
Check = Callable[["View"], CheckResult]


# --- what an observer can see ---------------------------------------------------------------------

@dataclass
class View:
    """The conversation as observers see it, rebuilt before every trigger."""

    user_messages: list[str] = field(default_factory=list)   # in order; the last is the newest
    tool_calls: list[str] = field(default_factory=list)      # canonical tool names, in order
    results: dict[str, Any] = field(default_factory=dict)    # canonical name -> last structured
    transcript: list[str] = field(default_factory=list)      # "[n] shopper: ..." lines for LLMs

    @property
    def last_message(self) -> str:
        return self.user_messages[-1] if self.user_messages else ""


# --- effects ----------------------------------------------------------------------------------------

@dataclass
class Effects:
    """What a condition does when it fires. Built by chaining on ``when(...)``, or handed to an
    effect function as ``ctx`` (with the report's ``evidence`` and the ``view``)."""

    enable: list[str] = field(default_factory=list)
    disable: list[str] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    evidence: str = ""
    view: View = field(default_factory=View)

    def enable_tools(self, *globs: str) -> Effects:
        self.enable += globs
        return self

    def disable_tools(self, *globs: str) -> Effects:
        self.disable += globs
        return self

    def enable_goal(self, text: str) -> Effects:
        self.goals.append(text)
        return self

    def enable_skill(self, name: str) -> Effects:
        self.skills.append(name)
        return self

    @property
    def empty(self) -> bool:
        return not (self.enable or self.disable or self.goals or self.skills)


EffectFn = Callable[[Effects], None]


# --- conditions and observers -------------------------------------------------------------------------

@dataclass
class Condition:
    """One ``when``: the condition text, how it is decided, and what it does."""

    observer: Observer
    id: str
    when: str
    check: Check | None                 # None: the observer model judges ``when``
    on: tuple[str, ...]
    then: Effects = field(default_factory=Effects)
    then_fns: list[EffectFn] = field(default_factory=list)
    otherwise_effects: Effects = field(default_factory=Effects)

    @property
    def key(self) -> str:
        return f"{self.observer.name}.{self.id}"

    @property
    def kind(self) -> str:
        return "llm" if self.check is None else "code"

    # chained effects (when the condition becomes true)
    def enable_tools(self, *globs: str) -> Condition:
        self.then.enable_tools(*globs)
        return self

    def disable_tools(self, *globs: str) -> Condition:
        self.then.disable_tools(*globs)
        return self

    def enable_goal(self, text: str) -> Condition:
        self.then.enable_goal(text)
        return self

    def enable_skill(self, name: str) -> Condition:
        self.then.enable_skill(name)
        return self

    @property
    def otherwise(self) -> Effects:
        """The effects when the condition becomes false: ``cond.otherwise.enable_goal(...)``."""
        return self.otherwise_effects

    def __call__(self, fn: EffectFn) -> EffectFn:
        """Use the condition as a decorator: ``fn(ctx)`` runs each time it becomes true."""
        self.then_fns.append(fn)
        return fn

    def effects(self, value: bool, evidence: str, view: View) -> Effects:
        """The effects to apply now the condition's value became ``value``."""
        base = self.then if value else self.otherwise_effects
        out = Effects(list(base.enable), list(base.disable), list(base.goals), list(base.skills),
                      evidence=evidence, view=view)
        if value:
            for fn in self.then_fns:
                fn(out)
        return out


class Observer:
    """An informant: a name, an identity (how it looks at the conversation) and its ``when``s."""

    def __init__(self, name: str, identity: str = "", *, on: str | Iterable[str] = "turn") -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError(f"observer name {name!r}: lowercase letters, digits and _")
        self.name, self.identity = name, identity
        self.on = _triggers(on)
        self.conditions: list[Condition] = []

    def when(self, condition: str, *, check: Check | None = None,
             on: str | Iterable[str] | None = None, id: str | None = None) -> Condition:
        """Declare a condition. With ``check`` a function decides it; without, the observer
        model reads the plain-English ``condition`` and answers true, false or unknown."""
        cid = id or _slug(condition)
        if any(c.id == cid for c in self.conditions):
            raise ValueError(f"{self.name}: two conditions named {cid!r}; pass id=")
        cond = Condition(self, cid, condition, check, _triggers(on) if on is not None else self.on)
        self.conditions.append(cond)
        return cond

    def __repr__(self) -> str:
        return f"Observer({self.name!r}, {len(self.conditions)} condition(s))"


@dataclass
class Policy:
    """A conversation's disclosure: the tools it starts with and the observers that grow it."""

    initial: list[str]
    observers: list[Observer]
    discover_tool: bool = True

    def conditions(self, trigger: str | None = None) -> list[Condition]:
        return [c for o in self.observers for c in o.conditions
                if trigger is None or trigger in c.on]


# --- ready-made checks ------------------------------------------------------------------------------

def user_says(pattern: str, *, anywhere: bool = False) -> Check:
    """True when the shopper's newest message (``anywhere``: any message) matches ``pattern``,
    case-insensitively; the evidence quotes the match."""
    rx = re.compile(pattern, re.IGNORECASE)

    def check(view: View) -> CheckResult:
        texts = view.user_messages if anywhere else view.user_messages[-1:]
        if not texts:
            return None, "no message yet"
        for text in reversed(texts):
            if m := rx.search(text):
                return True, f"matched {m.group(0).strip()[:60]!r}"
        return False, "no match"

    check.__doc__ = f"user_says({pattern!r})"
    return check


def tool_called(glob: str) -> Check:
    """True once the agent has called a tool matching ``glob``."""
    def check(view: View) -> CheckResult:
        hit = [n for n in view.tool_calls if fnmatch.fnmatch(n, glob)]
        return (True, f"{hit[0]} was called") if hit else (False, f"{glob} not called")

    return check


Matcher = Callable[[Any], bool]


def gte(n: float) -> Matcher:
    return lambda v: v is not None and v >= n


def lte(n: float) -> Matcher:
    return lambda v: v is not None and v <= n


def one_of(*values: Any) -> Matcher:
    return lambda v: v in values


def tool_result(glob: str, **where: Any) -> Check:
    """The LAST structured result of a tool matching ``glob``: true when every ``where`` field
    (dots for nesting: ``summary__total`` or ``**{"summary.total": ...}``) equals its value or
    passes its matcher (``gte(1)``, ``one_of("direct", "generic")``, any function); unknown
    until the tool has answered."""
    def check(view: View) -> CheckResult:
        names = [n for n in view.results if fnmatch.fnmatch(n, glob)]
        if not names:
            return None, f"{glob} has not answered yet"
        result = view.results[names[-1]]
        shown, ok = [], True
        for key, expected in where.items():
            value = _dig(result, key.replace("__", "."))
            shown.append(f"{names[-1]}.{key.replace('__', '.')} = {value!r}")
            ok = ok and (expected(value) if callable(expected) else value == expected)
        return ok, ", ".join(shown) or names[-1]

    return check


# --- helpers ------------------------------------------------------------------------------------------

def _dig(value: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _triggers(on: str | Iterable[str]) -> tuple[str, ...]:
    triggers = (on,) if isinstance(on, str) else tuple(on)
    if bad := [t for t in triggers if t not in TRIGGERS]:
        raise ValueError(f"unknown trigger(s) {bad}: one of {TRIGGERS}")
    return triggers


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "_".join(words[:6]) or "condition"


def normalize(result: CheckResult) -> tuple[bool | None, str]:
    """A check's answer as (value, evidence)."""
    if isinstance(result, tuple):
        value, evidence = result
        return (None if value is None else bool(value)), str(evidence)
    return (None if result is None else bool(result)), ""
