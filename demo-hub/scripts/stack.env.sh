# Shared settings for the demo stack scripts (sourced, not run). Override any of these in the
# environment. Paths assume the workspace layout from MOVE-TO-NEW-COMPUTER.md:
#   $WORKSPACE/{pantry-platform,mcp-sim,pantry-gateway,contextforge}
HUB_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PLATFORM_DIR=$(cd "$HUB_DIR/.." && pwd)
WORKSPACE=${WORKSPACE:-$(cd "$PLATFORM_DIR/.." && pwd)}
PANTRY_API_DIR=${PANTRY_API_DIR:-$PLATFORM_DIR/pantry-api}
FRONTEND_DIR=${FRONTEND_DIR:-$PLATFORM_DIR/pantry-frontend}
MCPSIM_DIR=${MCPSIM_DIR:-$WORKSPACE/mcp-sim}
GATEWAY_DIR=${GATEWAY_DIR:-$WORKSPACE/pantry-gateway}
MCPSIM_SKILL_DIR=${MCPSIM_SKILL_DIR:-$WORKSPACE/mcp-sim-local/simulate}

SECRETS_DIR=${SECRETS_DIR:-$HOME/.pantry-secrets}
STATE_DIR=${STATE_DIR:-$HOME/.pantry-demo}
DATA_DIR=${DATA_DIR:-$HOME/.pantry-data}
DEMO_DB_URL=${DEMO_DB_URL:-sqlite:///$DATA_DIR/pantry_demo.db}

PANTRY_PORT=${PANTRY_PORT:-8000}
CF_PORT=${CF_PORT:-4444}
FETCH_PORT=${FETCH_PORT:-9100}
RUNNER_PORT=${RUNNER_PORT:-8765}
HUB_PORT=${HUB_PORT:-8090}
BURR_PORT=${BURR_PORT:-7241}   # Burr UI: pantry's plan calls, one run each

# The planner's LLM when demo mode is off (switchable at runtime on the System tab). Gemini's free
# tier allows about 20 tool-calling requests per model per day, so the planner uses the Flash-Lite
# models and leaves the Flash models to the Assistant (gemini-3-flash-preview, then fallbacks).
PLANNER_NL2SQL_MODEL=${PLANNER_NL2SQL_MODEL:-gemini:gemini-flash-lite-latest}
PLANNER_SELECTOR_MODEL=${PLANNER_SELECTOR_MODEL:-gemini:gemini-3.1-flash-lite}
PLANNER_ESCALATION_MODEL=${PLANNER_ESCALATION_MODEL:-gemini:gemini-flash-lite-latest}
PLANNER_CLASSIFIER_MODEL=${PLANNER_CLASSIFIER_MODEL:-gemini:gemini-3.1-flash-lite}
# 1 = start in demo mode (deterministic, no LLM calls); 0 = start on Gemini.
START_DEMO_MODE=${START_DEMO_MODE:-0}
# The Assistant's default model, and the model that judges the observers' plain-English
# conditions ("" = none: only the code observers act). Local by default: the Assistant starts on
# Granite in Ollama and the observers call no model; a cloud model runs only when chosen.
AGENT_MODEL=${AGENT_MODEL:-ollama:granite4.2:8b#think=false}
OBSERVER_MODEL=${OBSERVER_MODEL-}

mkdir -p "$STATE_DIR/logs" "$STATE_DIR/pids" "$DATA_DIR"

# The Gemini key, read from mcp-sim/.env without echoing it and without importing anything else.
gemini_key() {
  if [ -n "${GEMINI_API_KEY:-}" ]; then printf '%s' "$GEMINI_API_KEY"; return; fi
  [ -f "$MCPSIM_DIR/.env" ] && sed -n 's/^GEMINI_API_KEY=//p' "$MCPSIM_DIR/.env" | head -1
}

healthy() { curl -fs -o /dev/null --max-time 3 "$1"; }

wait_for() {  # wait_for <label> <url> [seconds]
  local label=$1 url=$2 limit=${3:-60} i
  for ((i = 0; i < limit; i++)); do
    healthy "$url" && { echo "  ok    $label"; return 0; }
    sleep 1
  done
  echo "  FAIL  $label did not answer at $url within ${limit}s (see $STATE_DIR/logs)" >&2
  return 1
}

start_bg() {  # start_bg <name> <command...>: run detached, log to $STATE_DIR/logs/<name>.log
  local name=$1; shift
  nohup "$@" >"$STATE_DIR/logs/$name.log" 2>&1 &
  echo $! >"$STATE_DIR/pids/$name.pid"
}

# A checkout's release as git describe reads it against vX.Y.Z tags, without the v: 0.2.0, or
# 0.2.0-3-gabc1234 three commits past it (RELEASING.md). Prints nothing, and still succeeds, when
# the checkout has no such tag, so callers under set -e need no guard.
release_of() {
  local described
  described=$(git -C "$1" describe --tags --dirty --match 'v[0-9]*.[0-9]*.[0-9]*' 2>/dev/null) || return 0
  printf '%s\n' "${described#v}"
}

# Fetch a checkout's tags so release_of names the latest release. Best effort and never
# interactive: offline, or with no credentials to hand, the tags already fetched are used.
fetch_tags() {
  GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=5" \
    git -C "$1" fetch --quiet --tags >/dev/null 2>&1 || true
}
