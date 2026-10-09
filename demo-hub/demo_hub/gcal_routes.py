"""``/hub/calendar/*``: Google Calendar sync for the approved meal plan (docs/google-calendar.md).

Every POST here is behind guard.py like any other (Host, Origin, X-Pantry-Console, JSON); the
sign-in adds its own checks on top: a state the hub issued, unexpired and unused, bound to the
``pantry_oauth`` cookie. The callback is a GET Google sends the browser to, so it has the Host
check and those, and answers with no-store and no-referrer.

* ``GET  /hub/calendar/status``          booleans and labels only (no token, no client id)
* ``POST /hub/calendar/connect``         {return_to?} -> {auth_url} and the cookie; 409
                                         not_configured | origin_not_registered
* ``GET  /hub/calendar/oauth/callback``  303 to the console (?calendar=connected|denied|error),
                                         or 400 with nothing stored
* ``POST /hub/calendar/sync/preview``    {schedule, include?} -> the diff; no writes
* ``POST /hub/calendar/sync/apply``      {schedule, include?, preview_token, choices} -> results
* ``POST /hub/calendar/disconnect``      {delete_calendar} -> {revoked, calendar_deleted, note}

None of these routes go near the Assistant: the trace store never sees calendar data, and there
is no calendar tool. Every write is the shopper's click on a diff they reviewed.
"""

from __future__ import annotations

import html
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from demo_hub.gcal_oauth import COOKIE, COOKIE_PATH, PENDING_TTL_S, OAuthError, nonce_matches
from demo_hub.gcal_sync import CalendarSync, SyncRefused, schedule_size_ok

Include = Literal["trips", "cooks", "reminders"]
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff"}
CONSOLE = "/pantry/"
DEFAULT_RETURN = "#/mealplan"


class ConnectBody(BaseModel):
    # where the console goes back to after Google: a hash route of this console, nothing else
    return_to: str = Field(default=DEFAULT_RETURN, pattern=r"^#/[A-Za-z0-9/_-]{0,60}$")


class SyncBody(BaseModel):
    schedule: dict[str, Any]
    include: list[Include] = Field(default_factory=lambda: ["trips", "cooks", "reminders"],
                                   min_length=1, max_length=3)


class ApplyBody(SyncBody):
    preview_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    choices: dict[str, Literal["keep", "overwrite", "restore"]] = Field(default_factory=dict,
                                                                        max_length=500)


class DisconnectBody(BaseModel):
    delete_calendar: bool = False


def _refusal(exc: OAuthError | SyncRefused) -> HTTPException:
    if isinstance(exc, OAuthError):
        code = "needs_reconnect" if exc.code == "invalid_grant" else exc.code
        return HTTPException(exc.status, {"code": code, "message": exc.message})
    return HTTPException(exc.status, exc.body())


def _schedule(body: SyncBody) -> dict[str, Any]:
    if not schedule_size_ok(body.schedule):
        raise HTTPException(413, {"code": "too_large",
                                  "message": "The plan is too large to sync in one go."})
    return body.schedule


def _back(return_to: str, outcome: str, reason: str = "") -> RedirectResponse:
    """303 to the console with the outcome in its hash route; the cookie is cleared."""
    query = f"calendar={outcome}" + (f"&reason={reason}" if reason else "")
    joiner = "&" if "?" in return_to else "?"
    response = RedirectResponse(f"{CONSOLE}{return_to}{joiner}{query}", status_code=303,
                                headers=NO_STORE)
    response.delete_cookie(COOKIE, path=COOKIE_PATH, httponly=True, samesite="lax")
    return response


def _bad_callback(message: str) -> HTMLResponse:
    page = ("<!doctype html><meta charset=utf-8><title>Google Calendar</title>"
            f"<p>{html.escape(message)}</p>"
            f'<p><a href="{CONSOLE}{DEFAULT_RETURN}">Back to the console</a></p>')
    response = HTMLResponse(page, status_code=400, headers=NO_STORE)
    response.delete_cookie(COOKIE, path=COOKIE_PATH, httponly=True, samesite="lax")
    return response


_REASON_WORDS = frozenset({"access_denied", "scope_not_granted", "no_refresh_token",
                           "exchange_failed", "not_configured", "google_unavailable"})


def router(sync: CalendarSync) -> APIRouter:
    r = APIRouter()

    @r.get("/hub/calendar/status")
    async def calendar_status(request: Request) -> JSONResponse:
        return JSONResponse(sync.status(request.headers.get("host", "")), headers=NO_STORE)

    @r.post("/hub/calendar/connect")
    async def calendar_connect(body: ConnectBody, request: Request) -> JSONResponse:
        """A sign-in to start: Google's consent page for this client, the calendar scope only,
        PKCE S256, offline access, and the state cookie. Nothing is written to Google."""
        try:
            url, nonce = sync.connect(request.headers.get("origin"), body.return_to)
        except OAuthError as exc:
            raise _refusal(exc) from exc
        response = JSONResponse({"auth_url": url}, headers=NO_STORE)
        response.set_cookie(COOKIE, nonce, max_age=PENDING_TTL_S, path=COOKIE_PATH,
                            httponly=True, samesite="lax")
        return response

    @r.get("/hub/calendar/oauth/callback")
    async def calendar_callback(request: Request, state: str = "", code: str = "",
                                error: str = "") -> Response:
        pending = sync.pending.pop(state[:200])
        if pending is None or not nonce_matches(request.cookies.get(COOKIE), pending):
            return _bad_callback("This Google sign-in cannot be finished here: it expired, was "
                                 "already used, or was started in another browser. Nothing was "
                                 "stored. Start again from the console.")
        if error:
            word = error if error in _REASON_WORDS else "google_error"
            return _back(pending.return_to, "denied" if error == "access_denied" else "error",
                         word)
        if not code:
            return _bad_callback("Google sent no sign-in code. Nothing was stored.")
        try:
            await sync.finish(code[:2000], pending.code_verifier, pending.redirect_uri)
        except OAuthError as exc:
            word = exc.code if exc.code in _REASON_WORDS else "error"
            return _back(pending.return_to, "error", word)
        return _back(pending.return_to, "connected")

    @r.post("/hub/calendar/sync/preview")
    async def calendar_preview(body: SyncBody) -> dict[str, Any]:
        """What a sync would do, operation by operation. Writes nothing anywhere."""
        try:
            return await sync.preview(_schedule(body), list(body.include))
        except (OAuthError, SyncRefused) as exc:
            raise _refusal(exc) from exc

    @r.post("/hub/calendar/sync/apply")
    async def calendar_apply(body: ApplyBody) -> dict[str, Any]:
        """The reviewed diff, written: only when it is still exactly the one reviewed."""
        try:
            return await sync.apply(_schedule(body), list(body.include), body.preview_token,
                                    dict(body.choices))
        except (OAuthError, SyncRefused) as exc:
            raise _refusal(exc) from exc

    @r.post("/hub/calendar/disconnect")
    async def calendar_disconnect(body: DisconnectBody) -> dict[str, Any]:
        try:
            return await sync.disconnect(body.delete_calendar)
        except (OAuthError, SyncRefused) as exc:
            raise _refusal(exc) from exc

    return r
