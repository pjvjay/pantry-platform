# The demo hub for Render (context: the repo root): the Assistant, Google Calendar sync, address
# search, map tiles and recipe pictures behind the console's /hub routes. Recipe import reads the
# pinned pantry-api's recipe-shopper skill, so it is copied beside the hub.
FROM python:3.12-slim

WORKDIR /srv
COPY demo-hub/pyproject.toml ./demo-hub/
COPY demo-hub/demo_hub/ ./demo-hub/demo_hub/
RUN pip install --no-cache-dir ./demo-hub
COPY pantry-api/skills/recipe-shopper/ ./pantry-api/skills/recipe-shopper/
COPY deploy/urls.sh /urls.sh

# Everything the hub keeps (conversations, the Google token, caches) lives under HOME: a disk.
ENV HOME=/var/data HUB_HOST=0.0.0.0 HUB_PORT=8090 \
    RECIPE_SHOPPER_SKILL=/srv/pantry-api/skills/recipe-shopper/SKILL.md
EXPOSE 8090
CMD ["sh", "-c", ". /urls.sh && mkdir -p \"$HOME\" && exec python -m demo_hub.app"]
