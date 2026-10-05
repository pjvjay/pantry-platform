#!/bin/bash
# Compare the local models on the pantry tasks (README: "Comparing local models: the bench").
#
#   scripts/bench.sh [out-dir]
#
# Phase 1 runs every case once on Granite 4.2 8B without thinking, command-r7b and Granite with
# thinking (its Ollama default); phase 2 adds two more repetitions of the first two for
# consistency. Resumable: stop it any time and rerun with the same out-dir. pantry is put in demo
# mode while it runs (so its own planner is deterministic) and set back afterwards.
set -uo pipefail
source "$(dirname "$0")/stack.env.sh"
OUT=${1:-$STATE_DIR/bench/$(date +%Y%m%d-%H%M%S)}
RUNTIME=http://127.0.0.1:$PANTRY_PORT/settings/runtime
GRANITE=ollama:granite4.2:8b
COHERE=ollama:command-r7b

export PANTRY_MCP_TOKEN_FILE=$SECRETS_DIR/pantry_mcp_token_hub
export RECIPE_SHOPPER_SKILL=$PANTRY_API_DIR/skills/recipe-shopper/SKILL.md
[ -f "$RECIPE_SHOPPER_SKILL" ] || { echo "missing $RECIPE_SHOPPER_SKILL" >&2; exit 1; }
for model in granite4.2:8b command-r7b; do
  ollama show "$model" >/dev/null 2>&1 || { echo "pull it first: ollama pull $model" >&2; exit 1; }
done

was=$(curl -fs "$RUNTIME" | python3 -c 'import json,sys; print(str(json.load(sys.stdin)["demo_mode"]).lower())') \
  || { echo "pantry API is not running (scripts/up.sh)" >&2; exit 1; }
restore() { curl -fs -X POST "$RUNTIME" -H 'Content-Type: application/json' -d "{\"demo_mode\": $was}" >/dev/null \
  && echo "pantry demo mode set back to $was"; }
trap restore EXIT
curl -fs -X POST "$RUNTIME" -H 'Content-Type: application/json' -d '{"demo_mode": true}' >/dev/null
echo "bench output: $OUT (pantry demo mode on; was $was)"

cd "$HUB_DIR"
.venv/bin/python -m demo_hub.bench --out "$OUT" --repeat 1 \
  --model "$GRANITE#think=false" --model "$COHERE" --model "$GRANITE" || exit $?
.venv/bin/python -m demo_hub.bench --out "$OUT" --repeat 3 \
  --model "$GRANITE#think=false" --model "$COHERE"
