#!/bin/sh
# Render's pre-deploy step for pantry-api: pantry-db's migrations and seed, then (LOAD_DEMO_ORIGINS=1)
# the demo origin evidence the hub ships, so the origin rules have something to act on, then
# (PREFILL_MATCHING=1) the matching cache for common ingredient names, within the day's Gemini
# allowance. Both are idempotent: a second run adds no rows and makes no model call for what is
# cached; a prefill that stops at the allowance is not a failed deploy.
set -eu
python /migrate.py
if [ "${LOAD_DEMO_ORIGINS:-0}" = "1" ]; then
  python -m pantry_planner.ingest label /demo-data/demo-labels.json
  python -m pantry_planner.ingest origin /demo-data/demo-origins.json
fi
if [ "${PREFILL_MATCHING:-0}" = "1" ]; then
  python /app/scripts/prefill_matching.py || echo "prefill stopped at today's allowance; the next deploy carries on"
fi
