# Making local models faster: what worked

*Measured 2026-10-05 on the demo laptop (Intel Core i7-1068NG7, 4 cores, 32 GB, no GPU), Ollama
0.35.1, the Assistant through ContextForge with progressive tool disclosure, pantry in demo mode.
One run per model and case, so read the numbers as sizes, not a leaderboard. The laptop throttles
under sustained load (`pmset -g therm`, logged every 30 s during the runs): its CPU ran at 43-100%
of full speed, which moves every timing by up to 2x.*

<!-- SUMMARY -->

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
| **Stable tools** (`DEMO_LOCAL_STABLE_TOOLS`, off by default): the tools block stays as it was at the first step; tools offered later are announced in a message | re-reading the conversation after an observer adds a tool |

## How fast each model reads and writes

`python -m demo_hub.speed`: the Assistant's instructions and the recipe-shopper procedure (about
2,900 tokens) behind a random first line, so nothing is cached, then about 120 tokens written.

<!-- SPEED -->

## The three cases, per model

<!-- CASES -->

## What Ollama's cache does with a changing prompt

<!-- CACHE -->

## What works well

<!-- WORKS -->

## Reproduce

```bash
cd pantry-platform/demo-hub
.venv/bin/python -m demo_hub.speed --model granite4.2:8b --model granite4.2:3b --model granite4:7b-a1b-h
MODELS="ollama:granite4:7b-a1b-h ollama:granite4.2:3b#think=false ollama:granite4.2:8b#think=false" \
  scripts/speed-bench.sh ~/.pantry-demo/bench/speed
DEMO_LOCAL_STABLE_TOOLS=1 WARM=1 MODELS="ollama:granite4.2:8b#think=false" scripts/speed-bench.sh ~/.pantry-demo/bench/speed
.venv/bin/python -m demo_hub.report ~/.pantry-demo/bench/speed/*/
```
