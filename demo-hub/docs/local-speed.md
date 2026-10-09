# Making local models faster: what worked

*Measured 2026-10-05 on the demo laptop (Intel Core i7-1068NG7, 4 cores, 32 GB, no GPU), Ollama
0.35.1, the Assistant through ContextForge with progressive tool disclosure, pantry in demo mode.
One run per model and case, so read the numbers as sizes, not a leaderboard. The laptop throttles
under sustained load (`pmset -g therm`, logged every 30 s during the runs): its CPU ran at 39-100%
of full speed, which moves every timing by up to 2x.*

**Granite 4.2 8B answers the three shopper requests in 59-90 s each (median 85 s), 3 of 3
correct, down from 197-268 s (median 199 s, 2 of 3) before the changes, and from 7 min 24 s for the
console run that started this.** The laptop ran throttled (median 56% of full CPU speed) for the
new runs, so on a cool machine they are faster still. Almost all of it came from the hub, not the
model: code now writes what is structured (the plan's table, the recipe list), the model reads
less (compact plans, lean tool definitions, shorter instructions), its prompt cache holds
(tools in the order offered, a warm-up while the shopper types), and the hub supplies or guards
what a model got wrong (the shopper's location, a repeated call, an empty reply). A tools block
that never changes was faster still (median 74 s) but is off: the 8B ignored a tool announced in
a message instead.

Smaller models are faster but less reliable as agents here. Granite 4 H-Tiny (7B, about 1B active)
writes 5.4x faster than the 8B and answers in 16-109 s, but passed 5 of 12 runs (malformed tool
calls, an invented sum, the instructions' example taken as the request). Granite 4.2 3B passed 8
of 12 and answers in 31-112 s in the last two rounds; its misses were a made-up location and preference (both now
removed by the hub) and not knowing when to stop. **The 8B with these changes is the local
default to keep.**

## The starting point

From `local-models-report.md`, one Granite 4.2 8B answer (*"Plan tomato penne with nothing from
the United States…"*) took 7 min 24 s: reading its prompt 57% of the time (10-15 tokens/s), writing
43% (2-3.5 tokens/s). Its biggest pieces: reading 2,039 tokens of instructions and tool definitions
cold (2 min 15 s), reading the plan's JSON (1 min 38 s), and typing the plan's Markdown table
(286 tokens, 2 min 25 s).

## What changed in the hub

| Change | What it removes |
|---|---|
| **The plan table, built by code** (`answers.py`): the model writes two or three sentences; the hub appends the table from the tool result | typing the table (about 250 tokens at 2-3 tokens/s on the 8B); a made-up total in the table is impossible |
| **Compact plans for local models**: a plan reaches the model as short lines (769 characters for tomato penne) instead of JSON (2,725) | about 70% of the plan's reading |
| **Shorter instructions**: the answer template is gone (the code draws the table), the plan rules are a list | about 15% of the instructions |
| **Tools in the order offered**, `discover_tools` first | re-reading every tool after one that an observer inserts mid-list |
| **Warm-up** (`POST /hub/agent/warm`): the model reads the instructions and first tools while the shopper types | the cold first read, from the shopper's wait |
| **Stable tools** (`DEMO_LOCAL_STABLE_TOOLS`, measured and left off): the tools block stays as it was at the first step; tools offered later are announced in a message | re-reading the conversation after an observer adds a tool, but the 8B did not call a tool it was only told about |
| **Lean tool definitions**: no docstring indentation, schema titles or null wrappers; descriptions to their first paragraphs | 38% of the tool definitions (plan_recipe: 2,031 characters to 843) |
| **The shopper's location, by the hub**; a local model's plan tools take no lat/lon, distance or verbose | about 30 tokens of writing per plan call, and the made-up or dropped locations |
| **The recipe list, by code**, when a turn lists the library and plans nothing | typing seven recipes (57 s on the 8B) |

## How fast each model reads and writes

`python -m demo_hub.speed`: the Assistant's instructions and the recipe-shopper procedure (about
2,900 tokens) behind a random first line, so nothing is cached, then about 120 tokens written.

| Model | Reading | Writing | The same 2,900-token prompt and 120 tokens |
|---|---|---|---|
| Granite 4.2 8B (Q4_K_M, dense) | 19.1 tokens/s | 2.5 tokens/s | 3 min 21 s |
| Granite 4.2 3B (Q4_K_M, dense) | 29.6 tokens/s | 4.1 tokens/s | 2 min 8 s |
| Granite 4 H-Tiny (`granite4:7b-a1b-h`, 7B with about 1B active, hybrid Mamba) | 46.8 tokens/s | 13.6 tokens/s | 1 min 6 s |

Measured one after another with the CPU heating up (the 8B first, at full speed); the 3B is only
1.6x faster than the 8B although it is a third of the size, while H-Tiny, which runs about 1B
parameters per token, writes 5.4x faster. Writing is what a mixture of experts speeds up most.

## The three cases, per model

The report's three requests through ContextForge, progressive disclosure, one run each, in
seconds (✗: failed the bench's checks). Each round adds changes to the one before it; rounds ran
back to back on a throttled CPU (39-100% of full speed, median 56%).

| Round | What it adds | Granite 4.2 8B: cheapest-penne · tomato-penne · recipe-list | Passed | Median | Tokens read / written per answer |
|---|---|---|---|---|---|
| Before | (local-models-report.md) | 199 · 268 ✗ · 197 | 2/3 | 199 s | 4,815 / 251 |
| 1 | table by code, compact plans, shorter instructions, tool order | 202 · 166 · 162 | 3/3 | 166 s | 4,389 / 194 |
| 2 | lean tool definitions, the hub's location, listing questions add no plan tools, empty-reply nudge | 157 · 129 ✗¹ · 119 | 2/3 | 129 s | 2,891 / 180 |
| 3 | warm-up; no lat/lon/distance/verbose to send; country lists described | 109 · 110 · 106 | 3/3 | 109 s | 2,833 / 171 |
| Final | the recipe list by code too | **85 · 90 · 59** | **3/3** | **85 s** | 2,893 / 157 |
| Final, stable tools | the same with the tools block fixed at the first step | 74 · 97 · 61 | 3/3 | 74 s | 2,926 / 163 |

¹ It excluded the United States as "US", which pantry accepts (the same seven products leave the
basket); the bench's check wanted the full name and now takes the aliases.

| Model (final round, warm; stable tools for both) | cheapest-penne · tomato-penne · recipe-list | Passed | Median |
|---|---|---|---|
| Granite 4.2 8B | 74 · 97 · 61 | 3/3 | 74 s |
| Granite 4.2 3B | 32 · 89 ✗ · 31 | 2/3 | 32 s |
| Granite 4 H-Tiny (round 3: stable tools off / on) | 42 · 98 ✗ · 22 / 22 ✗ · 34 ✗ · 27 ✗ | 2/3 / 0/3 | 42 s / 27 s |

What the small models got wrong, and what the hub now does about it:

| Failure | Model | Now |
|---|---|---|
| took the instructions' example dish ("stir-fry veggies for 3 meals") as the request | H-Tiny, 3B | the example is gone; "to see which recipes can be planned, call list_recipes" |
| sent lat -74, lon -84 | 3B | plan tools have no lat/lon; the hub sends the shopper's |
| sent max_km 0 and verbose true | H-Tiny | a local model's plan tools have neither; an invalid distance falls back to the shopper's |
| sent preference ["local", "organic"] | 3B | the country lists say they take country names |
| wrote 50 tokens that were neither text nor a readable tool call | H-Tiny | one nudge to call a tool or answer |
| planned, then kept calling tools until the step budget | 3B | the answer is still the plan's table, with a note |
| added "5 × $20.26 = $101.30" to its summary | H-Tiny | `grounded_money` flags it; not preventable by the hub |
| sent the same plan_recipe call eight times (a vegetable stew, with stable tools) | 8B | a repeated call in a turn is not run again; the model is pointed at the result it has |

Every run is an Assistant trace: the Metrics tab's **run** view shows each one's steps, tokens,
reading, writing and queueing.

## What Ollama's cache does with a changing prompt

Ollama keeps the prompt it last read and reuses the longest unchanged beginning of the next
one. Granite's chat template renders the system prompt, then the tools, then the conversation, so
a change in the tools makes the model read everything after it again:

- **Tools in the order offered.** The catalog's order put a tool an observer added in the middle
  of the list; now it goes last, so the instructions and the tools before it stay cached. Across
  conversations the first step read 10-25 new tokens instead of 600-1,300.
- **Warm-up.** After another model or conversation used Ollama, the first step reads the
  instructions and tools again (1,056 tokens: 62 s on the throttled 8B). The Assistant asks for
  that read when a local model is chosen, while the shopper types; the bench measured each run
  after one.
- **Stable tools** (`DEMO_LOCAL_STABLE_TOOLS`). An observer that adds a tool mid-turn (the shelf
  clerk after find_product) still changes the tools block. With stable tools the block stays as
  it was at the first step and the new tool is described in a message at the end, so the prompt
  only grows: cheapest-penne 74 s instead of 85 s; the other cases, where nothing is added
  mid-turn, are unchanged. **But a model trusts the tools block, not a message**: asked for a
  vegetable stew (not in the library), the 8B was told about plan_from_text in a message, never
  called it, and repeated plan_recipe until the step budget (870 s); with plan_from_text in the
  block it planned the stew in 154 s. Stable tools stay off.
- **Hybrid models resume from checkpoints.** H-Tiny's Mamba layers cannot rewind to any position;
  llama.cpp saves checkpoints and restores the last one before the change (in one step it went
  back to token 746 for a change at 1,441, and read 1,308 tokens instead of 614). An append-only
  prompt matters even more for them.
- **Queueing.** Ollama answers one request at a time; two chats wait for each other. Every model
  step now records its queued time (its wall time minus Ollama's reading, writing and loading).

## What works well

1. **Let code write what is structured.** The plan's table and the recipe list cost a slow model
   minutes to type and come out exact from the tool result. The model writes two or three
   sentences. This was the largest single saving in writing time.
2. **Give a local model less to read.** Compact plans (a fifth of the JSON), lean tool definitions
   (38% smaller) and shorter instructions took an answer from about 4,800 tokens read to 2,900.
3. **Keep the prompt cache valid.** Tools in the order offered and a warm-up while the shopper
   types: the first step reads the question, not the instructions. Not by moving tools out of the
   tools block: a model calls the tools it was given there, not those described in a message.
4. **Take arguments the model gets wrong out of its hands.** The shopper's location and distance
   come from the hub, so no model can drop, mistype or invent them; country lists say what they
   take; an empty reply gets one nudge; a repeated call is not run again; a plan is shown even
   when the model runs out of steps.
5. **Keep Granite 4.2 8B as the local default** with all of the above. Use the
   3B where speed matters more than a sure answer, and keep it under the online evals; H-Tiny is
   fast enough for an observer or a classifier, not for this agent.
6. **Then the machine.** Cooling matters (the CPU spent the runs at a median 56% of its speed);
   any Apple Silicon Mac or recent NVIDIA GPU would make the 8B roughly 5-10x faster at writing.
   Speculative decoding (llama.cpp's server, with a small Granite as the draft) and a 3B
   fine-tuned on these traces are the next experiments.

The same table-by-code answer and shorter instructions apply to Gemini (fewer output tokens per
answer, so a lower cost); Gemini was not run again for this.

## Meal plans from one sentence (P8)

*Measured 2026-10-09 on the same laptop (Intel Core i7-1068NG7, 32 GB, no GPU), Ollama 0.40.2,
Granite 4.2 8B with thinking off, through the direct pantry MCP server, progressive disclosure,
pantry in demo mode. 3 runs per round (`--repeat 3`), each after a warm-up, as the Assistant warms
up while the shopper types. Gemini was not benched: it is a paid API and needs the user's OK.*

The case is the user's sentence, "3 Pepperoni Pizza + 2 Chicken Fried Rice + 3 chicken briyani +
7 mango milkshakes in 2 weeks" (`bench.py`, `meal-plan-fortnight`). The hub reads it with
pantry's Quick add parse before the model and tells the model in a `[meals]` note; a run passes
when the draft places 3 Pepperoni Pizza, 2 Chicken Fried Rice and 7 Mango Milkshake and only
proposes Chicken Biryani.

| Round | Runs passed | Tool-argument error rate | Time to first token, first call (median) | Total per run (median, range) | First call's prompt |
|---|---|---|---|---|---|
| plan_meals as first built | 3/3 | 0 of 3 | 160.4 s | 402.5 s (322-456 s) | 4,148 tokens |
| hidden arguments' types left out of the schema | 3/3 | 0 of 3 | 31.8 s | 175.7 s (175-177 s) | 1,910 tokens |

- **Tool-argument errors**: none. An error is malformed `dishes` (not a list of `{recipe,
  count}`), another plan tool called instead of `plan_meals`, or a call pantry refused. In all 6
  runs Granite called `plan_meals` itself with dishes it wrote, titles or slugs
  (`pepperoni_pizza`, `Pepperoni Pizza`), and pantry received 3/2/7 of the three exact and
  plural dishes, `days` 14 and Chicken Biryani as a proposal. It never sent the empty dishes the
  instructions ask for, so its own were used. These runs kept what the hub sent, not what the
  model wrote, so whether it also named Chicken Biryani (which the hub moves to the proposals,
  listing the difference) is not recorded; in the live check it did. The bench now keeps the
  model's own arguments (`plan_calls` in runs.jsonl). The hub never had to draft the plan itself.
- **What made it faster**: the first round's first call read 4,148 tokens although the warm-up
  had read the instructions. `plan_meals`' hidden arguments (`current`, `my_recipe_docs`,
  `proposed`) were taken out of the schema the model sees, but the types they use (a RecipeDoc,
  a meal plan and their parts) stayed in its `$defs`: 8,780 characters of tool definition for two
  arguments. `_plan_tools` now keeps only the `$defs` a shown argument reaches (1,590 characters),
  and the first token came 5x sooner. The machine was not throttled during the second round
  (`pmset -g therm`: CPU speed limit 100); the first round ran right after the test suite and
  wrote at 2.0 tokens/s against 3.6, so part of the total's drop is the CPU's, not the change's.
- **Steps**: 2 of the first 3 runs and all 3 of the second called `list_recipes` first, a step of
  about 35 s on a warm cache that the `[meals]` note makes unnecessary.
- Live, in the console (the same sentence, first round's schema, the suite running alongside):
  680 s, 3 steps, 7 of 7 online checks passed, including `no_invented_shelf_life`.

Reproduce (pantry-api in demo mode on `PANTRY_API_URL`):

```bash
DEMO_OBSERVER_MODEL= .venv/bin/python -m demo_hub.bench --model 'ollama:granite4.2:8b#think=false' \
  --case meal-plan-fortnight --repeat 3 --profile full --disclosure progressive --target pantry --warm
```

## Reproduce

```bash
cd pantry-platform/demo-hub
.venv/bin/python -m demo_hub.speed --model granite4.2:8b --model granite4.2:3b --model granite4:7b-a1b-h
MODELS="ollama:granite4:7b-a1b-h ollama:granite4.2:3b#think=false ollama:granite4.2:8b#think=false" \
  scripts/speed-bench.sh ~/.pantry-demo/bench/speed
WARM=1 MODELS="ollama:granite4.2:8b#think=false" scripts/speed-bench.sh ~/.pantry-demo/bench/speed-final
DEMO_LOCAL_STABLE_TOOLS=1 WARM=1 MODELS="ollama:granite4.2:8b#think=false" scripts/speed-bench.sh ~/.pantry-demo/bench/speed-stable
.venv/bin/python -m demo_hub.report ~/.pantry-demo/bench/speed/*/
```
