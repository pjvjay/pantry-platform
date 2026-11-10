# Deploying to Render

`render.yaml` deploys the whole app privately, behind a login, plus the free public demo. Every
push to `main` that moves a submodule pin, or changes `deploy/`, `demo-hub/` or `render.yaml`,
redeploys the private stack; changes to docs, `demo/`, `scripts/` or `.github/` do not.

```
browser ─▶ pantry-web (nginx, login) ─┬─ /pantry/      the console (pantry-frontend's build)
                                      ├─ /pantry/api/ ─▶ pantry-app :8000  pantry-api ─▶ pantry-db (Postgres 17)
                                      └─ /hub/        ─▶ pantry-app :8090  the demo hub ─▶ pantry-api (MCP)
                                                                     └──▶ Gemini (plans, matching)

pantry-app is one private service running both pantry-api and the demo hub
(`deploy/app.Dockerfile`, `deploy/run-app.sh`): together they use about 240 MB of a Starter
instance's 512 MB, and the hub's disk rides along. If either process dies the container exits
and Render restarts it.
```

| Service | Type | Plan | Monthly | Notes |
|---|---|---|---|---|
| pantry-web | web | starter | $7 | The only public service. Login (HTTP basic auth) on everything but `/healthz`. |
| pantry-app | private | starter | $7 + 1 GB disk | pantry-api (plans, the wizard, MCP) and the demo hub (Assistant, Google Calendar sync, address search, maps, pictures) in one container. Pre-deploy: pantry-db's migrations and seed, the demo origin evidence, and the matching cache for common ingredient names. |
| pantry-db | Postgres 17 | basic-256mb | $6 | |
| pantry-planner-demo | web | free | $0 | Public demo: one container in demo mode, no hub. |

About $20 a month for the private app (Render's prices on 2026-11-08; disks are $0.25 per GB).
Recipe names the seeds don't know are identified by Gemini Flash-Lite and embedded with
Gemini's embedding API, both on the same key as the planner, cached in Postgres, and kept
within the free tier's daily allowance (900 embedding texts, 200 describer calls a day; see
pantry-api's `docs/meal-planning.md`, "Describing names no seed knows"). No self-hosted model.

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
   | pantry-app | `GEMINI_API_KEY` | your Gemini API key |
   | pantry-app | `MCP_AUTH_TOKENS` | `hub:` followed by the token, e.g. `hub:3f9c…` |
   | pantry-app | `PANTRY_MCP_TOKEN` | the token alone |
   | pantry-app | `HUB_ALLOWED_HOSTS` | pantry-web's address without `https://`, e.g. `pantry-web.onrender.com` |
   | pantry-app | `HUB_OAUTH_ORIGINS` | the same with `https://`, e.g. `https://pantry-web.onrender.com` |

   pantry-web's address appears once the blueprint creates it. Render adds a suffix to every
   resource name of a blueprint (`pantry-web-rxyh`, `pantry-app-rxyh`, `pantry-db-rxyh`), so the
   address is `pantry-web-<suffix>.onrender.com`: enter the two `HUB_` values with that exact
   host (or enter them afterwards on pantry-app's Environment tab, which redeploys it). The
   `APP_HOST` value in `render.yaml` carries pantry-app's suffixed name for the same reason.
3. **Apply.** Render creates the database, builds the two images, runs pantry-app's pre-deploy step
   and starts everything. The pre-deploy step also fills the matching cache with 511 common
   ingredient names, paced to Gemini's free tier (100 embedding texts a minute, so about six
   minutes the first time), which uses most of the first day's allowance of 1,000; a shopper's
   imports that day may fall back to word matching for names not yet cached, and `/pantry/api/health`
   shows the allowance under `matching.cache.budget`. The next deploy carries on where it stopped
   (what is cached costs nothing again).
4. Open `https://<pantry-web address>/pantry/` and sign in with the login from step 2.

## Google Calendar sync (optional)

1. In Google Cloud's OAuth client (the one in `docs/google-calendar.md`), add the redirect URI
   `https://<pantry-web address>/hub/calendar/oauth/callback` and the JavaScript origin
   `https://<pantry-web address>`.
2. In Render, on pantry-app, add a **Secret File** named `google_oauth_client.json` with the
   client JSON you downloaded. The hub reads it at `/etc/secrets/google_oauth_client.json` and keeps
   the refresh token on its disk.

## Day to day

- **Ship a change:** merge it in its own repo, then merge the pin-move PR here. The verify
  workflow checks the new set (pins on `main`, shared seeds, schema parity, hub tests) before you
  merge, and Render deploys it after.
- **Roll back:** revert the pin-move commit, or use **Rollback** on a service in Render.
- **Downtime:** pantry-app has a disk, so a deploy stops the old instance before the new one
  starts: a few seconds for the API and the hub. pantry-web deploys with no downtime.
- **Two ports, one service:** pantry-web reaches the API on `pantry-app:8000` and the hub on
  `pantry-app:8090` over the private network, looking the hostname up again every 10 s
  (`resolver` in `deploy/web/default.conf.template`): a pantry-app redeploy moves it to a new
  address, and nginx would otherwise keep the old one until restarted. Should Render ever route only one port of a private
  service, the fallback is a small nginx inside pantry-app on one port, routing by path.
- **Not kept between deploys:** pantry-api's embedding cache and its Options log
  (`matching/events.jsonl`). Both are rebuilt or restarted on each deploy.
- **The embedder check:** pantry-api uses Gemini's vectors only if they reproduce the seeds'
  (`embedder_matches_seeds` in `/pantry/api/health`). If Google changes the model under the same
  name, recipe names fall back to word matching, and the health check says why.
- **Free-tier allowance:** Gemini counts every text of an embedding request, refused attempts
  included: 100 a minute and 1,000 a day on the free tier, the day resetting at 08:00 UTC
  (midnight Pacific). The matcher paces requests to `MATCHING_EMBED_TEXTS_PER_MINUTE` (100),
  stops calling Gemini once the day's own allowance is spent (`matching.cache.budget` in the
  health check, counted per UTC day) and never retries a refusal that names the day's quota.
  Cached names are never affected. To lift the limit, enable billing on the Gemini key and raise
  `MATCHING_DAILY_EMBED_TEXTS` and `MATCHING_EMBED_TEXTS_PER_MINUTE` (0 = unpaced; Gemini's
  embedding price is $0.15 per million tokens).
- **Demo origin evidence:** `LOAD_DEMO_ORIGINS=1` loads `demo-hub/data`'s label readings on every
  deploy (idempotent), so the origin rules have something to act on. Set it to `0` once real
  evidence is ingested.

## Checked before this was merged

- `render.yaml` validates against Render's schema (`https://render.com/schema/render.yaml.json`).
- Both images were built and run together locally under their Render names and ports, with
  pantry-api, pantry-frontend and pantry-db at the set the pins will move to. The checks:
  - the pre-deploy step runs on a fresh Postgres 17, and again with nothing left to apply;
  - the login works, and `/healthz` stays open;
  - the console, API, hub and wizard work through nginx;
  - the hub's guard still refuses foreign origins and requests without the console header;
- `deploy/migrate.py` builds the same schema and data as pantry-db's own runner.
