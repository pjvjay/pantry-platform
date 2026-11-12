"""Pictures for the agentic flow: an ingredient's thumbnail from Wikipedia, and the images a
recipe page holds, both fetched once by the hub and served from its cache (the browser never
calls a third party, and a second look costs nothing).

- ``ingredient(name)``: the ingredient's head words ("Canned Tomatoes" -> "tomato", "Extra Virgin
  Olive Oil 500ml" -> "olive oil"), looked up as a Wikipedia title, then by Wikipedia search; the
  page's lead image as a 240 px thumbnail.
- ``wiki(title)``: a recipe's picture, the lead image of the English Wikipedia article the title
  names (a demo recipe's ``image_wiki``; a redirect is followed, nothing is searched) as a 320 px
  thumbnail.
- ``remote(url)``: an image from a recipe page. Guarded: http(s) only, every address the host
  resolves to must be public (no loopback, private, link-local or reserved ranges), redirects are
  re-checked, the reply must be a raster image (SVG can carry script), at most 3 MB.

A Wikipedia miss is cached only when Wikipedia answered and had nothing: no article (for an
ingredient, no search hit either), or an article without a lead image. The index says which
("answered": true, "reason": "no-article" or "no-image"). A Wikipedia that does not answer (an
error, a timeout, a 429 or another non-2xx answer, an answer that cannot be read), or a thumbnail
that cannot be fetched, is a 502 and is not cached, so a moment offline or a rate limit never
hides a picture for good. A Wikipedia miss cached without that mark (older code cached a Wikipedia
that did not answer as a miss too) is looked up again, and so is any miss over 7 days old.

Wikipedia's API is asked as its etiquette and rate limits ask (mediawiki.org/wiki/API:Etiquette):
one request at a time for the whole hub, one lookup per picture however many ask for it at once,
and a 429 retried once, after its Retry-After when that is 2 s or less, else after 1 s. When
Wikipedia does not answer (for a 429, its retry neither), the hub leaves it alone for 10 s, or
after a 429 as long as its Retry-After says, up to a minute; a lookup in that time is a 502 at
once. Thumbnails come from Wikimedia's image servers, at most 2 downloads at once.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import math
import re
import socket
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

WIKI_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "pantry-demo-hub/0.1 (local demo; https://github.com/pjvjay/pantry-platform)"
MAX_BYTES = 3 * 1024 * 1024
INGREDIENT_PX, RECIPE_PX = 240, 320
# What no Wikipedia title holds (a "|" would ask the API for two pages), and a title's limit.
NOT_IN_TITLES = re.compile(r"[#<>\[\]|{}\x00-\x1f\x7f]")
MAX_TITLE_BYTES = 255
TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
         "image/avif": ".avif"}
DESCRIPTORS = {
    "fresh", "frozen", "canned", "dried", "dry", "ground", "whole", "crushed", "diced", "chopped",
    "sliced", "minced", "shredded", "grated", "smoked", "raw", "organic", "boneless", "skinless",
    "large", "small", "medium", "extra", "virgin", "light", "dark", "toasted", "roasted",
    "unsalted", "salted", "low", "reduced", "sodium", "free", "range", "rigate", "plain", "pure",
    "baby", "lean", "of", "and", "a", "the", "pack", "bag", "box", "jar", "can", "bottle", "bulb"}
SIZE = re.compile(r"\b\d+(\.\d+)?\s*(g|kg|ml|l|oz|lb|lbs|x|pack|ct)\b|\(.*?\)|~\S+",
                  re.IGNORECASE)
WIKIPEDIA_KEYS = ("ingredient:", "wiki:")    # the index's Wikipedia lookups (else "url:")
MISS_TTL = timedelta(days=7)                # how long a cached miss is believed
MAX_THUMBNAILS = 2                          # thumbnail downloads at once
RETRY_WAIT_S = 1.0                          # before a 429's one retry, unless its Retry-After
MAX_RETRY_WAIT_S = 2.0                      # asks for no longer than this: then that
PAUSE_S = 10.0                              # Wikipedia left alone after it did not answer;
MAX_PAUSE_S = 60.0                          # after a 429, Retry-After's seconds up to this

log = logging.getLogger(__name__)


def ingredient_query(name: str) -> str:
    """The words worth looking up: no sizes, no descriptors, singular."""
    words = [w for w in re.findall(r"[a-z]+", SIZE.sub(" ", name.lower())) if w not in DESCRIPTORS]
    words = [_singular(w) for w in words] or [_singular(name.lower().strip())]
    return " ".join(words[-2:])          # the head noun, with one modifier ("olive oil")


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("oes") and len(word) > 4:
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us")) and len(word) > 3:
        return word[:-1]
    return word


def retry_after(headers: httpx.Headers, now: float) -> float | None:
    """How many seconds a 429 asks to wait: Retry-After in seconds, or as a date against the
    answer's Date (or now); None when it says neither."""
    value = headers.get("retry-after", "").strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value).timestamp()
        date = parsedate_to_datetime(headers["date"]).timestamp() if "date" in headers else now
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    return max(0.0, when - date)


class ImageError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


async def public_host(host: str) -> None:
    """Raise unless every address ``host`` resolves to is a public one."""
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ImageError(f"cannot resolve {host}", 404) from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise ImageError(f"{host} is not a public address", 403)


class ImageCache:
    """Everything behind the image routes. Sleep and clock (for Wikipedia's pauses) are
    swappable for tests."""

    def __init__(self, directory: str | Path, client: httpx.AsyncClient | None = None,
                 check_host: Any = public_host, *,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.dir = Path(directory).expanduser()
        self.client = client
        self.check_host = check_host
        self.sleep, self.clock = sleep, clock
        self._index_path = self.dir / "index.json"
        self._index: dict[str, dict[str, Any]] = {}
        if self._index_path.is_file():
            try:
                self._index = json.loads(self._index_path.read_text(encoding="utf-8"))
            except ValueError:
                self._index = {}
        self._lock = asyncio.Lock()
        # Wikipedia: one API request at a time, a few thumbnails at once, one lookup per key
        self._api = asyncio.Lock()
        self._thumbnails = asyncio.Semaphore(MAX_THUMBNAILS)
        self._pending: dict[str, asyncio.Future[tuple[Path, str] | None]] = {}
        self._paused_until, self._paused_for = 0.0, ""

    def _client(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=10, headers={"User-Agent": USER_AGENT})
        return self.client

    def _hit(self, key: str) -> tuple[bool, Path | None, str]:
        """(known, path, type): the picture on disk, or a miss that still stands."""
        entry = self._index.get(key)
        if entry is None:
            return False, None, ""
        file = entry.get("file")
        if file:
            if (self.dir / file).is_file():
                return True, self.dir / file, str(entry.get("type", "image/jpeg"))
            return False, None, ""
        return self._miss_stands(key, entry), None, ""

    @staticmethod
    def _miss_stands(key: str, entry: dict[str, Any]) -> bool:
        """A cached miss is believed for MISS_TTL; a Wikipedia one only when Wikipedia answered.
        Older code also cached a Wikipedia that did not answer as a miss, without the mark: such
        a miss is looked up again."""
        if key.startswith(WIKIPEDIA_KEYS) and entry.get("answered") is not True:
            return False
        try:
            return datetime.now(UTC) - datetime.fromisoformat(entry["at"]) < MISS_TTL
        except (KeyError, TypeError, ValueError):
            return False

    async def _remember(self, key: str, content: bytes | None, ctype: str,
                        **about: Any) -> Path | None:
        async with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = None
            if content is not None:
                path = self.dir / (hashlib.sha256(key.encode()).hexdigest()[:24] + TYPES[ctype])
                path.write_bytes(content)
            self._index[key] = {"file": path.name if path else None, "type": ctype,
                                "at": datetime.now(UTC).isoformat(), **about}
            self._index_path.write_text(json.dumps(self._index, indent=1), encoding="utf-8")
            return path

    async def ingredient(self, name: str) -> tuple[Path, str] | None:
        query = ingredient_query(name)
        if not query:
            return None
        asks = [{"titles": query[:1].upper() + query[1:]},
                {"generator": "search", "gsrsearch": f"{query} food", "gsrlimit": "1"}]
        return await self._wikipedia(f"ingredient:{query}", asks, INGREDIENT_PX, query=query)

    async def wiki(self, title: str) -> tuple[Path, str] | None:
        title = " ".join(title.replace("_", " ").split())
        if not title or len(title.encode()) > MAX_TITLE_BYTES or NOT_IN_TITLES.search(title):
            raise ImageError("not a Wikipedia article title")
        return await self._wikipedia(f"wiki:{title}", [{"titles": title}], RECIPE_PX)

    async def _wikipedia(self, key: str, asks: list[dict[str, str]], width: int,
                         **about: Any) -> tuple[Path, str] | None:
        """``key``'s picture from the cache, else from Wikipedia: one lookup per key however many
        ask for it at once (the others wait for its answer, or its error)."""
        known, path, ctype = self._hit(key)
        if known:
            return (path, ctype) if path else None
        task = self._pending.get(key)
        if task is None:
            task = asyncio.ensure_future(self._look_up(key, asks, width, **about))
            self._pending[key] = task
            task.add_done_callback(lambda _t: self._pending.pop(key, None))
        return await asyncio.shield(task)

    async def _look_up(self, key: str, asks: list[dict[str, str]], width: int,
                       **about: Any) -> tuple[Path, str] | None:
        """The first lead image Wikipedia's API finds for ``asks``, asked in turn, downloaded and
        kept. A miss is kept only when Wikipedia answered every ask and none found one."""
        found = None                       # the title of a page found without a lead image
        for params in asks:
            thumb, title = await self._lead_image(params, width)
            if thumb:
                return await self._thumbnail(key, thumb, **about, title=title)
            found = found or title
        await self._remember(key, None, "image/jpeg", **about, title=found, source=None,
                             answered=True, reason="no-image" if found else "no-article")
        return None

    async def _thumbnail(self, key: str, thumb: str, **about: Any) -> tuple[Path, str] | None:
        """The lead image's thumbnail, downloaded (MAX_THUMBNAILS at once) and kept. One that
        cannot be fetched is a 502 and is not kept: Wikipedia has a picture, the hub did not get
        it this time."""
        try:
            async with self._thumbnails:
                content, ctype = await self._download(thumb)
        except ImageError as exc:
            if exc.status != 404:          # a refusal stays one (403: not a public address)
                raise
            # 404 is a host that did not resolve: Wikimedia's servers exist, the network failed
            raise ImageError("Wikipedia's image server could not be reached", 502) from exc
        if content is None:
            raise ImageError("the article's image could not be read", 502)
        path = await self._remember(key, content, ctype, **about, source=thumb)
        return (path, ctype) if path else None

    async def _lead_image(self, params: dict[str, str],
                          width: int) -> tuple[str | None, str | None]:
        """(thumbnail URL, title) of the first page Wikipedia's API finds for ``params`` that has
        a lead image, ``width`` px wide; (None, title) when the pages it finds have none, and
        (None, None) when it finds no page. Raises ImageError 502 when Wikipedia did not answer."""
        body = await self._ask({
            "action": "query", "format": "json", "formatversion": "2", "prop": "pageimages",
            "piprop": "thumbnail", "pithumbsize": str(width), "redirects": "1", **params})
        query = body.get("query")                  # none at all when a search has no hit
        pages = query.get("pages") if isinstance(query, dict) else None
        found = None
        for page in pages if isinstance(pages, list) else []:
            if not isinstance(page, dict) or "missing" in page or "invalid" in page:
                continue
            thumbnail = page.get("thumbnail")
            source = thumbnail.get("source") if isinstance(thumbnail, dict) else None
            title = str(page.get("title") or "")
            if source:
                return str(source), title
            found = found or title
        return None, found or None

    async def _ask(self, params: dict[str, str]) -> dict[str, Any]:
        """Wikipedia's API answer to ``params``: one request at a time for the whole hub, a 429
        retried once. No answer to read (an error, a timeout, a non-2xx answer, anything but a
        JSON object, an API error) is ImageError 502, and starts a pause."""
        async with self._api:
            if (left := self._paused_until - self.clock()) > 0:
                raise ImageError(f"{self._paused_for}; not asked again for {math.ceil(left)} s",
                                 502)
            for attempt in (1, 2):
                try:
                    r = await self._client().get(WIKI_API, params=params,
                                                 headers={"User-Agent": USER_AGENT})
                except httpx.HTTPError as exc:
                    raise self._pause("Wikipedia did not answer", PAUSE_S) from exc
                if r.status_code != 429:
                    break
                asked = retry_after(r.headers, time.time())
                if attempt == 2:
                    raise self._pause("Wikipedia answered 429 (too many requests)",
                                      PAUSE_S if asked is None else min(asked, MAX_PAUSE_S))
                await self.sleep(asked if asked is not None and asked <= MAX_RETRY_WAIT_S
                                 else RETRY_WAIT_S)
            if not r.is_success:
                raise self._pause(f"Wikipedia answered {r.status_code}", PAUSE_S)
            try:
                body = r.json()
            except ValueError as exc:
                raise self._pause("Wikipedia's answer could not be read", PAUSE_S) from exc
            if not isinstance(body, dict) or "error" in body:
                raise self._pause("Wikipedia's answer could not be read", PAUSE_S)
            return body

    def _pause(self, why: str, seconds: float) -> ImageError:
        """Wikipedia left alone for ``seconds`` after it did not answer: the error to raise."""
        self._paused_until, self._paused_for = self.clock() + seconds, why
        log.warning("picture lookups paused for %.0f s: %s", seconds, why)
        return ImageError(why, 502)

    async def remote(self, url: str) -> tuple[Path, str] | None:
        key = f"url:{url}"
        known, path, ctype = self._hit(key)
        if known:
            return (path, ctype) if path else None
        content, ctype = await self._download(url)
        path = await self._remember(key, content, ctype or "image/jpeg", source=url)
        return (path, ctype) if path else None

    async def _download(self, url: str) -> tuple[bytes | None, str]:
        """A raster image from a public http(s) URL, redirects re-checked; (None, "") when the
        reply is not one."""
        for _ in range(4):
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ImageError("only http(s) image URLs")
            await self.check_host(parts.hostname)
            try:
                async with self._client().stream("GET", url, follow_redirects=False,
                                                 headers={"User-Agent": USER_AGENT}) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        url = urljoin(url, r.headers["location"])
                        continue
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    if r.status_code != 200 or ctype not in TYPES:
                        return None, ""
                    body = bytearray()
                    async for chunk in r.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            return None, ""
                    return bytes(body), ctype
            except httpx.HTTPError:
                return None, ""
        return None, ""
