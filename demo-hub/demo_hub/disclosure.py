"""Progressive tool disclosure for the Assistant: runs an observer ``Policy`` on a conversation.

The policy (``assistant_policy.py``, written with the ``observers`` SDK) names the tools a
conversation starts with and the observers that watch it. Before each model call the conditions
listening on the trigger report: ``turn`` (the shopper's new message) or ``tool_result`` (after a
tool answered). A ``code`` condition's check runs in process; every ``llm`` condition due is
judged in ONE call to the observer model. A condition that BECOMES true applies its effects, one
that becomes false its ``otherwise`` (a repeated value fires nothing; unknown never fires):
tools are offered or withdrawn, goals and skills join the conversation. ``discover_tools(query)``
lets the agent ask for tools nobody enabled, and a call to a tool that is not offered is refused
as a scope violation, as in mcp-sim.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from demo_hub.observers import Condition, Effects, Policy, View, normalize

MODES = ("progressive", "all")
DISCOVER = "discover_tools"
DISCOVER_FUNCTION = {"type": "function", "function": {
    "name": DISCOVER,
    "description": ("Ask for more tools: say what you need to do (\"plan a week of dinners\", "
                    "\"where a product comes from\"). The best-matching tools you do not have "
                    "yet are added for your next step."),
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "What you need a tool for."}},
        "required": ["query"]}}}
WORD = re.compile(r"[a-z]+")
STOP = {"the", "a", "an", "of", "to", "and", "or", "for", "in", "on", "with", "is", "it", "by",
        "be", "as", "at", "this", "that", "from", "are", "i", "me", "my", "what", "how"}

# A judge answers every llm condition due: key -> (value, evidence).
Judge = Callable[[list[Condition], View], Awaitable[dict[str, tuple[bool | None, str]]]]


def canonical(name: str) -> str:
    """A target's tool name as policies write it: ``pantry-plan-recipe`` -> ``plan_recipe``."""
    return name.removeprefix("pantry-").replace("-", "_")


# --- the observer model ---------------------------------------------------------------------------

JUDGE_SYSTEM = """\
You are a panel of independent observers watching a conversation between a shopper and a
grocery assistant. Each observer has an identity and one or more conditions. For EVERY
condition listed, decide from the conversation alone whether it holds right now: "true",
"false", or "unknown" when the conversation does not say. Quote the few words that decide it as
evidence ("" when there are none). Judge what the shopper and the assistant actually said, never
what might happen next. Answer only with the JSON object."""

REPORT_SCHEMA = {
    "type": "object",
    "properties": {"reports": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"},
                       "value": {"type": "string", "enum": ["true", "false", "unknown"]},
                       "evidence": {"type": "string"}},
        "required": ["id", "value", "evidence"]}}},
    "required": ["reports"],
}


def judge_prompt(conditions: list[Condition], view: View, max_chars: int = 6000) -> str:
    transcript = "\n".join(view.transcript)[-max_chars:] or "(nothing yet)"
    lines = [f'- id: {c.key}\n  observer: {c.observer.identity or c.observer.name}\n'
             f'  condition: {c.when}' for c in conditions]
    return ("CONVERSATION (newest last):\n" + transcript + "\n\nCONDITIONS:\n" + "\n".join(lines)
            + '\n\nAnswer {"reports": [{"id", "value", "evidence"}, ...]} with one report per id.')


def parse_reports(text: str, conditions: list[Condition]) -> dict[str, tuple[bool | None, str]]:
    """The judge's JSON as key -> (value, evidence); a condition it omits is unknown."""
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        reports = json.loads(text[start:end]).get("reports") or []
    except (ValueError, AttributeError) as exc:
        return {c.key: (None, f"observer reply was not JSON ({type(exc).__name__})")
                for c in conditions}
    by_id = {str(r.get("id")): r for r in reports if isinstance(r, dict)}
    out: dict[str, tuple[bool | None, str]] = {}
    for c in conditions:
        r = by_id.get(c.key)
        if r is None:
            out[c.key] = (None, "observer omitted this condition")
            continue
        value = {"true": True, "false": False}.get(str(r.get("value")).lower())
        out[c.key] = (value, str(r.get("evidence") or "")[:200])
    return out


# --- one conversation's disclosure ----------------------------------------------------------------

@dataclass
class Disclosure:
    policy: Policy
    mode: str
    catalog: list[dict[str, Any]]                             # the target's tools, as listed
    skills: dict[str, str] = field(default_factory=dict)      # skill name -> its procedure text
    offered: list[str] = field(default_factory=list)          # raw tool names, in order
    values: dict[str, bool | None] = field(default_factory=dict)
    skills_loaded: set[str] = field(default_factory=set)

    @classmethod
    def start(cls, policy: Policy, catalog: list[dict[str, Any]], mode: str,
              skills: dict[str, str] | None = None) -> Disclosure:
        if mode not in MODES:
            raise ValueError(f"disclosure must be one of {MODES}, got {mode!r}")
        d = cls(policy, mode, catalog, dict(skills or {}))
        d.offered = [t["name"] for t in catalog] if mode == "all" else d.resolve(policy.initial)
        return d

    @property
    def discoverable(self) -> bool:
        return self.mode == "progressive" and self.policy.discover_tool

    def resolve(self, globs: Any) -> list[str]:
        patterns = [globs] if isinstance(globs, str) else list(globs or [])
        return [t["name"] for t in self.catalog
                if any(fnmatch.fnmatch(canonical(t["name"]), p) for p in patterns)]

    def offered_tools(self) -> list[dict[str, Any]]:
        """The offered tools in the order they were offered: one added later comes last, so a
        model's cached prompt (the tools are rendered in order) stays valid up to it."""
        by_name = {t["name"]: t for t in self.catalog}
        return [by_name[name] for name in self.offered if name in by_name]

    def is_offered(self, name: str) -> bool:
        return name in self.offered

    def due_llm(self, trigger: str) -> list[Condition]:
        return [c for c in self.policy.conditions(trigger) if c.kind == "llm"]

    async def observe(self, trigger: str, view: View,
                      judge: Judge | None = None) -> list[dict[str, Any]]:
        """Report every condition listening on ``trigger`` (llm ones through ``judge``, in one
        call; without a judge they are unknown) and apply what changed, in policy order."""
        conditions = self.policy.conditions(trigger)
        llm = [c for c in conditions if c.kind == "llm"]
        judged: dict[str, tuple[bool | None, str]] = {}
        if llm:
            judged = await judge(llm, view) if judge else {
                c.key: (None, "no observer model") for c in llm}
        events: list[dict[str, Any]] = []
        for cond in conditions:
            if cond.check is not None:
                try:
                    value, evidence = normalize(cond.check(view))
                except Exception as exc:  # noqa: BLE001 - a broken check reports unknown
                    value, evidence = None, f"check failed: {type(exc).__name__}: {exc}"
            else:
                value, evidence = judged.get(cond.key, (None, "not judged"))
            before = self.values.get(cond.key)
            self.values[cond.key] = value
            if value is None or value == before:
                continue
            changes = self.apply(cond.effects(value, evidence, view))
            if not (changes["added"] or changes["removed"] or changes["goals"]):
                continue        # e.g. tools already offered: the observation changes nothing
            events.append({"type": "observation", "observer": cond.observer.name,
                           "condition": cond.id, "kind": cond.kind, "when": cond.when,
                           "value": value, "evidence": evidence,
                           "added": changes["added"], "removed": changes["removed"]})
            events += [{"type": "goal_enabled", "reason": f"observer:{cond.key}", **g}
                       for g in changes["goals"]]
        return events

    def apply(self, effects: Effects) -> dict[str, Any]:
        added = [n for n in self.resolve(effects.enable) if n not in self.offered]
        removed = [n for n in self.resolve(effects.disable) if n in self.offered or n in added]
        if self.mode == "progressive":
            self.offered += [n for n in added if n not in removed]
            self.offered = [n for n in self.offered if n not in removed]
            added = [n for n in added if n not in removed]
        else:                       # every tool is already offered: nothing to grow or shrink
            added, removed = [], []
        goals = []
        for skill in effects.skills:
            if skill not in self.skills_loaded and self.skills.get(skill):
                self.skills_loaded.add(skill)
                goals.append({"skill": skill, "text": self.skills[skill]})
        goals += [{"skill": None, "text": text} for text in effects.goals]
        return {"added": added, "removed": removed, "goals": goals}

    def discover(self, query: str) -> tuple[str, list[str]]:
        """discover_tools: rank the tools not yet offered by the words they share with the
        query (a name word counts 3, a description word 1) and offer the best three."""
        wanted = {w for w in WORD.findall(query.lower()) if w not in STOP}
        scored = []
        for tool in self.catalog:
            if tool["name"] in self.offered:
                continue
            name_words = set(WORD.findall(canonical(tool["name"]).replace("_", " ")))
            desc_words = set(WORD.findall(str(tool.get("description", "")).lower())) - STOP
            score = 3 * len(wanted & name_words) + len(wanted & desc_words)
            if score:
                scored.append((score, tool))
        best = [t for _, t in sorted(scored, key=lambda st: -st[0])[:3]]
        self.offered += [t["name"] for t in best]
        if not best:
            return f"No other tool matches {query!r}.", []
        lines = [f"{t['name']}: {first_sentence(t.get('description', ''))} (now available)"
                 for t in best]
        return "\n".join(lines), [t["name"] for t in best]


def first_sentence(text: str) -> str:
    flat = " ".join(str(text).split())
    return flat.split(". ")[0].rstrip(".")[:200] + "."
