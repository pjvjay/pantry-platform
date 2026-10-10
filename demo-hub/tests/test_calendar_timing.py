"""Times and reminders in the Google Calendar sync (gcal_sync.py, gcal_routes.py) against
FakeGoogle: the body of a timed event, the content hash and its words in the review, the optional
``timing`` the routes check and pass to pantry-api, and FakeGoogle's own reading of times and
reminders. An event from a pantry-api without these fields keeps today's body exactly."""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from demo_hub.gcal_sync import changed_fields, content_hash, google_body
from tests.fake_google import CalendarEnv, make_env, schedule, timing


@pytest.fixture
def env(tmp_path: Path) -> CalendarEnv:
    e = make_env(tmp_path)
    assert e.connect().status_code == 303
    return e


def items(env: CalendarEnv) -> dict[str, list[dict[str, Any]]]:
    return env.google.by_item()


def one(env: CalendarEnv, item: str) -> dict[str, Any]:
    [ev] = items(env)[item]
    return ev


def op(diff: dict[str, Any], item: str) -> dict[str, Any]:
    return next(o for o in diff["ops"] if o["item_id"] == item)


def event(**over: Any) -> dict[str, Any]:
    """A trip as pantry-api sends it today: all-day, none of the time fields."""
    return {"item_id": "trip-t1", "kind": "trip", "date": "2026-10-12", "end": "2026-10-13",
            "all_day": True, "title": "Groceries: Pantry Mart Downtown",
            "location": "Pantry Mart Downtown (demo store)",
            "description": "Pantry Mart Downtown\n- 1 x Tomatoes", **over}


TIMED = {"all_day": False, "start_time": "09:30", "end_time": "11:00",
         "timezone": "America/Vancouver"}

# what google_body wrote for event() before times and reminders existed, hash included
TODAYS_BODY = {
    "id": "pp1af2807b9334aac1dd8d30a59c94d21314c60457a",
    "summary": "Groceries: Pantry Mart Downtown",
    "description": "Pantry Mart Downtown\n- 1 x Tomatoes",
    "start": {"date": "2026-10-12"}, "end": {"date": "2026-10-13"},
    "transparency": "transparent", "status": "confirmed", "reminders": {"useDefault": False},
    "location": "Pantry Mart Downtown (demo store)",
    "extendedProperties": {"private": {
        "pantry_v": "1", "pantry_schedule": "plan-abc", "pantry_item": "trip-t1",
        "pantry_kind": "trip", "pantry_rev": "1", "pantry_gen": "0",
        "pantry_hash": "bf9f9325dcb33b3dd83caa23dc9425afe7e0069f94dabf538dfc005865ef6b4e"}}}


# --- the body ---------------------------------------------------------------------------------


def test_an_event_without_the_new_fields_is_todays_body() -> None:
    assert google_body(event(), "plan-abc", 1, 0) == TODAYS_BODY
    # the hash is the one events already in Google carry, so none of them reads as a change
    assert content_hash(TODAYS_BODY) == TODAYS_BODY["extendedProperties"]["private"][
        "pantry_hash"]
    # a newer pantry-api says the same thing with its fields empty
    newer = event(start_time=None, end_time=None, timezone=None, reminders_min=[])
    assert google_body(newer, "plan-abc", 1, 0) == TODAYS_BODY


def test_a_timed_event_has_a_date_time_a_zone_and_popup_reminders() -> None:
    ev = event(**TIMED, reminders_min=[60, 10, 60])
    body = google_body(ev, "plan-abc", 1, 0)
    assert body["start"] == {"dateTime": "2026-10-12T09:30:00", "timeZone": "America/Vancouver"}
    assert body["end"] == {"dateTime": "2026-10-12T11:00:00", "timeZone": "America/Vancouver"}
    assert body["reminders"] == {"useDefault": False, "overrides": [
        {"method": "popup", "minutes": 60}, {"method": "popup", "minutes": 10}]}
    assert body["transparency"] == "transparent" and "attendees" not in body
    assert body["extendedProperties"]["private"]["pantry_hash"] == content_hash(body)
    # `end` is the all-day end: the body is the same whichever day pantry-api puts there
    assert google_body({**ev, "end": "2026-10-12"}, "plan-abc", 1, 0) == body
    # a meal that ends after midnight ends on the next day
    late = google_body({**ev, "start_time": "23:30", "end_time": "00:15"}, "plan-abc", 1, 0)
    assert late["start"]["dateTime"] == "2026-10-12T23:30:00"
    assert late["end"]["dateTime"] == "2026-10-13T00:15:00"


def test_an_all_day_event_takes_reminders_too() -> None:
    body = google_body(event(reminders_min=[720]), "plan-abc", 1, 0)
    assert body["start"] == {"date": "2026-10-12"} and body["end"] == {"date": "2026-10-13"}
    assert body["reminders"] == {"useDefault": False,
                                 "overrides": [{"method": "popup", "minutes": 720}]}


def test_the_words_for_what_changed() -> None:
    timed = google_body(event(**TIMED, reminders_min=[30]), "plan-abc", 1, 0)

    def words(**over: Any) -> list[str]:
        return changed_fields(google_body(event(**{**TIMED, "reminders_min": [30], **over}),
                                          "plan-abc", 1, 0), timed)

    assert words() == []
    assert words(start_time="10:00") == ["time"]
    assert words(end_time="12:00") == ["time"]
    assert words(date="2026-10-13", end="2026-10-14") == ["date"]
    assert words(date="2026-10-13", start_time="10:00") == ["date", "time"]
    assert words(timezone="America/Toronto") == ["time"]
    assert words(reminders_min=[30, 5]) == ["reminders"]
    assert words(reminders_min=[]) == ["reminders"]
    assert words(title="Groceries", start_time="10:00", reminders_min=[]) == \
        ["title", "time", "reminders"]
    # all-day to timed is a time, timed to all-day too
    all_day = google_body(event(reminders_min=[30]), "plan-abc", 1, 0)
    assert changed_fields(timed, all_day) == ["time"]
    assert changed_fields(all_day, timed) == ["time"]
    # Google lists reminders in its own order and may add its offset; neither is a change
    remote = copy.deepcopy(timed)
    remote["start"] = {"dateTime": "2026-10-12T09:30:00-07:00", "timeZone": "America/Vancouver"}
    remote["end"] = {"dateTime": "2026-10-12T11:00:00-07:00", "timeZone": "America/Vancouver"}
    both = google_body(event(**TIMED, reminders_min=[30, 10]), "plan-abc", 1, 0)
    flipped = copy.deepcopy(both)
    flipped["reminders"]["overrides"].reverse()
    assert changed_fields(both, flipped) == [] and content_hash(both) == content_hash(flipped)
    assert changed_fields(timed, remote) == [] and content_hash(timed) == content_hash(remote)


# --- the sync, end to end ---------------------------------------------------------------------


def test_timed_events_and_reminders_reach_google_as_previewed(env: CalendarEnv) -> None:
    sched = schedule()
    diff, result = env.sync_now(sched, timing=timing())
    assert diff["counts"]["create"] == 5 and result["status"] == "ok"
    assert env.pantry.requests[-1]["timing"] == timing()
    trip, cook, thaw = one(env, "trip-t1"), one(env, "cook-m1"), one(env, "thaw-m2-7")
    # Google answers a wall-clock time and zone with the offset that day
    assert trip["start"] == {"dateTime": "2026-10-12T09:30:00-07:00",
                             "timeZone": "America/Vancouver"}
    assert trip["end"] == {"dateTime": "2026-10-12T11:00:00-07:00",
                           "timeZone": "America/Vancouver"}
    assert trip["reminders"] == {"useDefault": False, "overrides": [
        {"method": "popup", "minutes": 60}, {"method": "popup", "minutes": 10}]}
    assert cook["start"]["dateTime"] == "2026-10-13T18:00:00-07:00"
    assert cook["end"]["dateTime"] == "2026-10-13T18:45:00-07:00"
    assert cook["reminders"]["overrides"] == [{"method": "popup", "minutes": 30}]
    assert thaw["start"] == {"date": "2026-10-13"} and thaw["end"] == {"date": "2026-10-14"}
    assert thaw["reminders"]["overrides"] == [{"method": "popup", "minutes": 720}]
    for ev in (trip, cook, thaw):
        assert "attendees" not in ev and ev["transparency"] == "transparent"
        assert ev["extendedProperties"]["private"]["pantry_hash"] == content_hash(ev)


def test_syncing_timed_events_again_writes_nothing(env: CalendarEnv) -> None:
    sched = schedule()
    env.sync_now(sched, timing=timing())
    before = env.google.writes
    diff, result = env.sync_now(sched, timing=timing())
    assert diff["counts"]["noop"] == 5 and sum(diff["counts"].values()) == 5
    assert result["results"] == [] and env.google.writes == before
    # a lost ledger finds the same events and sees them as planned
    env.paths["ledger"].unlink()
    diff, _ = env.sync_now(sched, timing=timing())
    assert diff["counts"]["noop"] == 5 and env.google.writes == before


def test_a_plan_across_the_clock_change_keeps_its_clock_time(env: CalendarEnv) -> None:
    sched, toronto = schedule(start="2026-10-30"), timing(timezone="America/Toronto")
    env.sync_now(sched, timing=toronto)              # the clocks go back on Sun 1 Nov
    assert one(env, "cook-m1")["start"]["dateTime"] == "2026-10-31T18:00:00-04:00"
    assert one(env, "cook-m2")["start"]["dateTime"] == "2026-11-01T18:00:00-05:00"
    diff, _ = env.sync_now(sched, timing=toronto)
    assert diff["counts"]["noop"] == 5


def test_google_may_answer_in_its_own_zone_and_it_is_still_the_same_event(
        env: CalendarEnv) -> None:
    env.google.render_zone = "America/Vancouver"     # the calendar's, not the event's
    sched, toronto = schedule(), timing(timezone="America/Toronto")
    env.sync_now(sched, timing=toronto)
    cook = one(env, "cook-m1")
    assert cook["start"] == {"dateTime": "2026-10-13T15:00:00-07:00",
                             "timeZone": "America/Toronto"}
    before = env.google.writes
    assert env.sync_now(sched, timing=toronto)[0]["counts"]["noop"] == 5
    env.paths["ledger"].unlink()
    assert env.sync_now(sched, timing=toronto)[0]["counts"]["noop"] == 5
    assert env.google.writes == before
    later = timing(timezone="America/Toronto", cooks={"start": "19:00", "minutes": 45})
    diff, _ = env.sync_now(sched, timing=later)
    assert op(diff, "cook-m1")["changes"] == ["time"]


def test_a_new_time_is_an_edit_in_the_review_and_applies_as_reviewed(env: CalendarEnv) -> None:
    sched = schedule()
    env.sync_now(sched, timing=timing())
    later = timing(cooks={"start": "19:00", "minutes": 45})
    diff = env.preview(sched, timing=later).json()
    assert diff["counts"]["update"] == 3 and diff["counts"]["noop"] == 2
    assert [(o["item_id"], o["changes"]) for o in diff["ops"] if o["op"] == "update"] == \
        [(f"cook-m{i}", ["time"]) for i in (1, 2, 3)]
    # applied only as reviewed: not without the timing, not with another
    for other in ({}, {"timing": timing()}, {"timing": timing(cooks=None)}):
        r = env.apply(sched, diff, **other)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "preview_stale"
    assert one(env, "cook-m1")["start"]["dateTime"] == "2026-10-13T18:00:00-07:00"
    r = env.apply(sched, diff, timing=later)
    assert r.json()["status"] == "ok"
    assert one(env, "cook-m1")["start"]["dateTime"] == "2026-10-13T19:00:00-07:00"
    assert one(env, "cook-m1")["end"]["dateTime"] == "2026-10-13T19:45:00-07:00"
    assert env.preview(sched, timing=later).json()["counts"]["noop"] == 5


def test_new_reminders_are_an_edit_in_the_review(env: CalendarEnv) -> None:
    sched = schedule()
    env.sync_now(sched, timing=timing())
    more = timing(reminders={"trips": [], "cooks": [30, 5], "reminders": [720]})
    diff, result = env.sync_now(sched, timing=more)
    assert result["status"] == "ok" and diff["counts"]["update"] == 4
    assert op(diff, "trip-t1")["changes"] == ["reminders"]
    assert op(diff, "cook-m2")["changes"] == ["reminders"]
    assert one(env, "trip-t1")["reminders"] == {"useDefault": False}
    assert one(env, "cook-m2")["reminders"]["overrides"] == [
        {"method": "popup", "minutes": 30}, {"method": "popup", "minutes": 5}]
    # the order the console lists them in is not a change
    flipped = timing(reminders={"trips": [], "cooks": [5, 30], "reminders": [720]})
    assert env.preview(sched, timing=flipped).json()["counts"]["noop"] == 5


def test_adding_and_removing_times_moves_events_between_all_day_and_timed(
        env: CalendarEnv) -> None:
    sched = schedule()
    env.sync_now(sched)                              # as it always was
    assert one(env, "trip-t1")["reminders"] == {"useDefault": False}
    diff, _ = env.sync_now(sched, timing=timing())
    assert op(diff, "trip-t1")["changes"] == ["time", "reminders"]
    assert op(diff, "thaw-m2-7")["changes"] == ["reminders"]     # still all-day
    assert "dateTime" in one(env, "cook-m3")["start"]
    diff, _ = env.sync_now(sched)                    # and back
    assert op(diff, "trip-t1")["changes"] == ["time", "reminders"]
    ev = one(env, "trip-t1")
    assert ev["start"] == {"date": "2026-10-12"} and ev["end"] == {"date": "2026-10-13"}
    assert ev["reminders"] == {"useDefault": False}
    assert env.preview(sched).json()["counts"]["noop"] == 5


def test_a_moved_meal_with_a_new_time_says_both(env: CalendarEnv) -> None:
    sched = schedule()
    env.sync_now(sched, timing=timing())
    moved = copy.deepcopy(sched)
    moved["rev"] = 2
    moved["cooks"][1]["date"] = "2026-10-16"
    diff = env.preview(moved, timing=timing(cooks={"start": "17:30", "minutes": 45})).json()
    assert op(diff, "cook-m2")["changes"] == ["date", "time"]
    assert op(diff, "cook-m1")["changes"] == ["time"]


def test_an_edit_to_a_time_or_a_reminder_in_google_is_a_conflict(env: CalendarEnv) -> None:
    sched = schedule()
    env.sync_now(sched, timing=timing())
    cal = env.google.calendar_id
    env.google.user_edit(cal, one(env, "cook-m1")["id"], reminders={"useDefault": True})
    env.google.user_edit(cal, one(env, "cook-m2")["id"], start={
        "dateTime": "2026-10-14T17:00:00-07:00", "timeZone": "America/Vancouver"})
    diff = env.preview(sched, timing=timing()).json()
    assert [(o["item_id"], o["op"], o["changes"]) for o in diff["ops"] if o["op"] != "noop"] \
        == [("cook-m1", "conflict", ["reminders"]), ("cook-m2", "conflict", ["time"])]
    # kept by default; overwrite puts the plan back
    r = env.apply(sched, diff, {"cook-m1": "keep", "cook-m2": "overwrite"}, timing=timing())
    assert r.json()["status"] == "ok"
    assert one(env, "cook-m1")["reminders"] == {"useDefault": True}
    assert one(env, "cook-m2")["start"]["dateTime"] == "2026-10-14T18:00:00-07:00"


# --- the routes -------------------------------------------------------------------------------


def test_without_a_timing_pantry_api_is_asked_as_before(env: CalendarEnv) -> None:
    sched = schedule()
    env.sync_now(sched)
    assert set(env.pantry.requests[-1]) == {"schedule", "include"}
    # a newer pantry-api that sends its time fields empty changes nothing
    env.pantry.newer = True
    diff = env.preview(sched).json()
    assert diff["counts"]["noop"] == 5 and sum(diff["counts"].values()) == 5


def test_a_timing_with_nothing_set_is_passed_on_as_it_is(env: CalendarEnv) -> None:
    sched = schedule()
    r = env.preview(sched, timing={"timezone": "UTC"})
    assert r.status_code == 200 and r.json()["counts"]["create"] == 5
    assert env.pantry.requests[-1]["timing"] == {
        "timezone": "UTC", "trips": None, "cooks": None,
        "reminders": {"trips": [], "cooks": [], "reminders": []}}


@pytest.mark.parametrize("wrong", [
    {"timezone": "Not/AZone"}, {"timezone": ""}, {"timezone": "america/vancouver"},
    {"timezone": "../etc/passwd"}, {"timezone": None},
    {"trips": {"start": "24:00", "minutes": 60}}, {"trips": {"start": "9:30", "minutes": 60}},
    {"trips": {"start": "09:60", "minutes": 60}}, {"trips": {"start": "09:30\n", "minutes": 60}},
    {"trips": {"start": "٠٩:٣٠", "minutes": 60}}, {"cooks": {"start": "18:00", "minutes": -1}},
    {"cooks": {"start": "18:00", "minutes": 40321}}, {"cooks": {"minutes": 45}},
    {"cooks": {"start": "18:00", "minutes": 45, "ends": "19:00"}},
    {"reminders": {"trips": [-1], "cooks": [], "reminders": []}},
    {"reminders": {"trips": [40321], "cooks": [], "reminders": []}},
    {"reminders": {"trips": [1, 2, 3, 4], "cooks": [], "reminders": []}},
    {"reminders": {"trips": [], "cooks": [], "reminders": [1, 2, 3, 4]}},
    {"reminders": {"trips": ["soon"], "cooks": [], "reminders": []}},
    {"reminders": {"trips": [1.5], "cooks": [], "reminders": []}},
    {"reminders": {"trip": [10]}}, {"remind": [10]},
])
def test_a_timing_pantry_api_could_not_read_is_a_422_and_goes_nowhere(
        env: CalendarEnv, wrong: dict[str, Any]) -> None:
    sched = schedule()
    r = env.preview(sched, timing=timing(**wrong))
    assert r.status_code == 422, r.text
    diff = env.preview(sched).json()
    r = env.apply(sched, diff, timing=timing(**wrong))
    assert r.status_code == 422
    assert env.pantry.calls == 1 and env.google.writes == 0     # only the diff just above


def test_the_limits_themselves_are_fine(env: CalendarEnv) -> None:
    edge = timing(trips={"start": "00:00", "minutes": 0}, cooks={"start": "23:59", "minutes": 40320},
                  reminders={"trips": [0, 40320, 1], "cooks": [], "reminders": [5]},
                  timezone="Etc/GMT+8")
    assert env.preview(schedule(), timing=edge).status_code == 200


def test_a_pantry_api_that_ignores_the_timing_is_refused_not_written(env: CalendarEnv) -> None:
    env.pantry.ignores_timing = True                 # an older pantry-api: all-day, no reminders
    r = env.preview(schedule(), timing=timing())
    assert r.status_code == 502 and r.json()["detail"]["code"] == "pantry_too_old"
    assert env.google.writes == 0
    assert env.preview(schedule()).status_code == 200            # without a timing it is fine
    # a timing that sets nothing for the kinds in the plan has nothing to ignore
    only_trips = timing(cooks=None, reminders={"trips": [10], "cooks": [], "reminders": []})
    env.pantry.ignores_timing = False
    r = env.preview(schedule(), include=["cooks"], timing=only_trips)
    assert r.status_code == 200 and r.json()["counts"]["create"] == 3
    env.pantry.ignores_timing = True
    assert env.preview(schedule(), include=["cooks"], timing=only_trips).status_code == 200


@pytest.mark.parametrize("alter", [
    lambda e: e.update(start_time="9:30"), lambda e: e.update(end_time=None),
    lambda e: e.update(timezone="Mars/Olympus"), lambda e: e.update(timezone=None),
    lambda e: e.update(date="2026-13-40"),
    lambda e: e.update(reminders_min=[-1]), lambda e: e.update(reminders_min=[True]),
    lambda e: e.update(reminders_min=[40321]), lambda e: e.update(reminders_min=[1] * 6),
    lambda e: e.update(reminders_min="10"),
])
def test_an_event_the_hub_could_not_write_is_refused_whole(
        env: CalendarEnv, alter: Callable[[dict[str, Any]], None]) -> None:
    env.pantry.alter = lambda e: alter(e) if e["kind"] == "trip" else None
    r = env.preview(schedule(), timing=timing())
    assert r.status_code == 502 and r.json()["detail"]["code"] == "pantry_refused"
    assert env.google.writes == 0
