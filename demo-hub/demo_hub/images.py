"""Pictures for the agentic flow: an ingredient's thumbnail from Wikipedia, and the images a
recipe page holds, both fetched once by the hub and served from its cache (the browser never
calls a third party, and a second look costs nothing).

- ``ingredient(name)``: the ingredient's head words ("Canned Tomatoes" -> "tomato", "Extra Virgin
  Olive Oil 500ml" -> "olive oil"), looked up as a Wikipedia title, then by Wikipedia search; the
  page's lead image as a 240 px thumbnail. A miss is cached too.
- ``remote(url)``: an image from a recipe page. Guarded: http(s) only, every address the host
  resolves to must be public (no loopback, private, link-local or reserved ranges), redirects are
  re-checked, the reply must be a raster image (SVG can carry script), at most 3 MB.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

WIKI_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "pantry-demo-hub/0.1 (local demo; https://github.com/pjvjay/pantry-platform)"
MAX_BYTES = 3 * 1024 * 1024
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
    def __init__(self, directory: str | Path, client: httpx.AsyncClient | None = None,
                 check_host: Any = public_host) -> None:
        self.dir = Path(directory).expanduser()
        self.client = client
        self.check_host = check_host
        self._index_path = self.dir / "index.json"
        self._index: dict[str, dict[str, Any]] = {}
        if self._index_path.is_file():
            try:
                self._index = json.loads(self._index_path.read_text(encoding="utf-8"))
            except ValueError:
                self._index = {}
        self._lock = asyncio.Lock()

    def _client(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=10, headers={"User-Agent": USER_AGENT})
        return self.client

    def _hit(self, key: str) -> tuple[bool, Path | None, str]:
        entry = self._index.get(key)
        if entry is None:
            return False, None, ""
        file = entry.get("file")
        if file and (self.dir / file).is_file():
            return True, self.dir / file, str(entry.get("type", "image/jpeg"))
        return (not file), None, ""          # a cached miss stays a miss

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
        key = f"ingredient:{query}"
        known, path, ctype = self._hit(key)
        if known:
            return (path, ctype) if path else None
        thumb, title = await self._wiki_thumbnail(query)
        content, ctype = (await self._download(thumb)) if thumb else (None, "")
        path = await self._remember(key, content, ctype or "image/jpeg", query=query,
                                    title=title, source=thumb)
        return (path, ctype) if path else None

    async def _wiki_thumbnail(self, query: str) -> tuple[str | None, str | None]:
        base = {"action": "query", "format": "json", "formatversion": "2", "prop": "pageimages",
                "piprop": "thumbnail", "pithumbsize": "240", "redirects": "1"}
        for params in ({"titles": query[:1].upper() + query[1:]},
                       {"generator": "search", "gsrsearch": f"{query} food", "gsrlimit": "1"}):
            try:
                r = await self._client().get(WIKI_API, params={**base, **params},
                                             headers={"User-Agent": USER_AGENT})
                pages = (r.json().get("query") or {}).get("pages") or []
            except (httpx.HTTPError, ValueError):
                return None, None
            for page in pages:
                source = (page.get("thumbnail") or {}).get("source")
                if source:
                    return str(source), str(page.get("title") or "")
        return None, None

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
