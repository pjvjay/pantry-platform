#!/bin/sh
# Start pantry-api and the demo hub in one container and watch both: when either exits, stop the
# other and exit with its status, so Render restarts the container rather than running half of it.
# The hub's data (HOME) is the mounted disk; it is created on a first run without one.
set -u
mkdir -p "$HOME"
uvicorn pantry_planner.api:app --host 0.0.0.0 --port "${API_PORT:-8000}" &
api=$!
python -m demo_hub.app &
hub=$!
stop() { kill -TERM "$api" "$hub" 2>/dev/null; }
trap 'stop; wait; exit 143' TERM INT
# wait for whichever ends first (sh has no `wait -n`): poll both every second
while kill -0 "$api" 2>/dev/null && kill -0 "$hub" 2>/dev/null; do sleep 1; done
if ! kill -0 "$api" 2>/dev/null; then wait "$api"; status=$?; echo "pantry-api exited ($status); stopping the hub" >&2
else wait "$hub"; status=$?; echo "demo hub exited ($status); stopping pantry-api" >&2; fi
stop; wait
exit "$status"
