#!/bin/bash
# The runs behind docs/local-models-report.md: the same three shopper requests through ContextForge
# (pantry-recipes), every one kept as an Assistant trace with its online evals and gateway spans.
#
#   Granite 4.2 8B (thinking off), progressive tool disclosure and all tools from the start;
#   WITH_GEMINI=1 also: Gemini 3 Flash and 3.1 Flash-Lite, progressive (about 6 conversations,
#   ~15-25 requests, inside the free tier's 20 requests per model per day).
#
# pantry runs in demo mode (its planner calls no model, so only the agent's model differs) and the
# observers act on code only (DEMO_OBSERVER_MODEL empty). Usage:
#   scripts/report-bench.sh [OUT]            SKIP_LOCAL=1 skips the Granite runs
set -uo pipefail
source "$(dirname "$0")/stack.env.sh"
OUT=${1:-$STATE_DIR/bench/report-$(date +%Y%m%d-%H%M%S)}
RUNTIME=http://127.0.0.1:$PANTRY_PORT/settings/runtime
CASES=(--case tomato-penne-no-us --case cheapest-penne --case recipe-list)

export PANTRY_MCP_TOKEN_FILE=$SECRETS_DIR/pantry_mcp_token_hub CF_JWT_FILE=$SECRETS_DIR/contextforge_jwt
export RECIPE_SHOPPER_SKILL=$PANTRY_API_DIR/skills/recipe-shopper/SKILL.md DEMO_OBSERVER_MODEL=
[ -s "$CF_JWT_FILE" ] || { echo "no ContextForge JWT (scripts/up.sh mints it)" >&2; exit 1; }

was=$(curl -fs "$RUNTIME" | python3 -c 'import json,sys; print(str(json.load(sys.stdin)["demo_mode"]).lower())') \
  || { echo "pantry API is not running (scripts/up.sh)" >&2; exit 1; }
restore() { curl -fs -X POST "$RUNTIME" -H 'Content-Type: application/json' -d "{\"demo_mode\": $was}" >/dev/null \
  && echo "pantry demo mode set back to $was"; }
trap restore EXIT
curl -fs -X POST "$RUNTIME" -H 'Content-Type: application/json' -d '{"demo_mode": true}' >/dev/null
echo "report runs: $OUT (pantry demo mode on; was $was)"

cd "$HUB_DIR"
run() {
  .venv/bin/python -m demo_hub.bench --repeat 1 --profile full --target gateway-recipes "${CASES[@]}" "$@"
}
if [ "${SKIP_LOCAL:-0}" != 1 ]; then
  run --out "$OUT/granite-progressive" --disclosure progressive --model "ollama:granite4.2:8b#think=false" || exit $?
  run --out "$OUT/granite-all" --disclosure all --model "ollama:granite4.2:8b#think=false" || exit $?
fi
if [ "${WITH_GEMINI:-0}" = 1 ]; then
  GEMINI_API_KEY=$(gemini_key) || { echo "no Gemini key" >&2; exit 1; }
  export GEMINI_API_KEY
  run --out "$OUT/gemini-progressive" --disclosure progressive \
    --model gemini:gemini-3-flash-preview --model gemini:gemini-3.1-flash-lite || exit $?
fi
echo "done: $OUT"
