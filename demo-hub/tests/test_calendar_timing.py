"""Times and reminders in the Google body (gcal_sync.py): a timed event, popup reminders, the
content hash and its words in the review. An event from a pantry-api without the new fields keeps
today's body exactly."""

from __future__ import annotations

import copy
from typing import Any

from demo_hub.gcal_sync import changed_fields, content_hash, google_body


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
