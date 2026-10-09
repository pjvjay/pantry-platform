# pantry demo hub

One browser entry point for the whole pantry stack: the grocery planner, its MCP server, the
ContextForge gateway, a live MCP agent and the mcp-sim agent simulations. One command starts
everything; one URL demos it.

```bash
demo-hub/scripts/up.sh
```

Then open **http://127.0.0.1:8090/pantry/**. Stop with `scripts/down.sh`; check with
`scripts/status.sh`.

## What each tab demos

| Tab | Feature | Under the hood |
|---|---|---|
| **Overview** | Architecture diagram and a 7-step guided tour | — |
| **Planner** | Plan a pasted recipe (location, distance limit, partial plans, origin prefer/exclude), a week of dinners (budget, diet tags) or a library recipe (priced at stores near the chosen location, with a trip split). Shows the parsed lines, the per-step SQL query plan, the store split, coverage, and what could not be bought | pantry REST `/plan/nl`, `/plan/week`, `/plan/{slug}` |
| **Assistant** | Chat with a grocery agent that plans by calling MCP tools; every call and result is shown live. Paste a recipe link or a YouTube link and the hub reads its ingredient lines before the model does; the model plans exactly those lines by their key, and pages the hub cannot read go through the gateway's fetch tool as before ([docs/recipe-import.md](docs/recipe-import.md)). Click a product in a plan's cart for its ranked alternatives, and swap it: the cart re-prices with no model call and the agent hears of it next turn ([docs/cart-alternatives.md](docs/cart-alternatives.md)) | hub agent loop (Gemini, or Ollama), MCP via ContextForge `pantry-recipes` (or direct pantry, or `pantry-sim`) |
| **Catalog** | The planner's own lookup (`find_product`, with direct/generic/relaxed match levels) and a product's store offers, origin and evidence | REST `/products`, MCP `find_product`, `get_product` |
| **Provenance** | Coverage, label triage, submit a label reading (an MCP write), review queue (approve / reject), and the origin ranking changing live; one-click demo-data reset | MCP resource `pantry://origins/coverage`, tools `origin_triage`, `submit_origin_evidence`, `list_origin_submissions`, `review_origin_submission`; REST `/origins/rank` |
| **MCP explorer** | All 15 tools (forms generated from their schemas, annotations shown), 4 resources + 1 template, 3 prompts — directly on pantry with a bearer token, anonymously (refused with 401), or through ContextForge's two virtual servers | hub `/hub/mcp/*` with the official MCP SDK |
| **Simulations** | mcp-sim scenarios by category, last verdicts (judge checklist, deterministic matcher, observer reports) and conversations; start runs with a model preset and follow the job | the mcp-sim runner's API, proxied |
| **System** | Every service's health and links (API docs, ContextForge admin, runner, Burr); the planner's LLM switch: real planning on Gemini or demo mode, and its four model specs | `/hub/status`; pantry `/settings/runtime` |

## How it fits together

```
browser ── :8090 demo hub ─┬─ /pantry/          the built pantry-frontend
                           ├─ /pantry/api/*     → pantry API :8000 (REST + /mcp)
                           ├─ /hub/mcp/*        → MCP: pantry :8000/mcp (bearer) | ContextForge :4444 virtual servers
                           ├─ /hub/agent/chat   → agent loop: Gemini / Ollama  ⇄  MCP tools   (server-sent events)
                           ├─ /hub/agent/conversations/{id}/alternatives, /swap → the cart: pantry :8000/mcp directly
                           ├─ /hub/sims/*       → mcp-sim runner :8765
                           └─ /hub/status       → every service's health
```

The hub holds every secret (the pantry bearer token labelled `demo-hub`, the ContextForge JWT,
the Gemini key); the browser only talks to the hub.

The hub answers only to its own loopback address (and `HUB_ALLOWED_HOSTS`, by default Vite's
`localhost:5173`). Every request that changes something under `/hub/` or `/pantry/api/` must send
`X-Pantry-Console: 1` and JSON, which a page on another site cannot do. The console does this
for you; a script adds `-H 'X-Pantry-Console: 1' -H 'Content-Type: application/json'`. See
[docs/hub-security.md](docs/hub-security.md).

`scripts/up.sh` starts, or reuses when already healthy:

| Service | Port | From |
|---|---|---|
| pantry API + MCP | 8000 | `pantry-platform/pantry-api` with `RUNTIME_SETTINGS_ENABLED=1`, `MCP_AUTH_TOKENS` (`contextforge`, `demo-hub`), Gemini planner models; SQLite at `~/.pantry-data/pantry_demo.db` |
| ContextForge | 4444 | `pantry-gateway/scripts/run.sh`; registers `pantry-sim` and `pantry-recipes` |
| fetch MCP server | 9100 | `pantry-gateway/scripts/run_fetch.sh` |
| mcp-sim runner | 8765 | `mcp-sim/skills/simulate/scripts/run.sh ui --skill ~/Documents/workspace/mcp-sim-local/simulate` |
| demo hub | 8090 | this directory |

Options: `up.sh --reset` reseeds the demo data first; `up.sh --rebuild` rebuilds the frontend.
Logs and pid files live in `~/.pantry-demo/`. A service up.sh reuses keeps its settings: stop it
to apply new ones.

## Demo data

`scripts/reset-demo-data.sh` (also the Provenance tab's **Reset demo data** button) reseeds the
165-product catalog and 7 recipes, then loads `data/demo-labels.json` (36 label readings) and
`data/demo-origins.json` (5 database records): 38 products resolved, garlic conflicting (a label
says Mexico, a record says China), an importer-only soy sauce label that correctly does not count
as an origin, and enough US-origin items that excluding the United States changes baskets. It
also empties the review queue.

## Models and the Gemini free tier

- **Assistant**: local by default: IBM Granite 4.2 8B in Ollama, thinking off (`AGENT_MODEL` in
  `scripts/stack.env.sh`, passed as `DEMO_AGENT_MODEL`). Gemini models stay in the picker. On
  Gemini, when a model's daily quota is spent the agent switches to the next of
  `DEMO_AGENT_FALLBACKS` (default: 3-flash-preview, flash-latest, flash-lite-latest,
  3.1-flash-lite) and says so in the chat. Per-minute limits are waited out (Gemini's
  `retryDelay`, at most 60 s).
- **Observers** (tool disclosure, below): `OBSERVER_MODEL` judges the plain-English conditions;
  empty by default, so only the code observers act and no cloud model is called.
- **Planner** (pantry-api): Flash-Lite models by default, so it does not compete with the
  Assistant for quota (`PLANNER_*_MODEL` in `scripts/stack.env.sh`; changeable live on the
  System tab). Demo mode needs no model at all.
- The free tier allows about **20 tool-calling requests per model per day**. One Assistant answer
  is 3-10 requests; one planned recipe is 2-3. For longer demos use demo mode for the Planner, or
  a paid Gemini key.
- The Gemini key is read from `mcp-sim/.env` (`GEMINI_API_KEY`), never printed.

## Local models (Ollama)

The Assistant and the simulation presets can also run on local models:

| Model | Ollama tag | Source | Context |
|---|---|---|---|
| IBM Granite 4.2 8B | `granite4.2:8b` | byte-identical to `granite-4.2-8b-Q4_K_M.gguf` in [ibm-granite/granite-4.2-8b-GGUF](https://huggingface.co/ibm-granite/granite-4.2-8b-GGUF) (Apache-2.0) | 131,072 trained |
| Cohere command-r7b | `command-r7b` | Ollama library (CC-BY-NC-4.0) | 8,192 trained |

```bash
ollama pull granite4.2:8b
ollama pull command-r7b
```

- **Context.** Ollama's default window is 4,096 tokens and it silently drops the start of a
  longer prompt. The Assistant's prompt (instructions, the recipe-shopper skill, 15 tool
  definitions) is about 6,800 tokens, so with the default a model never sees most of its
  instructions or tools and answers from memory. The hub therefore calls Ollama's native
  `/api/chat` with `num_ctx` = `OLLAMA_NUM_CTX` (default 16,384), capped at each model's own
  trained context. The runner gets `MCPSIM_OLLAMA_NUM_CTX=16384` from `up.sh`.
- **Speed.** Without a GPU (an Intel Mac, say) an 8B model reads a prompt at about 20-30
  tokens/s and writes at about 3-4 tokens/s: the first call of a conversation takes about 4-5
  minutes. Granite is a plain transformer, so Ollama reuses the cached prompt prefix on later
  calls; command-r7b's sliding-window attention forces it to re-read the whole prompt every call.
  The Assistant shows each call's timing (prompt read, generation, model load).
- **Thinking.** Granite thinks by default, which on a CPU adds minutes of reasoning tokens to a
  step. The model picker offers it both ways: `ollama:granite4.2:8b#think=false` and
  `ollama:granite4.2:8b`. Any model spec takes `#think=true|false` and `#temperature=<n>`.
- **Progress estimate.** Every model call's timing is recorded in
  `~/.pantry-demo/llm-timings.jsonl` (`LLM_TIMINGS_PATH`; the bench adds to it too), per model
  and thinking setting. Before a call the hub estimates its two phases from that history: the
  prompt tokens the model must read (a model that keeps its prompt cache only reads what changed
  since its last call; whether a model does is measured) over its reading speed, then its usual
  reply length over its writing speed. Ollama's reply is streamed, so once writing starts the live
  token count and speed take over. The Assistant shows the phase, the time left and a progress
  bar; past the estimate it says so instead of sitting at 100%. A model with no history yet is
  estimated from default rates (20 tokens/s read, 2 written).
- Other knobs: `OLLAMA_TEMPERATURE` (default: the model's own), `OLLAMA_THINK` (default: the
  model's own), `OLLAMA_TIMEOUT_S` (default 900; with streaming it bounds the silent wait).

## Progressive tool disclosure

A conversation starts with two tools (`list_recipes`, `find_product`) and `discover_tools`; the
observers in `demo_hub/assistant_policy.py`, written with the SDK in `demo_hub/observers.py`,
enable the rest when they see what calls for them:

```python
menu_clerk.when("the shopper wants to make, cook or plan a dish or some meals",
                check=user_says(COOKING), on="turn", id="dish_to_cook") \
    .enable_tools("get_recipe", "plan_recipe")
compliance_officer.when("the shopper asks for help with something that would violate US or "
                        "Canadian law ...", id="unlawful_request") \
    .disable_tools("plan_recipe", "plan_from_text", "plan_week", "submit_origin_evidence") \
    .enable_goal("Decline the unlawful part plainly ...")
```

A condition with a `check` is code, free and instant; one without is judged by
`OBSERVER_MODEL` (one batched call per trigger). Effects fire when a condition becomes true
(or false, for `.otherwise`). The agent can ask for tools no observer enabled with
`discover_tools(query)`; a call to a tool it was not offered is refused as a scope violation.
The toolset picker in the Assistant switches to all tools from the start.

Why: a local model reads its prompt at ~15-20 tokens/s on a CPU, and tool definitions are most
of it. On "Plan tomato penne with nothing from the United States", step 1 is about 6,600 tokens
with every tool and the 2,000-token skill, about 1,900 progressive (plan tools only: the plan
takes `exclude_origin` itself). A local model's tool results are also shrunk to
`DEMO_LOCAL_RESULT_CHARS` (4,000 characters): long JSON lists are halved, staying valid JSON with
a "... N more not shown" note, so one catalog-wide lookup cannot add 4,500 tokens to every later
step. Caveat: Granite's template puts the tools right after the system prompt, so a tool added
mid-conversation makes it re-read the conversation once.

## Tracing: where the time goes

| Layer | Where | What |
|---|---|---|
| Model calls | the Assistant; `~/.pantry-demo/llm-timings.jsonl` | per step: tokens read (cached vs new), written, rates, wall time |
| Ollama | `tail -f ~/.ollama/logs/server.log \| grep -E "cached n_tokens\|print_timing"` | llama.cpp's own prompt-cache reuse and timings per request |
| Gateway | `scripts/gateway-traces.sh [minutes]` | every MCP request through ContextForge: tool, gateway time, tool time, status |
| pantry pipeline | Burr UI, http://127.0.0.1:7241 (started by `up.sh`); "Burr trace" links in the Planner and on Assistant tool cards | one run per plan call (`run-<recipe>-<time>-<id>`), every step's inputs and outputs |
| pantry's LLM calls | `~/.pantry-demo/logs/pantry-api.log`; Planner trace; Assistant "inside pantry" | each attempt phase by phase: DNS, connect, TLS, upload, waiting for Google (and Google's own server-timing), download, retry waits |

Every Assistant turn is also kept as one **trace** (`demo_hub/telemetry.py`), built from the agent's
own event stream, so the agent loop carries no tracing code, and shown on the **Metrics** tab as a
waterfall: the turn; each model step (tokens read, cached and new, and written; read and write
rates; time to first token; model load; the model's reasoning when it shows it, e.g. Granite with
thinking on; its cost at Google's list price, `pricing.py`); each MCP tool call (result size, the
characters the model actually reads, the plan's own confidence); under it ContextForge's request
and tool invocation (from its observability API, so the gateway's overhead shows), pantry's Burr
steps (`pipeline` on the plan) and pantry's own LLM calls; and the browser's measurements. Traces
are JSON lines in `~/.pantry-demo/traces/` (`DEMO_TRACES_DIR`); `GET /hub/traces`,
`/hub/traces/{id}` and `/hub/metrics` serve them, the last rolled up per model, tool, observer,
eval check, pantry step, browser measure and hub route.

**One run, every system** (`GET /hub/runs/{id}`, `demo_hub/runs.py`; the **run** button on the
Metrics tab and **run details** under each answer): the trace above joined with what lives
elsewhere, on one clock: each plan call's Burr run read from pantry's tracker files
(`DEMO_BURR_DIR`, set by `up.sh`): every action at its recorded start and end, its result, any
exception and the state it changed; and each tool call's ContextForge trace (request, tool
invocation, status, response size, tool and gateway ids). The page shows the run's numbers, where
its time went (queued, reading, writing, tools, ContextForge, observers, the hub), a timeline you
can zoom into (a 0.6 s tool call inside a 7-minute turn), each model step, each tool call with its
gateway and Burr detail, the observers, the evals, the browser's numbers and the answer.

**Online evals and confidence** (`demo_hub/evals.py`): every answer is graded the moment it
finishes, deterministically, against the turn's own tool results: finished, only offered tools,
no failed calls, every dollar amount and every store named came from a tool result, a plan's
answer has the table and names every product, a retried plan kept its location, no scope
violation. The share that passed is the **answer confidence**, shown under the answer with the
plan's own confidence (the selector's per-line confidence, exact matches, origin coverage).

**The browser** (`pantry-frontend/src/telemetry.ts`) measures itself and posts to
`/hub/telemetry`: the page's first byte, Largest Contentful Paint, Interaction to Next Paint,
layout shift and long tasks; every `/hub` and `/pantry/api` call (with the hub's own time from its
`Server-Timing` header); and each chat stream: first byte, first event, how late events arrived,
time to paint. So a slow page shows up as the browser, the hub, the model, the gateway or pantry.

**Pictures**: plan tool calls show a card per purchase with the ingredient's photo (its Wikipedia
thumbnail, `GET /hub/images/ingredient?name=`), where the trip buys it, the price, origin and
confidence; a fetched recipe page shows the photos it holds (`GET /hub/images/remote?url=`,
public http(s) hosts only, raster images up to 3 MB). The hub fetches each once and serves it from
`~/.pantry-demo/images/`, so the browser never calls a third party.

ContextForge records the gateway traces with `OBSERVABILITY_ENABLED=true` in its `.env`, but the
admin UI's Observability tab does not render them in 1.0.11 (its dashboard component needs eval,
which the admin page's CSP and Alpine's CSP build do not allow); `gateway-traces.sh` reads the
same data from its API.

## Making local models faster

A Granite 8B answer on the demo laptop (4 Intel cores, no GPU) spent about 57% of its time reading
its prompt and 43% writing (docs/local-models-report.md). What the hub does about it, measured in
`docs/local-speed.md`:

| | What | Setting |
|---|---|---|
| The table, by code | a plan's (or week's) table is built from the tool result and appended to the model's two or three sentences (`answers.py`); exact by construction. The answer event also carries the plans as data (`plans`) and the model's own sentences (`reply`): the Assistant draws each plan as a cart, per store, with what was left out and the swaps pantry found | always |
| Compact plans | a local model reads a plan as short lines, about a fifth of the JSON | `DEMO_LOCAL_COMPACT_PLANS` (on) |
| Tool order | `discover_tools` first, then tools in the order offered: one added later goes last, so the cached prompt holds up to it | always |
| Warm-up | the model reads the instructions and first tools while the shopper types (`POST /hub/agent/warm`; the Assistant calls it when a local model is chosen) | always for `ollama:` models |
| Lean tool definitions | a local model's tools without docstring indentation, schema titles and null wrappers, descriptions to their first paragraphs: the catalog's definitions from 16,540 characters to 10,214 | `DEMO_LOCAL_LEAN_TOOLS` (on) |
| The shopper's location, by the hub | a plan call without lat/lon gets the shopper's (downtown Vancouver, 5 km): no tokens spent typing it, and no plan loses its stores | `DEMO_SHOPPER_LOCATION` (`49.2827,-123.1207,5`; empty: off) |
| Stable tools | the tools block stays as it was at the first step; later tools are announced in a message, so the prompt only grows. Measured and left off: the 8B did not call a tool it was only told about | `DEMO_LOCAL_STABLE_TOOLS` (off) |
| Keep the model loaded | how long Ollama keeps the model, and its cache, after a call | `DEMO_OLLAMA_KEEP_ALIVE` (30m) |

`python -m demo_hub.speed --model M [--model M2]` measures a model's read and write rates on the
Assistant's own instructions; `MODELS="spec ..." [WARM=1] scripts/speed-bench.sh OUT` runs the
report's three cases per model.

## Comparing local models: the bench

`scripts/report-bench.sh` runs the cases behind `docs/local-models-report.md` through ContextForge,
each kept as a trace with its evals (`WITH_GEMINI=1` adds Gemini 3 Flash and 3.1 Flash-Lite), and
`python -m demo_hub.report <run dirs>` computes the report's tables from them.

`demo_hub/bench.py` runs the same shopper requests through the Assistant's own agent loop (same
prompt, same MCP tools on the direct pantry server) for each model, several times, and grades
every run deterministically against the tool results that run received — no LLM judge:

```bash
# pantry in demo mode first (System tab), so its planner is deterministic
.venv/bin/python -m demo_hub.bench --model ollama:granite4.2:8b --model ollama:command-r7b --repeat 3
```

- **Cases**: cheapest penne, "stir fry veggies for 3 meals", tomato penne with no US origin and the
  verified share, the recipe list, and two negatives (a product the catalog does not stock, a
  weather question that needs no tool).
- **Correctness**: did it call tools at all, the right ones with valid arguments, and does the
  answer state the product, store, price, total and coverage exactly as returned; any `$` amount
  in the answer that no tool returned counts as invented.
- **Consistency**: across repetitions, how often the same tool sequence, the same first call and
  the same `$` amounts recur, and which cases are flaky.
- **Performance**: seconds per model call (median and p90), prompt tokens read on first and later
  calls (prefix caching), prompt and generation tokens/s, model load time, memory.

`--profile core` (the default) offers 7 read-only tools; `--profile full` offers all 15, exactly
as the Assistant does. Runs are interleaved (repetition, case, model) and stream to
`~/.pantry-demo/bench/<time>/runs.jsonl`; `report.md` and `summary.json` are rewritten after every
run, and rerunning with the same `--out` resumes.

## Development

```bash
cd demo-hub && python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q          # 316 tests; no network, no keys
.venv/bin/ruff check demo_hub tests
```

The tests cover the chat client (request shapes, retries, quota handling, every failure path),
the agent loop (tool calls, failing tools, step budget, model fallback), the MCP client against a
real MCP server over HTTP (catalog, calls, resources, prompts, 401), the runner client, and every
HTTP route with its upstreams mocked. The frontend lives in `pantry-frontend` (`npm run build`
type-checks it); in dev, `npm run dev` proxies `/hub` to this hub on :8090.
