# Public demo image (Hugging Face Space) — composes the SAME artifacts CI
# publishes for the Kubernetes deployment; there is no separate build path.
# The SPA is lifted out of the frontend image; the API image serves both
# via pantry_planner.demo_server (SPA at /pantry, API at /pantry/api).
#
# DEMO_MODE=1 swaps the two Claude call sites for deterministic stand-ins
# (see pantry-api/pantry_planner/demomode.py) so the public demo needs no
# API key and costs nothing per visitor. Everything else — the staged
# query plan, abort gates, brand stats, split-trip and weekly optimizers —
# runs exactly as deployed.
FROM ghcr.io/pjvjay/pantry-frontend:latest AS spa

FROM ghcr.io/pjvjay/pantry-api:latest

COPY --from=spa /usr/share/nginx/html /app/spa

# HF Spaces runs the container with an arbitrary non-root uid: /tmp is the
# writable surface, so the SQLite DB, Burr trace dir (cwd/.burr), and HOME
# all live there.
ENV DEMO_MODE=1 \
    SPA_DIST=/app/spa \
    DB_URL=sqlite:////tmp/pantry.db \
    HOME=/tmp
WORKDIR /tmp

EXPOSE 7860

CMD ["sh", "-c", "python -m pantry_planner.db seed && exec uvicorn pantry_planner.demo_server:root --host 0.0.0.0 --port 7860"]
