"""The Assistant's tool disclosure, declared with the observer SDK (``observers.py``).

A conversation starts with two tools and ``discover_tools``; the observers below enable the rest
when they see what calls for them. Code conditions (with a ``check``) are free and instant: they
decide facts that a pattern or a tool result settles. LLM conditions (no ``check``) are read by
the observer model, one batched call per trigger, for what only reading can judge: intent in
other words, tone, a request that crosses the law.
"""

from __future__ import annotations

import re
from typing import Any

from demo_hub.observers import (
    CheckResult,
    Effects,
    Observer,
    Policy,
    View,
    gte,
    tool_called,
    tool_result,
    user_says,
)

# The shopper wants to cook or shop for something.
COOKING = (r"\b(make|cook|plan|recipe|meals?|dinners?|lunch|breakfast|ingredients?"
           r"|shopping list)\b")
_WORD = re.compile(r"[a-z]+")
# Asking what the library holds is not asking to cook: "which recipes can you plan?" names no
# dish, so no plan tools join (they cost reading time, and joining mid-turn breaks the cache).
LISTING = re.compile(r"\b(which|what) (recipes|dishes|meals)\b|\blist (the |your |all )?recipes\b"
                     r"|\bwhat can you (plan|make|cook)\b|\brecipes? (do|can) you\b", re.IGNORECASE)


def wants_a_dish(view: View) -> CheckResult:
    """The shopper's newest message is about making or planning food, and not just asking which
    recipes there are."""
    text = view.user_messages[-1] if view.user_messages else ""
    if not text:
        return None, "no message yet"
    if LISTING.search(text):
        return False, "asking what the library holds"
    m = re.search(COOKING, text, re.IGNORECASE)
    return (True, f"said {m.group(0)!r}") if m else (False, "nothing about cooking")


def dish_not_in_library(view: View) -> CheckResult:
    """Once list_recipes has answered (in this turn or an earlier one): true when the shopper's
    newest message wants to cook and names no library recipe (every word of its name, or of its
    slug: "the chicken curry" is Simple Chicken Curry, chicken_curry), so the dish must be
    written and planned as text. Only the newest message counts: after "plan tomato penne", "a
    similar recipe with fish" is a new dish, not the library's Tomato Penne."""
    listed = view.results.get("list_recipes")
    if listed is None:
        return None, "list_recipes has not answered yet"
    asked = view.user_messages[-1] if view.user_messages else ""
    if not re.search(COOKING, asked, re.IGNORECASE) or LISTING.search(asked):
        return False, "the shopper is not asking to cook anything"
    words = set(_WORD.findall(asked.lower()))
    recipes: Any = listed.get("result", listed) if isinstance(listed, dict) else listed
    for recipe in recipes if isinstance(recipes, list) else []:
        if not isinstance(recipe, dict):
            continue
        name = str(recipe.get("name", ""))
        for said in (str(recipe.get("name", "")), str(recipe.get("slug", ""))):
            if (named := set(_WORD.findall(said.lower())) - {"and", "with", "the"}) \
                    and named <= words:
                return False, f"the library has {name!r}"
    return True, f"none of the {len(recipes)} library recipes is named in the request"


def library_miss(view: View) -> CheckResult:
    """True when a library lookup (get_recipe, plan_recipe) answered without a result: an
    unknown slug, or a plan that failed."""
    called = [n for n in view.tool_calls if n in ("get_recipe", "plan_recipe")]
    if not called:
        return None, "no library lookup yet"
    missed = [n for n in called if n not in view.results]
    return (True, f"{missed[0]} returned no result") if missed else \
        (False, "the library lookups answered")

# --- code observers: a pattern or a tool result decides -------------------------------------------

link_reader = Observer("link_reader", "Watches the shopper's messages for a recipe page to read.")
link_reader.when("the shopper's message contains a web link", check=user_says(r"https?://\S+"),
                 id="recipe_link") \
    .enable_tools("fetch*", "plan_from_text") \
    .enable_skill("recipe-shopper")

recipe_reader = Observer("recipe_reader", "Notices when the shopper pastes a recipe.")
recipe_reader.when(
    "the shopper's message holds an ingredient list (three or more list lines)",
    check=user_says(r"(?m)(^\s*([-*•]|\d+[.)])\s+\S.*\n){2,}^\s*([-*•]|\d+[.)])\s+\S"),
    id="pasted_recipe") \
    .enable_tools("plan_from_text") \
    .enable_skill("recipe-shopper")

# A dish is planned from the library first (get_recipe, plan_recipe); plan_from_text (~600
# tokens of tool definition) joins only when the dish is not in the library, a library lookup
# fails, or a recipe arrives as text or a link (recipe_reader, link_reader above).
menu_clerk = Observer("menu_clerk", "Listens for a dish the shopper wants to cook or shop for.",
                      on=["turn", "tool_result"])
menu_clerk.when("the shopper wants to make, cook or plan a dish or some meals",
                check=wants_a_dish, on="turn", id="dish_to_cook") \
    .enable_tools("get_recipe", "plan_recipe")
menu_clerk.when("the agent listed the recipe library", check=tool_called("list_recipes"),
                on="tool_result", id="library_listed") \
    .enable_tools("get_recipe", "plan_recipe")
menu_clerk.when("the shopper wants to cook something the recipe library does not have",
                check=dish_not_in_library, on=["turn", "tool_result"], id="not_in_library") \
    .enable_tools("plan_from_text")
menu_clerk.when("a library lookup found no recipe", check=library_miss, on="tool_result",
                id="library_miss") \
    .enable_tools("plan_from_text")

# Two origin questions, two tools. Leaving a country out of a plan needs neither: the plan tools
# take exclude_origin and report the basket's coverage themselves.
origin_desk = Observer("origin_desk", "Hears questions about where products come from.")
origin_desk.when(
    "the shopper asks where a product comes from, or for the origin evidence",
    check=user_says(r"\bwhere (does|do|did|is|are)\b.{0,60}\b(come from|made|grown|produced)\b"
                    r"|\b(origins? of|country of origin|which countr(y|ies)|made in|product of"
                    r"|imported|provenance|evidence|label says)\b"),
    id="origin_lookup") \
    .enable_tools("get_product_origins")
origin_desk.when(
    "the shopper wants products chosen or ranked by a country preference",
    check=user_says(r"\b(prefer\w*|rather have|local(ly)?|rank\w*|most canadian"
                    r"|(buy|choose|pick|favou?r) (\w+ )?(canadian|american|mexican|italian"
                    r"|spanish|local)|which (products|items)\b.{0,40}\bfrom)\b"),
    id="origin_preference") \
    .enable_tools("rank_products_by_origin")

week_planner = Observer("week_planner", "Hears a request for several meals at once.")
week_planner.when(
    "the shopper wants a week of meals, several dinners or a budget for them",
    check=user_says(r"\b(week|weekly|meal plan|\d+\s+(dinners|meals|days|nights)"
                    r"|(two|three|four|five|six|seven)\s+(dinners|meals|days|nights)|budget)\b"),
    id="several_meals") \
    .enable_tools("plan_week")

shelf_clerk = Observer("shelf_clerk", "Reads find_product's results and nothing else.",
                       on="tool_result")


@shelf_clerk.when("find_product has returned at least one product",
                  check=tool_result("find_product", total=gte(1)), id="product_found")
def _offer_product_detail(ctx: Effects) -> None:
    ctx.enable_tools("get_product", "list_products")


ops_desk = Observer("ops_desk", "Hears questions about the pipeline itself.")
ops_desk.when("the shopper asks about the server's status, health or the label triage list",
              check=user_says(r"\b(status|health|pipeline|triage)\b"), id="pipeline_question") \
    .enable_tools("pipeline_status", "origin_triage")

# --- LLM observers: the observer model reads the plain-English condition ---------------------------

origin_listener = Observer("origin_listener", "A shopper's advocate who notices what people "
                           "care about, whatever words they use.")
origin_listener.when("the shopper wants to know or control where products come from (a "
                     "country to avoid or prefer, local food, imports)", id="cares_about_origin") \
    .enable_tools("get_product_origins", "rank_products_by_origin")

label_desk = Observer("label_desk", "The provenance desk clerk, who records label readings.")
label_desk.when("the shopper reports what a product's label says (a country of origin, "
                "\"Product of ...\", \"Made in ...\") or asks to record or review such evidence",
                id="label_report") \
    .enable_tools("submit_origin_evidence", "list_origin_submissions",
                  "review_origin_submission", "origin_triage")

diet_watch = Observer("diet_watch", "A dietitian who listens for allergies and diets.")
diet_watch.when("the shopper mentions an allergy, an intolerance or a diet (vegan, vegetarian, "
                "gluten-free, dairy-free, halal, kosher, low-sodium ...)", id="dietary_need") \
    .enable_tools("list_products", "get_product") \
    .enable_goal("Check every product against the shopper's diet from its name, category and "
                 "description; say plainly when the catalog cannot confirm a product suits it.")

tone_watch = Observer("tone_watch", "A customer-care lead who reads tone, not content.")
tone_watch.when("the shopper is frustrated, upset, impatient or swearing", id="frustrated") \
    .enable_goal("Acknowledge the frustration in one short sentence, apologise once, then get "
                 "straight to the answer.")

compliance_officer = Observer("compliance_officer", "A consumer-protection officer who listens "
                              "for requests that cross the law.")
compliance_officer.when("the shopper asks for help with something that would violate US or "
                        "Canadian law (buying alcohol or tobacco for a minor, reselling recalled "
                        "food, mislabelling a product's origin, dodging import rules)",
                        id="unlawful_request") \
    .disable_tools("plan_recipe", "plan_from_text", "plan_week", "submit_origin_evidence") \
    .enable_goal("Decline the unlawful part plainly, without lecturing, and help with whatever "
                 "is lawful.")

POLICY = Policy(
    initial=["list_recipes", "find_product"],
    observers=[link_reader, recipe_reader, menu_clerk, origin_desk, week_planner, shelf_clerk,
               ops_desk, origin_listener, label_desk, diet_watch, tone_watch, compliance_officer],
    # The cart's follow-ups to a plan: the hub calls them for the shopper's Options dialog and
    # "Use this", on the plan's basis, which the model never holds.
    hidden=["rank_alternatives", "reprice_plan"],
)
