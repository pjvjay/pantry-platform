#!/bin/sh
# Render's pre-deploy step for pantry-api: pantry-db's migrations and seed, then (LOAD_DEMO_ORIGINS=1)
# the demo origin evidence the hub ships, so the origin rules have something to act on. The
# ingest is idempotent: a second run adds no rows.
set -eu
python /migrate.py
if [ "${LOAD_DEMO_ORIGINS:-0}" = "1" ]; then
  python -m pantry_planner.ingest label /demo-data/demo-labels.json
  python -m pantry_planner.ingest origin /demo-data/demo-origins.json
fi
