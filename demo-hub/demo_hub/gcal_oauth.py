"""Google Calendar sign-in for the local hub: the user's own OAuth client, PKCE, a state bound to
a cookie, and the refresh token kept in a mode-600 file (docs/google-calendar.md).

* **The client** is the JSON the user downloaded from their own Cloud project, read at runtime
  from ``GOOGLE_OAUTH_CLIENT_FILE``. A ``web`` client (the documented default) lists its
  redirect URIs, and the hub only offers a redirect that is listed there; an ``installed``
  (Desktop) client is accepted too. Its secret never leaves this module's objects (repr=False).
* **Connect** makes a PKCE verifier (S256), a 32-byte ``state`` and a nonce. The state maps to
  the verifier, the nonce's hash, the redirect URI and where to return, for 10 minutes, at most
  five at once, and is popped on first use. The nonce goes to the browser as the HttpOnly,
  SameSite=Lax ``pantry_oauth`` cookie, path ``/hub/calendar/oauth``. The redirect URI is the
  console's own origin (``<Origin>/hub/calendar/oauth/callback``), so the callback lands where
  the cookie was set: the hub on 127.0.0.1:8090, or Vite on localhost:5173 through its proxy.
* **The callback** needs a state the hub issued, unexpired and unused, and the matching cookie;
  then it exchanges the code with the verifier (and the secret, for a web client) and stores the
  refresh token only when Google granted exactly the calendar scope and sent a refresh token.
* **The refresh token** is written to a temp file opened O_EXCL with mode 0600, fsynced and
  moved into place; a token file readable by others is refused. The access token lives in memory
  only. ``invalid_grant`` (about weekly while the OAuth app is in Testing) asks for a reconnect.

Scope: ``calendar.app.created`` only, so the hub can see and change only calendars it created.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

SCOPE = "https://www.googleapis.com/auth/calendar.app.created"
CALLBACK_PATH = "/hub/calendar/oauth/callback"
COOKIE = "pantry_oauth"
COOKIE_PATH = "/hub/calendar/oauth"
PENDING_TTL_S = 600
MAX_PENDING = 5
# While the OAuth consent screen is in Testing, Google ends a refresh token after 7 days.
TESTING_DAYS = 7
TESTING_NOTE = ("While your Google OAuth app is in Testing, Google ends the connection after 7 "
                "days: connect again when the console asks.")

ClientType = Literal["web", "installed"]


class OAuthError(Exception):
    """A sign-in step that failed. ``code`` is a short machine word the console can show:
    not_configured, bad_client_file, origin_not_registered, exchange_failed, scope_not_granted,
    no_refresh_token, invalid_grant, token_file."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


# --- the user's OAuth client ------------------------------------------------------------------


@dataclass(frozen=True)
class OAuthClientConfig:
    client_type: ClientType
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    redirect_uris: tuple[str, ...] = field(default=(), repr=False)

    @staticmethod
    def parse(raw: Any) -> OAuthClientConfig:
        """The ``web`` or ``installed`` block of a client JSON from the Cloud console."""
        if not isinstance(raw, dict):
            raise OAuthError("bad_client_file", "The OAuth client file is not a JSON object.")
        for kind in ("web", "installed"):
            block = raw.get(kind)
            if isinstance(block, dict):
                cid, secret = block.get("client_id"), block.get("client_secret", "")
                uris = block.get("redirect_uris") or []
                if not isinstance(cid, str) or not cid or not isinstance(secret, str) \
                        or not isinstance(uris, list):
                    break
                return OAuthClientConfig(kind, cid, secret,  # type: ignore[arg-type]
                                         tuple(u for u in uris if isinstance(u, str)))
        raise OAuthError("bad_client_file", "The OAuth client file has no usable 'web' or "
                         "'installed' client (download it again from the Cloud console).")

    @staticmethod
    def load(path: str) -> OAuthClientConfig | None:
        """The client, or None when no file is configured or it is missing. A file that is there
        but unreadable raises OAuthError(bad_client_file)."""
        if not path:
            return None
        p = Path(path).expanduser()
        if not p.is_file():
            return None
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OAuthError("bad_client_file",
                             "The OAuth client file is not readable JSON.") from exc
        return OAuthClientConfig.parse(raw)

    def accepts(self, redirect_uri: str) -> bool:
        """A web client takes only the redirect URIs registered on it. A Desktop client takes
        loopback redirects (Google's documented rule for installed apps)."""
        if self.client_type == "web":
            return redirect_uri in self.redirect_uris
        return redirect_uri.startswith(("http://127.0.0.1:", "http://localhost:",
                                        "http://[::1]:"))


# --- PKCE and the pending sign-ins ------------------------------------------------------------


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def code_challenge(verifier: str) -> str:
    """RFC 7636 S256: BASE64URL(SHA256(verifier)), no padding."""
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class Pending:
    code_verifier: str = field(repr=False)
    nonce_hash: str = field(repr=False)
    redirect_uri: str
    return_to: str
    created: float


class PendingAuths:
    """Sign-ins started and not yet finished, by state. In memory: a hub restart forgets them,
    and the shopper starts again."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, ttl_s: float = PENDING_TTL_S,
                 cap: int = MAX_PENDING) -> None:
        self.clock, self.ttl_s, self.cap = clock, ttl_s, cap
        self._by_state: dict[str, Pending] = {}

    def start(self, redirect_uri: str, return_to: str) -> tuple[str, str, str]:
        """(state, nonce, code_verifier) for a new sign-in; the oldest is dropped past the cap."""
        self._expire()
        while len(self._by_state) >= self.cap:
            self._by_state.pop(next(iter(self._by_state)))
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)          # 86 characters, within RFC 7636's 43-128
        self._by_state[state] = Pending(verifier, digest(nonce), redirect_uri, return_to,
                                        self.clock())
        return state, nonce, verifier

    def pop(self, state: str) -> Pending | None:
        """The sign-in for ``state``, removed whatever happens next; None when unknown, used or
        expired."""
        self._expire()
        return self._by_state.pop(state, None) if state else None

    def _expire(self) -> None:
        now = self.clock()
        for s in [s for s, p in self._by_state.items() if now - p.created > self.ttl_s]:
            del self._by_state[s]

    def __len__(self) -> int:
        return len(self._by_state)


def nonce_matches(cookie: str | None, pending: Pending) -> bool:
    return bool(cookie) and hmac.compare_digest(digest(cookie or ""), pending.nonce_hash)


def auth_url(base: str, client: OAuthClientConfig, redirect_uri: str, state: str,
             verifier: str) -> str:
    params = {
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "S256",
    }
    return f"{base}?{urlencode(params)}"


# --- the refresh token on disk ----------------------------------------------------------------


@dataclass(frozen=True)
class Connection:
    """What the token file holds. calendar_id is the app's "Pantry plan" calendar once made: kept
    here too, so a lost sync ledger never leads to a second calendar."""
    refresh_token: str = field(repr=False)
    scope: str
    connection_id: str
    connected_at: str
    calendar_id: str | None = None

    def reconnect_by(self) -> str | None:
        try:
            at = dt.datetime.fromisoformat(self.connected_at)
        except ValueError:
            return None
        return (at + dt.timedelta(days=TESTING_DAYS)).date().isoformat()


class TokenStore:
    """``google_calendar_token.json``: mode 0600 in a 0700 directory, written atomically."""

    def __init__(self, path: str) -> None:
        self.path = Path(path).expanduser() if path else None

    def load(self) -> Connection | None:
        """The connection, or None when there is none. Raises OAuthError(token_file) for a file
        others can read or one that is not a token file: it is never used."""
        if self.path is None or not self.path.exists():
            return None
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & 0o077:
            raise OAuthError("token_file", f"The token file {self.path} can be read by others "
                             f"(mode {mode:o}); it was not used. Delete it and connect again.")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return Connection(refresh_token=str(raw["refresh_token"]), scope=str(raw["scope"]),
                              connection_id=str(raw["connection_id"]),
                              connected_at=str(raw["connected_at"]),
                              calendar_id=raw.get("calendar_id") or None)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise OAuthError("token_file", f"The token file {self.path} is not readable; "
                             "connect again.") from exc

    def save(self, conn: Connection) -> None:
        if self.path is None:
            raise OAuthError("not_configured", "No token file is configured.")
        write_private(self.path, json.dumps({
            "refresh_token": conn.refresh_token, "scope": conn.scope,
            "connection_id": conn.connection_id, "connected_at": conn.connected_at,
            "calendar_id": conn.calendar_id}, indent=1) + "\n")

    def delete(self) -> bool:
        if self.path is None:
            return False
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so no other user can ever read it, and no reader ever sees half
    a file: a temp file beside it created O_EXCL with mode 0600, fsynced, then os.replace."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)        # whatever the umask did
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:                                   # the rename itself, durably
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


# --- Google's token endpoint ------------------------------------------------------------------


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("error", "")) if isinstance(body, dict) else ""


class GoogleOAuth:
    """Code exchange, refresh and revoke against Google's endpoints (overridable for tests).
    Nothing here logs a request body or a token."""

    def __init__(self, token_url: str, revoke_url: str,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.token_url, self.revoke_url, self.transport = token_url, revoke_url, transport

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=20, transport=self.transport, trust_env=False)

    @staticmethod
    def _client_fields(client: OAuthClientConfig) -> dict[str, str]:
        fields = {"client_id": client.client_id}
        if client.client_secret:
            fields["client_secret"] = client.client_secret
        return fields

    async def exchange(self, client: OAuthClientConfig, code: str, verifier: str,
                       redirect_uri: str) -> dict[str, Any]:
        """The token response for an authorization code; OAuthError(exchange_failed) else."""
        form = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                "redirect_uri": redirect_uri, **self._client_fields(client)}
        try:
            async with self._http() as http:
                r = await http.post(self.token_url, data=form)
        except httpx.HTTPError as exc:
            raise OAuthError("exchange_failed", "Google's token endpoint could not be reached "
                             f"({type(exc).__name__}).", 502) from exc
        if r.status_code != 200:
            raise OAuthError("exchange_failed", "Google refused the sign-in code "
                             f"({_error_code(r) or r.status_code}).", 400)
        body = r.json()
        if not isinstance(body, dict):
            raise OAuthError("exchange_failed", "Google's token answer was not JSON.", 502)
        return body

    async def refresh(self, client: OAuthClientConfig, refresh_token: str) -> tuple[str, float]:
        """(access_token, seconds it lasts). OAuthError(invalid_grant) when Google ended the
        connection (expired in Testing, revoked, or the password changed)."""
        form = {"grant_type": "refresh_token", "refresh_token": refresh_token,
                **self._client_fields(client)}
        try:
            async with self._http() as http:
                r = await http.post(self.token_url, data=form)
        except httpx.HTTPError as exc:
            raise OAuthError("google_unavailable", "Google's token endpoint could not be "
                             f"reached ({type(exc).__name__}).", 502) from exc
        if r.status_code == 200:
            body = r.json()
            return str(body["access_token"]), float(body.get("expires_in", 3600))
        if _error_code(r) in ("invalid_grant", "unauthorized_client", "invalid_client"):
            raise OAuthError("invalid_grant", "Google ended the connection: connect again. "
                             + TESTING_NOTE, 409)
        raise OAuthError("google_unavailable",
                         f"Google's token endpoint answered {r.status_code}.", 502)

    async def revoke(self, token: str) -> bool:
        try:
            async with self._http() as http:
                r = await http.post(self.revoke_url, data={"token": token})
        except httpx.HTTPError:
            return False
        return r.status_code == 200


class Connector:
    """The connection in use: the client, the token file, and an access token in memory."""

    def __init__(self, client_file: str, token_file: str, oauth: GoogleOAuth,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.client_file, self.store, self.oauth, self.clock = client_file, \
            TokenStore(token_file), oauth, clock
        self.needs_reconnect = False
        self._access: tuple[str, float] | None = None       # (token, expires at), never on disk

    def client(self) -> OAuthClientConfig:
        client = OAuthClientConfig.load(self.client_file)
        if client is None:
            raise OAuthError("not_configured", "Google Calendar sync is not set up: save your "
                             "OAuth client JSON as ~/.pantry-secrets/google_oauth_client.json "
                             "(docs/google-calendar.md).")
        return client

    def connection(self) -> Connection:
        conn = self.store.load()
        if conn is None:
            raise OAuthError("not_connected", "Connect Google Calendar first.")
        return conn

    def connected(self, conn: Connection, access_token: str, expires_in: float) -> None:
        self.store.save(conn)
        self.needs_reconnect = False
        self._access = (access_token, self.clock() + expires_in)

    def remember_calendar(self, calendar_id: str | None) -> None:
        conn = self.store.load()
        if conn is not None and conn.calendar_id != calendar_id:
            self.store.save(replace(conn, calendar_id=calendar_id))

    async def access_token(self, force: bool = False) -> str:
        """A live access token: the one in memory, else a refresh. Raises OAuthError."""
        if not force and self._access and self._access[1] - 60 > self.clock():
            return self._access[0]
        client, conn = self.client(), self.connection()
        try:
            token, expires_in = await self.oauth.refresh(client, conn.refresh_token)
        except OAuthError as exc:
            if exc.code == "invalid_grant":
                self.needs_reconnect = True
                self._access = None
            raise
        self.needs_reconnect = False
        self._access = (token, self.clock() + expires_in)
        return token

    def forget(self) -> bool:
        self._access = None
        self.needs_reconnect = False
        return self.store.delete()


def new_connection(token_response: dict[str, Any], calendar_id: str | None = None) -> Connection:
    """The connection from a code exchange's answer. Refused (nothing stored) unless Google
    granted the calendar scope and sent a refresh token."""
    granted = str(token_response.get("scope", "")).split()
    if SCOPE not in granted:
        raise OAuthError("scope_not_granted", "Google did not grant access to the calendars "
                         "this app creates; connect again and leave that box ticked.", 400)
    refresh = token_response.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        raise OAuthError("no_refresh_token", "Google sent no refresh token; connect again.", 400)
    if not isinstance(token_response.get("access_token"), str):
        raise OAuthError("exchange_failed", "Google sent no access token.", 400)
    now = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
    return Connection(refresh_token=refresh, scope=SCOPE, connection_id=secrets.token_hex(8),
                      connected_at=now, calendar_id=calendar_id)
