"""``GET /hub/tiles/{z}/{x}/{y}.png``: OpenStreetMap's map tiles for the wizard's store map, through
the hub, so the browser never calls a third party and the hub keeps OpenStreetMap's tile usage
policy (operations.osmfoundation.org/policies/tiles) for every console (docs in README.md):

- a User-Agent that names the app and nothing of the shopper (``geocode.USER_AGENT``, the one the
  hub already sends to OpenStreetMap); no cookie, no Referer, nothing of the browser's request;
- a disk cache (``DEMO_TILE_DIR``, default ``~/.pantry-demo/tiles``, folders 0700, files 0600): a
  tile stays fresh as long as the tile server's Cache-Control max-age (else its Expires) says,
  and never less than 7 days. A stale tile is asked again with its ETag and Last-Modified (304
  keeps it), and is served as it is while the server cannot be reached;
- zoom 0 to 19, and x and y inside the zoom's 2^z grid (else 400);
- at most 2 requests to the tile server at once for the whole process, and one per tile however
  many ask for it at the same time;
- only the tile asked for: nothing is prefetched (no neighbours, no other zoom), nothing is
  downloaded in bulk.

The map must show "© OpenStreetMap contributors", linked to openstreetmap.org/copyright: that is
the console's part. ``TILES_URL`` points elsewhere (with ``{z}``, ``{x}`` and ``{y}`` in it), and
"" turns the route off (503 tiles_off).

Refusals are ``{detail: {code, message}}`` like the other hub routes: 400 bad_tile, 404 no_tile
(the tile server has none), 502 tiles_unavailable (an error, an answer that is not an image, or
none in 10 s, with no copy cached), 503 tiles_off.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from demo_hub import geocode
from demo_hub.settings import Settings

USER_AGENT = geocode.USER_AGENT
MAX_ZOOM = 19
MIN_FRESH_S = 7 * 24 * 3600                # the policy's floor when the server says less
STALE_MAX_AGE_S = 60                       # what the browser keeps a stale tile served for
MAX_UPSTREAM = 2                           # requests to the tile server at once
TIMEOUT_S = 10.0
MAX_BYTES = 1024 * 1024
TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
NUMBER = re.compile(r"^\d{1,7}$")
MAX_AGE = re.compile(r"(?:^|,)\s*(?:s-maxage|max-age)\s*=\s*\"?(\d+)", re.IGNORECASE)
SERVED = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"}

log = logging.getLogger(__name__)


class TileRefused(Exception):
    """A tile the hub does not serve or cannot get: REST {code, message}."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def lifetime(headers: httpx.Headers, now: float) -> int:
    """How long the tile server says a tile stays fresh, in seconds: Cache-Control's max-age,
    else Expires against its Date (or now); 0 when it says neither."""
    found = MAX_AGE.search(headers.get("cache-control", ""))
    if found:
        return int(found.group(1))
    try:
        expires = parsedate_to_datetime(headers["expires"]).timestamp()
        date = parsedate_to_datetime(headers["date"]).timestamp() if "date" in headers else now
    except (KeyError, TypeError, ValueError, IndexError):
        return 0
    return max(0, int(expires - date))


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


class Tiles:
    """Everything behind the route. Transport and clock are swappable for tests."""

    def __init__(self, settings: Settings, directory: str | Path, *,
                 transport: httpx.AsyncBaseTransport | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.url = settings.tiles_url
        self.dir = Path(directory).expanduser()
        self.transport, self.clock = transport, clock
        self.upstream = asyncio.Semaphore(MAX_UPSTREAM)
        self.pending: dict[tuple[int, int, int], asyncio.Future[tuple[bytes, str, int]]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.url) and all(p in self.url for p in ("{z}", "{x}", "{y}"))

    async def tile(self, z: str, x: str, y: str) -> tuple[bytes, str, int]:
        """(content, content type, seconds the browser may keep it) for tile z/x/y."""
        if not self.enabled:
            raise TileRefused(503, "tiles_off", "Map tiles are turned off on this hub (TILES_URL "
                              "is empty, or has no {z}/{x}/{y}).")
        if not (NUMBER.match(z) and NUMBER.match(x) and NUMBER.match(y)):
            raise TileRefused(400, "bad_tile", "A tile is /hub/tiles/{z}/{x}/{y}.png in whole "
                              "numbers.")
        zi, xi, yi = int(z), int(x), int(y)
        if not (zi <= MAX_ZOOM and xi < 2 ** zi and yi < 2 ** zi):
            raise TileRefused(400, "bad_tile", f"Zoom is 0 to {MAX_ZOOM}, and x and y 0 to "
                              "2^zoom - 1.")
        cached = self._read(zi, xi, yi)
        now = self.clock()
        if cached is not None and cached[2]["expires"] > now:
            return cached[0], cached[1], int(cached[2]["expires"] - now)
        key = (zi, xi, yi)
        task = self.pending.get(key)
        if task is None:               # one request per tile, however many wait for it
            task = asyncio.ensure_future(self._fetch(zi, xi, yi, cached))
            self.pending[key] = task
            task.add_done_callback(lambda _t: self.pending.pop(key, None))
        return await asyncio.shield(task)

    # the cache: <dir>/<z>/<x>/<y>.json says what <y>.<ext> holds and until when it is fresh

    def _folder(self, z: int, x: int) -> Path:
        return self.dir / str(z) / str(x)

    def _read(self, z: int, x: int, y: int) -> tuple[bytes, str, dict[str, Any]] | None:
        folder = self._folder(z, x)
        try:
            meta = json.loads((folder / f"{y}.json").read_text(encoding="utf-8"))
            meta["expires"] = float(meta["expires"])
            content = (folder / Path(meta["file"]).name).read_bytes()
            return content, str(meta["type"]), meta
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write(self, z: int, x: int, y: int, content: bytes | None, meta: dict[str, Any]) -> None:
        """The tile and what it is, on disk. A cache that cannot be written (a full disk, a
        folder of someone else's) costs a fetch next time, never the tile now."""
        folder = self._folder(z, x)
        try:
            for level in (self.dir, self.dir / str(z), folder):
                _private_dir(level)
            if content is not None:
                self._replace(folder / meta["file"], content)
            self._replace(folder / f"{y}.json", json.dumps(meta).encode())
        except OSError as exc:
            log.warning("map tile %s/%s/%s not cached: %s", z, x, y, exc)

    @staticmethod
    def _replace(path: Path, data: bytes) -> None:
        """Written whole or not at all (a 0600 temporary file, renamed over the old one)."""
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tile-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise

    # the tile server: at most MAX_UPSTREAM requests at once

    async def _fetch(self, z: int, x: int, y: int,
                     cached: tuple[bytes, str, dict[str, Any]] | None) -> tuple[bytes, str, int]:
        url = self.url.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
        headers = {"User-Agent": USER_AGENT}
        if cached is not None:
            if cached[2].get("etag"):
                headers["If-None-Match"] = cached[2]["etag"]
            if cached[2].get("last_modified"):
                headers["If-Modified-Since"] = cached[2]["last_modified"]
        try:
            async with self.upstream, httpx.AsyncClient(
                    timeout=TIMEOUT_S, transport=self.transport) as http, \
                    asyncio.timeout(TIMEOUT_S), http.stream("GET", url, headers=headers) as r:
                body = bytearray()
                if r.status_code == 200:
                    async for chunk in r.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            break
        except (httpx.HTTPError, TimeoutError):
            return self._stale(cached)
        now = self.clock()
        fresh = max(lifetime(r.headers, now), MIN_FRESH_S)
        if r.status_code == 304 and cached is not None:
            meta = {**cached[2], "expires": now + fresh,
                    "etag": r.headers.get("etag") or cached[2].get("etag"),
                    "last_modified": r.headers.get("last-modified")
                    or cached[2].get("last_modified")}
            self._write(z, x, y, None, meta)
            return cached[0], cached[1], fresh
        if r.status_code == 404:
            raise TileRefused(404, "no_tile", "The tile server has no such tile.")
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if r.status_code != 200 or ctype not in TYPES or not body or len(body) > MAX_BYTES:
            return self._stale(cached)
        meta = {"file": f"{y}{TYPES[ctype]}", "type": ctype, "expires": now + fresh,
                "etag": r.headers.get("etag"), "last_modified": r.headers.get("last-modified"),
                "fetched": now}
        self._write(z, x, y, bytes(body), meta)
        return bytes(body), ctype, fresh

    @staticmethod
    def _stale(cached: tuple[bytes, str, dict[str, Any]] | None) -> tuple[bytes, str, int]:
        """The copy on disk, past its time, while the tile server cannot give a new one."""
        if cached is None:
            raise TileRefused(502, "tiles_unavailable", "The map tile server did not answer: "
                              "try again later.")
        return cached[0], cached[1], STALE_MAX_AGE_S


def router(tiles: Tiles) -> APIRouter:
    r = APIRouter()

    @r.get("/hub/tiles/{z}/{x}/{y}.png")
    async def tile(z: str, x: str, y: str) -> Response:
        """One map tile from the hub's cache, or from the tile server once. The map shows the
        attribution, "© OpenStreetMap contributors"."""
        try:
            content, ctype, max_age = await tiles.tile(z, x, y)
        except TileRefused as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.message},
                                headers={"Cache-Control": "no-store"}) from None
        return Response(content, media_type=ctype,
                        headers={"Cache-Control": f"public, max-age={max_age}", **SERVED})

    return r
