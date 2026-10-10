"""FakeGoogle's reading of event times and reminders, so the sync's tests rest on what Google
does: a start and end of one kind, a zone for a wall-clock time, an answer with the day's offset,
and reminders that are either the defaults or a few overrides."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from demo_hub.gcal_client import BadRequest
from tests.fake_google import CalendarEnv, make_env, schedule

VANCOUVER = "America/Vancouver"
TIMED: dict[str, Any] = {
    "summary": "Cook: Dinner 1 (dinner)", "transparency": "transparent", "status": "confirmed",
    "extendedProperties": {"private": {"pantry_schedule": "plan-abc", "pantry_item": "cook-m1"}},
    "start": {"dateTime": "2026-10-13T18:00:00", "timeZone": VANCOUVER},
    "end": {"dateTime": "2026-10-13T18:45:00", "timeZone": VANCOUVER},
    "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]}}


@pytest.fixture
def env(tmp_path: Path) -> CalendarEnv:
    e = make_env(tmp_path)
    assert e.connect().status_code == 303
    e.sync_now(schedule())
    return e


def update(env: CalendarEnv, **over: Any) -> dict[str, Any]:
    """PUT TIMED (with ``over``) over the first cook, through the hub's client."""
    cal, [ev] = env.google.calendar_id, env.google.by_item()["cook-m1"]
    body = {**TIMED, "id": ev["id"], **over}
    return asyncio.run(env.sync.client().events_update(cal, ev["id"], body, ev["etag"]))


@pytest.mark.parametrize(("over", "reason"), [
    ({"start": {"dateTime": "2026-10-13T18:00:00"}}, "required"),
    ({"start": {"dateTime": "2026-10-13T18:00:00", "timeZone": "Not/AZone"}}, "invalid"),
    ({"start": {"dateTime": "half past six", "timeZone": VANCOUVER}}, "invalid"),
    ({"start": {"date": "2026-10-13"}}, "invalid"),
    ({"end": {"date": "2026-10-14"}}, "invalid"),
    ({"end": {"dateTime": "2026-10-13T17:00:00", "timeZone": VANCOUVER}}, "timeRangeEmpty"),
    ({"start": {"date": "2026-10-13"}, "end": {"date": "2026-10-13"}}, "timeRangeEmpty"),
    ({"start": None}, "required"),
    ({"reminders": {"useDefault": True, "overrides": [{"method": "popup", "minutes": 10}]}},
     "cannotUseDefaultRemindersAndSpecifyOverride"),
    ({"reminders": {"useDefault": False, "overrides": [{"method": "sms", "minutes": 10}]}},
     "invalid"),
    ({"reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 40321}]}},
     "invalid"),
    ({"reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": -1}]}},
     "invalid"),
    ({"reminders": {"useDefault": False,
                    "overrides": [{"method": "popup", "minutes": m} for m in range(6)]}},
     "invalid"),
])
def test_what_google_refuses_the_fake_refuses(
        env: CalendarEnv, over: dict[str, Any], reason: str) -> None:
    with pytest.raises(BadRequest) as refused:
        update(env, **over)
    assert refused.value.reason == reason
    assert env.google.by_item()["cook-m1"][0]["start"] == {"date": "2026-10-13"}   # not stored


def test_a_wall_clock_time_and_zone_come_back_with_that_days_offset(env: CalendarEnv) -> None:
    answer = update(env)
    assert answer["start"] == {"dateTime": "2026-10-13T18:00:00-07:00", "timeZone": VANCOUVER}
    assert answer["end"] == {"dateTime": "2026-10-13T18:45:00-07:00", "timeZone": VANCOUVER}
    assert answer["reminders"] == TIMED["reminders"]
    assert env.google.by_item()["cook-m1"][0]["start"] == answer["start"]
    # the clocks go back in Toronto on Sun 1 Nov
    answer = update(env, start={"dateTime": "2026-10-31T18:00:00", "timeZone": "America/Toronto"},
                    end={"dateTime": "2026-11-01T18:00:00", "timeZone": "America/Toronto"})
    assert answer["start"]["dateTime"] == "2026-10-31T18:00:00-04:00"
    assert answer["end"]["dateTime"] == "2026-11-01T18:00:00-05:00"


def test_an_offset_is_enough_without_a_zone_and_defaults_are_kept(env: CalendarEnv) -> None:
    answer = update(env, start={"dateTime": "2026-10-13T18:00:00-07:00"},
                    end={"dateTime": "2026-10-13T18:45:00-07:00"},
                    reminders={"useDefault": True})
    assert answer["start"] == {"dateTime": "2026-10-13T18:00:00-07:00"}
    assert answer["reminders"] == {"useDefault": True}


def test_an_event_may_be_read_back_in_another_zone_than_its_own(env: CalendarEnv) -> None:
    env.google.render_zone = VANCOUVER
    answer = update(env, start={"dateTime": "2026-10-13T18:00:00", "timeZone": "America/Toronto"},
                    end={"dateTime": "2026-10-13T18:45:00", "timeZone": "America/Toronto"})
    assert answer["start"] == {"dateTime": "2026-10-13T15:00:00-07:00",
                               "timeZone": "America/Toronto"}
