#!/bin/bash
# Reseed pantry's demo database (165 products, 7 recipes) and load the demo origin evidence:
# 36 label readings and 5 database records, including US-origin items, a conflicting product
# (garlic: China vs Mexico) and an importer-only label that must never count as an origin.
# Clears the review queue. Safe to run while the API is up (SQLite, one writer).
set -euo pipefail
source "$(dirname "$0")/stack.env.sh"
cd "$PANTRY_API_DIR"
export DB_URL=$DEMO_DB_URL
.venv/bin/python -m pantry_planner.db seed
.venv/bin/python -m pantry_planner.ingest label "$HUB_DIR/data/demo-labels.json"
.venv/bin/python -m pantry_planner.ingest origin "$HUB_DIR/data/demo-origins.json"
echo "demo data reset in $DEMO_DB_URL"
