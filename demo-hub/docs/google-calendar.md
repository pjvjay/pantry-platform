# Google Calendar sync (optional, local hub only)

The Meal plan's **Add to calendar** dialog always offers the `.ics` download and one Google
add-event link per event, with no account. With this setup it also offers **Google Calendar**:
the hub keeps a calendar named **Pantry plan** in your Google account in step with the approved
plan. Every change is shown first as a list ("Add 9, change 2, remove 1 in Pantry plan") and
nothing is written until you click it.

- **One way, one calendar.** The hub writes only to the Pantry plan calendar it created, and only
  to events it created there. It cannot see your other calendars (the scope below).
- **What the shopper reviews is what gets written.** pantry-api builds every event
  (`POST /calendar/preview`, the same builder as the `.ics`); the hub previews the difference
  with what is in Google, and applies that exact difference or nothing (`preview_stale`).
- **Your edits in Google win unless you say otherwise.** An event you edited in Google is shown
  as "Edited in Google" with **Keep** chosen. One you deleted there is shown as "Deleted in
  Google" and comes back only if you tick **Restore**.
- **No invitations, no attendees.** Every write sends `sendUpdates=none`, and no event has
  attendees.
- **Every write is a click.** The Assistant has no calendar tool and never sees calendar data.

This works on the local stack only (`scripts/up.sh`, the hub at `127.0.0.1:8090`). The AKS
console and the HF Space have no hub, so the dialog shows the download and links only.

## One-time setup (done by you, in your own Google Cloud project)

Claude cannot create Google accounts, projects or keys; these steps are yours. They are already
done on this machine if `~/.pantry-secrets/google_oauth_client.json` exists.

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project (or pick
   one) and enable the **Google Calendar API** (APIs & Services, Library).
2. Configure the consent screen (Google Auth Platform):
   - **Audience:** External, publishing status **Testing**, and add your own Google account
     under **Test users**. Only test users can connect.
   - **Data access:** add the scope `https://www.googleapis.com/auth/calendar.app.created`
     ("Make secondary Google calendars, and see, create, change, and delete events on them").
     Add no other scope.
3. Create an OAuth client (Clients, Create client), type **Web application**, with these
   **Authorized redirect URIs**, exactly:
   - `http://127.0.0.1:8090/hub/calendar/oauth/callback` (the console served by the hub)
   - `http://localhost:5173/hub/calendar/oauth/callback` (only if you use `npm run dev`)
4. Download the client JSON and save it, private to you:

   ```bash
   mkdir -m 700 -p ~/.pantry-secrets
   mv ~/Downloads/client_secret_*.json ~/.pantry-secrets/google_oauth_client.json
   chmod 600 ~/.pantry-secrets/google_oauth_client.json
   ```

5. Run `scripts/up.sh`. It prints `ok    Google OAuth client: calendar sync is set up` (it
   never prints the file) and restarts the hub with the paths below.

A **Desktop app** client (`"installed"` in the JSON) also works, on loopback addresses; the Web
client is the documented default because its exact redirect URIs are what the hub checks.

## Connecting, syncing, disconnecting

Open the console at **http://127.0.0.1:8090/pantry/** (or `http://localhost:5173/pantry/` under
`npm run dev`). Other addresses are refused with `origin_not_registered`, because Google sends
you back to the address you connected from and only registered addresses are allowed.

1. **Meal plan**, approve a trip or place meals, **Add to calendar**. The Google Calendar
   section appears when the hub has a client.
2. **Connect Google Calendar** takes you to Google. Sign in as a test user. While the app is in
   Testing, Google shows "Google hasn't verified this app": choose **Continue**. Leave the
   calendar permission ticked. Google sends you back to the Meal plan with the dialog open and
   "Connected".
3. **Review changes** lists what a sync would do, grouped: Add, Change, Remove, Edited in Google
   (Keep or Overwrite, Keep chosen), Deleted in Google (Restore?), and Past (left as it is). The
   button says exactly what will happen, for example "Create the Pantry plan calendar and add 5
   events". Each row then shows its result; **Retry failed** reviews again and applies again.
4. **Disconnect** revokes the connection at Google and deletes the token file (even when Google
   does not answer). Tick "Also delete the Pantry plan calendar" to remove the calendar and its
   events from your account too; otherwise it stays, and a later connection starts a new one.

### The weekly reconnect

While the consent screen is in **Testing**, Google ends a refresh token **7 days** after it was
issued. The dialog shows "reconnect by <date>", and after that the next review says the
connection ended: click **Connect Google Calendar** again. The hub keeps the same Pantry plan
calendar when the account can still open it. Moving the app to "In production" would remove the
limit but needs Google's verification for a calendar scope; that is not planned.

### Revoking access yourself

Besides **Disconnect**, you can remove the app at any time in your Google Account: Security, then
"Your connections to third-party apps & services" (myaccount.google.com/connections). The hub
then sees `invalid_grant` on its next call and asks you to connect again. To forget the
connection on this machine as well, delete `~/.pantry-secrets/google_calendar_token.json`.

## What the scope can reach

`calendar.app.created` lets the hub create secondary calendars and read, create, change and
delete events **on calendars it created**. It cannot list or read your primary calendar or any
other calendar, and it asks for nothing else (no email, no profile). Within the Pantry plan
calendar the hub further limits itself to events carrying its private properties for the plan
being synced (`privateExtendedProperty=pantry_schedule=<plan>`); anything you add there yourself
is never read or touched.

## Files

| File | Written by | Holds | Mode |
|---|---|---|---|
| `~/.pantry-secrets/google_oauth_client.json` | you (step 4) | the OAuth client id and secret | 600 (up.sh re-applies it) |
| `~/.pantry-secrets/google_calendar_token.json` | the hub, on connect | `refresh_token`, `scope`, `connection_id`, `connected_at`, `calendar_id` | 600, in a 700 folder; written to a temp file opened `O_EXCL` 0600, fsynced, then `os.replace`d. A file others can read is refused, not used. |
| `~/.pantry-demo/calendar/ledger.json` | the hub, after each write | per plan and item: event id, gen, etag, content hash; the calendar id; the last sync | 600, in a 700 folder; no secrets |

The access token lives in the hub's memory only. The ledger is a cache: lose it and the next
review finds the same calendar (its id is also in the token file) and the same events (by their
private properties), with nothing duplicated.

## How a sync decides

Each event's Google id is `pp1` + the first 40 hex digits of `sha256("<plan>/<item>/<gen>")`
(Google ids are base32hex, 5-1024 characters), so the same planned item always lands on the same
event and a repeated or interrupted apply cannot duplicate it. `gen` starts at 0 and goes up only
when Google refuses to restore an event that was deleted there.

The event is all-day (`start.date`/`end.date`, the end exclusive, as in the `.ics`), marked free,
with pantry-api's title, description and location word for word, no reminders (the `.ics` has
none either), and private properties `pantry_v`, `pantry_schedule`, `pantry_item`, `pantry_kind`,
`pantry_rev`, `pantry_gen` and `pantry_hash` (a hash of the title, description, location, dates
and free/busy). **The Google event body is built in the hub** (`gcal_sync.google_body`) from the
events pantry-api's `/calendar/preview` returns: pantry-api stays the one builder of what an event
says, and the hub adds only the sync identity, which depends on its ledger.

| Operation | When | What apply does |
|---|---|---|
| Add (`create`) | not in Google | insert under the item's id; if the id is taken (an earlier run, or an event the hub deleted), update that event; if Google refuses, insert under the next gen |
| Unchanged (`noop`) | as planned | nothing (the ledger learns the event if it did not know it) |
| Change (`update`) | changed in the plan, untouched in Google since the hub wrote it | update with `If-Match: <etag>` |
| Edited in Google (`conflict`) | its etag is not the one the hub wrote, and its text differs from the plan | **Keep** (default): nothing; **Overwrite**: update with `If-Match` the etag seen in the review |
| Remove (`delete`) | an event of this plan the plan no longer has | delete with `If-Match`; one edited in Google is a conflict instead |
| Deleted in Google (`deleted_in_google`) | cancelled in Google, not by the hub | nothing; **Restore**: update it back to confirmed, else insert under the next gen |
| Past (`skip`) | its day is before today (Vancouver) and it would need a change | nothing |

A `412 Precondition Failed` during apply means the event changed in Google after the review: it
is reported as "Changed in Google Calendar since you reviewed it" and never overwritten. With no
ledger entry (a lost ledger, or a crash before it was saved) an event whose text still matches
the hash the hub stamped on it counts as untouched; one whose text differs is a conflict.

Apply runs one operation at a time under a lock (`409 sync_in_progress` for a second apply or a
disconnect meanwhile), creates the Pantry plan calendar (time zone America/Vancouver) on first
use, and saves the ledger after each successful write. Rate limits (429, or 403
`rateLimitExceeded`/`userRateLimitExceeded`) and 5xx answers are retried after 1, 2, 4, 8 and
16 s with jitter, or what `Retry-After` asks, for at most 60 s; `quotaExceeded` is not retried.
A failure that stops the run (quota, rate limit, the connection ended, the calendar gone) marks
the remaining writes "Not tried". Writes are spaced at least 0.2 s apart.

## The routes

All `POST`s are behind the hub's guard (docs/hub-security.md): the hub's own Host, the console's
Origin, `X-Pantry-Console: 1` and JSON. The sign-in adds its own checks on top.

| Route | Body | Answer |
|---|---|---|
| `GET /hub/calendar/status` | | `{configured, client_type, connected, needs_reconnect, reconnect_by, can_connect_here, connect_url, calendar: {summary} or null, scope, all_day, testing_note, last_sync, problem}`: booleans and labels, never a token, client id or secret |
| `POST /hub/calendar/connect` | `{return_to?: "#/mealplan"}` | `{auth_url}` and the `pantry_oauth` cookie; 409 `not_configured` or `origin_not_registered` |
| `GET /hub/calendar/oauth/callback` | `?code&state` or `?error&state` from Google | 303 to `/pantry/#/mealplan?calendar=connected`, `=denied&reason=access_denied` or `=error&reason=<word>`; 400 (nothing stored) for an unknown, expired or reused state, or a missing or wrong cookie. `Cache-Control: no-store`, `Referrer-Policy: no-referrer` |
| `POST /hub/calendar/sync/preview` | `{schedule, include?}` (the Meal plan's `approved_schedule`, as for `/calendar/preview`) | `{preview_token, calendar_action: create or existing, calendar, plan, rev, counts, ops[{item_id, op, kind, title, date, changes, origin?, unverified?, note?}]}`. Writes nothing |
| `POST /hub/calendar/sync/apply` | `{schedule, include?, preview_token, choices: {item_id: keep, overwrite or restore}}` | `{status: ok, partial or failed, results[{item_id, op, ok, error_code?, message?}], calendar}` |
| `POST /hub/calendar/disconnect` | `{delete_calendar: bool}` | `{revoked, calendar_deleted, note}` |

Refusals are `{detail: {code, message}}`: 409 `not_configured`, `not_connected`,
`needs_reconnect`, `token_file`, `calendar_missing`, `preview_stale`, `sync_in_progress`, and
pantry-api's own `not_approved`, `needs_review`, `no_longer_stocked`; 422 `choice_needed` (every
"Edited in Google" row needs Keep or Overwrite) or `bad_choice`; 413 for a plan over 512 KB; 429
`quota_exceeded`/`rate_limited`; 502 `pantry_unavailable`, `pantry_too_old` or
`google_unavailable`. `/hub/status` reports `keys.google_calendar_client` and
`keys.google_calendar_connected` (booleans).

## The sign-in, and keeping secrets out of logs

- **PKCE (S256).** The hub makes a verifier per sign-in and sends Google only its SHA-256.
- **State bound to a cookie.** A 32-byte `state` maps, in memory, to the verifier, a nonce's
  hash, the redirect URI and where to return, for 10 minutes (at most five at once), and is
  removed on first use. The nonce goes to the browser as the `pantry_oauth` cookie: HttpOnly,
  SameSite=Lax, path `/hub/calendar/oauth`, 10 minutes. The callback needs both, so a link
  someone else started cannot finish in your browser.
- **The redirect URI is the console's own origin**, checked against the client's registered
  URIs, so the callback lands where the cookie was set (the hub, or Vite through its proxy).
- **The callback stores a token only** when Google granted `calendar.app.created` and sent a
  refresh token (`prompt=consent`, `access_type=offline`).
- **Logs.** uvicorn's access log records query strings. `RedactQuery` (`demo_hub/redact.py`,
  installed by `python -m demo_hub.app`) masks `code`, `state`, `error`, `token` and similar
  values: the log reads `GET /hub/calendar/oauth/callback?state=***&code=***`. A hub started
  another way (plain `uvicorn ...`) does not get it.
- Tokens and the client secret never appear in `/hub/status`, the calendar status, the settings'
  repr, the ledger, the Assistant's traces or the metrics; the tests check each of these.

## Settings

| Variable | Default (`Settings.from_env`) | Meaning |
|---|---|---|
| `GOOGLE_OAUTH_CLIENT_FILE` | `~/.pantry-secrets/google_oauth_client.json` | Your client JSON; missing turns sync off |
| `GOOGLE_CALENDAR_TOKEN_FILE` | `~/.pantry-secrets/google_calendar_token.json` | Where the hub keeps the refresh token |
| `DEMO_CALENDAR_DIR` | `~/.pantry-demo/calendar` | The ledger's folder |
| `HUB_OAUTH_ORIGINS` | `http://127.0.0.1:8090,http://localhost:5173` | Console origins allowed to start a sign-in (each must also be a registered redirect) |
| `GOOGLE_AUTH_URL`, `GOOGLE_TOKEN_URL`, `GOOGLE_REVOKE_URL`, `GOOGLE_CALENDAR_URL` | Google's | Overridable for tests only |

## Smoke test (manual, with your test account)

Run once after setup and record pass or fail per step in the PR. Use a test Google account, or
check that nothing else of yours is in a calendar named Pantry plan.

1. `demo-hub/scripts/up.sh` prints `ok    Google OAuth client: calendar sync is set up`.
2. Open http://127.0.0.1:8090/pantry/#/mealplan, place three meals and approve a trip, then
   **Add to calendar**. The Google Calendar section shows **Connect Google Calendar** and the
   7-day note.
3. Connect (step 2 above). The dialog reopens with "Connected" and a reconnect-by date;
   `ls -l ~/.pantry-secrets/google_calendar_token.json` shows `-rw-------`.
4. **Review changes**: the button reads "Create the Pantry plan calendar and add N events".
   Apply. Every row says Added.
5. In Google Calendar (web) a calendar **Pantry plan** has N all-day events on the plan's dates;
   open one and compare its text with the dialog's "What the event says". No guests, no
   invitation email.
6. **Review changes** again: "Nothing to change" (N unchanged).
7. Move a dinner to another day on the board, then Review: one "Change" row (date). Apply; the
   event moves in Google.
8. In Google, edit one event's title. Review: it is under "Edited in Google" with Keep chosen.
   Apply: your title stays. Review again: quiet. Then edit another, choose Overwrite, apply: the
   plan's title is back.
9. In Google, delete one event. Review: "Deleted in Google" with Restore unticked; apply changes
   nothing. Tick Restore and apply: the event is back.
10. Take a meal off the board. Review: one "Remove" row. Apply; it is gone from Google.
11. **Disconnect** with "Also delete the Pantry plan calendar" ticked: the calendar is gone, the
    token file is gone, and myaccount.google.com/connections no longer lists the app.
12. `grep -o 'oauth/callback?[^ ]*' ~/.pantry-demo/logs/hub.log` shows only `state=***` and
    `code=***` values.

## Troubleshooting

| You see | Why | Do |
|---|---|---|
| No Google Calendar section | no hub (HF Space, AKS), or no client file | set up as above and run up.sh |
| `origin_not_registered` | the console is open at an address whose callback is not registered | open http://127.0.0.1:8090/pantry/, or add that address's callback to the client |
| Google says `redirect_uri_mismatch` | the client lists the URI differently (trailing slash, `localhost` vs `127.0.0.1`) | make the redirect URIs exactly the two above |
| Google says "access blocked" or `access_denied` | the account is not a test user, or you cancelled | add the account under Test users; connect again |
| `scope_not_granted` | the calendar permission was unticked on the consent screen | connect again and leave it ticked |
| "The connection ended" (`needs_reconnect`) | Testing's 7 days are up, or access was revoked | connect again |
| `calendar_missing` | the Pantry plan calendar was deleted in Google | Disconnect, then connect: the next sync makes a new one |
| `token_file` | the token file is readable by others | delete it and connect again |

## Unverified Google behaviours, and what covers each

- Restoring an event deleted in Google by updating it back to `confirmed`: if Google refuses,
  the hub inserts the event under the next gen's id (tested against the fake both ways).
- Whether Google returns an event's description exactly as written: the hub's "untouched since
  written" check compares the text; if Google changed it, such events show as "Edited in Google"
  (Keep by default), never overwritten. Normal syncs rely on etags and are not affected.
- Whether `calendar.app.created` is listed in every console's scope picker: it is requested by
  the hub regardless; a narrower or wider scope would need your explicit decision.
