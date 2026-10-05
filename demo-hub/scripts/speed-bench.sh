#!/bin/bash
# The speed experiments behind docs/local-speed.md: the report's three shopper requests through
# ContextForge, progressive disclosure, pantry in demo mode, one run per model and case, every
# run kept as an Assistant trace. Models are bench specs (MODELS="spec spec ..."); WARM=1 has each
# model read the instructions and first tools before each run, as the Assistant does while the
# shopper types (that time is recorded apart from the run's). Usage:
#   MODELS="ollama:granite4:7b-a1b-h" scripts/speed-bench.sh OUT
set -uo pipefail
source "$(dirname "$0")/stack.env.sh"
OUT=${1:-$STATE_DIR/bench/speed-$(date +%Y%m%d-%H%M%S)}
RUNTIME=http://127.0.0.1:$PANTRY_PORT/settings/runtime
MODELS=${MODELS:-"ollama:granite4.2:8b#think=false"}

export PANTRY_MCP_TOKEN_FILE=$SECRETS_DIR/pantry_mcp_token_hub CF_JWT_FILE=$SECRETS_DIR/contextforge_jwt
export RECIPE_SHOPPER_SKILL=$PANTRY_API_DIR/skills/recipe-shopper/SKILL.md DEMO_OBSERVER_MODEL=
[ -s "$CF_JWT_FILE" ] || { echo "no ContextForge JWT (scripts/up.sh mints it)" >&2; exit 1; }
was=$(curl -fs "$RUNTIME" | python3 -c 'import json,sys; print(str(json.load(sys.stdin)["demo_mode"]).lower())') \
  || { echo "pantry API is not running (scripts/up.sh)" >&2; exit 1; }
restore() { curl -fs -X POST "$RUNTIME" -H 'Content-Type: application/json' -d "{\"demo_mode\": $was}" >/dev/null; }
trap restore EXIT
curl -fs -X POST "$RUNTIME" -H 'Content-Type: application/json' -d '{"demo_mode": true}' >/dev/null

cd "$HUB_DIR"
for spec in $MODELS; do
  name=$(echo "$spec" | tr ':#=/' '----')
  .venv/bin/python -m demo_hub.bench --repeat "${REPEAT:-1}" --profile full --target gateway-recipes \
    --disclosure progressive --case tomato-penne-no-us --case cheapest-penne --case recipe-list \
    ${WARM:+--warm} --out "$OUT/$name${WARM:+-warm}" --model "$spec" || exit $?
done
echo "done: $OUT"
