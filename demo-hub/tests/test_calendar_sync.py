"""The reconcile sync (gcal_sync.py, gcal_client.py) against FakeGoogle: idempotence, If-Match and
412 conflicts, edits and deletions made in Google, crash recovery, a lost ledger, backoff, the
preview token, the lock, and events the hub did not make."""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import inspect
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from demo_hub.gcal_sync import SyncRefused, classify, content_hash, event_id, google_body
from tests.fake_google import CalendarEnv, Crash, make_env, preview, schedule

ID = re.compile(r"^pp1[0-9a-f]{40}$")


@pytest.fixture
def env(tmp_path: Path) -> CalendarEnv:
    e = make_env(tmp_path)
    assert e.connect().status_code == 303
    return e


def items(env: CalendarEnv) -> dict[str, list[dict[str, Any]]]:
    return env.google.by_item()


def ledger(env: CalendarEnv) -> dict[str, Any]:
    return json.loads(env.paths["ledger"].read_text())


def writes(env: CalendarEnv) -> list[httpx.Request]:
    return [r for r in env.google.requests if r.method != "GET" and "/calendar/v3" in r.url.path]


def test_the_first_apply_makes_the_calendar_and_every_event(env: CalendarEnv) -> None:
    sched = schedule()
    diff, result = env.sync_now(sched)
    assert diff["calendar_action"] == "create" and diff["counts"]["create"] == 5
    assert result["status"] == "ok" and [r["ok"] for r in result["results"]] == [True] * 5
    cal = env.google.calendars[env.google.calendar_id]
    assert cal["summary"] == "Pantry plan" and cal["timeZone"] == "America/Vancouver"
    by_item = items(env)
    assert sorted(by_item) == ["cook-m1", "cook-m2", "cook-m3", "thaw-m2-7", "trip-t1"]
    built = {e["item_id"]: e for e in preview(sched)["events"]}
    for item, [ev] in by_item.items():
        assert ID.match(ev["id"]) and ev["id"] == event_id("plan-abc", item, 0)
        # pantry-api's words, as they are: the hub writes nothing of its own into an event
        assert ev["summary"] == built[item]["title"]
        assert ev["description"] == built[item]["description"]
        assert ev.get("location") == (built[item]["location"] or None)
        assert ev["start"] == {"date": built[item]["date"]}
        assert ev["end"] == {"date": built[item]["end"]}
        assert ev["transparency"] == "transparent" and "attendees" not in ev
        assert ev["reminders"] == {"useDefault": False}
        props = ev["extendedProperties"]["private"]
        assert props["pantry_schedule"] == "plan-abc" and props["pantry_item"] == item
        assert props["pantry_hash"] == content_hash(ev) and props["pantry_gen"] == "0"
    for req in writes(env)[1:]:
        assert req.url.params["sendUpdates"] == "none"
    assert ledger(env)["schedules"]["plan-abc"]["synced_rev"] == 1


def test_syncing_again_writes_nothing(env: CalendarEnv) -> None:
    sched = schedule()
    env.sync_now(sched)
    before = env.google.writes
    diff, result = env.sync_now(sched)
    assert diff["counts"]["noop"] == 5 and sum(diff["counts"].values()) == 5
    assert result == {"status": "ok", "results": [], "calendar": {"summary": "Pantry plan"}}
    assert env.google.writes == before
    again = env.preview(sched).json()
    assert again["preview_token"] == diff["preview_token"]       # same plan, same Google: same diff


def test_moving_one_meal_is_exactly_one_update_with_if_match(env: CalendarEnv) -> None:
    sched = schedule()
    env.sync_now(sched)
    etag = ledger(env)["schedules"]["plan-abc"]["events"]["cook-m2"]["etag"]
    moved = copy.deepcopy(sched)
    moved["rev"] = 2
    moved["cooks"][1]["date"] = "2026-10-16"
    before = len(writes(env))
    diff, result = env.sync_now(moved)
    assert diff["counts"]["update"] == 1 and diff["counts"]["noop"] == 4
    op = next(o for o in diff["ops"] if o["op"] == "update")
    assert op["item_id"] == "cook-m2" and op["changes"] == ["date"]
    sent = writes(env)[before:]
    assert [(r.method, r.headers.get("if-match")) for r in sent] == [("PUT", etag)]
    assert items(env)["cook-m2"][0]["start"] == {"date": "2026-10-16"}
    assert result["results"] == [{"item_id": "cook-m2", "op": "update", "ok": True}]


def test_a_meal_taken_off_the_plan_is_one_delete(env: CalendarEnv) -> None:
    sched = schedule()
    env.sync_now(sched)
    fewer = copy.deepcopy(sched)
    fewer["cooks"].pop()
    before = len(writes(env))
    diff, result = env.sync_now(fewer)
    assert diff["counts"]["delete"] == 1
    assert [o["item_id"] for o in diff["ops"] if o["op"] == "delete"] == ["cook-m3"]
    assert [r.method for r in writes(env)[before:]] == ["DELETE"]
    assert writes(env)[-1].headers["if-match"]
    assert "cook-m3" not in items(env) and result["status"] == "ok"
    # unticking a kind removes its events too, and only those
    diff, _ = env.sync_now(fewer, include=["trips", "cooks"])
    assert [o["item_id"] for o in diff["ops"] if o["op"] == "delete"] == ["thaw-m2-7"]
    # and putting the meal back brings its event back under the same id
    diff, result = env.sync_now(sched)
    assert diff["counts"]["create"] == 2 and result["status"] == "ok"
    assert {k: len(v) for k, v in items(env).items()} == dict.fromkeys(items(env), 1)
    assert items(env)["cook-m3"][0]["id"] == event_id("plan-abc", "cook-m3", 0)


def test_an_edit_in_google_is_a_conflict_kept_by_default(env: CalendarEnv) -> None:
    sched = schedule()
    env.sync_now(sched)
    cal, ev = env.google.calendar_id, items(env)["cook-m1"][0]
    env.google.user_edit(cal, ev["id"], summary="Cook: pizza night (moved to 7pm)")
    diff = env.preview(sched).json()
    conflict = next(o for o in diff["ops"] if o["op"] == "conflict")
    assert conflict["item_id"] == "cook-m1" and conflict["origin"] == "update"
    assert conflict["changes"] == ["title"]
    # every conflict needs a choice
    r = env.apply(sched, diff)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "choice_needed"
    before = env.google.writes
    r = env.apply(sched, diff, {"cook-m1": "keep"})
    assert r.json()["results"] == [{"item_id": "cook-m1", "op": "conflict", "ok": True,
                                    "message": "Kept your edit in Google Calendar"}]
    assert env.google.writes == before
    assert items(env)["cook-m1"][0]["summary"] == "Cook: pizza night (moved to 7pm)"
    # kept: the next review shows it unchanged, until the plan changes that meal
    assert env.preview(sched).json()["counts"]["conflict"] == 0
    moved = copy.deepcopy(sched)
    moved["cooks"][0]["date"] = "2026-10-14"
    diff = env.preview(moved).json()
    assert [o["item_id"] for o in diff["ops"] if o["op"] == "conflict"] == ["cook-m1"]
    # overwrite: one PUT, If-Match the etag Google had when the diff was made
    remote_etag = items(env)["cook-m1"][0]["etag"]
    before = len(writes(env))
    r = env.apply(moved, diff, {"cook-m1": "overwrite"})
    assert r.json()["status"] == "ok"
    assert [(q.method, q.headers.get("if-match")) for q in writes(env)[before:]] == \
        [("PUT", remote_etag)]
    assert items(env)["cook-m1"][0]["summary"] == "Cook: Dinner 1 (dinner)"
    assert env.preview(moved).json()["counts"]["noop"] == 5


def test_an_edit_in_google_after_the_preview_is_a_412_never_overwritten(
        env: CalendarEnv) -> None:
    sched = schedule()
    env.sync_now(sched)
    moved = copy.deepcopy(sched)
    moved["cooks"][1]["date"] = "2026-10-17"
    diff = env.preview(moved).json()
    # the shopper edits the event in Google while the diff is on screen; the hub's list has
    # not changed for the token (the fake returns the edited etag only on the PUT)
    cal, ev = env.google.calendar_id, items(env)["cook-m2"][0]
    original = env.google.events[cal][ev["id"]]["etag"]

    real = env.google._api

    def edit_first(request: httpx.Request, path: str) -> httpx.Response:
        if request.method == "PUT":
            env.google.user_edit(cal, ev["id"], description="my own notes")
        return real(request, path)

    env.google._api = edit_first                     # type: ignore[method-assign]
    r = env.apply(moved, diff)
    assert r.json()["status"] == "failed"
    assert r.json()["results"] == [{
        "item_id": "cook-m2", "op": "update", "ok": False, "error_code": "edited_in_google",
        "message": "Changed in Google Calendar since you reviewed it: not overwritten. Review "
                   "the changes again."}]
    assert env.google.events[cal][ev["id"]]["description"] == "my own notes"
    assert env.google.events[cal][ev["id"]]["etag"] != original
    env.google._api = real                           # type: ignore[method-assign]
    assert env.preview(moved).json()["counts"]["conflict"] == 1


def test_a_deletion_in_google_is_reported_and_restored_only_when_asked(env: CalendarEnv) -> None:
    sched = schedule()
    env.sync_now(sched)
    cal, ev = env.google.calendar_id, items(env)["cook-m2"][0]
    env.google.user_delete(cal, ev["id"])
    before = env.google.writes
    diff, result = env.sync_now(sched)
    assert diff["counts"]["deleted_in_google"] == 1 and result["results"] == []
    assert env.google.writes == before and "cook-m2" not in items(env)
    # restore: brought back as it was, same id
    diff = env.preview(sched).json()
    r = env.apply(sched, diff, {"cook-m2": "restore"}).json()
    assert r["results"] == [{"item_id": "cook-m2", "op": "deleted_in_google", "ok": True}]
    assert items(env)["cook-m2"][0]["id"] == ev["id"]
    assert env.preview(sched).json()["counts"]["noop"] == 5


def test_a_restore_google_refuses_gets_a_new_id(env: CalendarEnv) -> None:
    sched = schedule()
    env.sync_now(sched)
    cal, ev = env.google.calendar_id, items(env)["cook-m2"][0]
    env.google.user_delete(cal, ev["id"])
    env.google.refuse_restore = True
    diff = env.preview(sched).json()
    r = env.apply(sched, diff, {"cook-m2": "restore"}).json()
    assert r["status"] == "ok"
    [back] = items(env)["cook-m2"]
    assert back["id"] == event_id("plan-abc", "cook-m2", 1) != ev["id"]
    assert back["extendedProperties"]["private"]["pantry_gen"] == "1"
    assert ledger(env)["schedules"]["plan-abc"]["events"]["cook-m2"]["gen"] == 1
    # the cancelled gen-0 event stays cancelled; the next review is quiet
    diff = env.preview(sched).json()
    assert diff["counts"]["noop"] == 5 and sum(diff["counts"].values()) == 5


def test_a_crash_mid_apply_leaves_no_duplicates(env: CalendarEnv) -> None:
    sched = schedule()
    env.google.crash_after_writes = 3                # the calendar and two events, then death
    diff = env.preview(sched).json()
    with pytest.raises(Crash):
        env.apply(sched, diff)
    assert len(env.google.live()) == 2
    saved = ledger(env)["schedules"]["plan-abc"]["events"]
    assert len(saved) == 1                           # the second write's answer never came
    diff, result = env.sync_now(sched)
    assert diff["calendar_action"] == "existing"
    assert diff["counts"]["create"] == 3 and diff["counts"]["noop"] == 2
    assert result["status"] == "ok"
    assert {k: len(v) for k, v in items(env).items()} == dict.fromkeys(items(env), 1)
    assert len(items(env)) == 5 and len(env.google.calendars) == 1
    assert len(ledger(env)["schedules"]["plan-abc"]["events"]) == 5


def test_a_lost_ledger_finds_the_same_calendar_and_events(env: CalendarEnv) -> None:
    sched = schedule()
    env.sync_now(sched)
    env.paths["ledger"].unlink()
    before = env.google.writes
    diff, _ = env.sync_now(sched)
    assert diff["calendar_action"] == "existing" and diff["counts"]["noop"] == 5
    assert env.google.writes == before and len(env.google.calendars) == 1
    assert len(ledger(env)["schedules"]["plan-abc"]["events"]) == 5    # rebuilt from Google
    # lost again, and the plan changed: an untouched event is updated, an edited one is not
    env.paths["ledger"].unlink()
    cal = env.google.calendar_id
    env.google.user_edit(cal, items(env)["cook-m3"][0]["id"], description="mine")
    moved = copy.deepcopy(sched)
    moved["cooks"][1]["date"] = moved["cooks"][2]["date"] = "2026-10-18"
    diff = env.preview(moved).json()
    ops = {o["item_id"]: o for o in diff["ops"]}
    assert ops["cook-m2"]["op"] == "update" and "unverified" not in ops["cook-m2"]
    assert ops["cook-m3"]["op"] == "conflict"


def test_a_calendar_deleted_in_google_is_reported_not_recreated(env: CalendarEnv) -> None:
    sched = schedule()
    env.sync_now(sched)
    env.google.calendars.clear()
    env.google.events.clear()
    r = env.preview(sched)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "calendar_missing"
    assert env.google.calendars == {}
    # disconnecting and connecting again starts a new one
    env.client.post("/hub/calendar/disconnect", json={"delete_calendar": False})
    env.connect()
    diff, result = env.sync_now(sched)
    assert diff["calendar_action"] == "create" and result["status"] == "ok"


def test_rate_limits_are_waited_out_with_the_injected_sleep(env: CalendarEnv) -> None:
    env.google.fail("POST", r"/events$", 429, "rateLimitExceeded", retry_after="7")
    env.google.fail("POST", r"/events$", 403, "userRateLimitExceeded")
    _, result = env.sync_now(schedule())
    assert result["status"] == "ok" and len(env.google.live()) == 5
    assert 7.0 in env.sleeps                         # Retry-After, as asked
    env.google.fail("GET", r"/events$", 503, "backendError")
    assert env.preview(schedule()).json()["counts"]["noop"] == 5
    backoff = [s for s in env.sleeps if s >= 0.5 and s != 7.0]
    assert len(backoff) == 2 and all(0.5 <= s <= 2 for s in backoff)


def test_quota_exceeded_is_not_retried_and_stops_the_rest(env: CalendarEnv) -> None:
    env.sync_now(schedule())                         # the calendar and events exist
    moved = schedule(rev=2)
    for c in moved["cooks"]:
        c["date"] = "2026-10-19"
    env.google.fail("PUT", r"/events/", 403, "quotaExceeded", times=1)
    diff = env.preview(moved).json()
    puts = len(env.google.calls("PUT"))
    r = env.apply(moved, diff).json()
    assert r["status"] == "failed"
    assert [x["error_code"] for x in r["results"]] == ["quota_exceeded"] * 3
    assert r["results"][1]["message"].startswith("Not tried:")
    assert len(env.google.calls("PUT")) == puts + 1               # one try, no retries
    assert [s for s in env.sleeps if s >= 0.5] == []


def test_writes_are_throttled_to_five_a_second(env: CalendarEnv) -> None:
    env.sync_now(schedule())
    gaps = [s for s in env.sleeps if s < 0.5]
    assert len(gaps) >= 4 and all(0 < s <= 0.2 for s in gaps)


def test_a_preview_that_no_longer_matches_is_refused(env: CalendarEnv) -> None:
    sched = schedule()
    diff = env.preview(sched).json()
    changed = copy.deepcopy(sched)
    changed["cooks"][0]["title"] = "Something else"
    r = env.apply(changed, diff)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "preview_stale"
    assert env.google.writes == 0
    # Google changing under the diff is stale too
    env.sync_now(sched)
    diff = env.preview(sched).json()
    env.google.user_edit(env.google.calendar_id, items(env)["trip-t1"][0]["id"], summary="x")
    r = env.apply(sched, diff)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "preview_stale"
    # a token that is not a token is a 422
    assert env.client.post("/hub/calendar/sync/apply", json={
        "schedule": sched, "preview_token": "nope"}).status_code == 422
    # and a choice for an operation that has none
    diff = env.preview(sched).json()
    r = env.apply(sched, diff, {"cook-m2": "overwrite", "trip-t1": "keep"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "bad_choice"


def test_two_applies_at_once_one_is_refused(env: CalendarEnv) -> None:
    sync = env.sync
    sched = schedule()
    gate = asyncio.Event()
    entered = asyncio.Event()
    real = env.pantry.handle

    async def slow(request: httpx.Request) -> httpx.Response:
        entered.set()
        await gate.wait()
        return real(request)

    async def run() -> tuple[Any, Any]:
        sync.pantry_transport = httpx.MockTransport(env.pantry.handle)
        token = (await sync.preview(sched, ["trips", "cooks", "reminders"]))["preview_token"]
        sync.pantry_transport = httpx.MockTransport(slow)
        first = asyncio.ensure_future(sync.apply(sched, ["trips", "cooks", "reminders"], token,
                                                 {}))
        await entered.wait()
        with pytest.raises(SyncRefused) as second:
            await sync.apply(sched, ["trips", "cooks", "reminders"], token, {})
        with pytest.raises(SyncRefused) as disconnect:
            await sync.disconnect(False)
        gate.set()
        return await first, (second.value, disconnect.value)

    result, (second, disconnect) = asyncio.run(run())
    assert second.status == 409 and second.code == "sync_in_progress"
    assert disconnect.code == "sync_in_progress"
    assert result["status"] == "ok" and len(env.google.live()) == 5


def test_events_the_hub_did_not_make_are_never_touched(env: CalendarEnv) -> None:
    sched = schedule()
    env.sync_now(sched)
    cal = env.google.calendar_id
    env.google.user_add(cal, {"id": "mine12345", "summary": "Dentist",
                              "start": {"date": "2026-10-13"}, "end": {"date": "2026-10-14"}})
    env.google.user_add(cal, {"id": "other12345", "summary": "Another plan's dinner",
                              "start": {"date": "2026-10-13"}, "end": {"date": "2026-10-14"},
                              "extendedProperties": {"private": {
                                  "pantry_schedule": "plan-other", "pantry_item": "cook-m1"}}})
    empty = copy.deepcopy(sched)
    empty["trips"], empty["cooks"], empty["reminders"] = [], [], []
    diff, result = env.sync_now(empty, include=["trips"])
    assert diff["counts"]["delete"] == 5 and result["status"] == "ok"
    for req in env.google.requests:
        assert "mine12345" not in req.url.path and "other12345" not in req.url.path
    assert {e["id"] for e in env.google.live()} == {"mine12345", "other12345"}
    # every list asked Google for this plan's events only
    for req in env.google.calls("GET", r"/events$"):
        assert req.url.params.get_list("privateExtendedProperty") == ["pantry_schedule=plan-abc"]


def test_the_past_is_left_alone(env: CalendarEnv) -> None:
    sched = schedule(start="2026-10-06")             # today is Fri 9 Oct: only cook-m3 is ahead
    diff, result = env.sync_now(sched)
    past = sorted(o["item_id"] for o in diff["ops"] if o["op"] == "skip")
    assert past == ["cook-m1", "cook-m2", "thaw-m2-7", "trip-t1"]
    assert [o["item_id"] for o in diff["ops"] if o["op"] == "create"] == ["cook-m3"]
    assert result["status"] == "ok" and list(items(env)) == ["cook-m3"]


def test_events_are_listed_page_by_page(env: CalendarEnv) -> None:
    env.google.page_size = 2
    env.sync_now(schedule())
    assert env.preview(schedule()).json()["counts"]["noop"] == 5
    pages = env.google.calls("GET", r"/events$")
    assert [r.url.params.get("pageToken") for r in pages[-3:]] == [None, "2", "4"]


def test_pantry_refusals_pass_through_and_nothing_is_written(env: CalendarEnv) -> None:
    env.pantry.refuse = (409, {"error": "needs_review", "detail": "The trip on Mon 12 Oct "
                                                                  "changed since you approved it."})
    r = env.preview(schedule())
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "needs_review", "message": "The trip on Mon 12 Oct "
                                                                     "changed since you approved it."}
    env.pantry.refuse = (500, {"error": "boom"})
    r = env.preview(schedule())
    assert r.status_code == 502 and r.json()["detail"]["code"] == "pantry_unavailable"
    env.pantry.refuse = (404, {"detail": "Not Found"})
    assert env.preview(schedule()).json()["detail"]["code"] == "pantry_too_old"
    assert env.google.writes == 0


def test_not_connected_is_a_409(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    r = env.preview(schedule())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_connected"
    assert env.pantry.calls == 0


def test_an_oversized_schedule_is_refused(env: CalendarEnv) -> None:
    big = schedule()
    big["padding"] = "x" * (600 * 1024)
    r = env.preview(big)
    assert r.status_code == 413 and env.pantry.calls == 0


def test_classify_is_pure_and_flags_unverified_updates() -> None:
    sched = schedule()
    events = preview(sched)["events"]
    today = dt.date(2026, 10, 9)
    ops = classify(events, [], {}, key="plan-abc", rev=1, today=today)
    assert [o.op for o in ops] == ["create"] * 5
    # a remote event of ours with no stamp of what the hub wrote and no ledger entry
    body = google_body(events[0], "plan-abc", 1, 0)
    body["extendedProperties"]["private"].pop("pantry_hash")
    remote = {**body, "summary": "changed", "etag": '"9"'}
    ops = classify(events, [remote], {}, key="plan-abc", rev=1, today=today)
    assert ops[0].op == "update" and ops[0].unverified
    assert ops[0].public()["note"] == "Could not check for edits made in Google Calendar."


def test_the_assistant_has_no_calendar_tool_and_traces_no_calendar_data(
        env: CalendarEnv) -> None:
    """Every calendar write is the shopper's click on a reviewed diff: nothing the model can
    call reaches the sync, and the Assistant's traces never hold calendar data."""
    from demo_hub import agent, assistant_policy, observers
    for module in (agent, assistant_policy, observers):
        source = inspect.getsource(module)
        assert "gcal" not in source and "/hub/calendar" not in source, module.__name__
    env.sync_now(schedule())
    traces = env.paths["traces"]
    text = "".join(p.read_text() for p in traces.rglob("*") if p.is_file()) \
        if traces.exists() else ""
    assert "Pantry plan" not in text and "pp1" not in text
