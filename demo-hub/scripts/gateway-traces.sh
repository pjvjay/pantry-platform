#!/bin/bash
# Print ContextForge's recent traces: one line per MCP request through the gateway, with the
# tool it invoked and how long the gateway and the tool took. A stand-in for the admin UI's
# Observability tab, whose dashboard does not render in ContextForge 1.0.11 (its Alpine
# component needs eval, which the admin page's CSP and Alpine CSP build do not allow).
#
#   scripts/gateway-traces.sh [MINUTES=30] [LIMIT=200]
#
# Needs OBSERVABILITY_ENABLED=true in contextforge/.env. Mints a 10-minute admin token with
# ContextForge's own tool; the token is never printed.
set -euo pipefail
. "$(dirname "$0")/stack.env.sh"
MINUTES=${1:-30} LIMIT=${2:-200}
CF_HOME=${CONTEXTFORGE_HOME:-$WORKSPACE/contextforge}
TOKEN=$(cd "$CF_HOME" && set -a && . ./.env && set +a && \
  .venv/bin/python -m mcpgateway.utils.create_jwt_token --username "${PLATFORM_ADMIN_EMAIL:-admin@example.com}" \
    --exp 10 --admin --secret "$JWT_SECRET_KEY" 2>/dev/null | tail -1)
CF_TOKEN=$TOKEN CF_URL=http://127.0.0.1:$CF_PORT MINUTES=$MINUTES LIMIT=$LIMIT \
  "$HUB_DIR/.venv/bin/python" - <<'EOF'
import os
from datetime import datetime, timedelta, timezone

import httpx

base, h = os.environ["CF_URL"] + "/observability", {"Authorization": f"Bearer {os.environ['CF_TOKEN']}"}
since = datetime.now(timezone.utc) - timedelta(minutes=int(os.environ["MINUTES"]))
with httpx.Client(headers=h, timeout=30) as c:
    r = c.get(f"{base}/traces", params={"limit": os.environ["LIMIT"]})
    if r.status_code == 404:
        raise SystemExit("ContextForge has no observability API: set OBSERVABILITY_ENABLED=true and restart it")
    r.raise_for_status()
    traces = [t for t in r.json()
              if datetime.fromisoformat(t["start_time"]).replace(tzinfo=timezone.utc) >= since]
    print(f"{len(traces)} gateway request(s) in the last {os.environ['MINUTES']} min (local time):")
    for t in reversed(traces):
        start = datetime.fromisoformat(t["start_time"]).replace(tzinfo=timezone.utc).astimezone()
        spans = c.get(f"{base}/traces/{t['trace_id']}").json().get("spans") or []
        tools = [(s["attributes"].get("tool.name", "?"), s["duration_ms"], s["status"])
                 for s in spans if s["name"] == "tool.invoke"]
        what = ", ".join(f"{name} {ms / 1000:.2f} s{'' if st == 'ok' else ' ' + st.upper()}"
                         for name, ms, st in tools) or "(no tool call: session, tools/list ...)"
        print(f"  {start:%H:%M:%S}  {t['duration_ms'] / 1000:7.2f} s  HTTP {t['http_status_code']}  {what}")
EOF
