"""The approved meal plan, synced into a "Pantry plan" calendar the hub creates in the shopper's
Google account: previewed as a diff, applied only as previewed (docs/google-calendar.md).

**Where the events come from.** pantry-api's ``POST /calendar/preview`` builds every event (the
one builder, PLAN.md C7): all-day, with title, description and location. The hub turns each into
a Google event body here and adds only what the sync needs: a deterministic id, private
properties naming the plan and item, and a content hash. Nothing in an event is written by the
hub itself, and no event ever has attendees.

**Identity.** An event's id is ``pp1`` + the first 40 hex of sha256("<plan>/<item>/<gen>"), so
the same item always maps to the same event and a crashed or repeated apply cannot duplicate it.
``gen`` starts at 0 and is bumped only when Google refuses to restore a deleted event.

**The ledger** (``~/.pantry-demo/calendar/ledger.json``, dir 0700, file 0600, no secrets) keeps
the calendar id and, per plan and item, the event id, gen, the etag and content hash of what the
hub last wrote. It is a cache: Google, read by private property, is the authority, so a lost
ledger finds the same events again.

**The diff.** Each planned item is one of: create (not in Google), noop (as planned), update
(changed in the plan, untouched in Google), conflict (edited in Google: Keep by default, or
Overwrite), deleted_in_google (deleted there: restored only when asked), skip (in the past).
An event of this plan that is no longer planned is a delete (a conflict when it was edited in
Google). ``preview_token`` hashes the plan, its revision and every operation; apply recomputes
the diff and refuses (409 preview_stale) unless it is exactly the one the shopper reviewed.

**Apply** runs one at a time under a lock (409 sync_in_progress), creates the calendar on first
use, writes each operation with If-Match where an event exists and ``sendUpdates=none``, and saves
the ledger after each success. A 412 is reported as an edit made in Google, never overwritten.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from demo_hub.gcal_client import (
    BadRequest,
    CalendarClient,
    Duplicate,
    Forbidden,
    GoogleError,
    NotFound,
    PreconditionFailed,
    QuotaExceeded,
    RateLimited,
    Unauthorized,
    Unavailable,
)
from demo_hub.gcal_oauth import (
    CALLBACK_PATH,
    TESTING_NOTE,
    Connection,
    Connector,
    GoogleOAuth,
    OAuthError,
    PendingAuths,
    auth_url,
    new_connection,
    write_private,
)
from demo_hub.settings import Settings

CALENDAR_SUMMARY = "Pantry plan"
CALENDAR_ZONE = "America/Vancouver"
CALENDAR_DESCRIPTION = ("Made by the pantry demo hub from your approved meal plan. The hub "
                        "changes only events it created here.")
MAX_SCHEDULE_BYTES = 512 * 1024
PROPERTY_VERSION = "1"

OpKind = Literal["create", "update", "delete", "conflict", "deleted_in_google", "noop", "skip"]
Choice = Literal["keep", "overwrite", "restore"]
OP_ORDER: tuple[OpKind, ...] = ("create", "update", "delete", "conflict", "deleted_in_google",
                                "noop", "skip")


class SyncRefused(Exception):
    """A preview or apply the hub refuses: REST {code, message}."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def body(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


# --- events -----------------------------------------------------------------------------------


def plan_key(schedule: dict[str, Any]) -> str:
    """The plan's identity, as pantry-api's UIDs use it: its id, or its start date."""
    pid = str(schedule.get("plan_id") or "")
    return pid or f"plan-{schedule.get('start_date')}"


def event_id(key: str, item_id: str, gen: int) -> str:
    """Google wants 5-1024 characters of base32hex (0-9, a-v): 'pp1' and 40 hex digits are."""
    return "pp1" + hashlib.sha256(f"{key}/{item_id}/{gen}".encode()).hexdigest()[:40]


def _when(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    if value.get("date"):
        return {"date": str(value["date"])}
    return {"dateTime": str(value.get("dateTime", ""))}


def _content(event: dict[str, Any]) -> dict[str, Any]:
    """The fields the plan decides, as Google reports them."""
    return {"summary": event.get("summary", ""), "description": event.get("description", ""),
            "location": event.get("location", ""), "start": _when(event.get("start")),
            "end": _when(event.get("end")),
            "transparency": event.get("transparency", "opaque")}


def content_hash(event: dict[str, Any]) -> str:
    canon = json.dumps(_content(event), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def private(event: dict[str, Any]) -> dict[str, str]:
    props = (event.get("extendedProperties") or {}).get("private") or {}
    return props if isinstance(props, dict) else {}


def gen_of(event: dict[str, Any]) -> int:
    try:
        return int(private(event).get("pantry_gen", "0"))
    except ValueError:
        return 0


def google_body(ev: dict[str, Any], key: str, rev: int, gen: int) -> dict[str, Any]:
    """A Google event for one of pantry-api's events, word for word. All-day (start and end
    dates, the end exclusive, as the .ics), free time, no reminders (the .ics has none either),
    no attendees."""
    body: dict[str, Any] = {
        "id": event_id(key, ev["item_id"], gen),
        "summary": ev["title"],
        "description": ev.get("description", ""),
        "start": {"date": ev["date"]},
        "end": {"date": ev["end"]},
        "transparency": "transparent",
        "status": "confirmed",
        "reminders": {"useDefault": False},
    }
    if ev.get("location"):
        body["location"] = ev["location"]
    body["extendedProperties"] = {"private": {
        "pantry_v": PROPERTY_VERSION, "pantry_schedule": key, "pantry_item": ev["item_id"],
        "pantry_kind": str(ev.get("kind", "")), "pantry_rev": str(rev), "pantry_gen": str(gen),
        "pantry_hash": content_hash(body)}}
    return body


_FIELD_WORDS = (("summary", "title"), ("start", "date"), ("end", "date"),
                ("description", "description"), ("location", "location"),
                ("transparency", "busy or free"))


def changed_fields(desired: dict[str, Any], remote: dict[str, Any]) -> list[str]:
    a, b = _content(desired), _content(remote)
    out: list[str] = []
    for key, word in _FIELD_WORDS:
        if a[key] != b[key] and word not in out:
            out.append(word)
    return out


# --- the ledger -------------------------------------------------------------------------------


def _empty() -> dict[str, Any]:
    return {"v": 1, "connection_id": None, "calendar": None, "schedules": {}, "last_sync": None}


class Ledger:
    """What the hub last wrote, per plan and item. Missing or unreadable is simply empty."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.data = _empty()
        if path is not None and path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("v") == 1:
                    self.data = {**_empty(), **raw}
            except (OSError, ValueError):
                pass

    def save(self) -> None:
        if self.path is not None:
            write_private(self.path, json.dumps(self.data, indent=1, sort_keys=True) + "\n")

    @property
    def calendar_id(self) -> str | None:
        cal = self.data.get("calendar") or {}
        return cal.get("id") if isinstance(cal, dict) else None

    def set_calendar(self, calendar_id: str | None, summary: str = CALENDAR_SUMMARY) -> None:
        self.data["calendar"] = {"id": calendar_id, "summary": summary} if calendar_id else None

    def entries(self, key: str) -> dict[str, dict[str, Any]]:
        sched = self.data["schedules"].setdefault(key, {"synced_rev": None, "events": {}})
        return sched.setdefault("events", {})

    def reset(self) -> None:
        self.data = _empty()


# --- the diff ---------------------------------------------------------------------------------


@dataclass
class Op:
    item_id: str
    op: OpKind
    kind: str
    title: str
    date: str
    event_id: str | None = None
    etag: str | None = None              # the Google event's etag when the diff was made
    hash: str | None = None              # the planned content's hash
    gen: int = 0
    origin: Literal["", "update", "delete"] = ""     # what a conflict would have been
    unverified: bool = False             # no ledger entry, so edits in Google could not be checked
    adopt: bool = False                  # a noop the ledger did not know yet
    changes: list[str] = field(default_factory=list)
    note: str = ""
    source: dict[str, Any] | None = None  # pantry-api's event
    body: dict[str, Any] | None = None    # the Google body to write

    def token_part(self) -> list[Any]:
        return [self.item_id, self.op, self.event_id or "", self.etag or "", self.hash or "",
                self.origin, self.unverified]

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"item_id": self.item_id, "op": self.op, "kind": self.kind,
                               "title": self.title, "date": self.date, "changes": self.changes}
        if self.origin:
            out["origin"] = self.origin
        if self.unverified:
            out["unverified"] = True
        if self.note:
            out["note"] = self.note
        return out


@dataclass
class Diff:
    key: str
    rev: int
    calendar_id: str | None
    ops: list[Op]
    token: str = ""

    def public(self) -> dict[str, Any]:
        counts = {k: sum(1 for o in self.ops if o.op == k) for k in OP_ORDER}
        return {"preview_token": self.token,
                "calendar_action": "existing" if self.calendar_id else "create",
                "calendar": {"summary": CALENDAR_SUMMARY}, "plan": self.key, "rev": self.rev,
                "counts": counts, "ops": [o.public() for o in self.ops]}


def _remote_date(event: dict[str, Any]) -> str:
    start = event.get("start") or {}
    return str(start.get("date") or str(start.get("dateTime", ""))[:10])


def _pick(events: list[dict[str, Any]], led: dict[str, Any] | None) -> dict[str, Any] | None:
    """The one Google event for an item: the ledger's when live, else a live one (latest gen),
    else the ledger's cancelled one, else the latest."""
    if not events:
        return None
    live = [e for e in events if e.get("status") != "cancelled"]
    want = (led or {}).get("event_id")
    for e in live:
        if e.get("id") == want:
            return e
    if live:
        return max(live, key=gen_of)
    for e in events:
        if e.get("id") == want:
            return e
    return max(events, key=gen_of)


def classify(desired: list[dict[str, Any]], remote: list[dict[str, Any]],
             entries: dict[str, dict[str, Any]], *, key: str, rev: int,
             today: dt.date) -> list[Op]:
    """The operations that bring this plan's events in Google to the plan. No I/O."""
    by_item: dict[str, list[dict[str, Any]]] = {}
    for e in remote:
        item = private(e).get("pantry_item")
        if item and private(e).get("pantry_schedule") == key:
            by_item.setdefault(item, []).append(e)
    first_day = today.isoformat()
    ops: list[Op] = []
    planned: set[str] = set()

    for ev in desired:
        item = str(ev["item_id"])
        planned.add(item)
        led = entries.get(item)
        events = by_item.get(item, [])
        remote_ev = _pick(events, led)
        gen = gen_of(remote_ev) if remote_ev else int((led or {}).get("gen", 0))
        body = google_body(ev, key, rev, gen)
        h = body["extendedProperties"]["private"]["pantry_hash"]
        past = str(ev["date"]) < first_day
        op = Op(item_id=item, op="create", kind=str(ev.get("kind", "")), title=str(ev["title"]),
                date=str(ev["date"]), event_id=body["id"], hash=h, gen=gen, source=ev,
                body=body)
        for extra in events:            # a second live event for one item: ours, and one too many
            if extra is not remote_ev and extra.get("status") != "cancelled":
                ops.append(_removal(extra, f"{item}~{extra.get('id')}", entries.get(item),
                                    first_day, duplicate=True))
        if remote_ev is not None:
            op.etag = remote_ev.get("etag")
        we_deleted = bool(led and led.get("deleted"))
        active = led if led and not we_deleted else None

        if remote_ev is None or (remote_ev.get("status") == "cancelled" and we_deleted):
            op.op = "create"
        elif remote_ev.get("status") == "cancelled":
            op.op = "deleted_in_google"
            op.note = "Deleted in Google Calendar."
        else:
            remote_h = content_hash(remote_ev)
            stamped = private(remote_ev).get("pantry_hash")
            op.changes = changed_fields(body, remote_ev)
            if active and active.get("etag") == remote_ev.get("etag"):
                op.op = "noop" if active.get("hash") == h else "update"
            elif active and active.get("kept_etag") == remote_ev.get("etag") \
                    and active.get("hash") == h:
                op.op, op.note = "noop", "Your edit in Google Calendar is kept."
            elif remote_h == h:
                op.op, op.adopt = "noop", True
            elif not active and stamped and remote_h == stamped:
                op.op = "update"              # untouched since the hub wrote it
            elif active or stamped:
                op.op, op.origin = "conflict", "update"
                op.note = "Edited in Google Calendar since the last sync."
            else:
                op.op, op.unverified = "update", True
                op.note = "Could not check for edits made in Google Calendar."
            if op.op == "noop" and active is None:
                op.adopt = True
        if past and op.op != "noop":
            op.op, op.origin, op.unverified = "skip", "", False
            op.note = "In the past: left as it is."
        ops.append(op)

    for item, events in sorted(by_item.items()):
        if item in planned:
            continue
        live = [e for e in events if e.get("status") != "cancelled"]
        for e in live:
            ops.append(_removal(e, item if len(live) == 1 else f"{item}~{e.get('id')}",
                                entries.get(item), first_day))
    return ops


def _removal(e: dict[str, Any], item_id: str, led: dict[str, Any] | None, first_day: str,
             duplicate: bool = False) -> Op:
    """A live event of this plan that the plan no longer has (or a duplicate of one it has)."""
    op = Op(item_id=item_id, op="delete", kind=private(e).get("pantry_kind", ""),
            title=str(e.get("summary", "")), date=_remote_date(e), event_id=e.get("id"),
            etag=e.get("etag"), gen=gen_of(e))
    unedited = (led and not led.get("deleted") and led.get("event_id") == e.get("id")
                and led.get("etag") == e.get("etag")) \
        or content_hash(e) == private(e).get("pantry_hash")
    if duplicate:
        op.note = "A second copy of a planned event."
    if op.date < first_day and not duplicate:
        op.op, op.note = "skip", "In the past: left in your calendar."
    elif not unedited:
        op.op, op.origin = "conflict", "delete"
        op.note = "No longer planned, and edited in Google Calendar since the last sync."
    return op


def diff_token(key: str, rev: int, calendar_id: str | None, ops: list[Op]) -> str:
    canon = json.dumps([key, rev, calendar_id or "new", [o.token_part() for o in ops]],
                       separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# --- the service ------------------------------------------------------------------------------


@dataclass
class Result:
    item_id: str
    op: str
    ok: bool
    error_code: str | None = None
    message: str | None = None

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"item_id": self.item_id, "op": self.op, "ok": self.ok}
        if self.error_code:
            out["error_code"] = self.error_code
        if self.message:
            out["message"] = self.message
        return out


class _Stop(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def vancouver_today() -> dt.date:
    try:
        return dt.datetime.now(ZoneInfo(CALENDAR_ZONE)).date()
    except ZoneInfoNotFoundError:            # pragma: no cover - a system without tz data
        return dt.datetime.now().astimezone().date()


def oauth_origins(settings: Settings) -> tuple[str, ...]:
    """The console origins that may start a sign-in (HUB_OAUTH_ORIGINS): by default the hub's
    own address and Vite's dev server, the two the setup guide registers."""
    raw = settings.oauth_origins or (f"http://127.0.0.1:{settings.hub_port}",
                                     "http://localhost:5173")
    return tuple(o.strip().rstrip("/").lower() for o in raw if o.strip())


class CalendarSync:
    """Everything behind /hub/calendar/*. Transports, sleep and today are swappable for tests."""

    def __init__(self, settings: Settings, *,
                 google_transport: httpx.AsyncBaseTransport | None = None,
                 pantry_transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 today: Callable[[], dt.date] = vancouver_today,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self.pantry_transport = pantry_transport
        self.sleep, self.today, self.clock = sleep, today, clock
        self.pending = PendingAuths(clock=clock)
        self.connector = Connector(settings.google_oauth_client_file, settings.google_token_file,
                                   GoogleOAuth(settings.google_token_url,
                                               settings.google_revoke_url, google_transport),
                                   clock=clock)
        self.lock = asyncio.Lock()
        self.ledger_path = (Path(settings.calendar_dir).expanduser() / "ledger.json"
                            if settings.calendar_dir else None)

    @property
    def google_transport(self) -> httpx.AsyncBaseTransport | None:
        return self.connector.oauth.transport

    @google_transport.setter
    def google_transport(self, transport: httpx.AsyncBaseTransport | None) -> None:
        self.connector.oauth.transport = transport

    def client(self) -> CalendarClient:
        return CalendarClient(self.settings.google_calendar_url, self.connector.access_token,
                              transport=self.google_transport, sleep=self.sleep, clock=self.clock)

    def ledger(self) -> Ledger:
        return Ledger(self.ledger_path)

    # status

    def status(self, host: str = "") -> dict[str, Any]:
        """Booleans and labels only: never a token, a client id or a secret."""
        problem: str | None = None
        client = None
        try:
            client = self.connector.client()
        except OAuthError as exc:
            if exc.code != "not_configured":
                problem = exc.message
        conn: Connection | None = None
        try:
            conn = self.connector.store.load()
        except OAuthError as exc:
            problem = exc.message
        origin = f"http://{host}".lower() if host else ""
        here = bool(client and origin in oauth_origins(self.settings)
                    and client.accepts(origin + CALLBACK_PATH))
        ledger = self.ledger()
        calendar = ledger.calendar_id or (conn.calendar_id if conn else None)
        return {
            "configured": client is not None,
            "client_type": client.client_type if client else None,
            "connected": conn is not None and not self.connector.needs_reconnect,
            "needs_reconnect": self.connector.needs_reconnect,
            "reconnect_by": conn.reconnect_by() if conn else None,
            "can_connect_here": here,
            "connect_url": f"http://127.0.0.1:{self.settings.hub_port}/pantry/",
            "calendar": {"summary": CALENDAR_SUMMARY} if calendar and conn else None,
            "scope": "calendar.app.created",
            "all_day": True,
            "testing_note": TESTING_NOTE,
            "last_sync": ledger.data.get("last_sync"),
            "problem": problem,
        }

    def connected(self) -> bool:
        try:
            return self.connector.store.load() is not None and not self.connector.needs_reconnect
        except OAuthError:
            return False

    def configured(self) -> bool:
        try:
            return self.connector.client() is not None
        except OAuthError:
            return False

    # sign-in

    def connect(self, origin: str | None, return_to: str) -> tuple[str, str]:
        """(auth_url, nonce) for a new sign-in from the console at ``origin``."""
        client = self.connector.client()
        origin = (origin or "").strip().rstrip("/").lower()
        redirect = origin + CALLBACK_PATH
        if not origin or origin not in oauth_origins(self.settings) \
                or not client.accepts(redirect):
            here = f"http://127.0.0.1:{self.settings.hub_port}/pantry/"
            raise OAuthError("origin_not_registered", (
                f"This console address ({origin or 'unknown'}) is not registered on your OAuth "
                f"client: open the console at {here} to connect, or add {redirect} to the "
                "client's redirect URIs (docs/google-calendar.md)."))
        state, nonce, verifier = self.pending.start(redirect, return_to)
        return auth_url(self.settings.google_auth_url, client, redirect, state, verifier), nonce

    async def finish(self, code: str, verifier: str, redirect_uri: str) -> None:
        """Exchange the code and store the connection. Keeps the calendar from an earlier
        connection when this account can still open it (a weekly reconnect), else forgets it."""
        client = self.connector.client()
        answer = await self.connector.oauth.exchange(client, code, verifier, redirect_uri)
        conn = new_connection(answer)
        ledger = self.ledger()
        try:
            old = self.connector.store.load()
        except OAuthError:
            old = None
        calendar = ledger.calendar_id or (old.calendar_id if old else None)
        self.connector.connected(conn, str(answer["access_token"]),
                                 float(answer.get("expires_in", 3600)))
        if calendar:
            try:
                await self.client().calendars_get(calendar)
                self.connector.remember_calendar(calendar)
            except (NotFound, Forbidden):
                ledger.reset()        # another account, or the calendar was deleted
            except (GoogleError, OAuthError):
                self.connector.remember_calendar(calendar)   # checked again on the next sync
        ledger.data["connection_id"] = conn.connection_id
        ledger.save()

    async def disconnect(self, delete_calendar: bool) -> dict[str, Any]:
        if self.lock.locked():
            raise SyncRefused(409, "sync_in_progress", "A sync is running; try again when it "
                              "has finished.")
        async with self.lock:
            try:
                conn = self.connector.store.load()
            except OAuthError:
                conn = None
            ledger = self.ledger()
            calendar = ledger.calendar_id or (conn.calendar_id if conn else None)
            deleted, notes = False, []
            if delete_calendar and calendar and conn:
                try:
                    await self.client().calendars_delete(calendar)
                    deleted = True
                except NotFound:
                    deleted = True
                except (GoogleError, OAuthError) as exc:
                    notes.append("The Pantry plan calendar could not be deleted "
                                 f"({_reason(exc)}): delete it in Google Calendar's settings.")
            elif calendar and conn:
                notes.append("The Pantry plan calendar stays in your Google account; a new "
                             "connection starts a new one.")
            revoked = await self.connector.oauth.revoke(conn.refresh_token) if conn else False
            if conn and not revoked:
                notes.append("Google did not confirm the revoke: remove the app at "
                             "myaccount.google.com/connections.")
            self.connector.forget()
            ledger.reset()
            ledger.save()
            return {"revoked": revoked, "calendar_deleted": deleted,
                    "note": " ".join(notes) or None}

    # the diff

    async def _events(self, schedule: dict[str, Any], include: list[str]) -> list[dict[str, Any]]:
        """pantry-api's events for the schedule: the one builder."""
        try:
            async with httpx.AsyncClient(base_url=self.settings.pantry_api_url, timeout=60,
                                         transport=self.pantry_transport,
                                         trust_env=False) as http:
                r = await http.post("/calendar/preview",
                                    json={"schedule": schedule, "include": include})
        except httpx.HTTPError as exc:
            raise SyncRefused(502, "pantry_unavailable", "pantry-api could not be reached "
                              f"({type(exc).__name__}).") from exc
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code != 200:
            detail = body.get("detail") if isinstance(body, dict) else None
            code = detail.get("error") if isinstance(detail, dict) else None
            text = detail.get("detail") if isinstance(detail, dict) else detail
            if isinstance(text, list):                    # FastAPI's validation errors
                first = text[0] if text and isinstance(text[0], dict) else {}
                text = f"pantry-api could not read the plan ({first.get('msg', 'invalid')})."
            if r.status_code in (409, 413, 422, 429):     # pantry's own refusal, as it said it
                raise SyncRefused(r.status_code, code or "pantry_refused",
                                  str(text or f"pantry-api answered {r.status_code}."))
            if r.status_code == 404:
                raise SyncRefused(502, "pantry_too_old", "This pantry-api has no calendar "
                                  "export (POST /calendar/preview).")
            raise SyncRefused(502, "pantry_unavailable",
                              f"pantry-api answered {r.status_code}.")
        events = body.get("events") if isinstance(body, dict) else None
        if not isinstance(events, list) or not all(
                isinstance(e, dict) and {"item_id", "date", "end", "title"} <= e.keys()
                for e in events):
            raise SyncRefused(502, "pantry_refused", "pantry-api's preview was not a list of "
                              "calendar events.")
        return events

    def _ready(self) -> Connection:
        self.connector.client()
        conn = self.connector.connection()
        if self.connector.needs_reconnect:
            raise OAuthError("needs_reconnect", "Google ended the connection: connect again. "
                             + TESTING_NOTE)
        return conn

    async def _diff(self, schedule: dict[str, Any], include: list[str]) -> tuple[Diff, Ledger]:
        conn = self._ready()
        desired = await self._events(schedule, include)
        ledger = self.ledger()
        key = plan_key(schedule)
        rev = int(schedule.get("rev") or 0)
        calendar = ledger.calendar_id or conn.calendar_id
        remote: list[dict[str, Any]] = []
        if calendar:
            client = self.client()
            try:
                await client.calendars_get(calendar)
                remote = await client.events_list(calendar, {"pantry_schedule": key})
            except NotFound as exc:
                raise SyncRefused(409, "calendar_missing", (
                    "The Pantry plan calendar is gone from your Google account. Disconnect and "
                    "connect again to start a new one.")) from exc
            except GoogleError as exc:
                raise _google_refusal(exc) from exc
        ops = classify(desired, remote, ledger.entries(key), key=key, rev=rev, today=self.today())
        diff = Diff(key, rev, calendar, ops)
        diff.token = diff_token(key, rev, calendar, ops)
        return diff, ledger

    async def preview(self, schedule: dict[str, Any], include: list[str]) -> dict[str, Any]:
        """The diff; nothing is written, in Google or in the ledger."""
        diff, _ = await self._diff(schedule, include)
        return diff.public()

    # apply

    async def apply(self, schedule: dict[str, Any], include: list[str], preview_token: str,
                    choices: dict[str, Choice]) -> dict[str, Any]:
        if self.lock.locked():
            raise SyncRefused(409, "sync_in_progress", "A sync is already running.")
        async with self.lock:
            diff, ledger = await self._diff(schedule, include)
            if diff.token != preview_token:
                raise SyncRefused(409, "preview_stale", "The plan or the Pantry plan calendar "
                                  "changed since you reviewed it: review the changes again.")
            _check_choices(diff.ops, choices)
            results: list[Result] = []
            client = self.client()
            entries = ledger.entries(diff.key)
            todo = [o for o in diff.ops if o.op != "skip"]
            stopped: _Stop | None = None
            calendar = diff.calendar_id
            if calendar is None and any(o.op == "create" for o in todo):
                try:
                    made = await client.calendars_insert(CALENDAR_SUMMARY, CALENDAR_ZONE,
                                                         CALENDAR_DESCRIPTION)
                except (GoogleError, OAuthError) as exc:
                    stopped = _stop_for(exc)
                else:
                    calendar = str(made["id"])
                    ledger.set_calendar(calendar)
                    ledger.save()
                    self.connector.remember_calendar(calendar)
            for op in todo:
                if stopped is not None:
                    if _writes(op, choices.get(op.item_id)):
                        results.append(Result(op.item_id, op.op, False, stopped.code,
                                              f"Not tried: {stopped.message}"))
                    continue
                try:
                    result = await self._run(client, calendar or "", diff, op, entries,
                                             choices.get(op.item_id))
                except _Stop as exc:
                    stopped = exc
                    results.append(Result(op.item_id, op.op, False, exc.code, exc.message))
                    continue
                ledger.save()
                if result is not None:
                    results.append(result)
            if stopped is None and all(r.ok for r in results):
                ledger.data["schedules"][diff.key]["synced_rev"] = diff.rev
            status = ("ok" if all(r.ok for r in results)
                      else "failed" if not any(r.ok for r in results) else "partial")
            ledger.data["last_sync"] = {
                "at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
                "status": status, "plan": diff.key}
            ledger.save()
            return {"status": status, "results": [r.public() for r in results],
                    "calendar": {"summary": CALENDAR_SUMMARY} if calendar else None}

    async def _run(self, client: CalendarClient, calendar: str, diff: Diff, op: Op,
                   entries: dict[str, dict[str, Any]], choice: str | None) -> Result | None:
        """One operation. Returns its result (None for one that writes nothing and needs no
        row), raises _Stop when nothing more can be written."""
        try:
            if op.op == "noop":
                if op.adopt:
                    entries[op.item_id] = {"event_id": op.event_id, "gen": op.gen,
                                           "etag": op.etag, "hash": op.hash}
                return None
            if op.op == "create":
                event = await self._insert(client, calendar, diff, op)
                self._record(entries, op, event)
                return Result(op.item_id, "create", True)
            if op.op == "update":
                assert op.body is not None
                try:
                    event = await client.events_update(calendar, op.event_id or "", op.body,
                                                       op.etag)
                except PreconditionFailed:
                    return _edited(op)
                self._record(entries, op, event)
                return Result(op.item_id, "update", True)
            if op.op == "delete":
                return await self._delete(client, calendar, op, entries)
            if op.op == "conflict":
                if choice != "overwrite":
                    # nothing goes to Google; the ledger remembers the edit was kept, so the
                    # next review shows it as unchanged until the plan changes the event
                    led = entries.get(op.item_id) or {"event_id": op.event_id, "gen": op.gen,
                                                      "etag": None, "hash": None}
                    led["kept_etag"] = op.etag
                    if op.origin == "update" and led.get("hash") is None:
                        led["hash"] = op.hash
                    entries[op.item_id] = led
                    return Result(op.item_id, "conflict", True,
                                  message="Kept your edit in Google Calendar.")
                if op.origin == "delete":
                    return await self._delete(client, calendar, op, entries)
                assert op.body is not None
                try:
                    event = await client.events_update(calendar, op.event_id or "", op.body,
                                                       op.etag)
                except PreconditionFailed:
                    return _edited(op)
                self._record(entries, op, event)
                return Result(op.item_id, "conflict", True, message="Overwritten with the plan.")
            if op.op == "deleted_in_google":
                if choice != "restore":
                    return None
                event = await self._restore(client, calendar, diff, op)
                self._record(entries, op, event)
                return Result(op.item_id, "deleted_in_google", True, message="Restored.")
        except (QuotaExceeded, RateLimited, Unavailable, Unauthorized, OAuthError) as exc:
            raise _stop_for(exc) from exc
        except NotFound as exc:
            if op.op == "create":            # an insert into a calendar that is gone
                raise _Stop("calendar_missing", "The Pantry plan calendar is gone from your "
                            "Google account.") from exc
            return Result(op.item_id, op.op, False, "gone",
                          "Gone from Google Calendar: review the changes again.")
        except GoogleError as exc:
            return Result(op.item_id, op.op, False, exc.reason or "google_error",
                          f"Google refused it ({exc.status} {exc.reason}).")
        return None

    async def _insert(self, client: CalendarClient, calendar: str, diff: Diff,
                      op: Op) -> dict[str, Any]:
        """Insert under the item's id. If Google already has that id (an earlier run, or one
        the hub deleted), update that event instead; if Google refuses that too, bump gen and
        insert under a new id."""
        assert op.body is not None and op.source is not None
        try:
            return await client.events_insert(calendar, op.body)
        except Duplicate:
            pass
        try:
            existing = await client.events_get(calendar, op.body["id"])
            return await client.events_update(calendar, op.body["id"], op.body,
                                              existing.get("etag"))
        except (NotFound, Forbidden, BadRequest, PreconditionFailed):
            return await self._new_gen(client, calendar, diff, op)

    async def _restore(self, client: CalendarClient, calendar: str, diff: Diff,
                       op: Op) -> dict[str, Any]:
        assert op.body is not None
        try:
            return await client.events_update(calendar, op.event_id or "", op.body, op.etag)
        except (NotFound, Forbidden, BadRequest):
            return await self._new_gen(client, calendar, diff, op)

    async def _new_gen(self, client: CalendarClient, calendar: str, diff: Diff,
                       op: Op) -> dict[str, Any]:
        assert op.source is not None
        op.gen += 1
        op.body = google_body(op.source, diff.key, diff.rev, op.gen)
        op.event_id = op.body["id"]
        return await client.events_insert(calendar, op.body)

    async def _delete(self, client: CalendarClient, calendar: str, op: Op,
                      entries: dict[str, dict[str, Any]]) -> Result:
        try:
            await client.events_delete(calendar, op.event_id or "", op.etag)
        except PreconditionFailed:
            return _edited(op)
        except NotFound:
            pass                                     # already gone: what was asked for
        item = op.item_id.split("~", 1)[0]
        if item == op.item_id or entries.get(item, {}).get("event_id") == op.event_id:
            entries[item] = {"event_id": op.event_id, "gen": op.gen, "deleted": True}
        return Result(op.item_id, op.op, True)

    @staticmethod
    def _record(entries: dict[str, dict[str, Any]], op: Op, event: dict[str, Any]) -> None:
        entries[op.item_id] = {"event_id": event.get("id", op.event_id), "gen": op.gen,
                               "etag": event.get("etag"), "hash": op.hash}


def _writes(op: Op, choice: str | None) -> bool:
    if op.op in ("create", "update", "delete"):
        return True
    if op.op == "conflict":
        return choice == "overwrite"
    return op.op == "deleted_in_google" and choice == "restore"


def _edited(op: Op) -> Result:
    return Result(op.item_id, op.op, False, "edited_in_google",
                  "Changed in Google Calendar since you reviewed it: not overwritten. Review "
                  "the changes again.")


def _reason(exc: Exception) -> str:
    if isinstance(exc, GoogleError):
        return f"{exc.status} {exc.reason}"
    if isinstance(exc, OAuthError):
        return exc.code
    return type(exc).__name__


def _stop_for(exc: Exception) -> _Stop:
    if isinstance(exc, OAuthError):
        if exc.code in ("invalid_grant", "needs_reconnect"):
            return _Stop("needs_reconnect", "Google ended the connection: connect again.")
        return _Stop(exc.code, exc.message)
    if isinstance(exc, QuotaExceeded):
        return _Stop("quota_exceeded", "Google's daily Calendar quota is used up; try "
                     "tomorrow.")
    if isinstance(exc, RateLimited):
        return _Stop("rate_limited", "Google asked the hub to slow down; retry in a minute.")
    if isinstance(exc, Unauthorized):
        return _Stop("needs_reconnect", "Google refused the connection: connect again.")
    if isinstance(exc, GoogleError):
        return _Stop("google_unavailable", f"Google Calendar did not answer ({exc.status}).")
    return _Stop("google_error", type(exc).__name__)


def _google_refusal(exc: GoogleError) -> SyncRefused:
    """A Google failure while reading for a preview."""
    stop = _stop_for(exc)
    if isinstance(exc, QuotaExceeded | RateLimited):
        return SyncRefused(429, stop.code, stop.message)
    if isinstance(exc, Unauthorized):
        return SyncRefused(409, stop.code, stop.message)
    return SyncRefused(502, "google_unavailable", stop.message)


def _check_choices(ops: list[Op], choices: dict[str, Choice]) -> None:
    """Every conflict needs keep or overwrite; restore is only for an event deleted in Google;
    a choice for anything else is a mistake (422)."""
    by_item = {o.item_id: o for o in ops}
    for item, choice in choices.items():
        op = by_item.get(item)
        ok = op is not None and (
            (op.op == "conflict" and choice in ("keep", "overwrite"))
            or (op.op == "deleted_in_google" and choice in ("keep", "restore")))
        if not ok:
            raise SyncRefused(422, "bad_choice", f"{choice!r} is not a choice for {item!r}.")
    missing = [o.item_id for o in ops if o.op == "conflict" and o.item_id not in choices]
    if missing:
        raise SyncRefused(422, "choice_needed", "Choose Keep or Overwrite for every event "
                          "edited in Google Calendar.", items=missing)


def schedule_size_ok(schedule: dict[str, Any]) -> bool:
    return len(json.dumps(schedule, ensure_ascii=False).encode("utf-8")) <= MAX_SCHEDULE_BYTES

