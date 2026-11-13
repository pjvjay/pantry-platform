"""Pictures (images.py): a demo recipe's, the lead image of the Wikipedia article it names
(``GET /hub/images/wiki?title=``, ``wiki``); an ingredient's, its words looked up as a title and
then by search (``GET /hub/images/ingredient?name=``, ``ingredient``); and a recipe page's image
(``GET /hub/images/remote?url=``, ``remote``), through the image cache with the same User-Agent.
A title is looked up in Wikipedia's REST page summary, words in its action API's search, and a
thumbnail is fetched only from Wikimedia's own image servers, checked by name, with no DNS
lookup. A miss is cached only when the server answered and had nothing; one that does not answer
is a 502 and is asked again later. Wikipedia is asked as its etiquette asks: one request at a
time to each endpoint, one lookup per picture, a 429 retried once only when it asks for a short
wait, then a pause for that endpoint. Against a fake Wikipedia and fake image servers on a mock
transport; never the real ones."""

from __future__ import annotations

import asyncio
import json
import socket
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import httpx
import pytest
from fastapi import FastAPI

from demo_hub import app as app_module
from demo_hub import images
from demo_hub.images import ImageCache, ImageError
from demo_hub.settings import Settings
from tests.conftest import console_client

API, UPLOAD, THUMBS = "en.wikipedia.org", "upload.wikimedia.org", "thumb.wikimedia.org"
SUMMARY = "/api/rest_v1/page/summary/"
THUMB = "https://upload.wikimedia.org/wikipedia/commons/thumb/5/58/Congee.jpg/330px-Congee.jpg"
ORIGINAL = "https://upload.wikimedia.org/wikipedia/commons/5/58/Congee.jpg"
JPEG = b"\xff\xd8congee"
PNG = b"\x89PNG\r\n\x1a\nrecipe"
PAGE_IMAGE = "https://recipes.example.org/img/congee.png"

Answer = Callable[[httpx.Request], httpx.Response]


def served(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})


def png(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})


def refused(status: int, headers: dict[str, str] | None = None) -> Answer:
    return lambda request: httpx.Response(status, text="refused", headers=headers)


def raises(error: type[httpx.TransportError]) -> Answer:
    def answer(request: httpx.Request) -> httpx.Response:
        raise error("no answer", request=request)
    return answer


def redirect(location: str) -> Answer:
    return lambda request: httpx.Response(302, headers={"location": location})


def endpoint(request: httpx.Request) -> str:
    """Which server a request asked: "summary" or "search" (Wikipedia's two endpoints), else
    the image server's host."""
    if request.url.host != API:
        return request.url.host
    return "summary" if request.url.raw_path.startswith(SUMMARY.encode()) else "search"


def asked_for(request: httpx.Request) -> str:
    """What a request asked for: the title a page summary was asked for, the words a search
    looked for, or the image's address."""
    kind = endpoint(request)
    if kind == "summary":
        return unquote(request.url.raw_path.decode()[len(SUMMARY):]).replace("_", " ")
    if kind == "search":
        return request.url.params["gsrsearch"]
    return str(request.url)


class Wikipedia:
    """Wikipedia's two endpoints and Wikimedia's image servers, and every request the hub sends
    them. The page summary answers per title: ``thumbs`` maps a title to its lead image's
    thumbnail (None: an article without one; a title not there has no article, a 404),
    ``redirects`` a title to the article it redirects to, and a title in ``disambiguation`` is a
    disambiguation page. The search answers per words searched: ``hits`` maps them to the title
    of the hit, whose thumbnail ``thumbs`` names. ``refuse`` queues answers an endpoint
    ("summary" or "search") gives first, in order (None: its usual answer); ``thumbnail`` is the
    image servers' answer. An endpoint in ``delay`` answers that many seconds late, ``hold``
    keeps thumbnails waiting until it is set, and ``most`` is the most requests an endpoint (or
    image server) had at once."""

    def __init__(self, thumbs: dict[str, str | None] | None = None,
                 hits: dict[str, str] | None = None, redirects: dict[str, str] | None = None,
                 disambiguation: set[str] | None = None) -> None:
        self.thumbs = {"Congee": THUMB} if thumbs is None else thumbs
        self.hits = hits or {}
        self.redirects = redirects or {}
        self.disambiguation = disambiguation or set()
        self.requests: list[httpx.Request] = []
        self.down = False
        self.refusals: dict[str, list[Answer | None]] = {"summary": [], "search": []}
        self.thumbnail: Answer = served
        self.delay: dict[str, float] = {}
        self.hold: asyncio.Event | None = None
        self.busy: Counter[str] = Counter()
        self.most: Counter[str] = Counter()

    def refuse(self, kind: str, *answers: Answer | None) -> None:
        self.refusals[kind].extend(answers)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        kind = endpoint(request)
        self.busy[kind] += 1
        self.most[kind] = max(self.most[kind], self.busy[kind])
        try:
            if kind in self.delay:
                await asyncio.sleep(self.delay[kind])
            if self.hold is not None and kind not in self.refusals:
                await self.hold.wait()
            return self.answer(request)
        finally:
            self.busy[kind] -= 1

    def answer(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("no route", request=request)
        kind = endpoint(request)
        if kind not in self.refusals:
            return self.thumbnail(request)
        refusal = self.refusals[kind].pop(0) if self.refusals[kind] else None
        if refusal is not None:
            return refusal(request)
        return self.summary(asked_for(request)) if kind == "summary" else self.search(request)

    def summary(self, title: str) -> httpx.Response:
        if title in self.redirects:            # as Wikipedia does: relative, to the target's
            return httpx.Response(302, headers={
                "location": quote(self.redirects[title].replace(" ", "_"))})
        if title in self.disambiguation:
            return httpx.Response(200, json={"type": "disambiguation", "title": title,
                                             "titles": {"normalized": title}})
        if title not in self.thumbs:
            return httpx.Response(404, json={
                "type": "https://mediawiki.org/wiki/HyperSwitch/errors/not_found",
                "title": "Not found.", "detail": "Page or revision not found."})
        page: dict[str, Any] = {"type": "standard", "title": title, "pageid": 1, "titles": {
            "canonical": title.replace(" ", "_"), "normalized": title, "display": title}}
        if self.thumbs[title]:
            page["thumbnail"] = {"source": self.thumbs[title], "width": 330, "height": 220}
            page["originalimage"] = {"source": ORIGINAL, "width": 2000, "height": 1333}
        return httpx.Response(200, json=page)

    def search(self, request: httpx.Request) -> httpx.Response:
        title = self.hits.get(request.url.params["gsrsearch"])
        if title is None:                      # no hit: the answer has no "query" at all
            return httpx.Response(200, json={"batchcomplete": True})
        page: dict[str, Any] = {"pageid": 1, "title": title}
        if self.thumbs.get(title):
            page["thumbnail"] = {"source": self.thumbs[title], "width": 330, "height": 220}
        return httpx.Response(200, json={"query": {"pages": [page]}})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def asked(self, kind: str) -> list[httpx.Request]:
        return [r for r in self.requests if endpoint(r) == kind]


class Site:
    """A recipe site's image server: ``answer`` is its answer (a PNG unless set otherwise), and
    every request it had."""

    def __init__(self) -> None:
        self.answer: Answer = png
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


@dataclass
class Clock:
    """The cache's sleep and clock: a sleep never waits, the clock moves instead."""
    now: float = 1000.0
    sleeps: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def public(host: str) -> None:
    return None


def cache_on(folder: Path, fake: Wikipedia | Site, clock: Clock | None = None) -> ImageCache:
    clock = clock or Clock()
    return ImageCache(folder, fake.client(), check_host=public, sleep=clock.sleep, clock=clock)


def wiki(cache: ImageCache, title: str) -> tuple[Path, str] | None:
    return asyncio.run(cache.wiki(title))


def ingredient(cache: ImageCache, name: str) -> tuple[Path, str] | None:
    return asyncio.run(cache.ingredient(name))


def remote(cache: ImageCache, url: str) -> tuple[Path, str] | None:
    return asyncio.run(cache.remote(url))


def index(folder: Path) -> dict[str, Any]:
    """The cache's index.json as it is on disk; {} before anything is kept."""
    path = folder / "index.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def make_app(tmp_path: Path, fake: Wikipedia, clock: Clock | None = None) -> FastAPI:
    app = app_module.create_app(Settings(
        traces_dir=str(tmp_path / "traces"), images_dir=str(tmp_path / "images"),
        usage_dir=str(tmp_path / "usage"), calendar_dir=str(tmp_path / "calendar"),
        timings_path=str(tmp_path / "timings.jsonl")))
    clock = clock or Clock()
    cache = app.state.images
    cache.client, cache.check_host, cache.sleep, cache.clock = (fake.client(), public,
                                                                clock.sleep, clock)
    return app


# --- a recipe's picture ------------------------------------------------------------------------


def test_the_articles_lead_image_is_the_thumbnail_its_page_summary_names(tmp_path: Path) -> None:
    fake = Wikipedia()
    checked: list[str] = []

    async def guard(host: str) -> None:            # remote()'s guard: no thumbnail goes through it
        checked.append(host)

    first = wiki(ImageCache(tmp_path, fake.client(), check_host=guard), "Congee")
    assert first is not None and first[1] == "image/jpeg" and first[0].read_bytes() == JPEG
    summary, thumb = fake.requests
    assert str(summary.url) == "https://en.wikipedia.org/api/rest_v1/page/summary/Congee"
    # the same User-Agent on both; the thumbnail as the summary names it (not the original image)
    assert {r.headers["user-agent"] for r in (summary, thumb)} == {images.USER_AGENT}
    assert str(thumb.url) == THUMB and checked == []
    assert index(tmp_path)["wiki:Congee"]["source"] == THUMB
    # a second look, even from a new cache on the same folder, asks nothing
    again = wiki(ImageCache(tmp_path, Wikipedia({}).client(), check_host=guard), " Congee ")
    assert again == first and len(fake.requests) == 2


def test_only_the_named_article_is_asked_its_redirect_followed_never_a_search(
        tmp_path: Path) -> None:
    fake = Wikipedia({"Pa amb tomàquet": THUMB, "Fish & chips/History?": THUMB,
                      "Bolognese sauce": THUMB},
                     redirects={"Spaghetti alla bolognese": "Bolognese sauce"})
    cache = cache_on(tmp_path, fake)
    assert wiki(cache, "Pa amb tomàquet") is not None
    assert wiki(cache, "Fish & chips/History?") is not None
    assert wiki(cache, "Spaghetti_alla_bolognese") is not None
    assert wiki(cache, "Biryani") is None                    # no article: nothing is searched
    assert [str(r.url).removeprefix(f"https://{API}{SUMMARY}") for r in fake.asked("summary")] == [
        "Pa_amb_tom%C3%A0quet", "Fish_%26_chips%2FHistory%3F", "Spaghetti_alla_bolognese",
        "Bolognese_sauce", "Biryani"]
    assert fake.asked("search") == []
    assert index(tmp_path)["wiki:Spaghetti alla bolognese"]["title"] == "Bolognese sauce"


@pytest.mark.parametrize("thumbs,disambiguation,reason", [
    ({"Congee": None}, set(), "no-image"), ({}, set(), "no-article"),
    ({"Congee": THUMB}, {"Congee"}, "no-article")],
    ids=["no lead image", "no article", "a disambiguation page"])
def test_an_article_without_a_lead_image_is_a_miss_and_the_miss_is_cached(
        tmp_path: Path, thumbs: dict[str, str | None], disambiguation: set[str],
        reason: str) -> None:
    fake = Wikipedia(thumbs, disambiguation=disambiguation)
    assert wiki(ImageCache(tmp_path, fake.client(), check_host=public), "Congee") is None
    assert len(fake.requests) == 1
    entry = index(tmp_path)["wiki:Congee"]
    assert (entry["file"], entry["answered"], entry["reason"]) == (None, True, reason)
    later = Wikipedia()
    assert wiki(ImageCache(tmp_path, later.client(), check_host=public), "Congee") is None
    assert later.requests == []


def test_a_wikipedia_that_does_not_answer_is_502_and_never_cached(tmp_path: Path) -> None:
    fake = Wikipedia()
    fake.down = True
    with pytest.raises(ImageError) as exc:
        wiki(ImageCache(tmp_path, fake.client(), check_host=public), "Congee")
    assert exc.value.status == 502
    fake.down = False
    fake.thumbnail = refused(503)
    with pytest.raises(ImageError) as exc:
        wiki(ImageCache(tmp_path, fake.client(), check_host=public), "Congee")
    assert exc.value.status == 502 and index(tmp_path) == {}
    fake.thumbnail = served
    found = wiki(ImageCache(tmp_path, fake.client(), check_host=public), "Congee")
    assert found is not None and found[0].read_bytes() == JPEG


@pytest.mark.parametrize("title", ["", "   ", "Congee|Biryani", "[[Congee]]", "Congee#History",
                                   "a\x07b", "x" * 256, "é" * 128])
def test_a_title_no_article_can_have_is_refused_before_wikipedia_is_asked(
        tmp_path: Path, title: str) -> None:
    fake = Wikipedia()
    with pytest.raises(ImageError) as exc:
        wiki(ImageCache(tmp_path, fake.client(), check_host=public), title)
    assert exc.value.status == 400 and fake.requests == []


def test_the_route_serves_the_picture_and_says_404_or_400(tmp_path: Path) -> None:
    client = console_client(make_app(tmp_path, Wikipedia({"Congee": THUMB, "Toast": None})))
    r = client.get("/hub/images/wiki", params={"title": "Congee"})
    assert r.status_code == 200 and r.content == JPEG
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "public, max-age=604800"
    assert r.headers["x-content-type-options"] == "nosniff"
    miss = client.get("/hub/images/wiki", params={"title": "Toast"})
    assert miss.status_code == 404 and miss.headers["cache-control"] == "max-age=3600"
    assert client.get("/hub/images/wiki", params={"title": "A|B"}).status_code == 400
    assert client.get("/hub/images/wiki").status_code == 422


# --- only from Wikimedia's image servers, with no DNS lookup -----------------------------------


ON_THUMBS = ("https://thumb.wikimedia.org/wikipedia/commons/thumb/2/29/Garlic.jpg/330px-Garlic.jpg"
             "?utm_source=enwiki&utm_medium=api")
ELSEWHERE = {
    "http": "http://upload.wikimedia.org/wikipedia/commons/5/58/Congee.jpg",
    "another Wikimedia host": "https://commons.wikimedia.org/wiki/File:Congee.jpg",
    "a look-alike host": "https://upload.wikimedia.org.example.com/Congee.jpg",
    "a trailing dot": "https://upload.wikimedia.org./Congee.jpg",
    "a user and password": "https://user:secret@upload.wikimedia.org/Congee.jpg",
    "a user named like the host": "https://upload.wikimedia.org@10.0.0.1/Congee.jpg",
    "another port": "https://upload.wikimedia.org:8443/Congee.jpg",
    "a port that is not one": "https://upload.wikimedia.org:abc/Congee.jpg",
    "a private address": "https://10.0.0.1/Congee.jpg",
    "no scheme": "//upload.wikimedia.org/Congee.jpg",
}


@pytest.mark.parametrize("url,ok", [
    (THUMB, True), (ON_THUMBS, True),
    ("https://upload.wikimedia.org:443/wikipedia/commons/5/58/Congee.jpg", True),
    ("https://Upload.Wikimedia.ORG/wikipedia/commons/5/58/Congee.jpg", True),
    *((url, False) for url in ELSEWHERE.values())],
    ids=["upload", "thumb", "the default port", "any case", *ELSEWHERE])
def test_only_wikimedias_image_servers_are_allowed(url: str, ok: bool) -> None:
    assert images.wikimedia_image(url) is ok


def test_a_thumbnail_is_fetched_without_a_dns_lookup_a_pages_image_still_has_one(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    looked_up: list[str] = []

    def getaddrinfo(host: str, *args: Any, **kwargs: Any) -> Any:
        looked_up.append(host)
        raise socket.gaierror("no DNS on this machine: names resolve at the proxy")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    fake = Wikipedia({"Congee": THUMB, "Garlic": ON_THUMBS})
    cache = ImageCache(tmp_path, fake.client())          # the hub's own guards
    assert wiki(cache, "Congee") is not None and ingredient(cache, "Fresh Garlic") is not None
    assert [r.url.host for r in fake.requests] == [API, UPLOAD, API, THUMBS]
    assert str(fake.requests[-1].url) == ON_THUMBS and looked_up == []
    # a recipe page's image may be anywhere: its host is still looked up (here, in vain)
    with pytest.raises(ImageError) as exc:
        remote(cache, PAGE_IMAGE)
    assert exc.value.status == 404 and looked_up == ["recipes.example.org"]
    assert len(fake.requests) == 4


@pytest.mark.parametrize("url", list(ELSEWHERE.values()), ids=list(ELSEWHERE))
def test_a_lead_image_anywhere_else_is_never_fetched_and_the_miss_says_why(
        tmp_path: Path, url: str) -> None:
    checked: list[str] = []

    async def guard(host: str) -> None:
        checked.append(host)

    fake = Wikipedia({"Congee": url})
    assert wiki(ImageCache(tmp_path, fake.client(), check_host=guard), "Congee") is None
    assert [endpoint(r) for r in fake.requests] == ["summary"] and checked == []
    entry = index(tmp_path)["wiki:Congee"]
    assert (entry["file"], entry["answered"], entry["reason"]) == (None, True, "not-wikimedia")
    assert (entry["title"], entry["source"]) == ("Congee", url)


def test_a_thumbnails_redirect_is_held_to_the_same_rule(tmp_path: Path) -> None:
    moved = ON_THUMBS.replace("Garlic", "Congee")
    fake = Wikipedia()
    fake.thumbnail = lambda r: redirect(moved)(r) if r.url.host == UPLOAD else served(r)
    found = wiki(cache_on(tmp_path / "a", fake), "Congee")
    assert found is not None and found[0].read_bytes() == JPEG
    assert [asked_for(r) for r in fake.requests] == ["Congee", THUMB, moved]
    # a redirect off Wikimedia's image servers is refused, and nothing is kept
    away = Wikipedia()
    away.thumbnail = redirect("http://10.0.0.1/congee.jpg")
    with pytest.raises(ImageError) as exc:
        wiki(cache_on(tmp_path / "b", away), "Congee")
    assert exc.value.status == 403 and index(tmp_path / "b") == {}
    assert [r.url.host for r in away.requests] == [API, UPLOAD]


# --- an ingredient's picture -------------------------------------------------------------------


def test_an_ingredient_is_looked_up_as_a_title_in_its_page_summary(tmp_path: Path) -> None:
    fake = Wikipedia({"Olive oil": THUMB})
    found = ingredient(cache_on(tmp_path, fake), "Extra Virgin Olive Oil 500ml")
    assert found is not None and found[0].read_bytes() == JPEG
    summary, thumb = fake.requests
    assert str(summary.url) == "https://en.wikipedia.org/api/rest_v1/page/summary/Olive_oil"
    assert str(thumb.url) == THUMB and fake.asked("search") == []
    entry = index(tmp_path)["ingredient:olive oil"]
    assert (entry["query"], entry["title"], entry["source"]) == ("olive oil", "Olive oil", THUMB)


@pytest.mark.parametrize("name,title", [("Fresh Mint 30g", "Mentha"),
                                        ("Granulated Sugar 2kg", "Sugar")])
def test_an_ingredient_whose_own_article_misleads_asks_for_the_right_one(
        tmp_path: Path, name: str, title: str) -> None:
    fake = Wikipedia({title: THUMB})
    found = ingredient(cache_on(tmp_path, fake), name)
    assert found is not None and found[0].read_bytes() == JPEG
    summary, _thumb = fake.requests
    assert str(summary.url) == f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
    (entry,) = index(tmp_path).values()
    assert entry["title"] == title


@pytest.mark.parametrize("thumbs,disambiguation", [
    ({}, set()), ({"Penne": THUMB}, {"Penne"}), ({"Penne": None}, set()),
    ({"Penne": "https://example.org/penne.jpg"}, set())],
    ids=["no article", "a disambiguation page", "no lead image", "a lead image elsewhere"])
def test_an_ingredient_without_a_usable_article_is_searched(
        tmp_path: Path, thumbs: dict[str, str | None], disambiguation: set[str]) -> None:
    fake = Wikipedia({**thumbs, "Penne alla vodka": THUMB}, hits={"penne food": "Penne alla vodka"},
                     disambiguation=disambiguation)
    found = ingredient(cache_on(tmp_path, fake), "Penne Rigate 500g")
    assert found is not None and found[0].read_bytes() == JPEG
    summary, search, thumb = fake.requests
    assert asked_for(summary) == "Penne" and str(thumb.url) == THUMB
    assert dict(search.url.params) == {
        "action": "query", "format": "json", "formatversion": "2", "prop": "pageimages",
        "piprop": "thumbnail", "pithumbsize": "330", "redirects": "1",
        "generator": "search", "gsrsearch": "penne food", "gsrlimit": "1"}
    assert str(search.url).startswith(images.WIKI_API)
    assert index(tmp_path)["ingredient:penne"]["title"] == "Penne alla vodka"


@pytest.mark.parametrize("thumbs,reason,title,source", [
    ({}, "no-article", None, None), ({"Garlic": None}, "no-image", "Garlic", None),
    ({"Garlic": "https://example.org/garlic.jpg"}, "not-wikimedia", "Garlic",
     "https://example.org/garlic.jpg")],
    ids=["no article and no search hit", "an article without a lead image",
         "a lead image elsewhere"])
def test_a_definite_miss_is_cached_and_says_why(
        tmp_path: Path, thumbs: dict[str, str | None], reason: str, title: str | None,
        source: str | None) -> None:
    fake = Wikipedia(thumbs)
    assert ingredient(cache_on(tmp_path, fake), "Fresh Garlic") is None
    assert [asked_for(r) for r in fake.requests] == ["Garlic", "garlic food"]
    entry = index(tmp_path)["ingredient:garlic"]
    assert entry["file"] is None and entry["answered"] is True
    assert (entry["reason"], entry["title"], entry["source"]) == (reason, title, source)
    later = Wikipedia({"Garlic": THUMB})
    assert ingredient(cache_on(tmp_path, later), "garlic bulb") is None
    assert later.requests == []


# --- a server that does not answer is a 502, never a cached miss -------------------------------


NO_ANSWER: dict[str, list[Answer | None]] = {
    "connect error": [raises(httpx.ConnectError)],
    "timeout": [raises(httpx.ReadTimeout)],
    "429, and 429 again": [refused(429, {"retry-after": "1"})] * 2,
    "429 for 30 s": [refused(429, {"retry-after": "30"})],
    "503": [refused(503)],
    "503, as JSON": [lambda request: httpx.Response(503, json={
        "type": "https://mediawiki.org/wiki/HyperSwitch/errors/unknown_error",
        "title": "Service Unavailable"})],
    "403": [refused(403)],
    "not JSON": [lambda request: httpx.Response(200, text="<html>Wikimedia Error</html>")],
    "not an object": [lambda request: httpx.Response(200, json=["Garlic"])],
}
SUMMARY_NO_ANSWER: dict[str, list[Answer | None]] = {
    **NO_ANSWER,
    "a redirect off the page summaries": [redirect("https://example.org/Garlic")],
    "redirects without end": [redirect("Garlic")] * (images.MAX_REDIRECTS + 1),
}
SEARCH_NO_ANSWER: dict[str, list[Answer | None]] = {
    **NO_ANSWER,
    "an API error": [lambda request: httpx.Response(200, json={"error": {
        "code": "internal_api_error_DBQueryError", "info": "a database query error"}})],
}


@pytest.mark.parametrize("lookup", ["ingredient", "wiki"])
@pytest.mark.parametrize("answers", list(SUMMARY_NO_ANSWER.values()), ids=list(SUMMARY_NO_ANSWER))
def test_a_page_summary_that_does_not_answer_is_a_502_and_a_later_answer_is_served(
        tmp_path: Path, answers: list[Answer | None], lookup: str) -> None:
    fake, clock = Wikipedia({"Garlic": THUMB}), Clock()
    fake.refuse("summary", *answers)
    cache = cache_on(tmp_path, fake, clock)

    def look() -> tuple[Path, str] | None:
        if lookup == "ingredient":
            return ingredient(cache, "Fresh Garlic")
        return wiki(cache, "Garlic")

    with pytest.raises(ImageError) as exc:
        look()
    assert exc.value.status == 502 and index(tmp_path) == {}
    assert fake.asked("search") == []              # no answer is not "no article": no search
    clock.now += images.MAX_PAUSE_S                # past the pause: asked again, and found
    found = look()
    assert found is not None and found[0].read_bytes() == JPEG
    assert len(fake.asked("summary")) == len(answers) + 1
    assert {r.url.host for r in fake.requests} == {API, UPLOAD}    # no redirect elsewhere


@pytest.mark.parametrize("answers", list(SEARCH_NO_ANSWER.values()), ids=list(SEARCH_NO_ANSWER))
def test_a_search_that_does_not_answer_is_a_502_and_a_later_answer_is_served(
        tmp_path: Path, answers: list[Answer | None]) -> None:
    fake = Wikipedia({"Garlic (food)": THUMB}, hits={"garlic food": "Garlic (food)"})
    clock = Clock()
    fake.refuse("search", *answers)
    cache = cache_on(tmp_path, fake, clock)
    with pytest.raises(ImageError) as exc:
        ingredient(cache, "Fresh Garlic")
    assert exc.value.status == 502 and index(tmp_path) == {}
    clock.now += images.MAX_PAUSE_S
    found = ingredient(cache, "Fresh Garlic")
    assert found is not None and found[0].read_bytes() == JPEG
    assert len(fake.asked("search")) == len(answers) + 1


def test_an_ingredient_is_a_miss_only_when_its_search_was_answered_too(tmp_path: Path) -> None:
    fake, clock = Wikipedia({"Garlic": None}), Clock()
    fake.refuse("search", refused(503))       # the title answered (no lead image), the search not
    cache = cache_on(tmp_path, fake, clock)
    with pytest.raises(ImageError) as exc:
        ingredient(cache, "Fresh Garlic")
    assert exc.value.status == 502 and index(tmp_path) == {}
    clock.now += images.PAUSE_S
    assert ingredient(cache, "Fresh Garlic") is None
    assert index(tmp_path)["ingredient:garlic"]["reason"] == "no-image"


@pytest.mark.parametrize("answer", [
    refused(429), refused(503), refused(403), refused(404), raises(httpx.ConnectError),
    lambda request: httpx.Response(200, text="<html>", headers={"content-type": "text/html"})],
    ids=["429", "503", "403", "404", "connect error", "not an image"])
def test_a_thumbnail_that_cannot_be_fetched_is_a_502_and_never_a_cached_miss(
        tmp_path: Path, answer: Answer) -> None:
    fake = Wikipedia({"Garlic": THUMB})
    fake.thumbnail = answer
    with pytest.raises(ImageError) as exc:
        ingredient(cache_on(tmp_path, fake), "Fresh Garlic")
    assert exc.value.status == 502 and index(tmp_path) == {}
    fake.thumbnail = served
    found = ingredient(cache_on(tmp_path, fake), "Fresh Garlic")
    assert found is not None and found[0].read_bytes() == JPEG


def test_a_miss_older_code_kept_or_one_over_7_days_old_is_looked_up_again(tmp_path: Path) -> None:
    def miss(days: float, **about: Any) -> dict[str, Any]:
        at = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        return {"file": None, "type": "image/jpeg", "at": at, "source": None, **about}

    kept, unmarked, old = (f"https://example.org/{n}.png" for n in ("kept", "unmarked", "old"))
    (tmp_path / "index.json").write_text(json.dumps({
        # older code kept a server that did not answer as a miss too, and did not say so
        "ingredient:garlic": miss(0, query="garlic", title=None),
        "wiki:Congee": miss(8, title="Congee", answered=True, reason="no-image"),
        "ingredient:penne": miss(6, query="penne", title=None, answered=True,
                                 reason="no-article"),
        f"url:{kept}": miss(1, answered=True, reason="status-404"),
        f"url:{unmarked}": miss(1),
        f"url:{old}": miss(8, answered=True, reason="status-404"),
    }), encoding="utf-8")
    fake = Wikipedia({"Garlic": THUMB, "Congee": THUMB, "Penne": THUMB})
    cache = cache_on(tmp_path, fake)
    assert ingredient(cache, "Fresh Garlic") is not None
    assert wiki(cache, "Congee") is not None
    assert ingredient(cache, "Penne Rigate 500g") is None        # 6 days old: still believed
    assert remote(cache, kept) is None                           # a page's, 1 day old: believed
    assert remote(cache, unmarked) is not None
    assert remote(cache, old) is not None
    assert [asked_for(r) for r in fake.requests] == [
        "Garlic", THUMB, "Congee", THUMB, unmarked, old]
    stored = index(tmp_path)
    assert stored["ingredient:garlic"]["file"] and stored["wiki:Congee"]["file"]
    assert stored["ingredient:penne"]["reason"] == "no-article"


# --- a recipe page's image ---------------------------------------------------------------------


REMOTE_NO_ANSWER: dict[str, Answer] = {
    "connect error": raises(httpx.ConnectError),
    "timeout": raises(httpx.ReadTimeout),
    "429": refused(429, {"retry-after": "30"}),
    "500": refused(500),
    "503": refused(503),
    "a redirect to a 503": lambda r: (httpx.Response(503) if r.url.path == "/busy"
                                      else redirect("/busy")(r)),
}
REMOTE_DEFINITE: dict[str, tuple[Answer, str]] = {
    "404": (refused(404), "status-404"),
    "410": (refused(410), "status-410"),
    "403": (refused(403), "status-403"),
    "a page, not an image": (lambda r: httpx.Response(200, text="<html>", headers={
        "content-type": "text/html"}), "not-an-image"),
    "SVG": (lambda r: httpx.Response(200, content=b"<svg/>", headers={
        "content-type": "image/svg+xml"}), "not-an-image"),
    "too large": (lambda r: httpx.Response(200, content=PNG + bytes(64), headers={
        "content-type": "image/png"}), "too-large"),
    "redirects without end": (lambda r: redirect(r.url.path + "x")(r), "too-many-redirects"),
}


@pytest.mark.parametrize("answer", list(REMOTE_NO_ANSWER.values()), ids=list(REMOTE_NO_ANSWER))
def test_a_pages_image_whose_server_does_not_answer_is_a_502_and_never_a_cached_miss(
        tmp_path: Path, answer: Answer) -> None:
    site = Site()
    site.answer = answer
    with pytest.raises(ImageError) as exc:
        remote(cache_on(tmp_path, site), PAGE_IMAGE)
    assert exc.value.status == 502 and index(tmp_path) == {}
    site.answer = png                              # it answers later: served, and kept
    found = remote(cache_on(tmp_path, site), PAGE_IMAGE)
    assert found is not None and found[1] == "image/png" and found[0].read_bytes() == PNG
    assert index(tmp_path)[f"url:{PAGE_IMAGE}"]["file"]


@pytest.mark.parametrize("answer,reason", list(REMOTE_DEFINITE.values()), ids=list(REMOTE_DEFINITE))
def test_a_pages_image_its_server_definitely_does_not_have_is_a_cached_miss(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: Answer, reason: str) -> None:
    monkeypatch.setattr(images, "MAX_BYTES", 64)
    site = Site()
    site.answer = answer
    assert remote(cache_on(tmp_path, site), PAGE_IMAGE) is None
    entry = index(tmp_path)[f"url:{PAGE_IMAGE}"]
    assert (entry["file"], entry["answered"], entry["reason"]) == (None, True, reason)
    assert entry["source"] == PAGE_IMAGE
    asked = len(site.requests)
    site.answer = png                              # the miss is believed: not asked again
    assert remote(cache_on(tmp_path, site), PAGE_IMAGE) is None
    assert len(site.requests) == asked


# --- asking Wikipedia politely -----------------------------------------------------------------


@pytest.mark.parametrize("kind", ["summary", "search"])
@pytest.mark.parametrize("headers,waited", [
    ({"retry-after": "2"}, 2.0), ({"retry-after": "0"}, 0.0), ({}, 1.0)],
    ids=["Retry-After 2", "Retry-After 0", "no Retry-After"])
def test_a_429_that_asks_for_a_short_wait_is_retried_once_after_it(
        tmp_path: Path, headers: dict[str, str], waited: float, kind: str) -> None:
    fake = Wikipedia({"Penne alla vodka": THUMB}, hits={"penne food": "Penne alla vodka"})
    clock = Clock()
    fake.refuse(kind, refused(429, headers))
    found = ingredient(cache_on(tmp_path, fake, clock), "Penne")
    assert found is not None and found[0].read_bytes() == JPEG
    first, retry = fake.asked(kind)
    assert first.url == retry.url and clock.sleeps == [waited]


@pytest.mark.parametrize("kind", ["summary", "search"])
def test_a_429_that_asks_for_longer_is_not_retried_but_waited_out(
        tmp_path: Path, kind: str) -> None:
    fake = Wikipedia({"Penne alla vodka": THUMB}, hits={"penne food": "Penne alla vodka"})
    clock = Clock()
    fake.refuse(kind, refused(429, {"retry-after": "3"}))
    cache = cache_on(tmp_path, fake, clock)
    with pytest.raises(ImageError) as exc:
        ingredient(cache, "Penne")
    assert exc.value.status == 502 and str(exc.value).endswith("not asked again for 3 s")
    assert len(fake.asked(kind)) == 1 and clock.sleeps == []
    clock.now += 3
    assert ingredient(cache, "Penne") is not None and len(fake.asked(kind)) == 2


@pytest.mark.parametrize("answers,pause", [
    ([refused(429, {"retry-after": "30"})], 30.0),
    ([refused(429, {"retry-after": "3600"})], images.MAX_PAUSE_S),
    ([refused(429)] * 2, images.PAUSE_S),
    ([refused(503)], images.PAUSE_S),
    ([raises(httpx.ConnectTimeout)], images.PAUSE_S)],
    ids=["429 for 30 s", "429 for an hour", "429", "503", "timeout"])
def test_after_no_answer_the_page_summary_is_left_alone_for_a_while(
        tmp_path: Path, answers: list[Answer | None], pause: float) -> None:
    fake, clock = Wikipedia({"Congee": THUMB, "Garlic": THUMB}), Clock()
    fake.refuse("summary", *answers)
    cache = cache_on(tmp_path, fake, clock)
    with pytest.raises(ImageError) as exc:
        wiki(cache, "Congee")
    assert str(exc.value).endswith(f"; not asked again for {pause:.0f} s")
    asked = len(fake.requests)
    assert asked == len(answers)                   # only a 429 with no Retry-After is retried
    clock.now += pause - 0.5                       # in the pause, any lookup is a 502 at once
    with pytest.raises(ImageError) as exc:
        ingredient(cache, "Garlic")
    assert exc.value.status == 502 and str(exc.value).endswith("not asked again for 1 s")
    assert len(fake.requests) == asked
    clock.now += 1                                 # after it, Wikipedia is asked again
    assert ingredient(cache, "Garlic") is not None


def test_each_endpoint_is_left_alone_on_its_own(tmp_path: Path) -> None:
    fake = Wikipedia({"Congee": THUMB, "Garlic": THUMB, "Penne alla vodka": THUMB,
                      "Basil (herb)": THUMB},
                     hits={"penne food": "Penne alla vodka", "basil food": "Basil (herb)"})
    clock = Clock()
    fake.refuse("search", refused(429, {"retry-after": "30"}))
    cache = cache_on(tmp_path, fake, clock)
    with pytest.raises(ImageError) as exc:
        ingredient(cache, "Penne")                 # no article of its own; the search refuses
    assert str(exc.value) == ("Wikipedia's search answered 429 (too many requests); "
                              "not asked again for 30 s")
    # while the search is left alone, a picture the page summary finds is still found
    assert wiki(cache, "Congee") is not None and ingredient(cache, "Garlic") is not None
    searched = len(fake.asked("search"))
    with pytest.raises(ImageError) as exc:         # one that needs the search: a 502 at once
        ingredient(cache, "Basil")
    assert str(exc.value).endswith("not asked again for 30 s")
    assert len(fake.asked("search")) == searched
    clock.now += 30
    assert ingredient(cache, "Basil") is not None and ingredient(cache, "Penne") is not None


@pytest.mark.parametrize("headers,seconds", [
    ({"retry-after": "47"}, 47.0), ({"retry-after": " 2 "}, 2.0), ({}, None),
    ({"retry-after": "soon"}, None),
    ({"retry-after": "Thu, 12 Nov 2026 19:30:30 GMT", "date": "Thu, 12 Nov 2026 19:30:00 GMT"},
     30.0),
    ({"retry-after": "Thu, 12 Nov 2026 19:29:00 GMT", "date": "Thu, 12 Nov 2026 19:30:00 GMT"},
     0.0),
    ({"retry-after": "Thu, 01 Jan 1970 00:00:10 GMT"}, 10.0),
])
def test_how_long_a_429_asks_to_wait(headers: dict[str, str], seconds: float | None) -> None:
    assert images.retry_after(httpx.Headers(headers), now=0.0) == seconds


def test_the_same_picture_asked_for_at_once_is_looked_up_once(tmp_path: Path) -> None:
    fake = Wikipedia({"Garlic": THUMB})
    fake.delay["summary"] = 0.01
    cache = cache_on(tmp_path, fake)

    async def at_once(*lookups: Any) -> list[Any]:
        return await asyncio.gather(*lookups, return_exceptions=True)

    found = asyncio.run(at_once(cache.ingredient("Fresh Garlic"), cache.ingredient("garlic bulb"),
                                cache.ingredient("GARLIC")))
    assert found[0] is not None and found == [found[0]] * 3
    assert [endpoint(r) for r in fake.requests] == ["summary", UPLOAD]   # one lookup, one download
    # its error is shared the same way, and nothing is kept
    fake.down = True
    failed = asyncio.run(at_once(cache.wiki("Congee"), cache.wiki("Congee")))
    assert [type(e) for e in failed] == [ImageError] * 2 and len(fake.requests) == 3
    assert "wiki:Congee" not in index(tmp_path)


def test_each_endpoint_is_asked_one_request_at_a_time_and_thumbnails_two_at_once(
        tmp_path: Path) -> None:
    words = ["Garlic", "Penne", "Basil", "Ginger", "Butter", "Turmeric"]
    searched = ["Basil", "Ginger", "Butter"]       # no article of their own: found by search
    thumbs: dict[str, str | None] = {w: f"https://{UPLOAD}/{w}.jpg" for w in words
                                     if w not in searched}
    thumbs |= {f"{w} (food)": f"https://{UPLOAD}/{w}.jpg" for w in searched}
    thumbs |= {t: f"https://{UPLOAD}/{t}.jpg" for t in ("Congee", "Biryani")}
    fake = Wikipedia(thumbs, hits={f"{w.lower()} food": f"{w} (food)" for w in searched})
    fake.delay["summary"] = fake.delay["search"] = 0.001
    cache = cache_on(tmp_path, fake)

    async def page() -> list[Any]:
        fake.hold = asyncio.Event()                # thumbnails wait until every lookup has asked
        lookups = asyncio.gather(*(cache.ingredient(w) for w in words), cache.wiki("Congee"),
                                 cache.wiki("Biryani"))
        async with asyncio.timeout(5):
            while (len(fake.asked("summary")) < 8 or len(fake.asked("search")) < 3
                   or fake.busy["summary"] or fake.busy["search"]):
                await asyncio.sleep(0.001)
        fake.hold.set()
        return await lookups

    assert all(asyncio.run(page()))
    assert fake.most["summary"] == fake.most["search"] == 1    # ingredient() and wiki() alike
    assert fake.most[UPLOAD] == images.MAX_THUMBNAILS == 2
    assert (len(fake.asked("summary")), len(fake.asked("search")), len(fake.requests)) == (8, 3, 19)


def test_the_ingredient_route_is_a_502_while_wikipedia_refuses_then_the_picture(
        tmp_path: Path) -> None:
    fake, clock = Wikipedia({"Garlic": THUMB}), Clock()
    client = console_client(make_app(tmp_path, fake, clock))
    fake.refuse("summary", refused(429, {"retry-after": "20"}))
    r = client.get("/hub/images/ingredient", params={"name": "Fresh Garlic"})
    assert r.status_code == 502
    assert r.json()["detail"] == ("Wikipedia's page summary answered 429 (too many requests); "
                                  "not asked again for 20 s")
    r = client.get("/hub/images/ingredient", params={"name": "Garlic"})
    assert r.status_code == 502 and r.json()["detail"].endswith("not asked again for 20 s")
    assert len(fake.requests) == 1                 # 20 s is too long to retry: a pause
    clock.now += 20
    r = client.get("/hub/images/ingredient", params={"name": "Fresh Garlic"})
    assert r.status_code == 200 and r.content == JPEG
    assert r.headers["cache-control"] == "public, max-age=604800"
    assert index(tmp_path / "images")["ingredient:garlic"]["title"] == "Garlic"
