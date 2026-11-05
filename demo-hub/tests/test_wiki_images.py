"""``GET /hub/images/wiki?title=`` (images.py ``wiki``): a demo recipe's picture, the lead image of
the Wikipedia article it names, through the image cache with the same guards and User-Agent as an
ingredient's. Against a fake Wikipedia on a mock transport; never the real one."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from demo_hub import app as app_module
from demo_hub import images
from demo_hub.images import ImageCache, ImageError
from demo_hub.settings import Settings
from tests.conftest import console_client

THUMB = "https://upload.wikimedia.org/wikipedia/commons/thumb/5/58/Congee.jpg/330px-Congee.jpg"
JPEG = b"\xff\xd8congee"


class Wikipedia:
    """The API's answer per title (a thumbnail URL, or None for an article with no lead image),
    and every request the hub sends: to the API and for the thumbnail."""

    def __init__(self, thumbs: dict[str, str | None] | None = None) -> None:
        self.thumbs = {"Congee": THUMB} if thumbs is None else thumbs
        self.requests: list[httpx.Request] = []
        self.down = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("no route", request=request)
        if request.url.host == "en.wikipedia.org":
            title = request.url.params["titles"]
            if title not in self.thumbs:
                return httpx.Response(200, json={"query": {"pages": [
                    {"title": title, "missing": True}]}})
            page: dict[str, Any] = {"pageid": 1, "title": title}
            if self.thumbs[title]:
                page["thumbnail"] = {"source": self.thumbs[title], "width": 330, "height": 220}
            return httpx.Response(200, json={"query": {"pages": [page]}})
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


async def public(host: str) -> None:
    return None


def wiki(cache: ImageCache, title: str) -> tuple[Path, str] | None:
    return asyncio.run(cache.wiki(title))


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


@pytest.mark.parametrize("thumbs", [{"Congee": None}, {}], ids=["no lead image", "no article"])
def test_an_article_without_a_lead_image_is_a_miss_and_the_miss_is_cached(
        tmp_path: Path, thumbs: dict[str, str | None]) -> None:
    fake = Wikipedia(thumbs)
    assert wiki(ImageCache(tmp_path, fake.client(), check_host=public), "Congee") is None
    assert len(fake.requests) == 1
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
    app = app_module.create_app(Settings(
        traces_dir=str(tmp_path / "traces"), images_dir=str(tmp_path / "images"),
        usage_dir=str(tmp_path / "usage"), calendar_dir=str(tmp_path / "calendar"),
        timings_path=str(tmp_path / "timings.jsonl")))
    fake = Wikipedia({"Congee": THUMB, "Toast": None})
    app.state.images.client, app.state.images.check_host = fake.client(), public
    client = console_client(app)
    r = client.get("/hub/images/wiki", params={"title": "Congee"})
    assert r.status_code == 200 and r.content == JPEG
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "public, max-age=604800"
    assert r.headers["x-content-type-options"] == "nosniff"
    miss = client.get("/hub/images/wiki", params={"title": "Toast"})
    assert miss.status_code == 404 and miss.headers["cache-control"] == "max-age=3600"
    assert client.get("/hub/images/wiki", params={"title": "A|B"}).status_code == 400
    assert client.get("/hub/images/wiki").status_code == 422
