# Alternatives in the chat cart

When the Assistant plans a recipe, the hub draws the plan as a cart. Click a product in that cart
and the console opens **Options for garlic**:
- the cart's own pick is pinned on top;
- the other products that could fill that line follow, in the planner's own order, each with
  its reasons;
- **Use this** re-prices the cart in place.

No model is involved: pantry ranks and re-prices from the plan's basis, which only the hub holds.
The model hears about the change before the shopper's next message.

This page covers the hub's part of the contract. The ranking itself is pantry-api's
(`pantry_planner/alternatives.py`, `flow.reprice`, MCP tools `rank_alternatives` and
`reprice_plan`). The dialog is pantry-frontend's.

## How it flows

1. **The plan comes back with its basis.** When the model calls `plan_recipe`,
   `plan_from_text` or `plan_from_lines`, the hub adds `basis: true`
   (`agent._with_hub_args`). It does this only if the target's listed schema takes `basis`,
   so a gateway with an older schema never gets an unknown argument. The model never sees the
   argument (`_plan_tools`).
2. **The basis stays in the hub.** pantry returns `summary.basis`, which records what each
   line was planned from, the product bought, and the location and origin rules. The hub keeps
   it in the conversation's `tool_log` and strips it everywhere else: the model's copy, the
   browser's `tool_result` events, the trace and the cards. This is the `SERVER_ONLY` set in
   `agent.py`, next to `FOR_BROWSER` and `FOR_MODEL_NESTED`. A client never holds the basis,
   so it can never send back a doctored one.
3. **The card names its plan.** Each plan card carries `ref`, the plan's index in `tool_log`,
   and `pinned_lines`. A plan with no basis (from an older gateway) gets no `ref`, and its
   cart has no Options.
4. **Options.** The dialog posts `{ref, line_no}` to `/alternatives`. The hub calls pantry's
   `rank_alternatives` on the direct `pantry` target, with the basis and the shopper's pins at
   that ref. Anything else in the body is ignored. Nothing in the conversation changes.
5. **Use this.** The dialog posts `{ref, line_no, product_id}` to `/swap`. Under the
   conversation's lock, the hub:
   - pins the product on every recipe line the purchase covers;
   - calls `reprice_plan` with the whole pin set (none on the basis, so an undone pin is
     simply left out);
   - appends the result to `tool_log` as the cart's new ref;
   - queues the change for the model.

   The answer is the redrawn card and the note the model will read.
6. **The next turn.** Before the model reads the shopper's next message, each queued change
   becomes:
   - a `[cart] ...` note in front of the message (at most 300 characters);
   - a `cart_change` event right after `start`.

   The trace keeps the event on the turn's root span. The grounding evals count it as a fact
   the model was told (`bench.Run.shopper_changes`), so "your trip is now $41.20" passes
   `grounded_money`. The PREAMBLE tells the model to use the note's figures and not to plan
   again unless asked, because a new plan drops the shopper's swaps.

`rank_alternatives` and `reprice_plan` are in `assistant_policy.POLICY.hidden`. The model is
never offered them, `discover_tools` never finds them, and a call to one is refused as not
allowed.

## The order, and why

pantry sorts the rows by `ORDER`. Each row's `rank_reason` says, in plain words, why it sits
below the row above.

| # | Key | Why it is here |
|---|---|---|
| 1 | Tier: the same ingredient, then not the same ingredient (a related word or a same-aisle substitute), then "outside" | A substitute is a different recipe. It never outranks the real ingredient, whatever it costs. "Outside" is a pick that matches none of the line's words: the selector saw the whole catalog. It is shown honestly, never as a match. |
| 2 | How many of the recipe's words match, then whether the product is named for and mainly the line's ingredient word (`alternatives.closeness`) | The demo selector's `units.semantic_key`, with its head test read from the ingredient word instead of the line's last word: "cumin powder" is about cumin, so Cumin Seeds rank above Curry Powder. For a line that ends in its ingredient word this only breaks semantic_key's ties, so the dialog does not disagree with the plan about which product is closer |
| 3 | Pack fit: covers, unknown, short | A pack that is too small makes the trip total look cheaper than the recipe really costs |
| 4 | Origin preference, only when the shopper gave one | Demo mode and the week planner put it before price too. Without a preference this key does nothing. |
| 5 | Trip total after the swap: prices, an extra stop, travel | This is what the shopper pays. For the first 40 rows the figure is a real re-price, the same code `/swap` runs, so they agree to the cent. Rows past 40, and plans with no location, have no trip figure. |
| 6 | Cost of the recipe's amount, at the price a pack where the row's trip buys it (`trip.buys_at`) | Breaks ties between rows with equal trips. The row's `offer` is the lowest price in range, but the trip skips that store when the stop costs more than it saves, so the row's price, this cost and the unit price are the trip store's: what the cart charges after "Use this" |
| 7 | Rating, only to break an exact cent tie | The reviews are synthetic, so they never outweigh a price |
| 8 | Catalog id | Makes the order total, so it is stable from call to call |

The cart's own pick is always listed (`current: true`), even when it would rank lower. Products
the plan's origin exclusion drops are listed under `held_back` with their evidence. They cannot
be chosen; the shopper has to ask the assistant to relax the exclusion.

## What is synthetic, and how it is labelled

- **Store prices, stock and reviews** are seeded demo data: base price ±15%, every product at
  every store, reviews from a brand hash. pantry's `OFFERS_SYNTHETIC` (default true) puts
  `data_note: "Store prices, stock at every store and reviews are demo data."` on every
  ranking, and while it is set every rating carries `synthetic: true` and its reason says
  "(demo)". Trip differences between rows are
  therefore mostly noise in the demo.
- **Origin** is evidence, never a guess. An unchecked product says "Origin not checked", never
  a country. Evidence from a demo label photo has `origin.demo: true`.
- **Unknowns stay unknown.** A product with no reviews has no rating (never 0 stars). An
  amount that cannot be compared with the pack gives pack fit "unknown" and no cost for the
  recipe's amount, never a guess.

## The hub's contract

### `POST /hub/agent/conversations/{cid}/alternatives`

The body is `{ref: int ≥ 0, line_no: 1-60, limit?: 1-25 = 12}`. It returns 200 with pantry's
`AlternativeRanking` as is. The hub takes no lock, makes no model call and changes nothing.

| Status | When |
|---|---|
| 404 | Unknown conversation (`detail` says "ask again to re-plan"), unknown ref, or `DEMO_CART_ALTERNATIVES=0` |
| 422 | The ref is not a recipe plan; the plan has no basis; pantry's own refusal (e.g. "line 9 is not a planned line; planned: 1, 2, 3."), passed through as `detail` without the MCP SDK's "Error executing tool …:" prefix; a body out of bounds |
| 502 | pantry is not reachable |

### `POST /hub/agent/conversations/{cid}/swap`

The body is `{ref: int ≥ 0, line_no: 1-60, product_id: int ≥ 1 | null}`. `product_id` is
required; null puts the planner's pick back. On success it returns:

```json
{"card": {"kind": "plan", "ref": 1, "pinned_lines": [2, 4], "summary": {"...": "the re-priced plan, without basis or trace fields"}},
 "note": "[cart] The shopper changed line 2 (garlic) of Spaghetti Bolognese in the cart: Garlic Bulb 3-pack -> Fraser Farms Garlic 200g. Trip now $13.45 at Pantry Mart Downtown, was $13.95."}
```

`note` is `""` when the line is back to what the model last knew: there is nothing to tell. The
console replaces the card with the old `ref` by the new one.

| Status | When |
|---|---|
| 404 | As for alternatives |
| 409 | A turn is running ("The assistant is answering; choose again when it finishes."); a newer plan or swap of the same recipe exists ("This cart is older than the latest plan for this recipe."; a library recipe is its slug, a pasted one its title and the ingredient names in its basis, so two pasted recipes under one title are two carts); the conversation uses `gateway-sim` |
| 422 | As for alternatives, plus pantry's pin refusals (a held-back origin, out of range, unknown id) and a line that is not in the cart |
| 502 | pantry is not reachable |

### The `cart_change` event and the note

On the next `/hub/agent/chat` turn, right after `start`, the hub emits one event per changed
line, or per set of lines that went from the same product to the same product (a purchase
covering lines 2 and 4):

```json
{"type": "cart_change", "ref": 1, "line_no": 2, "lines": [2, 4],
 "recipe_name": "Spaghetti Bolognese", "ingredient": "garlic + garlic clove",
 "from": {"id": 21, "name": "Garlic Bulb 3-pack"}, "to": {"id": 22, "name": "Fraser Farms Garlic 200g"},
 "total_before": 13.95, "total_after": 13.45, "stores_after": ["Pantry Mart Downtown"],
 "undone": false, "note": "[cart] ...", "structured": {"summary": {"...": "..."}, "full": null}}
```

Changes are coalesced per recipe and recipe line, not per purchase. A swap can merge a line
into another line's purchase; a later swap or undo made on that purchase then updates the
merged line's own change, so the model never hears of a swap that was taken back.
- `from` is the product the model last knew for the line and `to` is the product the cart buys
  for it now; a line back to what the model last knew is not mentioned;
- `total_before` is the cart's total when the model last knew it;
- every change of one cart carries that cart's latest `total_after`, `stores_after`, `ref` and
  `structured`.

The same turn's user message to the model is the notes, a blank line, then the shopper's words.
The shopper's words alone are what the observers read. Earlier messages are never edited, so a
local model's cached prompt holds. If the turn fails before `start`, the events still come
before the error.

The model's conversation is append-only. A swap never rewrites earlier messages, and its
result is never shown again as the next turn's card: a turn's cards come only from that turn's
own plans.

## Settings

| Variable | Where | Default | Meaning |
|---|---|---|---|
| `DEMO_CART_ALTERNATIVES` | hub | on | Off (`0`): plans are made without the basis, cards carry no `ref`, and both routes answer 404 |
| `OFFERS_SYNTHETIC` | pantry-api | true | Puts the data note on every ranking. Turn it off only when offers are real. |

## Rollout

These pantry-api changes alter the MCP tool set: two new tools, and new fields on the basis.
After a merge, refresh the gateway with `REFRESH_PANTRY=true scripts/register_fetch.sh` in
pantry-gateway (`up.sh` does this locally), then regenerate pantry-sim's scenarios with
`make_scenarios.py`. The cart itself does not depend on the refresh: the hub calls pantry
directly. A gateway that still has the old plan-tool schema gives carts without Options.

## Accessibility checklist (manual, per PR)

Run this in a real browser against `scripts/up.sh` in demo mode:

1. Tab to a cart item and press Enter. The dialog opens, and focus lands on its heading or
   Close.
2. Tab through the rows. Focus stays inside the dialog and wraps.
3. Press Esc. The dialog closes, and focus returns to the item that opened it.
4. Click the backdrop. The dialog closes the same way.
5. While a choice is being applied, "Use this" is disabled and visible text says why. A 409
   (answering, older cart) and a 404 (conversation gone) show their messages in plain words.
6. After a swap, a status region announces the new product and the new total, and the line
   shows "Changed by you".
7. At 375 px wide there is no horizontal scroll, and the dialog is a full-width sheet.
8. Dark mode and forced colours keep every row readable. Reduced motion is honoured.
9. Undo (choosing the planner's pick again) restores the totals.

Then the S3 smoke run:
1. "spaghetti bolognese for 4".
2. Open Options on ground beef.
3. Choose rank 2. Check that the total changes and "Changed by you" shows.
4. Ask "what is my total now?". Check that the answer quotes the new total and the online
   evals pass.
5. Ask for a week plan. The hub gives its card "Open in Meal plan"; the console draws the link
   only once it has the Meal plan tab (P5), because on a console without it the link would
   land on the Overview and drop the conversation. Before P5, check that no link is drawn.
