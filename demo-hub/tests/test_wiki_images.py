"""Pictures from Wikipedia (images.py): a demo recipe's, the lead image of the article it names
(``GET /hub/images/wiki?title=``, ``wiki``), and an ingredient's, its words looked up as a title
and then by search (``GET /hub/images/ingredient?name=``, ``ingredient``), through the image cache
with the same guards and User-Agent. A miss is cached only when Wikipedia answered and had
nothing; a Wikipedia that does not answer is a 502 and is asked again later. Wikipedia is asked as
its etiquette asks: one API request at a time, one lookup per picture, a 429 retried once, then a
pause. Against a fake Wikipedia on a mock transport; never the real one."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from demo_hub import app as app_module
from demo_hub import images
from demo_hub.images import ImageCache, ImageError
from demo_hub.settings import Settings
from tests.conftest import console_client

API, UPLOAD = "en.wikipedia.org", "upload.wikimedia.org"
THUMB = "https://upload.wikimedia.org/wikipedia/commons/thumb/5/58/Congee.jpg/330px-Congee.jpg"
JPEG = b"\xff\xd8congee"

Answer = Callable[[httpx.Request], httpx.Response]


def served(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})


def refused(status: int, headers: dict[str, str] | None = None) -> Answer:
    return lambda request: httpx.Response(status, text="refused", headers=headers)


def raises(error: type[httpx.TransportError]) -> Answer:
    def answer(request: httpx.Request) -> httpx.Response:
        raise error("no answer", request=request)
    return answer


class Wikipedia:
    """The API's answer per title (a thumbnail URL, or None for an article with no lead image)
    and per search (the words searched -> the title of the hit), and every request the hub sends:
    to the API and for the thumbnail. ``refuse`` queues answers the API gives first, in order
    (None: its usual answer); ``thumbnail`` is the thumbnail's answer. A host in ``delay``
    answers that many seconds late, ``hold`` keeps thumbnails waiting until it is set, and
    ``most`` is the most requests a host had at once."""

    def __init__(self, thumbs: dict[str, str | None] | None = None,
                 hits: dict[str, str] | None = None) -> None:
        self.thumbs = {"Congee": THUMB} if thumbs is None else thumbs
        self.hits = hits or {}
        self.requests: list[httpx.Request] = []
        self.down = False
        self.refusals: list[Answer | None] = []
        self.thumbnail: Answer = served
        self.delay: dict[str, float] = {}
        self.hold: asyncio.Event | None = None
        self.busy: Counter[str] = Counter()
        self.most: Counter[str] = Counter()

    def refuse(self, *answers: Answer | None) -> None:
        self.refusals.extend(answers)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        self.busy[host] += 1
        self.most[host] = max(self.most[host], self.busy[host])
        try:
            if host in self.delay:
                await asyncio.sleep(self.delay[host])
            if self.hold is not None and host != API:
                await self.hold.wait()
            return self.answer(request)
        finally:
            self.busy[host] -= 1

    def answer(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("no route", request=request)
        if request.url.host != API:
            return self.thumbnail(request)
        refusal = self.refusals.pop(0) if self.refusals else None
        if refusal is not None:
            return refusal(request)
        params = request.url.params
        if "gsrsearch" in params:
            title = self.hits.get(params["gsrsearch"])
            if title is None:                  # no hit: the answer has no "query" at all
                return httpx.Response(200, json={"batchcomplete": True})
        else:
            title = params["titles"]
        if title not in self.thumbs:
            return httpx.Response(200, json={"query": {"pages": [
                {"title": title, "missing": True}]}})
        page: dict[str, Any] = {"pageid": 1, "title": title}
        if self.thumbs[title]:
            page["thumbnail"] = {"source": self.thumbs[title], "width": 330, "height": 220}
        return httpx.Response(200, json={"query": {"pages": [page]}})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def api(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == API]


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


def cache_on(folder: Path, fake: Wikipedia, clock: Clock | None = None) -> ImageCache:
    clock = clock or Clock()
    return ImageCache(folder, fake.client(), check_host=public, sleep=clock.sleep, clock=clock)


def wiki(cache: ImageCache, title: str) -> tuple[Path, str] | None:
    return asyncio.run(cache.wiki(title))


def ingredient(cache: ImageCache, name: str) -> tuple[Path, str] | None:
    return asyncio.run(cache.ingredient(name))


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


def test_the_articles_lead_image_is_fetched_once_at_320_px(tmp_path: Path) -> None:
    fake = Wikipedia()
    checked: list[str] = []

    async def guard(host: str) -> None:
        checked.append(host)

    first = wiki(ImageCache(tmp_path, fake.client(), check_host=guard), "Congee")
    assert first is not None and first[1] == "image/jpeg" and first[0].read_bytes() == JPEG
    api, thumb = fake.requests
    assert dict(api.url.params) == {
        "action": "query", "format": "json", "formatversion": "2", "prop": "pageimages",
        "piprop": "thumbnail", "pithumbsize": "320", "redirects": "1", "titles": "Congee"}
    # the same User-Agent as an ingredient's, on both requests, and the thumbnail's host checked
    assert {r.headers["user-agent"] for r in (api, thumb)} == {images.USER_AGENT}
    assert str(thumb.url) == THUMB and checked == ["upload.wikimedia.org"]
    # a second look, even from a new cache on the same folder, asks nothing
    again = wiki(ImageCache(tmp_path, Wikipedia({}).client(), check_host=public), " Congee ")
    assert again == first and len(fake.requests) == 2


def test_only_the_named_article_is_asked_never_a_search(tmp_path: Path) -> None:
    fake = Wikipedia()
    wiki(ImageCache(tmp_path, fake.client(), check_host=public), "Pa amb tomàquet")
    [api] = fake.requests
    assert api.url.params["titles"] == "Pa amb tomàquet"
    assert "generator" not in api.url.params and "gsrsearch" not in api.url.params


@pytest.mark.parametrize("thumbs,reason", [({"Congee": None}, "no-image"), ({}, "no-article")],
                         ids=["no lead image", "no article"])
def test_an_article_without_a_lead_image_is_a_miss_and_the_miss_is_cached(
        tmp_path: Path, thumbs: dict[str, str | None], reason: str) -> None:
    fake = Wikipedia(thumbs)
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

    def busy(request: httpx.Request) -> httpx.Response:
        if request.url.host == "en.wikipedia.org":
            return httpx.Response(200, json={"query": {"pages": [
                {"title": "Congee", "thumbnail": {"source": THUMB}}]}})
        return httpx.Response(503)

    with pytest.raises(ImageError) as exc:
        wiki(ImageCache(tmp_path, httpx.AsyncClient(transport=httpx.MockTransport(busy)),
                        check_host=public), "Congee")
    assert exc.value.status == 502
    fake.down = False
    found = wiki(ImageCache(tmp_path, fake.client(), check_host=public), "Congee")
    assert found is not None and found[0].read_bytes() == JPEG


def test_the_thumbnail_is_guarded_like_any_other_image(tmp_path: Path) -> None:
    fake = Wikipedia({"Congee": "http://10.0.0.1/congee.jpg"})

    async def guard(host: str) -> None:
        if host.startswith("10."):
            raise ImageError(f"{host} is not a public address", 403)

    with pytest.raises(ImageError) as exc:
        wiki(ImageCache(tmp_path, fake.client(), check_host=guard), "Congee")
    assert exc.value.status == 403


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


# --- an ingredient's picture -------------------------------------------------------------------


def test_an_ingredient_is_looked_up_by_its_words_then_by_search(tmp_path: Path) -> None:
    fake = Wikipedia({"Penne": None, "Penne alla vodka": THUMB},
                     hits={"penne food": "Penne alla vodka"})
    found = ingredient(cache_on(tmp_path, fake), "Penne Rigate 500g")
    assert found is not None and found[0].read_bytes() == JPEG
    title, search, thumb = fake.requests
    assert (title.url.params["titles"], title.url.params["pithumbsize"]) == ("Penne", "240")
    assert (search.url.params["generator"], search.url.params["gsrsearch"],
            search.url.params["gsrlimit"]) == ("search", "penne food", "1")
    assert str(thumb.url) == THUMB
    assert index(tmp_path)["ingredient:penne"]["title"] == "Penne alla vodka"


@pytest.mark.parametrize("thumbs,reason,title", [
    ({}, "no-article", None), ({"Garlic": None}, "no-image", "Garlic")],
    ids=["no article and no search hit", "an article without a lead image"])
def test_a_definite_miss_is_cached_and_says_why(
        tmp_path: Path, thumbs: dict[str, str | None], reason: str, title: str | None) -> None:
    fake = Wikipedia(thumbs)
    assert ingredient(cache_on(tmp_path, fake), "Fresh Garlic") is None
    asked = [r.url.params.get("titles") or r.url.params["gsrsearch"] for r in fake.requests]
    assert asked == ["Garlic", "garlic food"]
    entry = index(tmp_path)["ingredient:garlic"]
    assert entry["file"] is None and entry["answered"] is True
    assert (entry["reason"], entry["title"]) == (reason, title)
    later = Wikipedia({"Garlic": THUMB})
    assert ingredient(cache_on(tmp_path, later), "garlic bulb") is None
    assert later.requests == []


# --- a Wikipedia that does not answer is a 502, never a cached miss ----------------------------


NO_ANSWER: dict[str, list[Answer | None]] = {
    "connect error": [raises(httpx.ConnectError)],
    "timeout": [raises(httpx.ReadTimeout)],
    "429, and 429 again": [refused(429, {"retry-after": "1"})] * 2,
    "503": [refused(503)],
    "403": [refused(403)],
    "not JSON": [lambda request: httpx.Response(200, text="<html>Wikimedia Error</html>")],
    "an API error": [lambda request: httpx.Response(200, json={"error": {
        "code": "internal_api_error_DBQueryError", "info": "a database query error"}})],
}


@pytest.mark.parametrize("lookup", ["ingredient", "wiki"])
@pytest.mark.parametrize("answers", list(NO_ANSWER.values()), ids=list(NO_ANSWER))
def test_a_wikipedia_that_does_not_answer_is_a_502_and_a_later_answer_is_served(
        tmp_path: Path, answers: list[Answer | None], lookup: str) -> None:
    fake, clock = Wikipedia({"Garlic": THUMB}), Clock()
    fake.refuse(*answers)
    cache = cache_on(tmp_path, fake, clock)

    def look() -> tuple[Path, str] | None:
        return ingredient(cache, "Fresh Garlic") if lookup == "ingredient" else wiki(cache, "Garlic")

    with pytest.raises(ImageError) as exc:
        look()
    assert exc.value.status == 502 and index(tmp_path) == {}
    clock.now += images.MAX_PAUSE_S                # past the pause: asked again, and found
    found = look()
    assert found is not None and found[0].read_bytes() == JPEG
    assert len(fake.api()) == len(answers) + 1


def test_an_ingredient_is_a_miss_only_when_its_search_was_answered_too(tmp_path: Path) -> None:
    fake, clock = Wikipedia({"Garlic": None}), Clock()
    fake.refuse(None, refused(503))           # the title answered (no lead image), the search not
    cache = cache_on(tmp_path, fake, clock)
    with pytest.raises(ImageError) as exc:
        ingredient(cache, "Fresh Garlic")
    assert exc.value.status == 502 and index(tmp_path) == {}
    clock.now += images.PAUSE_S
    assert ingredient(cache, "Fresh Garlic") is None
    assert index(tmp_path)["ingredient:garlic"]["reason"] == "no-image"


@pytest.mark.parametrize("answer", [
    refused(429), refused(503), refused(403), raises(httpx.ConnectError),
    lambda request: httpx.Response(200, text="<html>", headers={"content-type": "text/html"})],
    ids=["429", "503", "403", "connect error", "not an image"])
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


def test_a_thumbnail_host_that_does_not_resolve_is_a_502(tmp_path: Path) -> None:
    async def unresolved(host: str) -> None:
        raise ImageError(f"cannot resolve {host}", 404)

    fake = Wikipedia({"Garlic": THUMB})
    with pytest.raises(ImageError) as exc:
        asyncio.run(ImageCache(tmp_path, fake.client(), check_host=unresolved).ingredient("Garlic"))
    assert exc.value.status == 502 and index(tmp_path) == {}


def test_a_miss_older_code_kept_or_one_over_7_days_old_is_looked_up_again(tmp_path: Path) -> None:
    def miss(days: float, **about: Any) -> dict[str, Any]:
        at = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        return {"file": None, "type": "image/jpeg", "at": at, "source": None, **about}

    old = "https://example.org/old.png"
    (tmp_path / "index.json").write_text(json.dumps({
        # older code kept a Wikipedia that did not answer as a miss too, and did not say so
        "ingredient:garlic": miss(0, query="garlic", title=None),
        "wiki:Congee": miss(8, title="Congee", answered=True, reason="no-image"),
        "ingredient:penne": miss(6, query="penne", title=None, answered=True,
                                 reason="no-article"),
        "url:https://example.org/new.png": miss(1),
        f"url:{old}": miss(8),
    }), encoding="utf-8")
    fake = Wikipedia({"Garlic": THUMB, "Congee": THUMB, "Penne": THUMB})
    cache = cache_on(tmp_path, fake)
    assert ingredient(cache, "Fresh Garlic") is not None
    assert wiki(cache, "Congee") is not None
    assert ingredient(cache, "Penne Rigate 500g") is None        # 6 days old: still believed
    assert asyncio.run(cache.remote("https://example.org/new.png")) is None
    assert asyncio.run(cache.remote(old)) is not None            # a page's image, 8 days old
    asked = [r.url.params.get("titles") or str(r.url) for r in fake.requests]
    assert asked == ["Garlic", THUMB, "Congee", THUMB, old]
    kept = index(tmp_path)
    assert kept["ingredient:garlic"]["file"] and kept["wiki:Congee"]["file"]
    assert kept["ingredient:penne"]["reason"] == "no-article"


# --- asking Wikipedia politely -----------------------------------------------------------------


@pytest.mark.parametrize("headers,waited", [
    ({"retry-after": "2"}, 2.0), ({"retry-after": "0"}, 0.0), ({"retry-after": "47"}, 1.0),
    ({}, 1.0)], ids=["Retry-After 2", "Retry-After 0", "Retry-After 47: too long", "none"])
def test_a_429_is_retried_once_after_its_retry_after_when_that_is_short_else_after_1_s(
        tmp_path: Path, headers: dict[str, str], waited: float) -> None:
    fake, clock = Wikipedia(), Clock()
    fake.refuse(refused(429, headers))
    found = wiki(cache_on(tmp_path, fake, clock), "Congee")
    assert found is not None and found[0].read_bytes() == JPEG
    first, retry, thumb = fake.requests
    assert first.url == retry.url and thumb.url.host == UPLOAD
    assert clock.sleeps == [waited]


@pytest.mark.parametrize("answers,pause", [
    ([refused(429, {"retry-after": "30"})] * 2, 30.0),
    ([refused(429, {"retry-after": "3600"})] * 2, images.MAX_PAUSE_S),
    ([refused(429)] * 2, images.PAUSE_S),
    ([refused(503)], images.PAUSE_S),
    ([raises(httpx.ConnectTimeout)], images.PAUSE_S)],
    ids=["429 for 30 s", "429 for an hour", "429", "503", "timeout"])
def test_after_no_answer_wikipedia_is_left_alone_for_a_while(
        tmp_path: Path, answers: list[Answer | None], pause: float) -> None:
    fake, clock = Wikipedia({"Congee": THUMB, "Garlic": THUMB}), Clock()
    fake.refuse(*answers)
    cache = cache_on(tmp_path, fake, clock)
    with pytest.raises(ImageError):
        wiki(cache, "Congee")
    asked = len(fake.requests)
    assert asked == len(answers)                   # a 429 is retried once, nothing else is
    clock.now += pause - 0.5                       # in the pause, any lookup is a 502 at once
    with pytest.raises(ImageError) as exc:
        ingredient(cache, "Garlic")
    assert exc.value.status == 502 and str(exc.value).endswith("not asked again for 1 s")
    assert len(fake.requests) == asked
    clock.now += 1                                 # after it, Wikipedia is asked again
    assert ingredient(cache, "Garlic") is not None


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
    fake.delay[API] = 0.01
    cache = cache_on(tmp_path, fake)

    async def at_once(*lookups: Any) -> list[Any]:
        return await asyncio.gather(*lookups, return_exceptions=True)

    found = asyncio.run(at_once(cache.ingredient("Fresh Garlic"), cache.ingredient("garlic bulb"),
                                cache.ingredient("GARLIC")))
    assert found[0] is not None and found == [found[0]] * 3
    assert [r.url.host for r in fake.requests] == [API, UPLOAD]     # one lookup, one download
    # its error is shared the same way, and nothing is kept
    fake.down = True
    failed = asyncio.run(at_once(cache.wiki("Congee"), cache.wiki("Congee")))
    assert [type(e) for e in failed] == [ImageError] * 2 and len(fake.requests) == 3
    assert "wiki:Congee" not in index(tmp_path)


def test_wikipedia_is_asked_one_request_at_a_time_and_thumbnails_two_at_once(
        tmp_path: Path) -> None:
    words = ["Garlic", "Penne", "Basil", "Ginger", "Butter", "Turmeric"]
    fake = Wikipedia({w: f"https://{UPLOAD}/{w}.jpg" for w in [*words, "Congee", "Biryani"]})
    fake.delay[API] = 0.001
    cache = cache_on(tmp_path, fake)

    async def page() -> list[Any]:
        fake.hold = asyncio.Event()                # thumbnails wait until every lookup has asked
        lookups = asyncio.gather(*(cache.ingredient(w) for w in words), cache.wiki("Congee"),
                                 cache.wiki("Biryani"))
        async with asyncio.timeout(5):
            while len(fake.api()) < 8 or fake.busy[API]:
                await asyncio.sleep(0.001)
        fake.hold.set()
        return await lookups

    assert all(asyncio.run(page()))
    assert fake.most[API] == 1                     # ingredient() and wiki() alike
    assert fake.most[UPLOAD] == images.MAX_THUMBNAILS == 2
    assert len(fake.api()) == 8 and len(fake.requests) == 16


def test_the_ingredient_route_is_a_502_while_wikipedia_refuses_then_the_picture(
        tmp_path: Path) -> None:
    fake, clock = Wikipedia({"Garlic": THUMB}), Clock()
    client = console_client(make_app(tmp_path, fake, clock))
    fake.refuse(*[refused(429, {"retry-after": "20"})] * 2)
    r = client.get("/hub/images/ingredient", params={"name": "Fresh Garlic"})
    assert r.status_code == 502
    assert r.json()["detail"] == "Wikipedia answered 429 (too many requests)"
    assert client.get("/hub/images/ingredient", params={"name": "Garlic"}).status_code == 502
    assert len(fake.requests) == 2                 # the try and its retry; then a pause
    clock.now += 20
    r = client.get("/hub/images/ingredient", params={"name": "Fresh Garlic"})
    assert r.status_code == 200 and r.content == JPEG
    assert r.headers["cache-control"] == "public, max-age=604800"
    assert index(tmp_path / "images")["ingredient:garlic"]["title"] == "Garlic"
