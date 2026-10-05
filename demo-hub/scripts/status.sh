#!/bin/bash
# One line per service, from the hub's /hub/status.
source "$(dirname "$0")/stack.env.sh"
body=$(curl -fs --max-time 10 "http://127.0.0.1:$HUB_PORT/hub/status") \
  || { echo "the demo hub is not answering on :$HUB_PORT (run scripts/up.sh)"; exit 1; }
printf '%s' "$body" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for s in d["services"]:
    state = "ok  " if s["ok"] else "DOWN"
    print(state, s["label"].ljust(30), s["url"])
print("keys:", ", ".join("%s=%s" % kv for kv in d["keys"].items()))
'
