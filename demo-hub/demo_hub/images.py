"""Pictures for the agentic flow: an ingredient's thumbnail from Wikipedia, and the images a
recipe page holds, both fetched once by the hub and served from its cache (the browser never
calls a third party, and a second look costs nothing).

- ``ingredient(name)``: the ingredient's head words ("Canned Tomatoes" -> "tomato", "Extra Virgin
  Olive Oil 500ml" -> "olive oil") as a Wikipedia title, looked up in that article's REST page
  summary; when there is no such article (a 404, or a disambiguation page) or it has no usable
  lead image, by Wikipedia search. The picture is the article's lead image, the thumbnail
  Wikipedia names.
- ``wiki(title)``: a recipe's picture, the lead image of the English Wikipedia article the title
  names (a demo recipe's ``image_wiki``), from its page summary: a redirect is followed, nothing
  is searched.
- ``remote(url)``: an image from a recipe page. Guarded: http(s) only, every address the host
  resolves to must be public (no loopback, private, link-local or reserved ranges), redirects are
  re-checked, the reply must be a raster image (SVG can carry script), at most 3 MB.

A thumbnail Wikipedia names is fetched only from Wikimedia's own image servers: https, the host
exactly upload.wikimedia.org or thumb.wikimedia.org, no user or password, the default port, and
a redirect is held to the same rule. Those addresses are checked by name, with no DNS lookup
(``wikimedia_image`` says why). Any other address in an answer is not fetched: the article counts
as one without a usable lead image.

A miss is cached only when the server answered and had nothing. For Wikipedia: no article (for an
ingredient, no search hit either), an article without a lead image, or a lead image elsewhere than
on Wikimedia's image servers; the index says which ("answered": true, "reason": "no-article",
"no-image" or "not-wikimedia"). For a recipe page's image: a status other than 200 that is
neither a 429 nor a 5xx (a 404, say), a reply that is not a raster image, one over 3 MB, or more
than 3 redirects ("reason": "status-404", "not-an-image", "too-large", "too-many-redirects").
Anything else is no answer: an error, a timeout, a 429, a 5xx; from Wikipedia also any other
status but a 2xx (or a page summary's 404) and an answer that cannot be read; and a thumbnail
that cannot be fetched. No answer is a 502 and is not cached, so a moment offline or a rate limit
never hides a picture for good. A miss cached without that mark (older code cached a server that
did not answer as a miss too) is looked up again, and so is any miss over 7 days old.

Wikipedia is asked as its etiquette and rate limits ask (mediawiki.org/wiki/API:Etiquette): one
request at a time to each of the two endpoints the hub uses, the REST page summary and the action
API's search; one lookup per picture however many ask for it at once; and a 429 retried once
only when it asks for a short wait: after its Retry-After when that is 2 s or less, after 1 s
when it gives none. When an endpoint does not answer (for a 429, its retry neither, or its
Retry-After asks for longer), the hub leaves that endpoint alone for 10 s, or after a 429 as
long as its Retry-After says, up to a minute; a lookup that needs it in that time is a 502 at
once. Each such 502 says how long the endpoint is left alone ("not asked again for N s"). Each
endpoint pauses on its own, so a refused search does not hold up the page summaries, which are
limited apart from the action API. Thumbnails download at most 2 at once.
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
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"
WIKIMEDIA_IMAGES = frozenset({"upload.wikimedia.org", "thumb.wikimedia.org"})
USER_AGENT = "pantry-demo-hub/0.1 (local demo; https://github.com/pjvjay/pantry-platform)"
MAX_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 3
# The width a search asks its thumbnails at, one of the standard widths Wikimedia's image servers
# make thumbnails at. A summary's thumbnail is taken as it is named.
SEARCH_PX = 330
# What no Wikipedia title holds, and a title's limit.
NOT_IN_TITLES = re.compile(r"[#<>\[\]|{}\x00-\x1f\x7f]")
MAX_TITLE_BYTES = 255
TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
         "image/avif": ".avif"}
DESCRIPTORS = {
    "fresh", "frozen", "canned", "dried", "dry", "ground", "whole", "crushed", "diced", "chopped",
    "sliced", "minced", "shredded", "grated", "smoked", "raw", "organic", "boneless", "skinless",
    "large", "small", "medium", "extra", "virgin", "light", "dark", "toasted", "roasted",
    "unsalted", "salted", "low", "reduced", "sodium", "free", "range", "rigate", "plain", "pure",
    "baby", "lean", "of", "and", "a", "the", "pack", "bag", "box", "jar", "can", "bottle", "bulb",
    "petite",
    # how a product is cut or packed, not what it is: "Broccoli Crown", "Bell Pepper Trio",
    # "Parmesan Wedge", "Cheddar Cheese Block", "Vegan Cheese Shreds", "Fusilli Family Pack",
    # "Eggs Large Dozen" (looked up with these words, each found an article about something else)
    "crown", "crowns", "trio", "wedge", "wedges", "block", "blocks", "shreds", "chunks", "family",
    "dozen"}
# The same, written as two words: "Chicken Thighs Bone-In" (which found bone marrow soup),
# "Skin-On Salmon", "Gluten-Free Bread" (which found gluten).
CUTS = re.compile(r"\bbone[- ]?in\b|\bskin[- ]?on\b|\b[a-z]+[- ]free\b", re.IGNORECASE)
# Lookup words whose own article is about something else or pictures it badly, and the article
# that pictures the food: "Mint" is a disambiguation page (its search finds an ice cream) and
# "Granulated sugar" leads to the sucrose molecule. Each comment says what the word found before.
# Such a word's cache key names its article, so a picture cached under the plain key before the
# word was listed here is never served again (a picture never expires).
ARTICLES = {
    "mint": "Mentha", "granulated sugar": "Sugar",
    "lime": "Lime (fruit)",                            # calcium hydroxide, a white powder
    "english cucumber": "Cucumber",                    # a sea cucumber
    "black bean": "Black turtle bean",                 # black-eyed peas
    "nestle pbj": "Peanut butter and jelly sandwich",  # a street in Glasgow, Montana
    "shoulder roast": "Boston butt",                   # lamb ("Pork Shoulder Roast")
    "chicken": "Chicken as food",                      # live hens ("Whole Chicken")
    "chocolate cadbury": "Chocolate",                  # the Cadbury logo
    "bean paste": "Doubanjiang",                       # sweet red bean paste
    "butter cream": "Peanut butter",                   # cupcakes ("Peanut Butter Cream")
    "cooking wine": "Huangjiu",                        # a dish cooked with wine ("Shaoxing ...")
    "coconut yogurt": "Yogurt",                        # frozen yogurt with fruit
    "sirloin steak": "Steak",                          # a diagram of beef cuts
    "shrimp": "Shrimp and prawn as food",              # a live shrimp under water
    "egg": "Eggs as food",                             # wild birds' eggs ("Eggs Large Dozen")
    "fusilli": "Pasta",                                # an article with no picture
    "ghee": "Clarified butter",                        # an article with no picture
    "queso fresco": "Cheese",                          # an article with no picture
}
SIZE = re.compile(r"\b\d+(\.\d+)?\s*(g|kg|ml|l|oz|lb|lbs|x|pack|ct)\b|\(.*?\)|~\S+",
                  re.IGNORECASE)
MISS_TTL = timedelta(days=7)                # how long a cached miss is believed
MAX_THUMBNAILS = 2                          # thumbnail downloads at once
RETRY_WAIT_S = 1.0                          # before a 429's one retry when it has no Retry-After
MAX_RETRY_WAIT_S = 2.0                      # a longer Retry-After is not retried: a pause
PAUSE_S = 10.0                              # an endpoint left alone after it did not answer;
MAX_PAUSE_S = 60.0                          # after a 429, Retry-After's seconds up to this

log = logging.getLogger(__name__)

# One way to find an article's lead image: (thumbnail URL, the article's title), as _summary
# and _search return them.
Ask = Callable[[], Awaitable[tuple[str | None, str | None]]]


def ingredient_query(name: str) -> str:
    """The words worth looking up: no sizes, cuts or descriptors, singular."""
    words = [w for w in re.findall(r"[a-z]+", CUTS.sub(" ", SIZE.sub(" ", name.lower())))
             if w not in DESCRIPTORS]
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


def wikimedia_image(url: str) -> bool:
    """Whether ``url`` is on Wikimedia's own image servers: https, the host exactly
    upload.wikimedia.org or thumb.wikimedia.org, no user or password, the default port. The URL
    is read as httpx reads it, since httpx makes the request.

    This, not ``public_host``, guards the thumbnails Wikipedia's answers name. Those should only
    ever point at Wikimedia, and two exact hosts are a narrower guard than a DNS check, which
    lets any host through whose addresses are public. Nor does it need a lookup: behind an HTTPS
    proxy that resolves names itself, on a machine with no DNS of its own, a DNS check fails for
    every host and no picture would ever arrive. ``remote`` fetches images from any site a
    recipe names, so it keeps the DNS check."""
    try:
        parts = httpx.URL(url)
    except httpx.InvalidURL:
        return False
    return (parts.scheme == "https" and parts.host in WIKIMEDIA_IMAGES and not parts.userinfo
            and parts.port is None)              # httpx reads ":443" on https as no port


@dataclass
class Endpoint:
    """One of Wikipedia's endpoints as the hub asks it: one request at a time, and after it did
    not answer, a pause in which it is not asked."""
    name: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    paused_until: float = 0.0
    paused_for: str = ""


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
        # Wikipedia: one request at a time to each endpoint, each with its own pause; a few
        # thumbnails at once; one lookup per key
        self._summaries = Endpoint("Wikipedia's page summary")
        self._searches = Endpoint("Wikipedia's search")
        self._thumbnails = asyncio.Semaphore(MAX_THUMBNAILS)
        self._pending: dict[str, asyncio.Future[tuple[Path, str] | None]] = {}

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
        return self._miss_stands(entry), None, ""

    @staticmethod
    def _miss_stands(entry: dict[str, Any]) -> bool:
        """A cached miss is believed for MISS_TTL, and only when the server answered. Older code
        also cached a server that did not answer as a miss, without the mark: such a miss is
        looked up again."""
        if entry.get("answered") is not True:
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
        article = ARTICLES.get(query)
        asks = (partial(self._summary, article or query[:1].upper() + query[1:]),
                partial(self._search, f"{query} food"))
        key = f"ingredient:{query}" + (f"={article}" if article else "")
        return await self._wikipedia(key, asks, query=query)

    async def wiki(self, title: str) -> tuple[Path, str] | None:
        title = " ".join(title.replace("_", " ").split())
        if not title or len(title.encode()) > MAX_TITLE_BYTES or NOT_IN_TITLES.search(title):
            raise ImageError("not a Wikipedia article title")
        return await self._wikipedia(f"wiki:{title}", (partial(self._summary, title),))

    async def _wikipedia(self, key: str, asks: Sequence[Ask],
                         **about: Any) -> tuple[Path, str] | None:
        """``key``'s picture from the cache, else from Wikipedia: one lookup per key however many
        ask for it at once (the others wait for its answer, or its error)."""
        known, path, ctype = self._hit(key)
        if known:
            return (path, ctype) if path else None
        task = self._pending.get(key)
        if task is None:
            task = asyncio.ensure_future(self._look_up(key, asks, **about))
            self._pending[key] = task
            task.add_done_callback(lambda _t: self._pending.pop(key, None))
        return await asyncio.shield(task)

    async def _look_up(self, key: str, asks: Sequence[Ask],
                       **about: Any) -> tuple[Path, str] | None:
        """The first lead image on Wikimedia's image servers that ``asks`` find, asked in turn,
        downloaded and kept. A miss is kept only when Wikipedia answered every ask and none found
        one; a lead image elsewhere is not fetched, and the miss keeps its address."""
        found: str | None = None             # the title of a page without a usable lead image
        elsewhere: str | None = None         # a lead image not on Wikimedia's image servers
        for ask in asks:
            thumb, title = await ask()
            if thumb and wikimedia_image(thumb):
                return await self._thumbnail(key, thumb, **about, title=title)
            if thumb:
                log.warning("picture not fetched, not on Wikimedia's image servers: %s", thumb)
                elsewhere = elsewhere or thumb
            found = found or title
        reason = "not-wikimedia" if elsewhere else "no-image" if found else "no-article"
        await self._remember(key, None, "image/jpeg", **about, title=found, source=elsewhere,
                             answered=True, reason=reason)
        return None

    async def _thumbnail(self, key: str, thumb: str, **about: Any) -> tuple[Path, str] | None:
        """The lead image's thumbnail, downloaded from Wikimedia's image servers (MAX_THUMBNAILS
        at once) and kept. One that cannot be fetched is a 502 and is not kept: Wikipedia has a
        picture, the hub did not get it this time."""
        async with self._thumbnails:
            content, kind = await self._download(thumb, wikimedia=True)
        if content is None:
            raise ImageError(f"the article's image could not be read ({kind})", 502)
        path = await self._remember(key, content, kind, **about, source=thumb)
        return (path, kind) if path else None

    async def _summary(self, title: str) -> tuple[str | None, str | None]:
        """(thumbnail URL, title) of the article ``title`` names, from its REST page summary,
        redirects followed (to other page summaries only): the thumbnail as the summary names it
        (Wikimedia makes thumbnails at a few widths only, so none is asked for), None when the
        article has no lead image; (None, None) when there is no article (a 404, or a
        disambiguation page). Any other answer is ImageError 502, and the endpoint's pause."""
        url = WIKI_SUMMARY + quote(title.replace(" ", "_"), safe="!*'()")
        for _ in range(MAX_REDIRECTS + 1):
            r = await self._get(self._summaries, url)
            if not r.is_redirect:
                break
            url = urljoin(url, r.headers["location"])
            if not url.startswith(WIKI_SUMMARY):
                break                              # not followed: a 3xx is not an answer here
        if r.status_code == 404:
            return None, None
        if not r.is_success:
            raise self._pause(self._summaries, f"{self._summaries.name} answered {r.status_code}",
                              PAUSE_S)
        page = self._read(self._summaries, r)
        if page.get("type") == "disambiguation":
            return None, None
        titles = page.get("titles")
        name = (titles.get("normalized") if isinstance(titles, dict) else None) or page.get("title")
        thumbnail = page.get("thumbnail")
        source = thumbnail.get("source") if isinstance(thumbnail, dict) else None
        return (str(source) if source else None), str(name or title).replace("_", " ")

    async def _search(self, words: str) -> tuple[str | None, str | None]:
        """(thumbnail URL, title) of the first page Wikipedia's search finds for ``words`` that
        has a lead image, through the action API; (None, title) when the pages it finds have
        none, and (None, None) when it finds no page. Any answer but a 2xx JSON object without an
        API error is ImageError 502, and the endpoint's pause."""
        r = await self._get(self._searches, WIKI_API, {
            "action": "query", "format": "json", "formatversion": "2", "prop": "pageimages",
            "piprop": "thumbnail", "pithumbsize": str(SEARCH_PX), "redirects": "1",
            "generator": "search", "gsrsearch": words, "gsrlimit": "1"})
        if not r.is_success:
            raise self._pause(self._searches, f"{self._searches.name} answered {r.status_code}",
                              PAUSE_S)
        body = self._read(self._searches, r)
        query = body.get("query")                  # none at all when the search has no hit
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

    async def _get(self, endpoint: Endpoint, url: str,
                   params: dict[str, str] | None = None) -> httpx.Response:
        """``endpoint``'s answer to a GET of ``url``, whatever its status (redirects are not
        followed): one request at a time to each endpoint, a 429 retried once when it asks for
        a short wait. No answer (an error, a timeout, a 429 that asks for longer or comes again)
        is ImageError 502, and starts the endpoint's pause, in which every request to it is that
        502 at once."""
        async with endpoint.lock:
            if (left := endpoint.paused_until - self.clock()) > 0:
                raise ImageError(f"{endpoint.paused_for}; not asked again for {math.ceil(left)} s",
                                 502)
            for attempt in (1, 2):
                try:
                    r = await self._client().get(url, params=params,
                                                 headers={"User-Agent": USER_AGENT})
                except httpx.HTTPError as exc:
                    raise self._pause(endpoint, f"{endpoint.name} did not answer", PAUSE_S) from exc
                if r.status_code != 429:
                    break
                asked = retry_after(r.headers, time.time())
                if attempt == 2 or (asked is not None and asked > MAX_RETRY_WAIT_S):
                    raise self._pause(endpoint, f"{endpoint.name} answered 429 (too many requests)",
                                      PAUSE_S if asked is None else min(asked, MAX_PAUSE_S))
                await self.sleep(RETRY_WAIT_S if asked is None else asked)
            return r

    def _read(self, endpoint: Endpoint, r: httpx.Response) -> dict[str, Any]:
        """The JSON object ``r`` holds. Anything else (not JSON, not an object, an API error) is
        an answer that cannot be read: ImageError 502, and the endpoint's pause."""
        unreadable = f"{endpoint.name} sent an answer that could not be read"
        try:
            body = r.json()
        except ValueError as exc:
            raise self._pause(endpoint, unreadable, PAUSE_S) from exc
        if not isinstance(body, dict) or "error" in body:
            raise self._pause(endpoint, unreadable, PAUSE_S)
        return body

    def _pause(self, endpoint: Endpoint, why: str, seconds: float) -> ImageError:
        """``endpoint`` left alone for ``seconds`` after it did not answer: the error to raise,
        which says for how long."""
        endpoint.paused_until, endpoint.paused_for = self.clock() + seconds, why
        log.warning("picture lookups paused for %.0f s: %s", seconds, why)
        return ImageError(f"{why}; not asked again for {math.ceil(seconds)} s", 502)

    async def remote(self, url: str) -> tuple[Path, str] | None:
        key = f"url:{url}"
        known, path, ctype = self._hit(key)
        if known:
            return (path, ctype) if path else None
        content, kind = await self._download(url)
        if content is None:                    # a definite answer, and not a picture: a miss
            await self._remember(key, None, "image/jpeg", source=url, answered=True, reason=kind)
            return None
        path = await self._remember(key, content, kind, source=url)
        return (path, kind) if path else None

    async def _download(self, url: str, *, wikimedia: bool = False) -> tuple[bytes | None, str]:
        """A raster image from ``url``: (content, its type), or (None, why) when the server's
        definite answer is not one: "status-N" for a status other than 200 that is neither a 429
        nor a 5xx (a 404, say), "not-an-image", "too-large" or "too-many-redirects". Every
        address, redirects included, is checked before it is asked: with ``wikimedia`` it must be
        on Wikimedia's image servers (``wikimedia_image``), else http(s) on a public host
        (``check_host``). No answer (an error, a timeout, a 429, a 5xx) is ImageError 502."""
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(url)
            if wikimedia:
                if not wikimedia_image(url):
                    raise ImageError("not an address on Wikimedia's image servers", 403)
            else:
                if parts.scheme not in ("http", "https") or not parts.hostname:
                    raise ImageError("only http(s) image URLs")
                await self.check_host(parts.hostname)
            try:
                async with self._client().stream("GET", url, follow_redirects=False,
                                                 headers={"User-Agent": USER_AGENT}) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        url = urljoin(url, r.headers["location"])
                        continue
                    if r.status_code == 429 or r.status_code >= 500:
                        raise ImageError(f"{parts.hostname} answered {r.status_code}", 502)
                    if r.status_code != 200:
                        return None, f"status-{r.status_code}"
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype not in TYPES:
                        return None, "not-an-image"
                    body = bytearray()
                    async for chunk in r.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            return None, "too-large"
                    return bytes(body), ctype
            except httpx.HTTPError as exc:
                raise ImageError(f"{parts.hostname} did not answer", 502) from exc
        return None, "too-many-redirects"
