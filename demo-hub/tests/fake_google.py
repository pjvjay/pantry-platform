"""FakeGoogle: Google's OAuth and Calendar API as the calendar sync sees them, on an
httpx.MockTransport, with state and fault injection. No test ever calls Google.

* OAuth: an authorization code bound to the PKCE challenge and redirect URI it was issued for
  (``authorize`` plays the consent page), the code exchange (verifier, client id and secret
  checked; the scope granted and whether a refresh token comes back are settable), refresh
  (``invalid_grant`` on demand) and revoke (which can fail).
* Calendar: calendars insert, get and delete; events list (privateExtendedProperty filter,
  showDeleted, pagination), insert (409 for an id already used, cancelled ones included), get,
  update (If-Match, 412) and delete (If-Match, a cancelled tombstone). Every write must carry
  sendUpdates=none and no attendees.
* The shopper in Google: ``user_edit``, ``user_delete``, ``user_add``.
* Faults: ``fail(...)`` answers the next matching requests with a status (429 with Retry-After,
  403 rate or quota, 503); ``crash_after_writes = N`` applies the Nth write and then raises
  Crash instead of answering, like a hub killed mid-apply.

FakePantry is pantry-api's POST /calendar/preview for a small schedule: one all-day event per
trip, cook and reminder, in the shape pantry-api sends.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import itertools
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

SCOPE = "https://www.googleapis.com/auth/calendar.app.created"
CLIENT_ID = "fake-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "fake-client-secret"
REGISTERED = ("http://127.0.0.1:8090/hub/calendar/oauth/callback",
              "http://localhost:5173/hub/calendar/oauth/callback")


class Crash(Exception):
    """The hub process 'dies' after Google applied a write and before it heard back."""


@dataclass
class Fault:
    method: str
    path_re: str
    status: int
    reason: str = ""
    retry_after: str | None = None
    times: int = 1


def client_json(kind: str = "web", redirect_uris: tuple[str, ...] = REGISTERED) -> str:
    return json.dumps({kind: {
        "client_id": CLIENT_ID, "project_id": "fake-project",
        "auth_uri": "https://accounts.fake/o/oauth2/auth", "token_uri": "https://oauth2.fake/token",
        "client_secret": CLIENT_SECRET, "redirect_uris": list(redirect_uris)}})


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _error(status: int, reason: str, message: str = "",
           headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers, json={"error": {
        "code": status, "message": message or reason,
        "errors": [{"domain": "calendar", "reason": reason, "message": message or reason}]}})


@dataclass
class FakeGoogle:
    AUTH_URL = "https://accounts.fake/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.fake/token"
    REVOKE_URL = "https://oauth2.fake/revoke"
    API = "https://calendar.fake/calendar/v3"

    page_size: int = 2
    grant_scope: str = SCOPE
    send_refresh_token: bool = True
    invalid_grant: bool = False
    revoke_fails: bool = False
    refuse_restore: bool = False
    crash_after_writes: int | None = None
    codes: dict[str, dict[str, str]] = field(default_factory=dict)
    refresh_tokens: set[str] = field(default_factory=set)
    access_tokens: set[str] = field(default_factory=set)
    revoked: list[str] = field(default_factory=list)
    calendars: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    faults: list[Fault] = field(default_factory=list)
    writes: int = 0
    token_calls: int = 0

    def __post_init__(self) -> None:
        self._n = itertools.count(1)
        self.transport = httpx.MockTransport(self.handle)

    # the consent page and the shopper's own edits

    def authorize(self, auth_url: str, *, error: str | None = None) -> dict[str, str]:
        """What Google's consent page sends back: {code, state} (or {error, state})."""
        q = {k: v[0] for k, v in parse_qs(urlsplit(auth_url).query).items()}
        if error:
            return {"error": error, "state": q["state"]}
        code = f"code-{next(self._n)}"
        self.codes[code] = {"challenge": q["code_challenge"], "redirect_uri": q["redirect_uri"],
                            "client_id": q["client_id"]}
        return {"code": code, "state": q["state"]}

    def user_edit(self, calendar_id: str, event_id: str, **fields: Any) -> None:
        ev = self.events[calendar_id][event_id]
        ev.update(fields)
        ev["etag"] = self._etag()

    def user_delete(self, calendar_id: str, event_id: str) -> None:
        ev = self.events[calendar_id][event_id]
        ev["status"], ev["etag"] = "cancelled", self._etag()

    def user_add(self, calendar_id: str, event: dict[str, Any]) -> None:
        self.events[calendar_id][event["id"]] = {**event, "etag": self._etag(),
                                                 "status": "confirmed"}

    def fail(self, method: str, path_re: str, status: int, reason: str = "",
             retry_after: str | None = None, times: int = 1) -> None:
        self.faults.append(Fault(method, path_re, status, reason, retry_after, times))

    def expire_access_tokens(self) -> None:
        self.access_tokens.clear()

    # views for the tests

    @property
    def calendar_id(self) -> str:
        assert len(self.calendars) == 1, self.calendars
        return next(iter(self.calendars))

    def live(self, calendar_id: str | None = None) -> list[dict[str, Any]]:
        cal = calendar_id or self.calendar_id
        return [e for e in self.events.get(cal, {}).values() if e.get("status") != "cancelled"]

    def by_item(self, calendar_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for e in self.live(calendar_id):
            item = e.get("extendedProperties", {}).get("private", {}).get("pantry_item", "")
            out.setdefault(item, []).append(e)
        return out

    def calls(self, method: str, path_re: str = "") -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method
                and re.search(path_re, r.url.path)]

    # the transport

    def _etag(self) -> str:
        return f'"{next(self._n)}"'

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for fault in self.faults:
            if fault.times > 0 and fault.method == request.method \
                    and re.search(fault.path_re, request.url.path):
                fault.times -= 1
                headers = {"Retry-After": fault.retry_after} if fault.retry_after else None
                return _error(fault.status, fault.reason or "backendError", headers=headers)
        url = str(request.url)
        if url.startswith(self.TOKEN_URL):
            return self._token(request)
        if url.startswith(self.REVOKE_URL):
            return self._revoke(request)
        if url.startswith(self.API):
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] not in self.access_tokens:
                return _error(401, "authError", "Invalid Credentials")
            write = request.method != "GET"
            response = self._api(request, request.url.path[len(urlsplit(self.API).path):])
            if write and response.status_code < 300:
                self.writes += 1
                if self.crash_after_writes is not None and self.writes >= self.crash_after_writes:
                    self.crash_after_writes = None
                    raise Crash(f"crashed after write {self.writes}")
            return response
        return httpx.Response(404, json={"error": "not a fake Google URL"})

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.token_calls += 1
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return httpx.Response(401, json={"error": "invalid_client"})
        if form.get("grant_type") == "authorization_code":
            issued = self.codes.pop(form.get("code", ""), None)
            if issued is None or issued["redirect_uri"] != form.get("redirect_uri") \
                    or _b64(hashlib.sha256(form.get("code_verifier", "").encode()).digest()) \
                    != issued["challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            access = f"at-{next(self._n)}"
            self.access_tokens.add(access)
            body: dict[str, Any] = {"access_token": access, "expires_in": 3599,
                                    "scope": self.grant_scope, "token_type": "Bearer"}
            if self.send_refresh_token:
                refresh = f"rt-{next(self._n)}"
                self.refresh_tokens.add(refresh)
                body["refresh_token"] = refresh
            return httpx.Response(200, json=body)
        if form.get("grant_type") == "refresh_token":
            if self.invalid_grant or form.get("refresh_token") not in self.refresh_tokens:
                return httpx.Response(400, json={"error": "invalid_grant",
                                                 "error_description": "Token has been expired "
                                                 "or revoked."})
            access = f"at-{next(self._n)}"
            self.access_tokens.add(access)
            return httpx.Response(200, json={"access_token": access, "expires_in": 3599,
                                             "scope": SCOPE, "token_type": "Bearer"})
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _revoke(self, request: httpx.Request) -> httpx.Response:
        if self.revoke_fails:
            return httpx.Response(503, json={"error": "unavailable"})
        token = parse_qs(request.content.decode()).get("token", [""])[0]
        self.revoked.append(token)
        self.refresh_tokens.discard(token)
        return httpx.Response(200, json={})

    def _api(self, request: httpx.Request, path: str) -> httpx.Response:
        m = request.method
        if path == "/calendars" and m == "POST":
            body = json.loads(request.content)
            cal = {"id": f"cal{next(self._n)}@group.calendar.fake", "summary": body["summary"],
                   "timeZone": body.get("timeZone"), "etag": self._etag()}
            self.calendars[cal["id"]] = cal
            self.events[cal["id"]] = {}
            return httpx.Response(200, json=cal)
        match = re.fullmatch(r"/calendars/([^/]+)(/events(?:/([^/]+))?)?", path)
        if not match:
            return _error(404, "notFound")
        cal_id, events, event_id = match.group(1), match.group(2), match.group(3)
        if cal_id not in self.calendars:
            return _error(404, "notFound", "Not Found")
        if not events:
            if m == "GET":
                return httpx.Response(200, json=self.calendars[cal_id])
            if m == "DELETE":
                del self.calendars[cal_id]
                del self.events[cal_id]
                return httpx.Response(204)
            return _error(405, "methodNotAllowed")
        store = self.events[cal_id]
        if m != "GET" and request.url.params.get("sendUpdates") != "none":
            return _error(400, "sendUpdatesRequired", "the fake insists on sendUpdates=none")
        if event_id is None:
            if m == "GET":
                return self._list(request, store)
            if m == "POST":
                body = json.loads(request.content)
                if body.get("attendees"):
                    return _error(400, "attendeesNotAllowed")
                eid = body.get("id", "")
                if not re.fullmatch(r"[a-v0-9]{5,1024}", eid):
                    return _error(400, "invalid", "bad event id")
                if eid in store:
                    return _error(409, "duplicate", "The requested identifier already exists.")
                store[eid] = {**body, "status": body.get("status", "confirmed"),
                              "etag": self._etag()}
                return httpx.Response(200, json=store[eid])
            return _error(405, "methodNotAllowed")
        ev = store.get(event_id)
        if ev is None:
            return _error(404, "notFound", "Not Found")
        if_match = request.headers.get("if-match")
        if m == "GET":
            return httpx.Response(200, json=ev)
        if if_match is not None and if_match != ev["etag"]:
            return _error(412, "conditionNotMet", "Precondition Failed")
        if m == "PUT":
            body = json.loads(request.content)
            if body.get("attendees"):
                return _error(400, "attendeesNotAllowed")
            if ev.get("status") == "cancelled" and self.refuse_restore:
                return _error(403, "forbidden", "cannot restore a deleted event")
            store[event_id] = {**body, "id": event_id,
                               "status": body.get("status", "confirmed"), "etag": self._etag()}
            return httpx.Response(200, json=store[event_id])
        if m == "DELETE":
            if ev.get("status") == "cancelled":
                return _error(410, "deleted", "Resource has been deleted")
            ev["status"], ev["etag"] = "cancelled", self._etag()
            return httpx.Response(204)
        return _error(405, "methodNotAllowed")

    def _list(self, request: httpx.Request, store: dict[str, dict[str, Any]]) -> httpx.Response:
        params = request.url.params
        wanted = [p.split("=", 1) for p in params.get_list("privateExtendedProperty")]
        show_deleted = params.get("showDeleted") == "true"
        items = []
        for eid in sorted(store):
            ev = store[eid]
            props = ev.get("extendedProperties", {}).get("private", {})
            if all(props.get(k) == v for k, v in wanted) and \
                    (show_deleted or ev.get("status") != "cancelled"):
                items.append(ev)
        start = int(params.get("pageToken") or 0)
        page = items[start:start + self.page_size]
        body: dict[str, Any] = {"kind": "calendar#events", "items": page}
        if start + self.page_size < len(items):
            body["nextPageToken"] = str(start + self.page_size)
        return httpx.Response(200, json=body)


# --- pantry-api's /calendar/preview -----------------------------------------------------------


def schedule(plan_id: str = "plan-abc", rev: int = 1, start: str = "2026-10-12") -> dict[str, Any]:
    """A small approved schedule: one trip, three cook days, one thaw reminder."""
    d0 = dt.date.fromisoformat(start)

    def day(n: int) -> str:
        return (d0 + dt.timedelta(days=n)).isoformat()

    return {"v": 1, "plan_id": plan_id, "rev": rev, "start_date": start, "days": 7,
            "exportable": True, "blocked": [],
            "trips": [{"item_id": "trip-t1", "date": day(0), "stores": ["Pantry Mart Downtown"],
                       "list_text": "Pantry Mart Downtown\n- 1 x Tomatoes"}],
            "cooks": [{"item_id": f"cook-m{i}", "date": day(i), "title": f"Dinner {i}",
                       "slot": "dinner"} for i in (1, 2, 3)],
            "reminders": [{"item_id": "thaw-m2-7", "date": day(1), "product": "Chicken"}]}


@dataclass
class FakePantry:
    """pantry-api's POST /calendar/preview: all-day events from the schedule, word for word."""
    calls: int = 0
    refuse: tuple[int, dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/calendar/preview", request.url
        self.calls += 1
        if self.refuse:
            return httpx.Response(self.refuse[0], json={"detail": self.refuse[1]})
        body = json.loads(request.content)
        return httpx.Response(200, json=preview(body["schedule"], body.get("include")))


def preview(s: dict[str, Any], include: list[str] | None = None) -> dict[str, Any]:
    include = include or ["trips", "cooks", "reminders"]
    events = []

    def add(item: str, kind: str, date: str, title: str, description: str,
            location: str | None = None) -> None:
        end = (dt.date.fromisoformat(date) + dt.timedelta(days=1)).isoformat()
        events.append({"uid": f"{item}@pantry-planner", "item_id": item, "kind": kind,
                       "date": date, "end": end, "all_day": True, "title": title,
                       "location": location, "description": description,
                       "categories": ["Pantry plan"], "sequence": s.get("rev", 0), "labels": [],
                       "google_url": "https://calendar.google.com/calendar/render?action=TEMPLATE"})

    if "trips" in include:
        for t in s.get("trips", []):
            add(t["item_id"], "trip", t["date"], "Groceries: " + ", ".join(t["stores"]),
                t["list_text"] + "\n\nStore hours: unknown, check before you go.",
                "; ".join(f"{x} (demo store)" for x in t["stores"]))
    if "reminders" in include:
        for r in s.get("reminders", []):
            add(r["item_id"], "thaw", r["date"], f"Thaw: move {r['product']} to the fridge",
                "Source: your setting (thaw reminders: day before).")
    if "cooks" in include:
        for c in s.get("cooks", []):
            add(c["item_id"], "cook", c["date"], f"Cook: {c['title']} ({c['slot']})",
                "Ingredients, as the recipe gives them:\n- (the recipe lists none)")
    events.sort(key=lambda e: (e["date"], e["item_id"]))
    counts = {k: sum(1 for e in events if e["kind"] == k) for k in ("trip", "cook", "freeze",
                                                                     "thaw")}
    return {"calendar_name": "Pantry plan", "all_day": True, "events": events, "counts": counts,
            "filename": f"pantry-plan-{s['start_date']}.ics", "notes": []}


# --- the hub, wired to the fakes --------------------------------------------------------------

TODAY = dt.date(2026, 10, 9)
CONSOLE_ORIGIN = "http://127.0.0.1:8090"


@dataclass
class CalendarEnv:
    """A hub app whose Google and pantry-api are the fakes, with its files in a temp dir."""
    app: Any
    client: Any
    google: FakeGoogle
    pantry: FakePantry
    sleeps: list[float]
    paths: dict[str, Any]

    @property
    def sync(self) -> Any:
        return self.app.state.calendar

    def connect(self, origin: str = CONSOLE_ORIGIN) -> httpx.Response:
        """Connect as the shopper does: the console's POST, Google's consent, the callback."""
        r = self.client.post("/hub/calendar/connect", json={}, headers={"Origin": origin})
        assert r.status_code == 200, r.text
        back = self.google.authorize(r.json()["auth_url"])
        return self.client.get("/hub/calendar/oauth/callback", params=back,
                               follow_redirects=False)

    def preview(self, sched: dict[str, Any], **extra: Any) -> httpx.Response:
        return self.client.post("/hub/calendar/sync/preview", json={"schedule": sched, **extra})

    def apply(self, sched: dict[str, Any], diff: dict[str, Any],
              choices: dict[str, str] | None = None, **extra: Any) -> httpx.Response:
        return self.client.post("/hub/calendar/sync/apply", json={
            "schedule": sched, "preview_token": diff["preview_token"],
            "choices": choices or {}, **extra})

    def sync_now(self, sched: dict[str, Any], choices: dict[str, str] | None = None,
                 **extra: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        """Preview, then apply exactly that preview: (diff, result)."""
        diff = self.preview(sched, **extra)
        assert diff.status_code == 200, diff.text
        result = self.apply(sched, diff.json(), choices, **extra)
        assert result.status_code == 200, result.text
        return diff.json(), result.json()


def make_env(tmp_path: Any, *, kind: str = "web",
             redirect_uris: tuple[str, ...] = REGISTERED, **settings: Any) -> CalendarEnv:
    """The hub with a fake client JSON (mode 600) in a temp dir, never the real one."""
    from demo_hub import app as app_module
    from demo_hub.settings import Settings
    from tests.conftest import console_client

    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(mode=0o700)
    client_file = secrets_dir / "google_oauth_client.json"
    client_file.write_text(client_json(kind, redirect_uris))
    client_file.chmod(0o600)
    paths = {"client": client_file, "token": secrets_dir / "google_calendar_token.json",
             "calendar_dir": tmp_path / "calendar", "ledger": tmp_path / "calendar" / "ledger.json",
             "traces": tmp_path / "traces"}
    google, pantry = FakeGoogle(), FakePantry()
    app = app_module.create_app(Settings(**{
        "pantry_api_url": "http://pantry.test", "traces_dir": str(paths["traces"]),
        "images_dir": str(tmp_path / "images"),
        "google_oauth_client_file": str(client_file), "google_token_file": str(paths["token"]),
        "calendar_dir": str(paths["calendar_dir"]), "google_auth_url": FakeGoogle.AUTH_URL,
        "google_token_url": FakeGoogle.TOKEN_URL, "google_revoke_url": FakeGoogle.REVOKE_URL,
        "google_calendar_url": FakeGoogle.API, **settings}))
    sleeps: list[float] = []
    now = [1000.0]

    async def sleep(seconds: float) -> None:       # never waits; the clock moves instead
        sleeps.append(seconds)
        now[0] += seconds

    sync = app.state.calendar
    sync.google_transport, sync.pantry_transport = google.transport, pantry.transport
    sync.sleep, sync.today, sync.clock = sleep, lambda: TODAY, lambda: now[0]
    return CalendarEnv(app, console_client(app), google, pantry, sleeps, paths)
