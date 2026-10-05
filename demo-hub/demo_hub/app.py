"""The hub's HTTP surface. One origin for the whole demo:

* ``/pantry/``            the grocery app (pantry-frontend's built SPA, from ``SPA_DIST``)
* ``/pantry/api/*``       pantry-api, proxied (REST, and its ``/mcp`` endpoint)
* ``/hub/status``         every service's health, versions and links
* ``/hub/mcp/*``          the MCP explorer: targets, catalog, call a tool, read, prompt
* ``/hub/agent/*``        the Assistant: options, and a chat turn streamed as server-sent events
* ``/hub/sims/*``         the mcp-sim runner: scenarios, runs, start and follow jobs
* ``/hub/demo/reset``     reseed pantry's database and reload the demo origin evidence

Secrets (the pantry bearer token, the ContextForge JWT, the Gemini key) stay in this process;
the browser only ever talks to the hub.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from demo_hub import mcp_targets
from demo_hub.agent import AGENT_TARGETS, Agent
from demo_hub.llm import MODEL_CHOICES, ChatClient, LLMError
from demo_hub.mcp_targets import McpTargetError, Targets, open_session
from demo_hub.settings import Settings
from demo_hub.sims import PRESETS, SimsClient, SimsError

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
    agent = Agent(settings, targets, ChatClient(settings))
    sims = SimsClient(settings)
    app.state.settings, app.state.agent, app.state.targets = settings, agent, targets

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
                     "contextforge_jwt": bool(s.contextforge_jwt)},
            "agent": {"default_model": s.default_agent_model},
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

    @app.post("/hub/agent/chat")
    async def agent_chat(body: ChatBody) -> StreamingResponse:
        try:
            conv = agent.conversation(body.conversation_id, body.model
                                      or settings.default_agent_model, body.target,
                                      body.disclosure)
        except LLMError as exc:
            raise HTTPException(422, str(exc)) from exc

        async def events() -> AsyncIterator[bytes]:
            async for event in agent.run(conv, body.message):
                yield f"data: {json.dumps(event, default=str)}\n\n".encode()

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.delete("/hub/agent/conversations/{conversation_id}")
    async def agent_forget(conversation_id: str) -> dict[str, bool]:
        return {"forgotten": agent.conversations.pop(conversation_id, None) is not None}

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

    uvicorn.run(create_app(), host=os.environ.get("HUB_HOST", "127.0.0.1"),
                port=int(os.environ.get("HUB_PORT", "8090")))


if __name__ == "__main__":  # pragma: no cover
    main()
