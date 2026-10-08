"""YouTube links: the video's id, its title and channel, and (with a key) its description.

- ``video_id`` reads the id from ``watch?v=``, ``youtu.be/``, ``/shorts/``, ``/live/`` and
  ``/embed/`` links on youtube.com, m.youtube.com, music.youtube.com and
  youtube-nocookie.com; playlist, time and tracking parameters are ignored.
- ``oembed`` asks YouTube's oEmbed endpoint (no key) for the title and the channel, through the
  same guarded fetch as a recipe page.
- ``video_details`` is YouTube Data API v3 ``videos.list?part=snippet,contentDetails`` (1 unit of
  the default 10,000 a day): the description and the length. It runs only with a key the user
  created (``~/.pantry-secrets/youtube_api_key``), sent in the ``x-goog-api-key`` header so it is
  never in a URL, a log line or a trace.

Not used, on purpose: caption download (``captions.download`` needs the video owner's
permission) and reading the watch page or its transcript (YouTube's terms forbid scraping).
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlencode

import httpx

from demo_hub.recipe_import.fetch import ImportFailure, Limits, Resolver, fetch_page, resolve

YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com",
                           "music.youtube.com", "youtube-nocookie.com",
                           "www.youtube-nocookie.com"})
SHORT_HOSTS = frozenset({"youtu.be", "www.youtu.be"})
ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
PATH_FORMS = ("shorts", "live", "embed", "v", "e")
OEMBED_URL = "https://www.youtube.com/oembed"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
ISO_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def video_id(url: str) -> str | None:
    """The 11-character video id a YouTube link names, or None for anything else (a channel,
    a playlist without a video, another site)."""
    try:
        parsed = httpx.URL(url.strip())
    except httpx.InvalidURL:
        return None
    host = parsed.host.lower()
    parts = [p for p in parsed.path.split("/") if p]
    candidate = None
    if host in SHORT_HOSTS:
        candidate = parts[0] if parts else None
    elif host in YOUTUBE_HOSTS:
        if parts[:1] == ["watch"]:
            candidate = (parse_qs(parsed.query.decode()).get("v") or [None])[0]
        elif len(parts) >= 2 and parts[0] in PATH_FORMS:
            candidate = parts[1]
    return candidate if candidate and ID.match(candidate) else None


def watch_url(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


def duration_seconds(iso: str) -> int | None:
    """``PT1H2M3S`` -> 3723; None when it is not an ISO 8601 duration (a live stream's P0D is
    0, which is not a length either)."""
    m = ISO_DURATION.match(iso or "")
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    total = ((d * 24 + h) * 60 + mi) * 60 + s
    return total or None


async def oembed(vid: str, *, limits: Limits, resolver: Resolver = resolve,
                 transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """{title, channel, channel_url, thumbnail_url} for a public video. 422 not_public when
    YouTube has no public, embeddable video by that id (oEmbed answers 401, 403 or 404)."""
    url = f"{OEMBED_URL}?{urlencode({'url': watch_url(vid), 'format': 'json'})}"
    try:
        page = await fetch_page(url, limits=limits, resolver=resolver, transport=transport,
                                accept="application/json")
    except ImportFailure as exc:
        if exc.code == "http_status" and exc.detail.get("upstream_status") in (401, 403, 404):
            raise ImportFailure(422, "not_public", "YouTube has no public video at that link "
                                "(it may be private, removed or not embeddable).") from exc
        raise
    try:
        body = json.loads(page.text)
    except ValueError as exc:
        raise ImportFailure(502, "bad_upstream", "YouTube's oEmbed answer was not JSON.") from exc
    return {"title": str(body.get("title") or "")[:200],
            "channel": str(body.get("author_name") or "")[:200],
            "channel_url": str(body.get("author_url") or "")[:2000],
            "thumbnail_url": str(body.get("thumbnail_url") or "")[:2000]}


async def video_details(vid: str, api_key: str, *, timeout_s: float = 20,
                        transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """{title, channel, description, duration_s} from videos.list. The description is used
    here, for its ingredient lines and links, and never stored or shown to a model."""
    try:
        async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=timeout_s,
                                     follow_redirects=False) as client:
            r = await client.get(VIDEOS_URL, params={"part": "snippet,contentDetails", "id": vid},
                                 headers={"x-goog-api-key": api_key})
    except httpx.HTTPError as exc:
        raise ImportFailure(502, "unreachable", "The YouTube Data API could not be reached "
                            f"({type(exc).__name__}).") from exc
    if r.status_code == 403:
        raise ImportFailure(502, "youtube_api", "The YouTube Data API refused the key (quota "
                            "spent, or the API not enabled for it).")
    if r.status_code != 200:
        raise ImportFailure(502, "youtube_api", f"The YouTube Data API answered HTTP "
                            f"{r.status_code}.")
    items = (r.json() or {}).get("items") or []
    if not items:
        raise ImportFailure(422, "not_public", "YouTube has no public video at that link.")
    snippet = items[0].get("snippet") or {}
    details = items[0].get("contentDetails") or {}
    return {"title": str(snippet.get("title") or "")[:200],
            "channel": str(snippet.get("channelTitle") or "")[:200],
            "description": str(snippet.get("description") or ""),
            "duration_s": duration_seconds(str(details.get("duration") or ""))}
