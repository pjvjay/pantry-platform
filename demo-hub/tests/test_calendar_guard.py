"""The calendar routes behind guard.py. (Every POST route is also in test_guard.GUARDED, which
checks all of them the same way.)"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fake_google import CalendarEnv, make_env

CALLBACK = "/hub/calendar/oauth/callback"


@pytest.fixture
def env(tmp_path: Path) -> CalendarEnv:
    return make_env(tmp_path)


def test_calendar_reads_are_checked_for_the_host(env: CalendarEnv) -> None:
    for path in ("/hub/calendar/status", f"{CALLBACK}?state=s&code=c"):
        r = env.client.get(path, headers={"Host": "evil.example"})
        assert r.status_code == 403 and "reason" in r.json(), path
        r = env.client.get(path, headers={"Host": "attacker.test:8090"})
        assert r.status_code == 403, path
    assert env.client.get("/hub/calendar/status").status_code == 200


def test_connect_from_another_site_never_reaches_the_route(env: CalendarEnv) -> None:
    r = env.client.post("/hub/calendar/connect", json={},
                        headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and "set-cookie" not in r.headers
    r = env.client.post("/hub/calendar/connect", content=b"{}",
                        headers={"Origin": "http://127.0.0.1:8090", "X-Pantry-Console": "",
                                 "Content-Type": "application/json"})
    assert r.status_code == 403
    r = env.client.post("/hub/calendar/connect", content=b"{}",
                        headers={"Origin": "http://127.0.0.1:8090",
                                 "Content-Type": "text/plain"})
    assert r.status_code == 415
    assert len(env.sync.pending) == 0
    # the console through Vite's proxy is let through
    r = env.client.post("/hub/calendar/connect", json={},
                        headers={"Origin": "http://localhost:5173", "Host": "localhost:5173"})
    assert r.status_code == 200 and "auth_url" in r.json()


def test_disconnect_and_apply_from_another_site_are_refused(env: CalendarEnv) -> None:
    env.connect()
    for path, body in (("/hub/calendar/disconnect", {"delete_calendar": True}),
                       ("/hub/calendar/sync/apply", {"schedule": {}, "preview_token": "0" * 64})):
        r = env.client.post(path, json=body, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403, path
    assert env.paths["token"].exists()
