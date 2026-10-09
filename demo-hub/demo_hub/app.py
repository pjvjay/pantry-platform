"""The hub's HTTP surface. One origin for the whole demo:

* ``/pantry/``            the grocery app (pantry-frontend's built SPA, from ``SPA_DIST``)
* ``/pantry/api/*``       pantry-api, proxied (REST, and its ``/mcp`` endpoint)
* ``/hub/status``         every service's health, versions and links
* ``/hub/mcp/*``          the MCP explorer: targets, catalog, call a tool, read, prompt
* ``/hub/agent/*``        the Assistant: options, a chat turn streamed as server-sent events, and
                          a conversation's cart: a line's alternatives and the shopper's swap
* ``/hub/recipes/*``      recipe import: a recipe page or a YouTube video read into reviewed
                          lines, and, on the shopper's click, a video transcribed by Gemini
* ``/hub/sims/*``         the mcp-sim runner: scenarios, runs, start and follow jobs
* ``/hub/demo/reset``     reseed pantry's database and reload the demo origin evidence
* ``/hub/calendar/*``     opt-in Google Calendar sync of the approved meal plan (gcal_routes.py)

Secrets (the pantry bearer token, the ContextForge JWT, the Gemini key) stay in this process;
the browser only ever talks to the hub. ``guard.py`` checks every request first: the hub's own
Host on every route, and the console's header and JSON on every non-GET ``/hub/*`` and
``/pantry/api/*`` request (docs/hub-security.md).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from demo_hub import gcal_routes, mcp_targets, meal_plans, pricing, redact
from demo_hub.agent import AGENT_TARGETS, Agent, CartError
from demo_hub.evals import evaluate
from demo_hub.gcal_sync import CalendarSync
from demo_hub.guard import Guard
from demo_hub.images import ImageCache, ImageError
from demo_hub.llm import MODEL_CHOICES, ChatClient, LLMError
from demo_hub.mcp_targets import McpTargetError, Targets, open_session
from demo_hub.recipe_import import Importer, ImportFailure, RecipeDoc, without_query
from demo_hub.recipe_import.web import problem
from demo_hub.runs import run_view
from demo_hub.settings import Settings
from demo_hub.sims import PRESETS, SimsClient, SimsError
from demo_hub.telemetry import (
    HttpStats,
    TraceRecorder,
    TraceStore,
    add_gateway_spans,
    compute_metrics,
    import_trace,
)

TELEMETRY_KINDS = ("page", "api", "chat")
STORES_TTL_S = 600.0
MAX_RECIPE_DOC = 64_000          # ChatBody.recipe_doc, as UTF-8 JSON bytes

log = logging.getLogger(__name__)

HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
              "proxy-authorization", "proxy-authenticate", "host", "content-length",
              "content-encoding"}


class SpaFiles(StaticFiles):
    """The built frontend. Its page is revalidated on every load, so a rebuild shows up on the
    next reload; Vite's content-hashed files under ``assets/`` never change, so they are kept."""

    def file_response(self, full_path: Any, stat_result: os.stat_result, scope: Any,
                      status_code: int = 200) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        hashed = Path(full_path).parent.name == "assets"
        response.headers["cache-control"] = \
            "public, max-age=31536000, immutable" if hashed else "no-cache"
        return response


class CallBody(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ReadBody(BaseModel):
    uri: str


class PromptBody(BaseModel):
    name: str
    arguments: dict[str, str] = Field(default_factory=dict)


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=12_000)
    conversation_id: str | None = None
    model: str | None = None
    target: str = "gateway-recipes"
    disclosure: str | None = None       # "progressive" or "all"; the hub's default when omitted
    # A RecipeDoc the shopper reviewed in the import sheet ("Plan this now"): it becomes the
    # conversation's next imp:N and the model plans it with plan_from_lines. At most 64 KB.
    recipe_doc: dict[str, Any] | None = None
    # The console's Meal plan in brief (meal_plans.MealPlanBody), sent with every message: what
    # plan_meals drafts against. At most 64 KB, kept in memory with the conversation only.
    meal_plan: dict[str, Any] | None = None


class ImportBody(BaseModel):
    url: str = Field(min_length=1, max_length=2_000)


class VideoImportBody(BaseModel):
    """Gemini watches a video only on the shopper's click: ``consent`` must be true. The
    length, when the hub cannot read it (no YouTube key, or the API refused it), is the estimate
    the button showed, and is then required (422 needs_duration): the daily cap is checked in
    seconds of video before the call."""
    video_id: str = Field(pattern=r"^[A-Za-z0-9_-]{11}$")
    consent: Literal[True]
    duration_s: int | None = Field(default=None, ge=1, le=12 * 3600)


class AlternativesBody(BaseModel):
    """A line of the cart at ``ref`` (a plan card's ref). The hub builds the ranking from the
    plan's basis it holds; anything else in the body, a basis included, is ignored."""
    ref: int = Field(ge=0)
    line_no: int = Field(ge=1, le=60)
    limit: int = Field(default=12, ge=1, le=25)


class SwapBody(BaseModel):
    """The shopper's choice for a line of the cart at ``ref``; ``product_id`` null puts the
    planner's pick back."""
    ref: int = Field(ge=0)
    line_no: int = Field(ge=1, le=60)
    product_id: Annotated[int, Field(ge=1)] | None


class WarmBody(BaseModel):
    model: str | None = None
    target: str = "gateway-recipes"
    disclosure: str | None = None


class SimRunBody(BaseModel):
    scenarios: list[str] | str
    preset: str = "gemini"
    repeat: int = Field(default=1, ge=1, le=5)
    modes: list[str] | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="pantry demo hub", docs_url="/hub/docs", openapi_url="/hub/openapi.json",
                  redoc_url=None)
    targets = Targets(settings)
    importer = Importer(settings)
    agent = Agent(settings, targets, ChatClient(settings), importer=importer)
    sims = SimsClient(settings)
    scratch = Path(tempfile.gettempdir()) / "pantry-demo-hub"
    store = TraceStore(settings.traces_dir or scratch / "traces")
    images = ImageCache(settings.images_dir or scratch / "images")
    http_stats = HttpStats()
    stores_cache: dict[str, Any] = {"at": 0.0, "names": []}
    calendar = CalendarSync(settings)
    app.state.settings, app.state.agent, app.state.targets = settings, agent, targets
    app.state.calendar = calendar
    app.state.importer = importer
    app.state.traces, app.state.images, app.state.http_stats = store, images, http_stats

    # Before the timing middleware below, so the timing wraps it: a refused request is counted.
    app.add_middleware(Guard, port=settings.hub_port, extra_hosts=settings.allowed_hosts)

    @app.middleware("http")
    async def timing(request: Request, call_next: Any) -> Response:
        """Every hub request timed per route; the browser sees the hub's own time in a
        Server-Timing header (for a stream: until its headers)."""
        started = time.perf_counter()
        response = await call_next(request)
        ms = (time.perf_counter() - started) * 1000
        route = request.scope.get("route")
        name = getattr(route, "path", None) or request.url.path
        if not str(name).startswith("/pantry/assets"):
            http_stats.record(f"{request.method} {name}", ms, response.status_code)
        response.headers["Server-Timing"] = f"hub;dur={ms:.1f}"
        return response

    async def known_stores() -> list[str]:
        """pantry's store names (for the grounded_stores eval), cached for ten minutes."""
        if time.monotonic() - stores_cache["at"] < STORES_TTL_S and stores_cache["names"]:
            return list(stores_cache["names"])
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{settings.pantry_api_url}/stores")
            if r.status_code == 200:
                stores_cache.update(at=time.monotonic(), names=[s["name"] for s in r.json()])
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            pass
        return list(stores_cache["names"])

    async def add_gateway(trace: dict[str, Any]) -> None:
        if await add_gateway_spans(trace, settings.contextforge_url, settings.contextforge_jwt):
            store.save(trace)

    # --- status --------------------------------------------------------------------------------

    async def probe(client: httpx.AsyncClient, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await client.get(url, **kwargs)
            body: Any
            try:
                body = response.json()
            except ValueError:
                body = None
            return {"ok": response.status_code < 400, "status": response.status_code,
                    "body": body}
        except httpx.HTTPError as exc:
            return {"ok": False, "status": 0, "error": type(exc).__name__}

    @app.get("/hub/status")
    async def status() -> dict[str, Any]:
        s = settings
        cf_headers = {"Authorization": f"Bearer {s.contextforge_jwt}"} if s.contextforge_jwt else {}
        async with httpx.AsyncClient(timeout=4) as client:
            pantry, runtime, cf, fetch, sim, ollama, burr = await asyncio.gather(
                probe(client, f"{s.pantry_api_url}/health"),
                probe(client, f"{s.pantry_api_url}/settings/runtime"),
                probe(client, f"{s.contextforge_url}/health"),
                probe(client, f"{s.fetch_url}/healthz"),
                probe(client, f"{s.mcpsim_ui_url}/api/config"),
                probe(client, f"{s.ollama_url}/api/tags"),
                probe(client, s.burr_url),
            )
            servers = await probe(client, f"{s.contextforge_url}/servers", headers=cf_headers)
        ollama_models = [m.get("name") for m in (ollama.get("body") or {}).get("models", [])] \
            if ollama["ok"] else []
        sim_body = sim.get("body") or {}
        gateway_servers = []
        if servers["ok"] and isinstance(servers.get("body"), (list, dict)):
            raw = servers["body"]
            items = raw if isinstance(raw, list) else raw.get("servers", raw.get("items", []))
            gateway_servers = [{"name": x.get("name"), "id": x.get("id"),
                                "tools": len(x.get("associatedTools") or x.get("tools") or [])}
                               for x in items]
        return {
            "services": [
                {"id": "pantry-api", "label": "pantry API + MCP", "url": s.pantry_api_url,
                 **_strip(pantry), "runtime": runtime.get("body") if runtime["ok"] else None,
                 "links": {"API docs": f"{s.pantry_api_url}/docs"}},
                {"id": "contextforge", "label": "ContextForge gateway", "url": s.contextforge_url,
                 **_strip(cf), "servers": gateway_servers,
                 "links": {"Admin UI": f"{s.contextforge_url}/admin"}},
                {"id": "fetch", "label": "fetch MCP server", "url": s.fetch_url, **_strip(fetch)},
                {"id": "mcp-sim", "label": "mcp-sim runner", "url": s.mcpsim_ui_url,
                 **_strip(sim), "skill": (sim_body.get("skill") or {}).get("path"),
                 "links": {"Runner UI": s.mcpsim_ui_url}},
                {"id": "ollama", "label": "Ollama (local models)", "url": s.ollama_url,
                 **_strip(ollama), "models": ollama_models},
                {"id": "burr", "label": "Burr trace UI (optional)", "url": s.burr_url,
                 **_strip(burr), "links": {"Burr UI": s.burr_url}},
            ],
            "keys": {"gemini": bool(s.gemini_api_key), "pantry_token": bool(s.pantry_mcp_token),
                     "contextforge_jwt": bool(s.contextforge_jwt),
                     "youtube": bool(s.youtube_api_key),
                     "google_calendar_client": calendar.configured(),
                     "google_calendar_connected": calendar.connected()},
            "agent": {"default_model": s.default_agent_model},
            "recipe_import": importer.status(),
            "video_import": importer.video_status(),
        }

    # --- MCP explorer --------------------------------------------------------------------------

    @app.get("/hub/mcp/targets")
    async def mcp_target_list() -> list[dict[str, Any]]:
        return [t.public() for t in targets.catalog_of_targets()]

    async def _with_session(target_id: str, op: Any) -> Any:
        try:
            target = await targets.resolve(target_id)
            async with open_session(target) as session:
                return await op(session)
        except McpTargetError as exc:
            raise HTTPException(exc.status if 400 <= exc.status < 600 else 502, str(exc)) \
                from exc

    @app.get("/hub/mcp/{target_id}/catalog")
    async def mcp_catalog(target_id: str) -> dict[str, Any]:
        target = await _resolve_public(targets, target_id)
        result = await _with_session(target_id, mcp_targets.catalog)
        return {"target": target, **result}

    @app.post("/hub/mcp/{target_id}/call")
    async def mcp_call(target_id: str, body: CallBody) -> dict[str, Any]:
        return await _with_session(
            target_id, lambda s: mcp_targets.call_tool(s, body.tool, body.arguments))

    @app.post("/hub/mcp/{target_id}/read")
    async def mcp_read(target_id: str, body: ReadBody) -> dict[str, Any]:
        return await _with_session(target_id, lambda s: mcp_targets.read_resource(s, body.uri))

    @app.post("/hub/mcp/{target_id}/prompt")
    async def mcp_prompt(target_id: str, body: PromptBody) -> dict[str, Any]:
        return await _with_session(
            target_id, lambda s: mcp_targets.get_prompt(s, body.name, body.arguments))

    # --- Assistant -----------------------------------------------------------------------------

    @app.get("/hub/agent/options")
    async def agent_options() -> dict[str, Any]:
        public = {t.id: t.public() for t in targets.catalog_of_targets()}
        return {"models": list(MODEL_CHOICES), "default_model": settings.default_agent_model,
                "targets": [public[t] for t in AGENT_TARGETS], "default_target": AGENT_TARGETS[0],
                "max_steps": settings.agent_max_steps,
                "disclosures": ["progressive", "all"],
                "default_disclosure": settings.assistant_disclosure,
                "skill_loaded": bool(settings.recipe_shopper_skill
                                     and Path(settings.recipe_shopper_skill).expanduser().is_file())}

    warming: dict[tuple[str, str, str], asyncio.Task[dict[str, Any]]] = {}

    @app.post("/hub/agent/warm")
    async def agent_warm(body: WarmBody) -> dict[str, Any]:
        """A local model reads the Assistant's instructions and first tools now, while the shopper
        types, so the first step reads only the question. Answers when the model has read them
        (minutes on a CPU); asking again while it reads waits for the same read."""
        model = body.model or settings.default_agent_model
        key = (model, body.target, body.disclosure or settings.assistant_disclosure)
        task = warming.get(key)
        if task is None or task.done():
            task = warming[key] = asyncio.ensure_future(
                agent.warm(model, body.target, body.disclosure))
        try:
            return await asyncio.shield(task)
        except (LLMError, McpTargetError) as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/hub/agent/chat")
    async def agent_chat(body: ChatBody) -> StreamingResponse:
        doc = _reviewed_doc(body.recipe_doc) if body.recipe_doc is not None else None
        try:
            meal_plan = (meal_plans.meal_plan_body(body.meal_plan)
                         if body.meal_plan is not None else None)
        except meal_plans.MealPlanRefused as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        try:
            conv = agent.conversation(body.conversation_id, body.model
                                      or settings.default_agent_model, body.target,
                                      body.disclosure)
        except LLMError as exc:
            raise HTTPException(422, str(exc)) from exc

        recorder = TraceRecorder(conversation_id=conv.id, model=conv.model, target=conv.target,
                                 message=body.message, disclosure=conv.disclosure_mode,
                                 turn=len(conv.user_texts) + 1)

        def sse(event: dict[str, Any]) -> bytes:
            return f"data: {json.dumps(event, default=str)}\n\n".encode()

        async def events() -> AsyncIterator[bytes]:
            # Every event is recorded into the turn's trace and stamped for the browser; the
            # answer's evals arrive just before "done". The trace is kept even when the browser
            # goes away mid-answer, and ContextForge's spans join it once the turn is over.
            try:
                async for event in agent.run(conv, body.message, doc,
                                             **({"meal_plan": meal_plan} if meal_plan else {})):
                    if event.get("type") == "done":     # graded with the turn's end in view
                        recorder.evals = evaluate([*recorder.events, event],
                                                  await known_stores(), conv.model,
                                                  plans=agent.turn_plans(conv))
                        yield sse(recorder.on({"type": "evals", "trace_id": recorder.id,
                                               **recorder.evals}))
                    yield sse(recorder.on(event))
            finally:
                if recorder.status == "running":
                    recorder.status = "cancelled"
                trace = recorder.to_dict()
                store.save(trace)
                if any(s["kind"] == "tool" for s in trace["spans"]):
                    asyncio.get_running_loop().create_task(add_gateway(trace))

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # --- traces, metrics and the browser's own measurements ------------------------------------

    @app.get("/hub/traces")
    async def trace_list(limit: int = 50) -> list[dict[str, Any]]:
        return store.list(max(1, min(limit, 500)))

    @app.get("/hub/traces/{trace_id}")
    async def trace_detail(trace_id: str) -> dict[str, Any]:
        trace = store.get(trace_id)
        if trace is None:
            raise HTTPException(404, f"no trace {trace_id}")
        return trace

    @app.get("/hub/runs/{trace_id}")
    async def run_detail(trace_id: str) -> dict[str, Any]:
        """One run as every system saw it: the hub's trace, Burr's steps for each plan call and
        ContextForge's trace for each tool call, on one clock (runs.py)."""
        trace = store.get(trace_id)
        if trace is None:
            raise HTTPException(404, f"no trace {trace_id}")
        return await run_view(trace, settings)

    @app.get("/hub/metrics")
    async def metrics(limit: int = 200) -> dict[str, Any]:
        store.refresh()
        recent = list(store.recent.values())[-max(1, min(limit, 500)):]
        return {**compute_metrics(recent, http_stats, list(store.frontend)),
                "prices_usd_per_1m": {k: {"input": v[0], "output": v[1]}
                                      for k, v in pricing.PRICES.items()},
                "prices_source": pricing.PRICES_SOURCE,
                "free_tier_requests_per_day": pricing.FREE_TIER_REQUESTS_PER_DAY}

    @app.post("/hub/telemetry", status_code=204)
    async def telemetry(request: Request) -> Response:
        """The browser's own measurements: a page load (web vitals), a batch of API calls, or
        one chat turn's stream. Small JSON objects only."""
        raw = await request.body()
        if len(raw) > 64_000:
            raise HTTPException(413, "telemetry batch too large")
        try:
            record = json.loads(raw)
        except ValueError as exc:
            raise HTTPException(400, "telemetry must be JSON") from exc
        if not isinstance(record, dict) or record.get("kind") not in TELEMETRY_KINDS:
            raise HTTPException(422, f"telemetry kind must be one of {TELEMETRY_KINDS}")
        store.add_browser(record)
        return Response(status_code=204)

    # --- images --------------------------------------------------------------------------------

    def _image(found: tuple[Path, str] | None) -> Response:
        if found is None:
            return Response(status_code=404, headers={"Cache-Control": "max-age=3600"})
        path, ctype = found
        return FileResponse(path, media_type=ctype, headers={
            "Cache-Control": "public, max-age=604800", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'"})

    @app.get("/hub/images/ingredient")
    async def ingredient_image(name: str) -> Response:
        try:
            return _image(await images.ingredient(name[:120]))
        except ImageError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.get("/hub/images/remote")
    async def remote_image(url: str) -> Response:
        if len(url) > 2_000:
            raise HTTPException(414, "image URL too long")
        try:
            return _image(await images.remote(url))
        except ImageError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.post("/hub/agent/conversations/{conversation_id}/alternatives")
    async def agent_alternatives(conversation_id: str, body: AlternativesBody) -> dict[str, Any]:
        """The cart's Options for one line: pantry's rank_alternatives on the plan's basis. No
        model call, no lock, nothing in the conversation changes."""
        try:
            return await agent.alternatives(conversation_id, body.ref, body.line_no, body.limit)
        except CartError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.post("/hub/agent/conversations/{conversation_id}/swap")
    async def agent_swap(conversation_id: str, body: SwapBody) -> dict[str, Any]:
        """"Use this": pantry re-prices the plan with the shopper's choice (no model call). The
        answer is the redrawn cart, {card, note}; the model hears of it on the next turn."""
        try:
            return await agent.swap(conversation_id, body.ref, body.line_no, body.product_id)
        except CartError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.delete("/hub/agent/conversations/{conversation_id}")
    async def agent_forget(conversation_id: str) -> dict[str, bool]:
        return {"forgotten": agent.conversations.pop(conversation_id, None) is not None}

    # --- recipe import -------------------------------------------------------------------------

    async def _import(kind: str, url: str, call: Any) -> dict[str, Any]:
        """One import route's call, kept as a trace (source "import") for the metrics: method,
        host, lines, time and cost, the URL without its query."""
        started = time.perf_counter()
        try:
            out = await call
        except Exception as exc:
            failure = exc if isinstance(exc, ImportFailure) else None
            if failure is None:
                # as in the chat's _pre_import: an import the hub did not foresee failing is a
                # 502 the console can show, with the traceback in the hub's log, never a 500
                log.exception("recipe import failed: %s", without_query(url))
                failure = ImportFailure(502, "import_error", "The hub could not read the link "
                                        f"({type(exc).__name__}).")
            store.save(import_trace(kind, url, (time.perf_counter() - started) * 1000,
                                    error=failure.body()))
            raise HTTPException(failure.status, failure.body()) from exc
        store.save(import_trace(kind, url, (time.perf_counter() - started) * 1000, result=out))
        return out

    @app.post("/hub/recipes/import")
    async def recipe_import(body: ImportBody) -> dict[str, Any]:
        """A recipe page or a YouTube link read into a RecipeDoc for the shopper to review:
        ImportResult {doc, method, linked_pages, video, needs, warnings}. No model is called."""
        return await _import("link", body.url, importer.import_url(body.url))

    @app.post("/hub/recipes/import/video")
    async def recipe_import_video(body: VideoImportBody) -> dict[str, Any]:
        """Gemini's transcription of a public video's ingredient lines, every line unconfirmed
        until the shopper ticks it (needs: confirm_lines). Only on the shopper's click."""
        return await _import("video", f"https://www.youtube.com/watch?v={body.video_id}",
                             importer.import_video(body.video_id,
                                                   duration_estimate_s=body.duration_s))

    # --- Simulations ---------------------------------------------------------------------------

    async def _sims(call: Any) -> Any:
        try:
            return await call
        except SimsError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.get("/hub/sims/presets")
    async def sim_presets() -> dict[str, Any]:
        return {"presets": {k: {"label": v["label"], "models": v["models"]}
                            for k, v in PRESETS.items()},
                "runner_url": settings.mcpsim_ui_url}

    @app.get("/hub/sims/scenarios")
    async def sim_scenarios() -> Any:
        return await _sims(sims.get("/api/scenarios"))

    @app.get("/hub/sims/scenarios/{name}")
    async def sim_scenario(name: str) -> Any:
        return await _sims(sims.get(f"/api/scenarios/{name}"))

    @app.get("/hub/sims/runs/{name}/{run_id}")
    async def sim_run(name: str, run_id: str) -> Any:
        return await _sims(sims.get(f"/api/runs/{name}/{run_id}"))

    @app.get("/hub/sims/runs/{name}/{run_id}/transcripts/{file}")
    async def sim_transcript(name: str, run_id: str, file: str) -> Any:
        return await _sims(sims.get(f"/api/runs/{name}/{run_id}/transcripts/{file}"))

    @app.post("/hub/sims/run")
    async def sim_start(body: SimRunBody) -> Any:
        return await _sims(sims.start(body.scenarios, body.preset, body.repeat, body.modes))

    @app.get("/hub/sims/jobs/{job_id}")
    async def sim_job(job_id: str) -> Any:
        return await _sims(sims.get(f"/api/jobs/{job_id}"))

    @app.post("/hub/sims/jobs/{job_id}/cancel")
    async def sim_cancel(job_id: str) -> Any:
        return await _sims(sims.cancel(job_id))

    # --- Google Calendar sync (opt-in; nothing happens until the shopper connects) -------------

    app.include_router(gcal_routes.router(calendar))

    # --- demo data -----------------------------------------------------------------------------

    @app.post("/hub/demo/reset")
    async def demo_reset() -> dict[str, Any]:
        script = os.environ.get("DEMO_RESET_SCRIPT", "")
        if not script or not Path(script).is_file():
            raise HTTPException(501, "DEMO_RESET_SCRIPT is not configured")
        proc = await asyncio.to_thread(
            subprocess.run, [script], capture_output=True, text=True, timeout=120, check=False)
        if proc.returncode != 0:
            raise HTTPException(500, (proc.stderr or proc.stdout)[-800:])
        return {"ok": True, "output": proc.stdout[-1500:]}

    # --- pantry-api proxy and the SPA ----------------------------------------------------------

    proxy = httpx.AsyncClient(base_url=settings.pantry_api_url, timeout=180)

    @app.api_route("/pantry/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def pantry_proxy(path: str, request: Request) -> Response:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
        try:
            # multi_items keeps repeated keys (?exclude_origin=A&exclude_origin=B).
            upstream = await proxy.request(request.method, f"/{path}",
                                           params=list(request.query_params.multi_items()),
                                           headers=headers,
                                           content=await request.body())
        except httpx.HTTPError as exc:
            return JSONResponse({"detail": f"pantry-api is not reachable ({exc})"},
                                status_code=502)
        out = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP}
        return Response(upstream.content, status_code=upstream.status_code, headers=out)

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse("/pantry/")

    if settings.spa_dist and Path(settings.spa_dist).is_dir():
        app.mount("/pantry", SpaFiles(directory=settings.spa_dist, html=True), name="spa")

    return app


def _reviewed_doc(raw: dict[str, Any]) -> RecipeDoc:
    """ChatBody.recipe_doc checked before the turn starts: 413 over 64 KB, 422 when it is not a
    RecipeDoc or a line is still unconfirmed (planning it would plan what nobody reviewed)."""
    if len(json.dumps(raw, ensure_ascii=False).encode("utf-8")) > MAX_RECIPE_DOC:
        raise HTTPException(413, f"recipe_doc is over {MAX_RECIPE_DOC // 1000} KB")
    try:
        doc = RecipeDoc.model_validate(raw)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False)
        raise HTTPException(422, {"code": "bad_recipe_doc",
                                  "message": "The recipe cannot be planned as it stands ("
                                  + problem(errors[0]) + "). Correct it in the import sheet "
                                  "and try again.",
                                  "errors": errors}) from exc
    if not doc.lines:
        raise HTTPException(422, {"code": "no_lines", "message": "The recipe has no lines."})
    unconfirmed = [ln.line_no for ln in doc.lines if not ln.confirmed]
    if unconfirmed:
        raise HTTPException(422, {"code": "unconfirmed_lines", "lines": unconfirmed,
                                  "message": "Confirm or remove these lines before planning."})
    return doc


def _strip(probe_result: dict[str, Any]) -> dict[str, Any]:
    return {"ok": probe_result["ok"], "status": probe_result["status"],
            "detail": probe_result.get("body") if isinstance(probe_result.get("body"), dict)
            else None}


async def _resolve_public(targets: Targets, target_id: str) -> dict[str, Any]:
    try:
        return (await targets.resolve(target_id)).public()
    except McpTargetError as exc:
        raise HTTPException(exc.status if 400 <= exc.status < 600 else 502, str(exc)) from exc


def main() -> None:  # pragma: no cover - the console entry point
    import uvicorn

    settings = Settings.from_env()
    config = uvicorn.Config(create_app(settings), host=os.environ.get("HUB_HOST", "127.0.0.1"),
                            port=settings.hub_port)
    # after uvicorn has set up its loggers: the access log never shows an OAuth code or state
    redact.install()
    uvicorn.Server(config).run()


if __name__ == "__main__":  # pragma: no cover
    main()
