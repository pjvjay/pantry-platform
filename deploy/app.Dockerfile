# pantry-api and the demo hub in one image for Render (context: the repo root), built from this
# repo's pinned submodules. One private service runs both: the API on port 8000 and the hub on
# 8090, over the private network to pantry-web, which routes /pantry/api/ and /hub/ to them.
# deploy/run-app.sh starts and watches the two; if either dies the container exits and Render
# restarts it. pantry-db's migrations and seed ride along for the pre-deploy step
# (deploy/predeploy.sh), since a Render blueprint has no one-off job to run pantry-db's image.
FROM python:3.12-slim

WORKDIR /app
COPY pantry-api/pyproject.toml pantry-api/README.md ./
COPY pantry-api/pantry_planner/ ./pantry_planner/
COPY pantry-api/seeds/ ./seeds/
COPY pantry-api/scripts/prefill_matching.py ./scripts/prefill_matching.py
RUN pip install --no-cache-dir -e .

# The hub (pantry-platform/demo-hub) and the pinned recipe-shopper skill its recipe import reads.
COPY demo-hub/pyproject.toml /srv/demo-hub/
COPY demo-hub/demo_hub/ /srv/demo-hub/demo_hub/
RUN pip install --no-cache-dir /srv/demo-hub
COPY pantry-api/skills/recipe-shopper/ /srv/pantry-api/skills/recipe-shopper/

COPY pantry-db/migrations/ /migrations/
COPY pantry-db/seeds/seed.sql /seeds/seed.sql
COPY deploy/migrate.py /migrate.py
COPY deploy/predeploy.sh /predeploy.sh
COPY demo-hub/data/demo-labels.json demo-hub/data/demo-origins.json /demo-data/
COPY deploy/run-app.sh /run-app.sh

# The hub keeps conversations, the Google Calendar token and its caches under HOME: the disk.
ENV API_PORT=8000 HUB_PORT=8090 HUB_HOST=0.0.0.0 HOME=/var/data \
    PANTRY_API_URL=http://127.0.0.1:8000 \
    RECIPE_SHOPPER_SKILL=/srv/pantry-api/skills/recipe-shopper/SKILL.md
EXPOSE 8000 8090
CMD ["/run-app.sh"]
