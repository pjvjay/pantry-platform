"""The hub's one way to read a web page for an import: a GET that cannot be turned on the
shopper's own machine or network (server-side request forgery).

- **Limits come from the extractor.** ``MAX_BYTES`` and ``TIMEOUT_S`` are the recipe-shopper
  skill's own (``extract_recipe.py``, loaded by path from ``settings.recipe_extractor``), so the
  hub reads no more and waits no longer than the skill does on the shopper's computer.
- **Resolve once, validate every address.** ``getaddrinfo`` runs in a thread; every address the
  name has must be global (not loopback, private, link-local, multicast or reserved), the rule
  ``images.public_host`` applies to recipe images.
- **Connect to the address that was checked.** The request goes to ``http(s)://<ip>:<port>/path``
  with the real name in ``Host`` and as the TLS server name (httpcore's ``sni_hostname``
  extension), so the certificate is verified for the name while a second lookup, which a
  rebinding name could answer with 127.0.0.1, never happens.
- **Check the peer.** After connecting, the socket's remote address must be the pinned address,
  and global; otherwise the response is dropped before its body is read.
- **No ambient routes.** ``trust_env=False`` (an ``HTTPS_PROXY`` in the environment cannot route
  around the checks), a new client per hop (no cookies carried from one reply to the next
  request), redirects followed by hand, at most five, each re-validated and re-pinned, http and
  https only.
- **Bounded.** At most ``MAX_BYTES`` of decoded body (413 past it; a gzip body is counted after
  inflating), and the whole fetch, redirects included, within ``TIMEOUT_S`` (504).
"""

from __future__ import annotations

import asyncio
import importlib.util
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from urllib.parse import urljoin

import httpx

MAX_REDIRECTS = 5
MAX_URL = 2_000
REDIRECTS = frozenset({301, 302, 303, 307, 308})
DEFAULT_PORTS = {"http": 80, "https": 443}
USER_AGENT = ("pantry-demo-hub/0.1 (local demo; reads one recipe page a shopper linked; "
              "https://github.com/pjvjay/pantry-platform)")
HTML_ACCEPT = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"


class ImportFailure(Exception):
    """An import refused or failed, with the HTTP status and code the hub's routes answer and a
    message the shopper can act on."""

    def __init__(self, status: int, code: str, message: str, **detail: object) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, detail

    def body(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, **self.detail}


# --- the extractor and its limits ---------------------------------------------------------------

@dataclass(frozen=True)
class Limits:
    max_bytes: int
    timeout_s: float


_EXTRACTORS: dict[tuple[str, float], ModuleType] = {}


def load_extractor(path: str) -> ModuleType:
    """The skill's extract_recipe.py as a module, loaded once per file version. 503 when it is
    not configured or not there: link import is then off and the chat reads links as before."""
    if not path:
        raise ImportFailure(503, "import_unavailable", "Link import is not configured "
                            "(RECIPE_EXTRACTOR or RECIPE_SHOPPER_SKILL).")
    file = Path(path).expanduser()
    try:
        key = (str(file), file.stat().st_mtime)
    except OSError as exc:
        raise ImportFailure(503, "import_unavailable",
                            f"The recipe extractor is not at {file}.") from exc
    module = _EXTRACTORS.get(key)
    if module is None:
        spec = importlib.util.spec_from_file_location("pantry_extract_recipe", file)
        if spec is None or spec.loader is None:
            raise ImportFailure(503, "import_unavailable", f"{file} is not a Python module.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _EXTRACTORS[key] = module
    return module


def limits_of(extractor: ModuleType) -> Limits:
    return Limits(int(extractor.MAX_BYTES), float(extractor.TIMEOUT_S))


# --- addresses ------------------------------------------------------------------------------------

Resolver = Callable[[str, int], Awaitable[list[str]]]


async def resolve(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to, looked up in a thread (getaddrinfo blocks)."""
    infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def is_public(address: str) -> bool:
    """A global unicast address. An IPv4 address written as IPv6 (``::ffff:127.0.0.1``) is
    judged as the IPv4 address it is."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def _same(a: str, b: str) -> bool:
    try:
        x, y = ipaddress.ip_address(a.split("%", 1)[0]), ipaddress.ip_address(b.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(x, ipaddress.IPv6Address) and x.ipv4_mapped is not None:
        x = x.ipv4_mapped
    if isinstance(y, ipaddress.IPv6Address) and y.ipv4_mapped is not None:
        y = y.ipv4_mapped
    return x == y


async def pinned_address(host: str, port: int, resolver: Resolver) -> str:
    """The address to connect to: the first of ``host``'s, once all of them are public. One
    private address among public ones refuses the name: which one a client would use is not
    ours to choose."""
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            addresses = await resolver(host, port)
        except OSError as exc:
            raise ImportFailure(502, "unresolvable", f"{host} could not be found.") from exc
    if not addresses:
        raise ImportFailure(502, "unresolvable", f"{host} could not be found.")
    private = [a for a in addresses if not is_public(a)]
    if private:
        raise ImportFailure(403, "not_public", f"{host} is not a public address, so the hub "
                            "will not read it.")
    return addresses[0]


# --- one page -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Page:
    url: str                # the final URL, after redirects, with the real host name
    text: str
    content_type: str
    address: str            # the address the page was read from
    redirects: int


def check_url(url: str) -> httpx.URL:
    """``url`` as an http(s) URL with a host and no user name or password; 400 (403 for a
    scheme a redirect asked for) otherwise."""
    if len(url) > MAX_URL:
        raise ImportFailure(400, "bad_url", f"Links over {MAX_URL} characters are not read.")
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise ImportFailure(400, "bad_url", "That is not a link the hub can read.") from exc
    if parsed.scheme not in DEFAULT_PORTS:
        raise ImportFailure(400, "bad_url", "Only http and https links are read.")
    if not parsed.host:
        raise ImportFailure(400, "bad_url", "The link has no host name.")
    if parsed.userinfo:
        raise ImportFailure(400, "bad_url", "Links carrying a user name or password are not read.")
    return parsed


def _peer(response: httpx.Response) -> str | None:
    """The address the response's connection is to, as the socket reports it."""
    stream = response.extensions.get("network_stream")
    if stream is None:
        return None
    addr = stream.get_extra_info("server_addr")
    return str(addr[0]) if addr else None


def _text(body: bytes, response: httpx.Response) -> str:
    charset = response.charset_encoding
    if not charset:
        head = body[:4096].decode("ascii", errors="ignore")
        m = re.search(r"""<meta[^>]+charset=["']?([A-Za-z0-9_.:-]+)""", head, re.IGNORECASE)
        charset = m.group(1) if m else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


async def _body(response: httpx.Response, limit: int, host: str) -> bytes:
    too_big = ImportFailure(413, "too_large", f"The page at {host} is larger than "
                            f"{limit // (1024 * 1024)} MB, the most the hub reads.")
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise too_big
    body = bytearray()
    async for chunk in response.aiter_bytes():      # decoded: a gzip body counts as inflated
        body += chunk
        if len(body) > limit:
            raise too_big
    return bytes(body)


async def fetch_page(url: str, *, limits: Limits, resolver: Resolver = resolve,
                     transport: httpx.AsyncBaseTransport | None = None,
                     accept: str = HTML_ACCEPT) -> Page:
    """GET ``url`` under the rules in this module's docstring."""
    try:
        async with asyncio.timeout(limits.timeout_s):
            return await _fetch(url, limits, resolver, transport, accept)
    except TimeoutError as exc:
        raise ImportFailure(504, "timeout", f"Reading the page took longer than "
                            f"{limits.timeout_s:g} s, so the hub gave up.") from exc


async def _fetch(url: str, limits: Limits, resolver: Resolver,
                 transport: httpx.AsyncBaseTransport | None, accept: str) -> Page:
    current = url
    for hop in range(MAX_REDIRECTS + 1):
        target = check_url(current)
        host = target.raw_host.decode("ascii")
        port = target.port or DEFAULT_PORTS[target.scheme]
        address = await pinned_address(host, port, resolver)
        headers = {"Host": host if target.port is None else f"{host}:{target.port}",
                   "User-Agent": USER_AGENT, "Accept": accept,
                   "Accept-Encoding": "gzip, deflate"}
        extensions = {"sni_hostname": host} if target.scheme == "https" else {}
        # A client per hop: nothing (cookies above all) carries from one reply to the next.
        async with httpx.AsyncClient(transport=transport, trust_env=False,
                                     follow_redirects=False, verify=True,
                                     timeout=limits.timeout_s) as client:
            request = client.build_request("GET", target.copy_with(host=address),
                                           headers=headers, extensions=extensions)
            try:
                response = await client.send(request, stream=True)
            except httpx.TimeoutException as exc:
                raise ImportFailure(504, "timeout", f"{host} did not answer in time.") from exc
            except httpx.HTTPError as exc:
                raise ImportFailure(502, "unreachable", f"{host} could not be reached "
                                    f"({type(exc).__name__}).") from exc
            try:
                peer = _peer(response)
                if peer is None or not _same(peer, address) or not is_public(peer):
                    raise ImportFailure(403, "not_public", f"The connection to {host} did not "
                                        "end at the public address that was checked.")
                if response.status_code in REDIRECTS and response.headers.get("location"):
                    nxt = urljoin(str(target), response.headers["location"])
                    if httpx.URL(nxt).scheme not in DEFAULT_PORTS:
                        raise ImportFailure(403, "bad_redirect", f"{host} redirected to a "
                                            "link that is not http or https.")
                    current = nxt
                    continue
                if response.status_code != 200:
                    raise ImportFailure(502, "http_status", f"{host} answered HTTP "
                                        f"{response.status_code}.",
                                        upstream_status=response.status_code)
                body = await _body(response, limits.max_bytes, host)
            except httpx.TimeoutException as exc:
                raise ImportFailure(504, "timeout", f"{host} stopped sending in time.") from exc
            except httpx.HTTPError as exc:
                raise ImportFailure(502, "unreachable", f"Reading {host} failed "
                                    f"({type(exc).__name__}).") from exc
            finally:
                await response.aclose()
        ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
        return Page(url=str(target), text=_text(body, response), content_type=ctype,
                    address=address, redirects=hop)
    raise ImportFailure(502, "too_many_redirects",
                        f"The link redirected more than {MAX_REDIRECTS} times.")


def without_query(url: str) -> str:
    """``url`` with no query string or fragment, for traces (a query can carry a token)."""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        return ""
    return str(parsed.copy_with(query=None, fragment=None))
