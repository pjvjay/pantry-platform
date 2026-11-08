# Deploying to Render

`render.yaml` deploys the whole app privately, behind a login, plus the free public demo. Every
push to `main` that moves a submodule pin, or changes `deploy/`, `demo-hub/` or `render.yaml`,
redeploys the private stack; changes to docs, `demo/`, `scripts/` or `.github/` do not.

```
browser ─▶ pantry-web (nginx, login) ─┬─ /pantry/      the console (pantry-frontend's build)
                                      ├─ /pantry/api/ ─▶ pantry-api ─▶ pantry-db (Postgres 17)
                                      │                      └──────▶ pantry-embedder (Ollama)
                                      └─ /hub/        ─▶ pantry-hub  ─▶ pantry-api (MCP)
```

| Service | Type | Plan | Monthly | Notes |
|---|---|---|---|---|
| pantry-web | web | starter | $7 | The only public service. Login (HTTP basic auth) on everything but `/healthz`. |
| pantry-api | private | starter | $7 | Pre-deploy: pantry-db's migrations and seed, then the demo origin evidence. |
| pantry-hub | private | starter | $7 + 1 GB disk | Assistant, Google Calendar sync, address search, maps, pictures. |
| pantry-embedder | private | standard | $25 + 2 GB disk | Ollama with EmbeddingGemma, for recipe names the seeds don't know. |
| pantry-db | Postgres 17 | basic-256mb | $6 | |
| pantry-planner-demo | web | free | $0 | Public demo: one container in demo mode, no hub. |

About $53 a month for the private app (Render's prices on 2026-11-08; disks are $0.25 per GB).
Leaving out the embedder saves $25: recipe names the seeds don't know are then matched by words.

## Before the first deploy

1. **The pinned set must be current.** Render builds from this repo's submodule pins, so the pins
   must point at the commits you want live. Merge pantry-db's meal-planning PR first (its seeds
   must match pantry-api's, or `shared-seed` fails), then merge a PR that moves the pantry-api,
   pantry-frontend and pantry-db pins to their merged commits.
2. **A token for the hub to call pantry-api.** Make one random value and keep it for step 3:

   ```bash
   python3 -c "import secrets; print(secrets.token_hex(24))"
   ```

## First deploy

1. In Render, connect GitHub (the `pjvjay` account), then **New → Blueprint** and choose
   `pjvjay/pantry-platform`, branch `main`. Render reads `render.yaml` and lists the services.
2. Render asks for every value marked `sync: false`:

   | Service | Key | Value |
   |---|---|---|
   | pantry-web | `BASIC_AUTH_USER` | the login name you want |
   | pantry-web | `BASIC_AUTH_PASSWORD` | a long password: the web service will not start without it |
   | pantry-api | `GEMINI_API_KEY` | your Gemini API key |
   | pantry-api | `MCP_AUTH_TOKENS` | `hub:` followed by the token, e.g. `hub:3f9c…` |
   | pantry-hub | `GEMINI_API_KEY` | the same Gemini key |
   | pantry-hub | `PANTRY_MCP_TOKEN` | the token alone |
   | pantry-hub | `HUB_ALLOWED_HOSTS` | pantry-web's address without `https://`, e.g. `pantry-web.onrender.com` |
   | pantry-hub | `HUB_OAUTH_ORIGINS` | the same with `https://`, e.g. `https://pantry-web.onrender.com` |

   pantry-web's address appears once the blueprint creates it. If it is not
   `pantry-web.onrender.com`, update the two hub values to match and redeploy pantry-hub.
3. **Apply.** Render creates the database, builds the images, runs pantry-api's pre-deploy step
   and starts everything. The embedder downloads its model on the first start (about 600 MB) and
   keeps it on its disk; until it has, `/pantry/api/health` says `unknown_names: "words"`.
4. Open `https://<pantry-web address>/pantry/` and sign in with the login from step 2.

## Google Calendar sync (optional)

1. In Google Cloud's OAuth client (the one in `docs/google-calendar.md`), add the redirect URI
   `https://<pantry-web address>/hub/calendar/oauth/callback` and the JavaScript origin
   `https://<pantry-web address>`.
2. In Render, on pantry-hub, add a **Secret File** named `google_oauth_client.json` with the
   client JSON you downloaded. The hub reads it at `/etc/secrets/google_oauth_client.json` and keeps
   the refresh token on its disk.

## Day to day

- **Ship a change:** merge it in its own repo, then merge the pin-move PR here. The verify
  workflow checks the new set (pins on `main`, shared seeds, schema parity, hub tests) before you
  merge, and Render deploys it after.
- **Roll back:** revert the pin-move commit, or use **Rollback** on a service in Render.
- **Downtime:** pantry-hub and pantry-embedder have disks, so a deploy stops the old instance before
  the new one starts: a few seconds. pantry-web and pantry-api deploy with no downtime.
- **Not kept between deploys:** pantry-api's embedding cache and its Options log
  (`matching/events.jsonl`). Both are rebuilt or restarted on each deploy.
- **The embedder check:** pantry-api uses the embedder only if it reproduces the seeds' vectors
  (`embedder_matches_seeds` in `/pantry/api/health`). If Render's EmbeddingGemma build differs
  from the one the seeds were made with, recipe names fall back to word matching, and the health
  check says why.
- **Demo origin evidence:** `LOAD_DEMO_ORIGINS=1` loads `demo-hub/data`'s label readings on every
  deploy (idempotent), so the origin rules have something to act on. Set it to `0` once real
  evidence is ingested.

## Checked before this was merged

- `render.yaml` validates against Render's schema (`https://render.com/schema/render.yaml.json`).
- All three images were built and run together locally under their Render names and ports, with
  pantry-api, pantry-frontend and pantry-db at the set the pins will move to. The checks:
  - the pre-deploy step runs on a fresh Postgres 17, and again with nothing left to apply;
  - the login works, and `/healthz` stays open;
  - the console, API, hub and wizard work through nginx;
  - the hub's guard still refuses foreign origins and requests without the console header;
  - the embedder keeps serving while its model download retries.
- `deploy/migrate.py` builds the same schema and data as pantry-db's own runner.
