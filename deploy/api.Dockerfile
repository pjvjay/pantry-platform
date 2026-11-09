# pantry-api for Render, built from this repo's pinned submodules (context: the repo root), with
# pantry-db's migrations and seed beside it: Render's pre-deploy step runs deploy/migrate.py in
# this image, since a Render blueprint has no one-off job to run pantry-db's own image.
FROM python:3.12-slim

WORKDIR /app
COPY pantry-api/pyproject.toml pantry-api/README.md ./
COPY pantry-api/pantry_planner/ ./pantry_planner/
COPY pantry-api/seeds/ ./seeds/
COPY pantry-api/scripts/prefill_matching.py ./scripts/prefill_matching.py
RUN pip install --no-cache-dir -e .

COPY pantry-db/migrations/ /migrations/
COPY pantry-db/seeds/seed.sql /seeds/seed.sql
COPY deploy/migrate.py /migrate.py
COPY deploy/predeploy.sh /predeploy.sh
COPY demo-hub/data/demo-labels.json demo-hub/data/demo-origins.json /demo-data/
COPY deploy/urls.sh /urls.sh

ENV PORT=8000
EXPOSE 8000
# urls.sh turns Render's host:port references into the URLs the app reads (OLLAMA_URL).
CMD ["sh", "-c", ". /urls.sh && exec uvicorn pantry_planner.api:app --host 0.0.0.0 --port ${PORT}"]
