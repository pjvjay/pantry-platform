# Meal plans in the Assistant

The shopper types one sentence:

> 3 Pepperoni Pizza + 2 Chicken Fried Rice + 3 chicken briyani + 7 mango milkshakes in 2 weeks

and the Assistant answers with a **meal-plan card**: the two weeks as a strip of days, what
the card would add to the shopper's Meal plan, Chicken Biryani as a question ("chicken briyani
→ Chicken Biryani (demo starter)?" with **Use** and **Not this**), the suggested trips and
their total, and two buttons, **Apply** (into the Meal plan, as one undo step) and **Open in
Meal plan**. Nothing is saved anywhere but the shopper's browser, and no trip is approved: that
is the shopper's, in the Meal plan.

## The pieces

| Piece | Where | What it does |
|---|---|---|
| `plan_meals` | pantry-api MCP (`mealplan/assistant.py`), REST twin `POST /mealplan/plan` | Counted dishes drafted into a 1-14 day plan. Only exact and plural matches are placed; alias and fuzzy matches come back as proposals; other names as unmatched with what they could be. Returns `{summary, draft, full?}`; `summary.ops` are what the console applies. No approve argument. |
| `meal_planner` observer | `assistant_policy.py` | Fires on counted dishes joined by `+`, a comma or "and", on "in N weeks", "a fortnight", "10-14 days" or "meal plan for". Enables `plan_meals`. "plan 5 dinners under $60" stays `week_planner`'s (one count, no period); `week_planner` never heard "weeks". |
| `compliance_officer` | `assistant_policy.py` | Disables `plan_meals` with the other plan tools. |
| `_pre_meal_selection` | `agent.py`, `meal_plans.py` | Before the first model call: pantry's Quick add parse (`POST /mealplan/selection/parse`, no LLM) reads the sentence, and the result is kept for the turn. |
| `[meals]` note | `meal_plans.meals_note` | One line (at most 300 characters) before the shopper's words. |
| Hidden arguments | `meal_plans.fill`, `agent._with_hub_args`, `agent._plan_tools` | The hub fills what a model gets wrong. |
| Granite-first draft | `agent._draft_meals` | A model that answers without calling a tool still gets the plan. |
| `ChatBody.meal_plan` | `app.py`, `meal_plans.MealPlanBody` | The console's Meal plan in brief, sent with every message. |
| Card | `answers.plan_card` (kind `mealplan`), pantry-frontend `MealPlanCard` | Strip, diff, proposals, Apply, Open in Meal plan. |
| Evals | `evals.check_no_invented_shelf_life`, `evals.check_grounded_nutrition` | Every meal-plan answer is graded. |

## Reading the sentence in code (G10)

Writing `{"dishes": [{"recipe": "starter:pepperoni_pizza", "count": 3}, ...]}` for four dishes
is the most a local model is asked to write in this demo, at 2-3 tokens a second on the demo
laptop. The hub does it instead. When `meal_planner`'s condition holds for the newest message
(checked in code in either disclosure mode) and `plan_meals` is offered, the hub posts the
message to pantry's parse, with the shopper's own recipes from the Meal plan (so "grandma dal"
can match one) and the household's servings. The parse matches each name against the library,
the demo starters and those recipes: exact, plural, alias, then fuzzy. The hub keeps:

- **dishes**: exact and plural matches, by recipe key, with their counts;
- **proposed**: alias and fuzzy matches (`chicken briyani` is an alias of Chicken Biryani);
- **unmatched**: names no single recipe fits;
- **period**: "in 2 weeks" is 14 days.

The model reads one note before the shopper's words:

```
[meals] parsed: 3 Pepperoni Pizza, 2 Chicken Fried Rice, 3 Chicken Biryani (you wrote "briyani", needs your OK), 7 Mango Milkshake (snack); 14 days; unmatched: none
```

Long names are shortened, then the details, never the counts, so the note never passes 300
characters. A parse that matches nothing adds no note; a parse that fails (pantry down) leaves
the turn to the model, and the browser sees a `meal_selection` event saying so.

The PREAMBLE says what to do with it: call `plan_meals` once with no dishes, then say in two or
three sentences what was placed and ask about each dish that needs the shopper's OK and each
unmatched name; never state how long food keeps, never approve a trip.

## What the hub fills in

`plan_meals` as the model sees it takes `dishes`, `days` and `household_servings` (and, for a
cloud model, `max_km` and `verbose`); the types only hidden arguments use leave the schema with
them (`agent.used_defs`), which took Granite's tool definition from 8,780 characters to 1,590.
The hub sets the rest:

| Argument | From |
|---|---|
| `dishes` | the parse's exact and plural matches, when the model sent none (missing, empty or malformed) |
| `proposed` | the parse's alias and fuzzy matches, always; the model never sets them |
| `days` | the shopper's period ("in 2 weeks"), over the model's |
| `current` | `ChatBody.meal_plan` without its docs |
| `my_recipe_docs` | the docs in `ChatBody.meal_plan`: the lines of the shopper's own recipes |
| `start_date` | tomorrow in Vancouver; pantry uses it only when there is no current plan |
| `lat`, `lon`, `max_km` | the shopper's location, as for every plan tool |

**Model-supplied dishes are used**, with one exception: a dish naming a recipe the parse holds
only as a proposal ("Chicken Biryani" when the shopper wrote "briyani") stays a proposal. Every
difference from the parse (other counts, dishes missing or added, another period, a dish moved
to the proposals) is listed first among the result's warnings, prefixed "Differs from the
shopper's message:", where the model reads it.

## When the model makes no tool call (Granite-first)

If the model's answer ends the turn without a `plan_meals` result and the parse matched dishes,
the hub calls `plan_meals` itself, after the model's reply, with the parsed dishes. The call is
the hub's (`tool_call` event with `by_hub: true`), it joins the conversation as an assistant
tool call with no arguments and its result, so the model knows of the draft next turn, and the
card is labelled **drafted from your message**. An empty reply gets "I drafted this plan from
your message." No second model call is made: on the demo laptop that would cost minutes.

## The console's plan (`ChatBody.meal_plan`)

```
{start_date, days, rev, meals ≤ 56, approved_trips ≤ 14 [{date, strategy}],
 recipes ≤ 12 [{key, title, kind, slot}], prefs, docs {key: RecipeDoc}}
```

At most 64 KB (413 above; 422 `bad_meal_plan` naming the field when it is not a plan). The
conversation keeps the latest one in memory only and never writes it anywhere. Approved trips
travel as dates: approvals stay in the console. pantry keeps the current plan's meals where they
are, lengthens its window but never shortens it, and warns that new meals will put approved
trips up for review.

## The card, Apply and Open in Meal plan

The card (`plans[].kind === 'mealplan'`) carries the summary with its `ops`, the plan's
`base_rev` and the link. In the console:

- the **strip** shows each day with its meals, new ones marked;
- the **diff** says what Apply changes in the shopper's current plan: new recipes, more meals
  of one already there, a longer plan;
- each **proposal** asks its question, with **Use** (the dish joins the plan and the Meal plan
  spreads its meals over free slots) and **Not this** (it is left out);
- **Apply** applies every op and each proposal the shopper said Use to, in one edit: one undo
  step in the Meal plan. When the plan changed since the draft (`base_rev`), each op is tried
  in turn, and a meal whose cell is now taken waits in the tray; the card lists what was not
  applied;
- **Open in Meal plan** goes to the tab; the Assistant's conversation stays as it was when the
  shopper comes back.

## Why the assistant never approves

Approving a trip is the shopper's promise to buy that list on that day: pantry fingerprints it,
the calendar export takes only approved trips, and a later change puts it up for review.
`plan_meals` has no argument for it, its ops have no approve op, its result says "Trips are
suggestions: only the shopper approves them, in the Meal plan", and the PREAMBLE tells the
model not to.

## What the model reads

A cloud model reads the summary as JSON without `ops` and without the draft (`FOR_MODEL_NESTED`,
`FOR_BROWSER`). A local model reads at most 12 lines under 1,500 characters
(`answers.mealplan_for_model`):

```
meal plan draft (not applied yet): 12 new meal(s) over 14 days from Fri 9 Oct, each serving 2
placed: 3 Pepperoni Pizza, 2 Chicken Fried Rice, 7 Mango Milkshake (snack)
needs the shopper's OK, not placed: 3 Chicken Biryani (you wrote "chicken briyani")
not found: none
trips (fresh, suggested): 3: Sat 10 Oct $63.76; Mon 12 Oct $43.53; Mon 19 Oct $22.48; total $129.77 (demo prices)
other strategy (fewest_trips): 2 trip(s), $133.64
nutrition per person (demo amounts): ... at least 5,456 kcal
(the shopper sees the plan as a card under your answer; only they apply it and approve trips)
```

## Evals

- `no_invented_shelf_life`: every storage time in the model's reply ("keeps 3 days in the
  fridge") is a figure a tool result of the turn holds. Checked on every meal-plan turn.
- `grounded_nutrition`: every kcal or protein figure in the reply is in a tool result, and a
  reply quoting one for meals whose amounts are demo house amounts says "demo". The hub's own
  table says "demo amounts" anyway; the check reads the model's reply alone.

## Tests

`tests/test_pre_meal_selection.py`: the observer (fires on the sentence, not on "plan 5 dinners
under $60"), compliance, the parse before the first model call, the note (exact text, 300
characters), `plan_meals({})` built from the parse, fuzzy and alias matches only proposed,
model dishes used with their differences listed, malformed dishes replaced, the hub's draft
when the model makes no call, and `ChatBody.meal_plan`. `tests/test_meal_plans.py`: the card,
what the model and the browser read, the Markdown, the evals and the bench's meal-plan case.
pantry-api: `tests/test_mcp_meals.py`.

## Speed

`python -m demo_hub.bench --case meal-plan-fortnight --profile full --disclosure progressive`
runs the sentence; the report's "Meal-plan arguments" section counts the model's plan_meals
calls, their dishes (written, empty and filled by the hub, malformed), other plan tools called,
drafts the hub made, and the tool-argument error rate. Results: `docs/local-speed.md`.
