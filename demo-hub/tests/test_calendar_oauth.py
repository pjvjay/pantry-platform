"""Google Calendar sign-in (gcal_oauth.py, gcal_routes.py) against FakeGoogle: the consent URL,
the state and its cookie, the code exchange, the refresh token on disk, reconnecting and
disconnecting. No test reads the real client file or calls Google."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from demo_hub.gcal_oauth import (
    SCOPE,
    Connection,
    OAuthClientConfig,
    OAuthError,
    PendingAuths,
    TokenStore,
    code_challenge,
    write_private,
)
from tests.fake_google import CLIENT_SECRET, CalendarEnv, make_env, schedule

CALLBACK = "/hub/calendar/oauth/callback"


@pytest.fixture
def env(tmp_path: Path) -> CalendarEnv:
    return make_env(tmp_path)


def start(env: CalendarEnv, origin: str = "http://127.0.0.1:8090",
          **headers: str) -> tuple[Any, dict[str, str]]:
    r = env.client.post("/hub/calendar/connect", json={}, headers={"Origin": origin, **headers})
    assert r.status_code == 200, r.text
    return r, {k: v[0] for k, v in parse_qs(urlsplit(r.json()["auth_url"]).query).items()}


def test_the_consent_url_asks_for_the_app_calendar_scope_only_with_pkce(env: CalendarEnv) -> None:
    r, q = start(env)
    assert r.json()["auth_url"].startswith(env.google.AUTH_URL + "?")
    assert q["scope"] == SCOPE                       # exactly one scope, nothing else
    assert q["response_type"] == "code" and q["access_type"] == "offline"
    assert q["prompt"] == "consent" and "include_granted_scopes" not in q
    assert q["redirect_uri"] == "http://127.0.0.1:8090" + CALLBACK
    assert q["code_challenge_method"] == "S256" and len(q["state"]) >= 43
    assert r.headers["cache-control"] == "no-store"
    # the challenge is BASE64URL(SHA256(verifier)) of the verifier the hub kept
    pending = env.sync.pending.pop(q["state"])
    assert pending is not None
    digest = hashlib.sha256(pending.code_verifier.encode()).digest()
    assert q["code_challenge"] == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert 43 <= len(pending.code_verifier) <= 128
    assert code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == \
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"        # RFC 7636 appendix B


def test_the_state_cookie_is_httponly_lax_scoped_and_short_lived(env: CalendarEnv) -> None:
    r, _ = start(env)
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("pantry_oauth=")
    for part in ("HttpOnly", "Max-Age=600", "Path=/hub/calendar/oauth", "SameSite=lax"):
        assert part.lower() in cookie.lower(), cookie
    # the nonce in the cookie is not the state, and only its hash is kept
    nonce = cookie.split(";")[0].split("=", 1)[1]
    assert nonce not in r.json()["auth_url"]


def test_the_redirect_comes_from_the_consoles_registered_origin(env: CalendarEnv) -> None:
    # Vite's dev server keeps the browser's Host and Origin; its callback is registered too
    _, q = start(env, "http://localhost:5173", Host="localhost:5173")
    assert q["redirect_uri"] == "http://localhost:5173" + CALLBACK
    # an origin the guard lets through but the OAuth client does not list
    r = env.client.post("/hub/calendar/connect", json={},
                        headers={"Origin": "http://localhost:8090", "Host": "localhost:8090"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "origin_not_registered"
    assert "http://127.0.0.1:8090/pantry/" in r.json()["detail"]["message"]
    # a script with no Origin cannot start a browser sign-in
    r = env.client.post("/hub/calendar/connect", json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "origin_not_registered"
    # the status says whether this console can connect from where it is
    assert env.client.get("/hub/calendar/status").json()["can_connect_here"] is True
    assert env.client.get("/hub/calendar/status",
                          headers={"Host": "localhost:8090"}).json()["can_connect_here"] is False
    assert len(env.sync.pending) == 1


def test_hub_oauth_origins_narrow_who_may_sign_in(tmp_path: Path) -> None:
    env = make_env(tmp_path, oauth_origins=("http://127.0.0.1:8090",))
    start(env)
    r = env.client.post("/hub/calendar/connect", json={},
                        headers={"Origin": "http://localhost:5173", "Host": "localhost:5173"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "origin_not_registered"


def test_a_desktop_client_takes_loopback_redirects(tmp_path: Path) -> None:
    env = make_env(tmp_path, kind="installed", redirect_uris=("http://localhost",))
    assert env.client.get("/hub/calendar/status").json()["client_type"] == "installed"
    r = env.connect()
    assert r.status_code == 303 and r.headers["location"].endswith("calendar=connected")


def test_without_a_client_file_nothing_is_offered(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.paths["client"].unlink()
    status = env.client.get("/hub/calendar/status").json()
    assert status["configured"] is False and status["connected"] is False
    assert env.client.get("/hub/status").json()["keys"]["google_calendar_client"] is False
    r = env.client.post("/hub/calendar/connect", json={},
                        headers={"Origin": "http://127.0.0.1:8090"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_configured"
    r = env.preview(schedule())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_configured"
    # a file that is there but is not a client says so, without its contents
    env.paths["client"].write_text(json.dumps({"other": {"client_id": "x"}}))
    status = env.client.get("/hub/calendar/status").json()
    assert status["configured"] is False and "no usable" in status["problem"]


def test_connecting_stores_a_mode_600_refresh_token_and_returns_to_the_console(
        env: CalendarEnv) -> None:
    r = env.connect()
    assert r.status_code == 303
    assert r.headers["location"] == "/pantry/#/mealplan?calendar=connected"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert 'pantry_oauth=""' in r.headers["set-cookie"] or "Max-Age=0" in r.headers["set-cookie"]
    token = env.paths["token"]
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert stat.S_IMODE(token.parent.stat().st_mode) == 0o700
    saved = json.loads(token.read_text())
    assert saved["refresh_token"] in env.google.refresh_tokens and saved["scope"] == SCOPE
    assert set(saved) == {"refresh_token", "scope", "connection_id", "connected_at",
                          "calendar_id"}
    # the exchange sent the verifier, the secret (a web client) and the same redirect URI
    form = parse_qs(env.google.calls("POST", "/token")[0].content.decode())
    assert form["grant_type"] == ["authorization_code"] and form["code_verifier"][0]
    assert form["client_secret"] == [CLIENT_SECRET]
    assert form["redirect_uri"] == ["http://127.0.0.1:8090" + CALLBACK]
    status = env.client.get("/hub/calendar/status").json()
    assert status["connected"] is True and status["reconnect_by"]
    keys = env.client.get("/hub/status").json()["keys"]
    assert keys["google_calendar_client"] is True and keys["google_calendar_connected"] is True
    # connecting writes nothing to Google
    assert env.google.writes == 0


def test_return_to_is_a_console_route_and_nothing_else(env: CalendarEnv) -> None:
    r = env.client.post("/hub/calendar/connect", json={"return_to": "#/system"},
                        headers={"Origin": "http://127.0.0.1:8090"})
    back = env.google.authorize(r.json()["auth_url"])
    r = env.client.get(CALLBACK, params=back, follow_redirects=False)
    assert r.headers["location"] == "/pantry/#/system?calendar=connected"
    for bad in ("https://evil.example/", "//evil.example", "#/a?b=c", "javascript:x"):
        r = env.client.post("/hub/calendar/connect", json={"return_to": bad},
                            headers={"Origin": "http://127.0.0.1:8090"})
        assert r.status_code == 422, bad


@pytest.mark.parametrize("case", ["unknown", "expired", "reused", "other_cookie", "no_cookie"])
def test_a_bad_state_or_cookie_is_a_400_with_nothing_stored(env: CalendarEnv, case: str) -> None:
    r, _ = start(env)
    back = env.google.authorize(r.json()["auth_url"])
    if case == "unknown":
        back["state"] = "not-a-state-the-hub-issued"
    elif case == "expired":
        env.sync.pending.clock = lambda: 10.0 ** 9           # 600 s are long gone
    elif case == "reused":
        assert env.client.get(CALLBACK, params=back, follow_redirects=False).status_code == 303
        env.paths["token"].unlink()
    elif case == "other_cookie":
        env.client.cookies.set("pantry_oauth", "a-cookie-from-another-sign-in",
                               domain="127.0.0.1", path="/hub/calendar/oauth")
    elif case == "no_cookie":
        env.client.cookies.clear()
    exchanges = len(env.google.calls("POST", "/token"))
    r = env.client.get(CALLBACK, params=back, follow_redirects=False)
    assert r.status_code == 400 and "Nothing was stored" in r.text
    assert r.headers["cache-control"] == "no-store"
    assert not env.paths["token"].exists()
    assert len(env.google.calls("POST", "/token")) == exchanges      # the code was never used


def test_the_pending_sign_ins_expire_and_are_capped() -> None:
    now = [0.0]
    pending = PendingAuths(clock=lambda: now[0], ttl_s=600, cap=5)
    states = [pending.start("http://127.0.0.1:8090" + CALLBACK, "#/mealplan")[0]
              for _ in range(6)]
    assert len(pending) == 5 and pending.pop(states[0]) is None      # the oldest went first
    assert pending.pop(states[1]) is not None and pending.pop(states[1]) is None
    now[0] = 601
    assert pending.pop(states[2]) is None and len(pending) == 0
    assert pending.pop("") is None


def test_a_refusal_at_google_is_reported_and_stores_nothing(env: CalendarEnv) -> None:
    _, q = start(env)
    r = env.client.get(CALLBACK, params={"error": "access_denied", "state": q["state"]},
                       follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/pantry/#/mealplan?calendar=denied&reason=access_denied"
    assert not env.paths["token"].exists()
    # an error word Google might send is not echoed into the console's URL
    _, q = start(env)
    r = env.client.get(CALLBACK, params={"error": "<script>", "state": q["state"]},
                       follow_redirects=False)
    assert r.headers["location"].endswith("calendar=error&reason=google_error")


@pytest.mark.parametrize("setup,reason", [
    ({"send_refresh_token": False}, "no_refresh_token"),
    ({"grant_scope": "https://www.googleapis.com/auth/calendar.readonly"}, "scope_not_granted"),
])
def test_no_refresh_token_or_scope_is_an_error_with_nothing_stored(
        env: CalendarEnv, setup: dict[str, Any], reason: str) -> None:
    for k, v in setup.items():
        setattr(env.google, k, v)
    r = env.connect()
    assert r.status_code == 303
    assert r.headers["location"] == f"/pantry/#/mealplan?calendar=error&reason={reason}"
    assert not env.paths["token"].exists()
    assert env.client.get("/hub/calendar/status").json()["connected"] is False


def test_the_token_file_is_written_atomically_and_never_readable_by_others(
        tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "token.json"
    old = os.umask(0)                    # even with a permissive umask
    try:
        store = TokenStore(str(path))
        store.save(Connection("rt-1", SCOPE, "c1", "2026-10-09T00:00:00+00:00"))
        store.save(Connection("rt-2", SCOPE, "c2", "2026-10-09T00:00:00+00:00", "cal1"))
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["token.json"]     # no temp file left
    loaded = store.load()
    assert loaded is not None and loaded.refresh_token == "rt-2" and loaded.calendar_id == "cal1"
    assert "rt-2" not in repr(loaded)
    # a failed write leaves the old file whole
    real_replace = os.replace

    def broken(*a: Any) -> None:
        raise OSError("disk full")

    os.replace = broken                   # type: ignore[assignment]
    try:
        with pytest.raises(OSError):
            write_private(path, "half")
    finally:
        os.replace = real_replace         # type: ignore[assignment]
    assert json.loads(path.read_text())["refresh_token"] == "rt-2"
    assert [p.name for p in path.parent.iterdir()] == ["token.json"]
    # a token file others can read is refused, not used
    path.chmod(0o644)
    with pytest.raises(OAuthError) as e:
        store.load()
    assert e.value.code == "token_file" and "rt-2" not in e.value.message


def test_a_token_file_readable_by_others_is_not_used(env: CalendarEnv) -> None:
    env.connect()
    env.paths["token"].chmod(0o644)
    status = env.client.get("/hub/calendar/status").json()
    assert status["connected"] is False and "can be read by others" in status["problem"]
    assert env.client.get("/hub/status").json()["keys"]["google_calendar_connected"] is False
    r = env.preview(schedule())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "token_file"


def test_no_secret_reaches_status_logs_traces_settings_or_the_ledger(
        env: CalendarEnv, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    env.connect()
    env.sync_now(schedule())
    refresh = json.loads(env.paths["token"].read_text())["refresh_token"]
    access = sorted(env.google.access_tokens)
    secrets = [refresh, *access, CLIENT_SECRET, "fake-client-id"]
    exposed = {
        "settings repr": repr(env.app.state.settings),
        "client repr": repr(OAuthClientConfig.load(str(env.paths["client"]))),
        "hub status": env.client.get("/hub/status").text,
        "calendar status": env.client.get("/hub/calendar/status").text,
        "logs": caplog.text,
        "ledger": env.paths["ledger"].read_text(),
        "traces": "".join(p.read_text() for p in env.paths["traces"].rglob("*") if p.is_file())
        if env.paths["traces"].exists() else "",
        "metrics": env.client.get("/hub/metrics").text,
    }
    for where, text in exposed.items():
        for secret in secrets:
            assert secret not in text, f"a secret is in the {where}"
    assert stat.S_IMODE(env.paths["ledger"].stat().st_mode) == 0o600
    assert stat.S_IMODE(env.paths["calendar_dir"].stat().st_mode) == 0o700


def test_invalid_grant_asks_for_a_reconnect(env: CalendarEnv) -> None:
    env.connect()
    env.sync_now(schedule())
    env.google.invalid_grant = True                  # Testing mode's 7 days are up
    env.google.expire_access_tokens()
    env.sync.connector._access = None                # the hub restarted: no access token
    r = env.preview(schedule())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "needs_reconnect"
    status = env.client.get("/hub/calendar/status").json()
    assert status["needs_reconnect"] is True and status["connected"] is False
    assert env.client.get("/hub/status").json()["keys"]["google_calendar_connected"] is False
    # connecting again clears it, and keeps the same Pantry plan calendar
    env.google.invalid_grant = False
    assert env.connect().headers["location"].endswith("calendar=connected")
    diff, _ = env.sync_now(schedule())
    assert diff["calendar_action"] == "existing" and diff["counts"]["noop"] == 5
    assert len(env.google.calendars) == 1


def test_an_expired_access_token_is_refreshed_once(env: CalendarEnv) -> None:
    env.connect()
    env.google.expire_access_tokens()
    _, result = env.sync_now(schedule())
    assert result["status"] == "ok" and len(env.google.live()) == 5
    refreshes = [r for r in env.google.calls("POST", "/token")
                 if b"grant_type=refresh_token" in r.content]
    assert len(refreshes) == 1


def test_a_reconnect_as_another_account_starts_a_new_calendar(env: CalendarEnv) -> None:
    env.connect()
    env.sync_now(schedule())
    first = env.google.calendar_id
    env.google.calendars.clear()                     # this account cannot see that calendar
    env.google.events.clear()
    env.connect()
    diff, _ = env.sync_now(schedule())
    assert diff["calendar_action"] == "create" and env.google.calendar_id != first


@pytest.mark.parametrize("revoke_fails", [False, True])
def test_disconnect_revokes_and_always_deletes_the_token(env: CalendarEnv,
                                                         revoke_fails: bool) -> None:
    env.connect()
    env.sync_now(schedule())
    refresh = json.loads(env.paths["token"].read_text())["refresh_token"]
    env.google.revoke_fails = revoke_fails
    r = env.client.post("/hub/calendar/disconnect", json={"delete_calendar": False})
    assert r.status_code == 200
    body = r.json()
    assert body["revoked"] is (not revoke_fails) and body["calendar_deleted"] is False
    assert "stays in your Google account" in body["note"]
    assert not env.paths["token"].exists()
    assert (refresh in env.google.revoked) is (not revoke_fails)
    assert len(env.google.calendars) == 1            # kept unless asked
    status = env.client.get("/hub/calendar/status").json()
    assert status["connected"] is False and status["calendar"] is None
    assert json.loads(env.paths["ledger"].read_text())["calendar"] is None
    # revoke went to the revoke endpoint in a form body, never in a URL
    for req in env.google.calls("POST", "/revoke"):
        assert refresh not in str(req.url)


def test_disconnect_can_delete_the_pantry_plan_calendar(env: CalendarEnv) -> None:
    env.connect()
    env.sync_now(schedule())
    r = env.client.post("/hub/calendar/disconnect", json={"delete_calendar": True})
    assert r.json()["calendar_deleted"] is True and env.google.calendars == {}
    # not connected: nothing to revoke, nothing to delete, still an answer
    r = env.client.post("/hub/calendar/disconnect", json={"delete_calendar": True})
    assert r.status_code == 200 and r.json()["revoked"] is False


def test_status_reports_labels_and_booleans_only(env: CalendarEnv) -> None:
    status = env.client.get("/hub/calendar/status").json()
    assert status["scope"] == "calendar.app.created" and status["all_day"] is True
    assert "7 days" in status["testing_note"]
    for value in status.values():
        assert not isinstance(value, str) or "apps.googleusercontent" not in value
