#!/bin/bash
# Start the whole demo stack and print the one URL to open. Idempotent: a service that already
# answers is reused, so running it twice is harmless. Logs: ~/.pantry-demo/logs.
#
#   pantry API + MCP  :8000  (pantry-platform/pantry-api, Gemini or demo mode, bearer auth on /mcp)
#   ContextForge      :4444  (pantry-gateway; virtual servers pantry-sim and pantry-recipes)
#   fetch MCP server  :9100  (pantry-gateway)
#   mcp-sim runner    :8765  (mcp-sim, with the local Gemini skill)
#   demo hub          :8090  (this directory; serves the app at /pantry/)
#
# Options: --rebuild  rebuild the frontend even if dist/ is current
#          --reset    reseed the demo database and evidence before starting
set -euo pipefail
source "$(dirname "$0")/stack.env.sh"
REBUILD=0 RESET=0
for arg in "$@"; do
  case $arg in --rebuild) REBUILD=1 ;; --reset) RESET=1 ;; *) echo "unknown option $arg" >&2; exit 2 ;; esac
done

echo "== prerequisites"
for venv in "$PANTRY_API_DIR/.venv" "$MCPSIM_DIR/.venv" "$HUB_DIR/.venv" "$WORKSPACE/contextforge/.venv"; do
  [ -x "$venv/bin/python" ] || { echo "  FAIL  missing $venv (see MOVE-TO-NEW-COMPUTER.md and demo-hub/README.md)" >&2; exit 1; }
done
GEMINI=$(gemini_key || true)
[ -n "$GEMINI" ] && echo "  ok    Gemini key (from $MCPSIM_DIR/.env)" || echo "  warn  no GEMINI_API_KEY: the planner starts in demo mode and the Assistant cannot run"
[ -n "$GEMINI" ] || START_DEMO_MODE=1

echo "== secrets ($SECRETS_DIR)"
mkdir -m 700 -p "$SECRETS_DIR"
for name in pantry_mcp_token pantry_mcp_token_hub; do
  if [ ! -s "$SECRETS_DIR/$name" ]; then
    python3 -c "import secrets; print(secrets.token_urlsafe(32))" >"$SECRETS_DIR/$name"
    chmod 600 "$SECRETS_DIR/$name"
    echo "  made  $name"
  fi
done
echo "  ok    pantry tokens (labels: contextforge, demo-hub)"

echo "== frontend"
DIST=$FRONTEND_DIR/dist
if [ "$REBUILD" = 1 ] || [ ! -f "$DIST/index.html" ] || [ -n "$(find "$FRONTEND_DIR/src" -newer "$DIST/index.html" -print -quit)" ]; then
  (cd "$FRONTEND_DIR" && { [ -d node_modules ] || npm ci --no-audit --no-fund; } && npm run build) >"$STATE_DIR/logs/frontend-build.log" 2>&1 \
    || { echo "  FAIL  frontend build (see $STATE_DIR/logs/frontend-build.log)" >&2; exit 1; }
  echo "  ok    built $DIST"
else
  echo "  ok    $DIST is current"
fi

echo "== pantry API :$PANTRY_PORT"
DB_FILE=${DEMO_DB_URL#sqlite:///}
if [ "$RESET" = 1 ] || { [[ $DEMO_DB_URL == sqlite:* ]] && [ ! -f "$DB_FILE" ]; }; then
  "$HUB_DIR/scripts/reset-demo-data.sh" >"$STATE_DIR/logs/reset.log" 2>&1 && echo "  ok    demo data seeded"
fi
if healthy "http://127.0.0.1:$PANTRY_PORT/health"; then
  echo "  ok    already running (reused; stop it to apply new settings)"
else
  (
    cd "$PANTRY_API_DIR"
    export DB_URL=$DEMO_DB_URL GEMINI_API_KEY=$GEMINI RUNTIME_SETTINGS_ENABLED=1 \
      MCP_AUTH_TOKENS="contextforge:$(cat "$SECRETS_DIR/pantry_mcp_token"),demo-hub:$(cat "$SECRETS_DIR/pantry_mcp_token_hub")" \
      NL2SQL_MODEL=$PLANNER_NL2SQL_MODEL SELECTOR_MODEL_DEFAULT=$PLANNER_SELECTOR_MODEL \
      SELECTOR_MODEL_ESCALATION=$PLANNER_ESCALATION_MODEL CLASSIFIER_MODEL=$PLANNER_CLASSIFIER_MODEL
    if [ "$START_DEMO_MODE" = 1 ]; then export DEMO_MODE=1; else unset DEMO_MODE; fi
    start_bg pantry-api .venv/bin/uvicorn pantry_planner.api:app --host 127.0.0.1 --port "$PANTRY_PORT"
  )
  wait_for "pantry API" "http://127.0.0.1:$PANTRY_PORT/health" 60
fi

echo "== Burr UI :$BURR_PORT (pantry's plan traces)"
if healthy "http://127.0.0.1:$BURR_PORT/"; then
  echo "  ok    already running (reused)"
else
  (
    cd "$PANTRY_API_DIR"
    export burr_path=$PANTRY_API_DIR/.burr   # where pantry's tracker writes (tracing.py)
    start_bg burr-ui .venv/bin/burr --no-open --no-copy-demo_data --host 127.0.0.1 --port "$BURR_PORT"
  )
  wait_for "Burr UI" "http://127.0.0.1:$BURR_PORT/" 60
fi

echo "== ContextForge :$CF_PORT and fetch :$FETCH_PORT"
if healthy "http://127.0.0.1:$CF_PORT/health"; then
  echo "  ok    ContextForge already running (reused)"
else
  start_bg contextforge "$GATEWAY_DIR/scripts/run.sh"
  wait_for "ContextForge" "http://127.0.0.1:$CF_PORT/health" 120
fi
if [ ! -s "$SECRETS_DIR/contextforge_jwt" ]; then
  "$GATEWAY_DIR/scripts/mint_jwt.sh" >"$SECRETS_DIR/contextforge_jwt" && chmod 600 "$SECRETS_DIR/contextforge_jwt"
  echo "  made  ContextForge JWT"
fi
if healthy "http://127.0.0.1:$FETCH_PORT/healthz"; then
  echo "  ok    fetch already running (reused)"
else
  start_bg fetch "$GATEWAY_DIR/scripts/run_fetch.sh"
  wait_for "fetch" "http://127.0.0.1:$FETCH_PORT/healthz" 180
fi
(
  cd "$GATEWAY_DIR"
  export CF_JWT_FILE=$SECRETS_DIR/contextforge_jwt PANTRY_MCP_TOKEN=$(cat "$SECRETS_DIR/pantry_mcp_token")
  scripts/register_pantry.sh && REFRESH_PANTRY=true scripts/register_fetch.sh
) >"$STATE_DIR/logs/register.log" 2>&1 && echo "  ok    virtual servers pantry-sim and pantry-recipes registered" \
  || { echo "  FAIL  gateway registration (see $STATE_DIR/logs/register.log)" >&2; exit 1; }

echo "== mcp-sim runner :$RUNNER_PORT"
(cd "$MCPSIM_DIR" && CF_JWT_FILE=$SECRETS_DIR/contextforge_jwt skills/simulate/scripts/run.sh scenarios) \
  >"$STATE_DIR/logs/scenarios.log" 2>&1 && echo "  ok    gateway scenarios generated" || echo "  warn  scenario generation failed (see $STATE_DIR/logs/scenarios.log)"
if healthy "http://127.0.0.1:$RUNNER_PORT/api/config"; then
  echo "  ok    runner already running (reused)"
else
  # Local models: a 16k window holds the agent's whole prompt (Ollama drops the start otherwise).
  (cd "$MCPSIM_DIR" && export CF_JWT_FILE=$SECRETS_DIR/contextforge_jwt MCPSIM_OLLAMA_NUM_CTX=16384 \
    && start_bg mcp-sim skills/simulate/scripts/run.sh ui --skill "$MCPSIM_SKILL_DIR" --port "$RUNNER_PORT")
  wait_for "mcp-sim runner" "http://127.0.0.1:$RUNNER_PORT/api/config" 60
fi

echo "== demo hub :$HUB_PORT"
if [ -f "$STATE_DIR/pids/hub.pid" ] && kill -0 "$(cat "$STATE_DIR/pids/hub.pid")" 2>/dev/null; then
  kill "$(cat "$STATE_DIR/pids/hub.pid")" && sleep 1   # always restart: it is cheap and picks up new settings
fi
(
  cd "$HUB_DIR"
  export GEMINI_API_KEY=$GEMINI PANTRY_MCP_TOKEN_FILE=$SECRETS_DIR/pantry_mcp_token_hub \
    PANTRY_MCP_TOKEN_LABEL=demo-hub CF_JWT_FILE=$SECRETS_DIR/contextforge_jwt SPA_DIST=$DIST \
    RECIPE_SHOPPER_SKILL=$PANTRY_API_DIR/skills/recipe-shopper/SKILL.md \
    DEMO_RESET_SCRIPT=$HUB_DIR/scripts/reset-demo-data.sh HUB_PORT=$HUB_PORT \
    PANTRY_API_URL=http://127.0.0.1:$PANTRY_PORT CONTEXTFORGE_URL=http://127.0.0.1:$CF_PORT \
    FETCH_URL=http://127.0.0.1:$FETCH_PORT MCPSIM_UI_URL=http://127.0.0.1:$RUNNER_PORT \
    DEMO_AGENT_MODEL=$AGENT_MODEL DEMO_OBSERVER_MODEL=$OBSERVER_MODEL
  start_bg hub .venv/bin/python -m demo_hub.app
)
wait_for "demo hub" "http://127.0.0.1:$HUB_PORT/hub/mcp/targets" 30
healthy "http://127.0.0.1:11434/api/tags" && echo "  ok    Ollama (local models)" || echo "  info  Ollama not running (only needed for the local-model options)"

echo
echo "Open http://127.0.0.1:$HUB_PORT/pantry/"
