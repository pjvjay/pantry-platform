#!/bin/bash
# Stop the services up.sh started (the ones with a pid file); reused services are left alone.
set -uo pipefail
source "$(dirname "$0")/stack.env.sh"
for pidfile in "$STATE_DIR"/pids/*.pid; do
  [ -e "$pidfile" ] || continue
  name=$(basename "$pidfile" .pid) pid=$(cat "$pidfile")
  if kill -0 "$pid" 2>/dev/null; then
    pkill -TERM -P "$pid" 2>/dev/null; kill -TERM "$pid" 2>/dev/null && echo "stopped $name ($pid)"
  fi
  rm -f "$pidfile"
done
