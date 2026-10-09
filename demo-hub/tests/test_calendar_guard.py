"""The calendar routes behind guard.py, and RedactQuery keeping OAuth values out of the access
log. (Every POST route is also in test_guard.GUARDED, which checks all of them the same way.)"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from demo_hub import app as app_module
from demo_hub import redact
from demo_hub.redact import RedactQuery, redact_query
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


def test_redact_query_masks_oauth_values_in_an_access_log_line(
        caplog: pytest.LogCaptureFixture) -> None:
    line = (f"{CALLBACK}?state=Zm9vYmFyc3RhdGU&code=4/0AbCdEf-secret&scope=x&"
            "error=access_denied&token=abc&iss=https://accounts.google.com")
    masked = redact_query(line)
    for secret in ("Zm9vYmFyc3RhdGU", "4/0AbCdEf-secret", "access_denied", "=abc"):
        assert secret not in masked
    assert "state=***" in masked and "code=***" in masked and "scope=x" in masked
    assert "iss=https://accounts.google.com" in masked
    # as uvicorn logs it: the path with its query is the third argument
    logger = logging.getLogger("uvicorn.access.test")
    logger.addFilter(RedactQuery())
    with caplog.at_level(logging.INFO, logger="uvicorn.access.test"):
        logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5555", "GET", line, "1.1", 303)
    assert "4/0AbCdEf-secret" not in caplog.text and "Zm9vYmFyc3RhdGU" not in caplog.text
    assert f"GET {CALLBACK}?state=***&code=***" in caplog.text
    # a message without arguments, and arguments as a mapping
    record = logging.LogRecord("x", logging.INFO, "f", 1, f"GET {CALLBACK}?code=zzz", None, None)
    RedactQuery().filter(record)
    assert record.getMessage() == f"GET {CALLBACK}?code=***"
    record = logging.LogRecord("x", logging.INFO, "f", 1, "%(p)s", None, None)
    record.args = {"p": "/x?refresh_token=r1"}
    RedactQuery().filter(record)
    assert record.getMessage() == "/x?refresh_token=***"


def test_main_installs_the_redaction_on_uvicorns_access_log(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """main() builds uvicorn's Config (which sets up its loggers) and only then installs
    RedactQuery, so uvicorn's own logging setup cannot drop it."""
    seen: dict[str, Any] = {}
    access = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(access, "filters", [])

    class Config:
        def __init__(self, app: Any, host: str, port: int) -> None:
            seen["config"] = (host, port)
            assert not any(isinstance(f, RedactQuery) for f in access.filters)

    class Server:
        def __init__(self, config: Config) -> None:
            pass

        def run(self) -> None:
            seen["filters"] = list(access.filters)

    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(Config=Config,
                                                                      Server=Server))
    monkeypatch.setattr(app_module, "create_app", lambda settings: object())
    app_module.main()
    assert any(isinstance(f, RedactQuery) for f in seen["filters"])
    assert isinstance(seen["config"][1], int)
    assert redact.install() is redact.install()       # installing twice adds one filter
    assert sum(isinstance(f, RedactQuery) for f in access.filters) == 1
