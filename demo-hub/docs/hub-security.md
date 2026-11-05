# The hub's guard

The demo hub runs on your machine (`127.0.0.1:8090`) and holds every secret the demo uses: the
pantry bearer token labelled `demo-hub`, the ContextForge JWT and the Gemini key. The browser only
ever talks to the hub. That makes the hub worth protecting from the *other* pages your browser has
open, which can also send requests to `127.0.0.1`. `demo_hub/guard.py` is middleware that checks
every request before any route sees it.

## What it checks

| Requests | Check | Refused with |
|---|---|---|
| Every request, reads included | `Host` is the hub's own: `127.0.0.1:PORT`, `localhost:PORT` or `[::1]:PORT`, or one listed in `HUB_ALLOWED_HOSTS` | 403 |
| Every non-GET `/hub/*` and `/pantry/api/*` request | `Origin`, when the browser sends one, is one of those hosts with `http://` or `https://` | 403 |
| the same | `X-Pantry-Console: 1` | 403 |
| the same | `Content-Type: application/json` (parameters such as `charset` are fine) | 415 |

A refusal is JSON, `{"reason": "..."}`, saying which check failed. GET and HEAD need only the
Host check; everything else under `/hub/` and `/pantry/api/` needs all four. That includes
`DELETE`, `PUT` and `PATCH` through the pantry proxy, and requests with no body (send the JSON
content type anyway). The hub answers no CORS preflight: an `OPTIONS` request has no console
header, so it is refused like any other.

### Why each check

- **Host stops DNS rebinding.** A page on `attacker.test` can point its own name at 127.0.0.1.
  The browser then treats the hub as that page's own origin and lets it read the hub's answers:
  traces, status, conversations, plans. The request still carries `Host: attacker.test:8090`,
  so the Host allowlist refuses it. That is also why reads are checked, and not only writes.
- **The console header stops cross-site writes.** Without rebinding, a page on another site can
  still *send* a request to 127.0.0.1. It cannot read the answer, but a form post or a
  `text/plain` fetch would still start a chat turn (which spends Gemini quota), reset the demo
  data or call a write tool. Browsers send those "simple" requests without asking. A custom
  header such as `X-Pantry-Console`, or the `application/json` content type, makes the browser
  ask the server first with a preflight, and the hub never says yes. So only a page served by
  the hub itself, or a program that is not a browser, can send them.
- **Origin is a second check on the same thing.** Browsers add `Origin` to a non-GET request,
  and a page cannot change it. When it is present it must be the console's. When it is absent,
  the request is from a program, which still has to send the header.

### The telemetry beacon

When the page is hidden, the console sends its last measurements with `navigator.sendBeacon`,
which cannot set a header. `POST /hub/telemetry` is the only route that accepts a request
without `X-Pantry-Console`, and only when its `Origin` is the console's own and its body is JSON.
Another site cannot forge the `Origin`. Its regular reports send the header like every other
request.

## The console and scripts

- **The console** (`pantry-frontend`, `src/consoleRequest.ts`) adds `X-Pantry-Console: 1` and
  the JSON content type to every non-GET request it makes, through `api.ts`, `hub.ts` and
  `telemetry.ts`. pantry-api ignores the header.
- **Scripts** that change something through the hub send the same two headers:

  ```bash
  curl -s -X POST http://127.0.0.1:8090/hub/demo/reset \
    -H 'X-Pantry-Console: 1' -H 'Content-Type: application/json'
  curl -s -X POST http://127.0.0.1:8090/hub/agent/chat \
    -H 'X-Pantry-Console: 1' -H 'Content-Type: application/json' \
    -d '{"message": "spaghetti bolognese for 4", "target": "pantry"}'
  ```

  Reads (`scripts/status.sh`, up.sh's health checks) only need the hub's own address. When the
  guard landed, no repo script posted to the hub. `bench.py` runs the agent in process, the
  bench scripts switch demo mode on pantry-api's own port, and `sims.py` and `speed.py` talk to
  the mcp-sim runner and Ollama. A new script that posts to the hub needs the headers above.
- **Tests** use `tests/conftest.py`'s `console_client`, a TestClient on `127.0.0.1:8090` that
  sends both headers. `tests/test_guard.py` lists every non-GET route (`GUARDED`). For each one
  it checks a foreign Host, a rebound Host, a foreign Origin, a missing header and a non-JSON
  body, and that the console through Vite's proxy gets through. A route added to the app but
  not to that list fails `test_every_route_that_changes_something_is_listed`.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `HUB_PORT` | `8090` | The port the hub listens on. Its loopback names on this port are always allowed. |
| `HUB_ALLOWED_HOSTS` | `localhost:5173` | Other `host:port` values to answer to, comma-separated. The default is Vite's dev server: its `/hub` proxy keeps the browser's `Host`. |
| `HUB_HOST` | `127.0.0.1` | The address the hub binds to. If you bind another interface (`0.0.0.0`), add the name you reach it by to `HUB_ALLOWED_HOSTS`. The guard is no substitute for a login: anyone who can reach the hub and send the header can use it. |

Setting `HUB_ALLOWED_HOSTS` replaces the default, so keep `localhost:5173` in the list if you
still use `npm run dev`.

## What it does not cover

- **Other programs on your machine.** Any local process can send the header. The hub trusts
  your own user account, as it always has.
- **Reads by a program.** A local program can read traces and status. Nothing secret is in
  them: keys and tokens never leave the hub process.
- **The calendar sync.** Its routes (`/hub/calendar/*`) are guarded like every other; the
  sign-in adds a PKCE verifier and a `state` bound to an HttpOnly cookie on top, and the access
  log masks OAuth values ([google-calendar.md](google-calendar.md)).
