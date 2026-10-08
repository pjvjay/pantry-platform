"""The hub's guard: only this console, on this machine, can change anything through the hub.

The hub listens on loopback and holds every secret (the pantry bearer token, the ContextForge
JWT, the Gemini key), so any web page the shopper opens could otherwise talk to it:

* **DNS rebinding.** A page on ``attacker.test`` re-points its own name at 127.0.0.1 and then
  reads the hub as "same origin": traces, status, conversations. Every request's ``Host`` must
  be one the hub answers to: ``127.0.0.1``, ``localhost`` or ``[::1]`` on the hub's port, plus
  ``HUB_ALLOWED_HOSTS`` (default ``localhost:5173``, Vite's dev proxy, which keeps the browser's
  Host). A rebound request carries the attacker's name in Host, so it is refused.
* **Cross-site requests.** A page on another site can POST a form or a ``text/plain`` fetch to
  127.0.0.1 without asking (no CORS preflight). Every non-GET ``/hub/*`` and ``/pantry/api/*``
  request must therefore also send ``X-Pantry-Console: 1`` and ``Content-Type:
  application/json``: neither can be sent across sites without a preflight, and the hub answers
  no preflight. An ``Origin``, when the browser sends one, must be this console's.

The one exception is the browser's telemetry beacon (``navigator.sendBeacon`` cannot set a
header): ``POST /hub/telemetry`` without the header passes only with an allowed ``Origin``, which
a page on another site cannot forge, and JSON.

Refusals are 403 ``{"reason": ...}`` (415 for a body that is not JSON). See docs/hub-security.md.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

SAFE_METHODS = frozenset({"GET", "HEAD"})
GUARDED_PREFIXES = ("/hub/", "/pantry/api/")
CONSOLE_HEADER = "x-pantry-console"
# Routes a header-less browser beacon may post to, with an allowed Origin.
BEACON_PATHS = frozenset({"/hub/telemetry"})


def allowed_hosts(port: int, extra: Iterable[str] = ()) -> frozenset[str]:
    """The Host values the hub answers to: loopback on its own port, and ``extra``."""
    own = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    return frozenset(own | {h.strip().lower() for h in extra if h.strip()})


def allowed_origins(hosts: Iterable[str]) -> frozenset[str]:
    return frozenset(f"{scheme}://{h}" for h in hosts for scheme in ("http", "https"))


def refusal(method: str, path: str, headers: Mapping[str, str], hosts: frozenset[str],
            origins: frozenset[str]) -> tuple[int, str] | None:
    """(status, reason) when the request must be refused, None when it may pass. ``headers``
    are keyed in lower case."""
    host = headers.get("host", "").strip().lower()
    if host not in hosts:
        return 403, (f"Host {host or '(none)'} is not one this hub answers to; if it is yours, "
                     "add it to HUB_ALLOWED_HOSTS")
    if method.upper() in SAFE_METHODS or not path.startswith(GUARDED_PREFIXES):
        return None
    origin = headers.get("origin")
    if origin is not None and origin.strip().lower() not in origins:
        return 403, f"Origin {origin} is not this console"
    if headers.get(CONSOLE_HEADER, "").strip() != "1":
        beacon = path in BEACON_PATHS and origin is not None
        if not beacon:
            return 403, ("a request that changes something must come from the console: send "
                         "X-Pantry-Console: 1")
    media = headers.get("content-type", "").split(";")[0].strip().lower()
    if media != "application/json":
        return 415, "send the body as JSON (Content-Type: application/json)"
    return None


class Guard:
    """ASGI middleware applying ``refusal`` to every HTTP request before any route sees it. A
    plain ASGI wrapper rather than an ``@app.middleware`` function, so streamed answers (the
    Assistant's server-sent events) pass through untouched."""

    def __init__(self, app: ASGIApp, *, port: int, extra_hosts: Iterable[str] = ()) -> None:
        self.app = app
        self.hosts = allowed_hosts(port, extra_hosts)
        self.origins = allowed_origins(self.hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers") or []}
        refused = refusal(str(scope.get("method", "GET")), str(scope.get("path", "")), headers,
                          self.hosts, self.origins)
        if refused is None:
            await self.app(scope, receive, send)
            return
        status, reason = refused
        if scope["type"] == "websocket":       # none today; refuse the handshake all the same
            await send({"type": "websocket.close", "code": 1008})
            return
        response: Any = JSONResponse({"reason": reason}, status_code=status)
        await response(scope, receive, send)
