"""The MCP endpoints the hub can talk to, and the four things it does with them: list the
catalog, call a tool, read a resource, render a prompt.

Targets:

* ``pantry``: pantry's own ``/mcp`` with the hub's bearer token, so the write tools work and
  every submission is labelled with the hub's token label.
* ``pantry-anon``: the same endpoint with no token. With ``MCP_AUTH_TOKENS`` set, pantry
  answers 401, which is the point: it shows the endpoint is not open.
* ``gateway-sim`` / ``gateway-recipes``: ContextForge virtual servers federating pantry (all
  15 tools, prefixed ``pantry-``) and pantry's read-only tools plus ``fetch``. Their ids are
  looked up by name, so re-registering them needs no config change.

Every operation opens a fresh session and closes it: pantry is stateless and ContextForge is
cheap to reconnect to, and a demo never needs a long-lived session outside the agent loop.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from demo_hub.settings import Settings

GATEWAY_SERVERS = {"gateway-sim": "pantry-sim", "gateway-recipes": "pantry-recipes"}
_SERVER_ID_TTL_S = 60.0
TEXT_LIMIT = 60_000


class McpTargetError(RuntimeError):
    """A target that cannot be reached or refused the request; ``status`` is the HTTP status
    when the endpoint answered one (401, 404, 429...)."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Target:
    id: str
    label: str
    description: str
    auth: str
    url: str = ""
    token: str = ""

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "description": self.description,
                "auth": self.auth, "url": self.url}


class Targets:
    """Resolves target ids to URLs and credentials; caches ContextForge server ids."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client
        self._server_ids: dict[str, tuple[float, str]] = {}

    async def _gateway_url(self, server_name: str) -> str:
        cached = self._server_ids.get(server_name)
        if cached and time.monotonic() - cached[0] < _SERVER_ID_TTL_S:
            return cached[1]
        if not self.settings.contextforge_jwt:
            raise McpTargetError("no ContextForge JWT configured (CF_JWT_FILE)")
        client = self._client or httpx.AsyncClient(timeout=10)
        try:
            response = await client.get(
                f"{self.settings.contextforge_url}/servers",
                headers={"Authorization": f"Bearer {self.settings.contextforge_jwt}"},
            )
        except httpx.HTTPError as exc:
            raise McpTargetError(f"cannot reach ContextForge: {exc}") from exc
        finally:
            if self._client is None:
                await client.aclose()
        if response.status_code >= 400:
            raise McpTargetError(
                f"ContextForge answered {response.status_code} listing servers",
                response.status_code,
            )
        data = response.json()
        items = data if isinstance(data, list) else data.get("servers", data.get("items", []))
        found = [s.get("id") for s in items if s.get("name") == server_name]
        if not found:
            raise McpTargetError(f"no ContextForge virtual server named {server_name!r}", 404)
        url = f"{self.settings.contextforge_url}/servers/{found[0]}/mcp"
        self._server_ids[server_name] = (time.monotonic(), url)
        return url

    def catalog_of_targets(self) -> list[Target]:
        s = self.settings
        pantry_url = f"{s.pantry_api_url}/mcp"
        return [
            Target("pantry", "pantry, direct (bearer token)",
                   "pantry-api's own MCP endpoint, authenticated with the hub's token: all 15 "
                   "tools, write tools included.",
                   f"Bearer token ({s.pantry_token_label})", pantry_url, s.pantry_mcp_token),
            Target("pantry-anon", "pantry, direct (no token)",
                   "The same endpoint with no credentials: pantry refuses it with 401.",
                   "none", pantry_url),
            Target("gateway-sim", "ContextForge: pantry-sim",
                   "pantry federated through the ContextForge gateway: every tool, renamed "
                   "pantry-<tool>.", "ContextForge JWT"),
            Target("gateway-recipes", "ContextForge: pantry-recipes",
                   "pantry's read-only tools plus the fetch server, so an agent can read a "
                   "recipe page but cannot write.", "ContextForge JWT"),
        ]

    async def resolve(self, target_id: str) -> Target:
        for target in self.catalog_of_targets():
            if target.id != target_id:
                continue
            if target_id in GATEWAY_SERVERS:
                url = await self._gateway_url(GATEWAY_SERVERS[target_id])
                return Target(target.id, target.label, target.description, target.auth, url,
                              self.settings.contextforge_jwt)
            return target
        raise McpTargetError(f"unknown target {target_id!r}", 404)


class _HttpStatus:
    """Records the last non-2xx answer to a POST; the SDK drops status and body."""

    def __init__(self) -> None:
        self.status = 0
        self.body = ""

    async def on_response(self, response: Any) -> None:
        if response.status_code < 400 or response.request.method != "POST":
            return
        try:
            raw = await response.aread()
            self.body = " ".join(raw.decode("utf-8", "replace").split())[:300]
        except Exception:  # noqa: BLE001 - the status alone is still worth reporting
            self.body = ""
        self.status = response.status_code


@asynccontextmanager
async def open_session(target: Target) -> AsyncIterator[ClientSession]:
    """An initialised MCP session on ``target``; HTTP refusals become :class:`McpTargetError`."""
    headers = {"Authorization": f"Bearer {target.token}"} if target.token else None
    status = _HttpStatus()
    opened = False
    try:
        async with create_mcp_http_client(headers=headers) as http:
            http.event_hooks = {"request": [], "response": [status.on_response]}
            async with (
                streamable_http_client(target.url, http_client=http) as streams,
                ClientSession(streams[0], streams[1]) as session,
            ):
                await session.initialize()
                opened = True
                yield session
    except McpTargetError:
        raise
    except Exception as exc:
        if opened and not status.status:
            # The SDK's task group wraps the caller's own exception; hand back the original.
            leaf = _sole(exc)
            if leaf is exc:
                raise
            raise leaf from exc
        if status.status:
            raise McpTargetError(
                f"HTTP {status.status} from {target.url}"
                + (f": {status.body}" if status.body else ""), status.status) from exc
        raise McpTargetError(f"cannot reach {target.url}: {_leaf(exc)}") from exc


def _sole(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


def _leaf(exc: BaseException) -> str:
    exc = _sole(exc)
    return f"{type(exc).__name__}: {exc}"


def _dump(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


async def _all_pages(fetch: Any, key: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = None
    for _ in range(20):
        params = mcp_types.PaginatedRequestParams(cursor=cursor) if cursor else None
        page = await fetch(params=params)
        items += [_dump(item) for item in getattr(page, key)]
        cursor = getattr(page, "next_cursor", None)
        if not cursor:
            break
    return items


async def catalog(session: ClientSession) -> dict[str, Any]:
    """Tools, resources, resource templates and prompts; a list the server does not support is
    reported as empty with a note rather than failing the whole catalog."""
    out: dict[str, Any] = {"notes": []}
    for key, method, attr in (
        ("tools", session.list_tools, "tools"),
        ("resources", session.list_resources, "resources"),
        ("resource_templates", session.list_resource_templates, "resource_templates"),
        ("prompts", session.list_prompts, "prompts"),
    ):
        try:
            out[key] = await _all_pages(method, attr)
        except Exception as exc:  # noqa: BLE001 - one unsupported list must not hide the rest
            out[key] = []
            out["notes"].append(f"{key}: {_leaf(exc)}")
    return out


def _content_text(content: list[Any]) -> str:
    parts = []
    for block in content:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(_dump(block)))
    return "\n".join(parts)


async def call_tool(session: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    result = await session.call_tool(name, arguments)
    ms = round((time.perf_counter() - started) * 1000, 1)
    if not isinstance(result, mcp_types.CallToolResult):
        return {"name": name, "is_error": True, "structured": None, "ms": ms, "truncated": False,
                "text": f"unexpected result type {type(result).__name__}"}
    text = _content_text(list(result.content))
    return {
        "name": name,
        "is_error": bool(result.is_error),
        "structured": result.structured_content,
        "text": text[:TEXT_LIMIT],
        "truncated": len(text) > TEXT_LIMIT,
        "ms": ms,
    }


async def read_resource(session: ClientSession, uri: str) -> dict[str, Any]:
    result = await session.read_resource(uri)
    if not isinstance(result, mcp_types.ReadResourceResult):
        raise McpTargetError(f"unexpected result type {type(result).__name__}")
    return {"uri": uri, "contents": [_dump(c) for c in result.contents]}


async def get_prompt(
    session: ClientSession, name: str, arguments: dict[str, str] | None
) -> dict[str, Any]:
    result = await session.get_prompt(name, arguments or {})
    if not isinstance(result, mcp_types.GetPromptResult):
        raise McpTargetError(f"unexpected result type {type(result).__name__}")
    return _dump(result)
