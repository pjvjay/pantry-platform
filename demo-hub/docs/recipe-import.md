# Recipe import from links and YouTube

A shopper pastes a recipe link, or a YouTube link, and gets the recipe's ingredient lines to
review: each line's verbatim text, the amount pantry parsed from it, and where it came from.
**What the shopper reviews is exactly what gets planned.** The model names the recipe by its
key, and the hub hands pantry the reviewed lines (`plan_from_lines`); nothing re-reads the
recipe on the way to the cart.

This page covers the hub's part (`demo_hub/recipe_import/`, the import routes and the chat). The
line parser and the structured planning entry point are pantry-api's
(`POST /recipes/parse-lines`, `flow.run_spec`, MCP `plan_from_lines`); the import sheet is
pantry-frontend's. PLAN.md 4.4 is the design.

## Where it works

| Feature | Local stack (with hub) | AKS console (no hub, live LLM) | HF Space / Render demo (no hub, DEMO_MODE) |
|---|---|---|---|
| Import from a recipe link | yes | no: paste only (D13) | no: paste only (D13) |
| Import from a YouTube link (title, description) | yes | no (D13) | no (D13) |
| Gemini watching a video (on a click, off by default) | yes, with a Gemini key and `DEMO_VIDEO_IMPORT=1` | no | no |
| Paste import (`/recipes/parse-lines`) | yes | yes | yes |
| Plan the reviewed lines (`/plan/spec`, `plan_from_lines`) | yes | yes (REST) | yes (REST) |

The pantry server never fetches a URL. The hub, which runs on the shopper's own machine like the
recipe-shopper skill, does. Where there is no hub the import sheet hides Link and YouTube and
says "Reading links needs the local demo hub".

## Methods, in order

1. **A recipe page.** The hub fetches the page (below), and the skill's extractor
   (`pantry-api/skills/recipe-shopper/scripts/extract_recipe.py`, loaded by path) finds its
   schema.org Recipe: JSON-LD first, then microdata. The ingredient lines go to pantry's
   `POST /recipes/parse-lines` (no LLM), with `origin: "page"`, so every amount is
   `stated_by_source`. `source.method` is `jsonld` or `microdata`; `source.extractor` is
   `extract_recipe.py <version>`. A page with no structured recipe is 422 `no_recipe_found`,
   and in chat the model reads it with the fetch tool as before.
2. **A YouTube video.**
   - The id comes from `watch?v=`, `youtu.be/`, `/shorts/`, `/live/` or `/embed/`
     (youtube.com, m., music., youtube-nocookie.com).
   - oEmbed gives the title and channel, with no key, through the same guarded fetch. An oEmbed
     401, 403 or 404 is 422 `not_public`.
   - With a YouTube Data API key: `videos.list?part=snippet,contentDetails` (1 unit) gives the
     description and the length. The description's ingredient list (the block under an
     "Ingredients" heading, up to the next heading or method step; or else the longest run of
     three or more lines that start with an amount once any bullet or list number is set aside,
     so a method written as "1. Boil the water" is never read as ingredients) becomes the doc,
     `method: youtube_description`. Up to three links that
     could be the creator's written recipe (never social, shop, tip or shortener links) are
     offered as `linked_pages`; importing one of them is `method: youtube_linked_page`, with the
     channel kept on the source.
   - Without a key, or with no list in the description: `doc: null`, `needs: choose_method`.
     The shopper picks a linked page, pastes the list, or (if enabled) clicks Transcribe.
   - When the Data API refuses the key (not valid, quota spent, the API not enabled for it) or
     cannot be reached, the import goes on as without a key: the oEmbed title and channel,
     `needs: choose_method`, and a warning saying why the description was not read. A
     transcription goes on too, on the shopper's estimate of the length. Only a video the API
     does not list is 422 `not_public`. The recipe-shopper skill does the same.
3. **Gemini watching the video** (only on the shopper's click, off by default). See below.

## The guarantee: reviewed lines are planned as reviewed

- A link pasted in chat is read in code **before the model's first call** (`Agent._pre_import`).
  On success the doc is stored as `conv.docs["imp:N"]`, a `recipe_import` event goes to the
  browser, and the model reads an `[import]` note before the shopper's words:

  ```text
  [import] Red Lentil Dal (serves 4), from blog.example, 3 lines, doc_key imp:1:
  - 400 g red lentils
  - 1 tbsp cumin seeds
  - 2 cloves garlic, minced
  ```

  At most 8,000 characters; lines that do not fit are counted, and still planned.
- `link_reader` then offers `plan_from_lines` and **not** `fetch*` (nor the skill, which tells
  the model to fetch). The PREAMBLE says: plan it with `plan_from_lines(doc_key=...)`; do not
  fetch the link; do not retype the lines.
- When the model calls `plan_from_lines`, the hub fills `lines`, `title` and `servings` from
  `conv.docs[doc_key]` (`_with_hub_args`), and location and `basis` as for the other plan tools.
  The model never sees those arguments (`_plan_tools`), and any it sends are dropped. An unknown
  `doc_key` is refused by the hub, naming the known keys.
- The console's "Plan this now" sends the reviewed doc as `ChatBody.recipe_doc`. The hub checks
  it (413 over 64 KB of UTF-8 JSON; 422 `bad_recipe_doc`, with pantry's own bounds on a line,
  `no_lines` or `unconfirmed_lines`), stores it as the conversation's next `imp:N` and adds the
  same note. On a target without `plan_from_lines` (a gateway whose pantry tools were not
  refreshed) the turn ends with an `error` event before the model is called, since the model
  could only retype the lines.
- The online eval `import_grounded` (evals.py) checks every turn that imported a recipe: each
  plan of it was `plan_from_lines`, and every planned basis line's name, quantity and unit equals
  the doc's. `plan_from_text` on an imported recipe, a changed amount or unit, or an added or
  dropped line fails it. The basis never reaches the browser or the trace, so the hub passes its
  own record of the turn's plans to the eval (`Agent.turn_plans`).
- If the page cannot be read, nothing changes from before: `fetch*`, `plan_from_text` and the
  skill are offered. A YouTube link is never fetched: without lines, the model gets a one-line
  note and answers in one sentence that the shopper can choose how to read it.

**Injection safety.** Only parsed ingredient lines (at most 60, at most 300 characters each) and
the title reach the model. A page's prose, a description and a video's narration never do. The
description is used in memory for its list and links and is not stored.

## What is sent to whom

| To | What | When |
|---|---|---|
| The recipe site | One GET of the page the shopper linked (hub user agent, no cookies) | Each link import |
| YouTube oEmbed | The watch URL | Each YouTube import |
| YouTube Data API | The video id, with the key in `x-goog-api-key` | Only with a key |
| Gemini (`generateContent`) | The public watch URL and the prompt, the key in `x-goog-api-key` | Only on the shopper's Transcribe click, with video import on |
| pantry `/recipes/parse-lines` | The ingredient lines and the title or yield | Each import with lines |

Not used: YouTube caption download (`captions.download` needs the video owner's permission),
reading the watch page or its transcript (YouTube's terms forbid scraping), yt-dlp.

## The fetch (SSRF)

`recipe_import/fetch.py`. The hub runs on loopback next to every other service of the demo, so a
link must never reach them, or the shopper's network.

- **Limits from the extractor.** `MAX_BYTES` (5 MB) and `TIMEOUT_S` (20 s) are read from
  `extract_recipe.py`, never restated. Over the cap is 413 (a gzip body counts as inflated); the
  whole fetch, redirects included, has the deadline (504).
- **Resolve once, check every address.** `getaddrinfo` in a thread; every address must be
  global (not loopback, private, link-local, multicast, reserved or shared). One private address
  among public ones refuses the name.
- **Pinned connect.** The request goes to `https://<checked IP>:<port>/path` with `Host: <name>`
  and httpcore's `sni_hostname` extension, so TLS verifies the certificate for the name and no
  second lookup happens. Checked locally against a TLS server with a throwaway CA: the name
  verifies; no name or another name fails the handshake.
- **Peer check.** After connecting, the socket's remote address must equal the pinned address
  and be global, or the response is dropped before its body is read (403).
- **No ambient routes.** `trust_env=False` (an `HTTPS_PROXY` cannot route around the checks), a
  new client per hop (no cookies), redirects followed by hand: at most 5, each re-validated and
  re-pinned, http and https only (`file:`, `ftp:` are 403).
- **Off the event loop.** `extract()` runs in `asyncio.to_thread`, so parsing a multi-megabyte
  page never stalls the hub.

Residual risks: IPv6 and NAT64 edge cases rely on `ipaddress.is_global`; another process on the
machine could still race the loopback, which the peer check catches.

## The routes

Both are behind the guard (`docs/hub-security.md`): `X-Pantry-Console: 1`, JSON, the hub's Host.

`POST /hub/recipes/import` with `{url}` returns an ImportResult:

```json
{"doc": RecipeDoc | null, "method": "jsonld" | "microdata" | "youtube_description" |
 "youtube_linked_page" | "gemini_video" | null, "linked_pages": [{"url", "site"}],
 "video": null | {"id", "url", "title", "channel", "channel_url", "thumbnail_url",
                  "duration_s", "description_read", "transcribe": {"enabled", "reason"}},
 "needs": "none" | "confirm_lines" | "choose_method" | "needs_servings",
 "warnings": [str]}
```

A web page's result also has `structured_data` (`jsonld` or `microdata`, even when `method` is
`youtube_linked_page`); a video transcription's has `usage` (tokens and cost). The doc's key is
`imp:draft`; chat re-keys it. Errors are `{"detail": {"code", "message", ...}}`:
400 `bad_url`, 403 `not_public` / `bad_redirect`, 413 `too_large`, 422 `no_recipe_found` /
`not_public`, 502 (`unreachable`, `http_status`, `too_many_redirects`, `pantry_unreachable`,
`pantry_error`, `bad_upstream`, `import_error`), 503 `import_unavailable`, 504 `timeout`.
`bad_upstream` names a line pantry read into something a RecipeDoc cannot hold (a pantry-api
without the 1,000,000 quantity bound reads "2000000 g flour" as 2000000.0), with its
`line_no`; `import_error` is anything the hub did not foresee, with the traceback in its log.
An import route never answers 500.

`POST /hub/recipes/import/video` with `{video_id, consent: true, duration_s?}` returns an
ImportResult with `needs: confirm_lines`, every line `confirmed: false`, each with
`evidence: {quote, at: "mm:ss"}`, plus `usage` (tokens and cost) and `video.daily`. Errors: 422
without `consent: true`; 409 `video_import_disabled` or `no_gemini_key`; 429
`daily_video_limit` (with `seconds_used` and `limit_s`); 422 `not_public`; 502.

`GET /hub/status` gains `recipe_import: {links, youtube_description, reason?}`,
`video_import: {enabled, reason, model?, daily?}` and `keys.youtube`.

The chat stream gains the `recipe_import` event, before the first `thinking`:

```json
{"type": "recipe_import", "status": "ok", "url": "...", "doc_key": "imp:1" | null,
 "result": ImportResult, "note": "[import] ...", "ms": 412.0, "via": "console"?}
{"type": "recipe_import", "status": "failed", "url": "...",
 "error": {"status", "code", "message"}, "fallback": true, "ms": 80.0}
```

## Gemini watching a video

`recipe_import/gemini_video.py`, a native client apart from `llm.py` (Gemini's OpenAI-compatible
endpoint documents no video input).

- `POST {GEMINI_NATIVE_URL}/models/{DEMO_VIDEO_IMPORT_MODEL}:generateContent`, the key in
  `x-goog-api-key`, never in a URL or a log line.
- The body names the video (`file_data.file_uri`, the watch URL), asks for ingredient lines as
  said or shown with `at` (mm:ss), estimates nothing, and uses `responseMimeType:
  application/json` with a schema, `mediaResolution: MEDIA_RESOLUTION_LOW`, `temperature: 0`.
- Checked in code: a line with no valid `at`, or past the video's end, is dropped; at most 60;
  then parse-lines. Every line is `confirmed: false` with `amount_basis:
  transcribed_confirmed_by_you`; the shopper ticks each against the video (`&t=` links), and
  pantry refuses to plan an unticked line.
- **Usage.** `usageMetadata` (prompt, output and total tokens, per modality) is priced by
  `pricing.video_import` at `DEMO_VIDEO_IMPORT_PRICE` (0 by default: YouTube input is a preview
  "at no charge"; every figure says "preview pricing"). It goes on the import span and into
  Metrics (`imports.video_tokens`, `imports.video_cost_usd`).
- **Daily cap.** `~/.pantry-demo/usage/video-<UTC date>.json` counts seconds of video. Before a
  call the length (from the Data API, or the shopper's estimate) is checked against
  `DEMO_VIDEO_IMPORT_DAILY_SECONDS` (6 h, under the free tier's 8 h); after it, the length is
  added, or tokens / 100 when it is unknown. A failed call that Gemini already read still
  counts.
- **Availability.** Off unless `DEMO_VIDEO_IMPORT=1`. With no Gemini key (Ollama only) the
  button is disabled: "Needs a Gemini API key; not available with local models only." The chat
  never transcribes a video; only the route does, on a click.

**Day-1 feasibility gate: not run yet.** It needs the user's approval and a public cooking video
the user picks. Record here: the model, the key's tier, latency, token counts per modality, and
whether YouTube `file_uri` with `responseSchema` is accepted. If YouTube URLs are not accepted,
the video path is dropped and D4 falls back to the description and paste.

**Measured accuracy: not measured yet.** Smoke S4 records it against the video at each
timestamp.

## Copyright

Only ingredient lines and a link back (the source URL, site, title or channel) are stored. The
method text, the page's prose, its images and a video's description are never stored or shown.

## Settings

| Variable | Default | What |
|---|---|---|
| `RECIPE_EXTRACTOR` | `scripts/extract_recipe.py` beside `RECIPE_SHOPPER_SKILL` | The extractor; empty or missing turns link import off (503, and chat reads links as before) |
| `YOUTUBE_API_KEY` / `YOUTUBE_API_KEY_FILE` | `~/.pantry-secrets/youtube_api_key` | A YouTube Data API key you create (D4); never created by the assistant |
| `DEMO_VIDEO_IMPORT` | `0` | `1` allows Gemini video transcription, on a click |
| `DEMO_VIDEO_IMPORT_MODEL` | `gemini-3-flash-preview` | The model that watches |
| `DEMO_VIDEO_IMPORT_DAILY_SECONDS` | `21600` | The daily cap, seconds of video (UTC day) |
| `DEMO_VIDEO_IMPORT_PRICE` | `0,0` | USD per 1M tokens, input and output |
| `GEMINI_NATIVE_URL` | `https://generativelanguage.googleapis.com/v1beta` | The native Gemini API |
| `DEMO_USAGE_DIR` | `~/.pantry-demo/usage` | Where the daily count lives |

**YouTube key setup.** Create a Google Cloud project, enable "YouTube Data API v3", create an API
key restricted to that API, and save it with `umask 077; printf %s "<key>" >
~/.pantry-secrets/youtube_api_key`. `scripts/up.sh` passes the file to the hub when it exists.
Quota: about 1 unit per import of the default 10,000 a day (from memory, not re-verified).

## Telemetry

Each import is an `import` span: method, host, line count, what it needs next, time, and a
video's tokens and cost. The URL is recorded without its query string or fragment. In chat the
span sits on the turn (the turn's own `message` is still the shopper's text as typed); an import
from the console's sheet is a trace of its own with `source: "import"`. Metrics roll them up
under `imports`.
