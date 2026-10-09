"""Fakes for the recipe-import tests: the internet as the hub's import sees it (pages by host and
path, a resolver that can rebind, the socket's peer address), pantry's parse-lines, YouTube's
oEmbed and Data API, Gemini, and a small stand-in for the skill's extractor.

The real extractor lives in pantry-api (skills/recipe-shopper/scripts/extract_recipe.py), which
the hub-tests job checks out at the pinned submodule; ``REAL_EXTRACTOR`` is used where it is
there and new enough, and the tests that need it skip with the reason where it is not.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from demo_hub.recipe_import import Importer
from demo_hub.recipe_import.usage import VideoUsage
from demo_hub.settings import Settings

PUBLIC = "93.184.216.34"
PUBLIC_2 = "151.101.1.67"
REAL_EXTRACTOR = (Path(__file__).resolve().parents[2] / "pantry-api" / "skills"
                  / "recipe-shopper" / "scripts" / "extract_recipe.py")


def real_extractor_reason() -> str:
    """Why the tests on the real extractor cannot run here ("" when they can): the hub needs
    the version that says which markup held the recipe (__version__ 1.0.0 and later)."""
    if not REAL_EXTRACTOR.is_file():
        return "pantry-api's skills/recipe-shopper/scripts/extract_recipe.py is not checked out"
    if "__version__" not in REAL_EXTRACTOR.read_text(encoding="utf-8"):
        return "the checked-out extract_recipe.py predates 1.0.0 (no method, no __version__)"
    return ""

FAKE_EXTRACTOR = '''\
"""A stand-in for extract_recipe.py: the names the hub reads, JSON-LD in plain script tags."""
import json
import re
import threading

__version__ = "{version}"
MAX_BYTES = {max_bytes}
TIMEOUT_S = {timeout_s}
CALLS = []


def clean(value):
    return " ".join(str(value or "").split())


class _PageParser:
    def __init__(self):
        self.jsonld, self.title, self._parts = [], "", []

    def feed(self, page):
        self._parts.append(page)

    def close(self):
        page = "".join(self._parts)
        CALLS.append(threading.current_thread().name)
        {slow}
        self.jsonld = re.findall(r'<script type="application/ld\\+json">(.*?)</script>', page, re.S)
        m = re.search(r"<title>(.*?)</title>", page, re.S)
        self.title = m.group(1) if m else ""


def recipes_from_jsonld(blocks):
    out = []
    for block in blocks:
        node = json.loads(block)
        if node.get("@type") == "Recipe":
            out.append({{"name": node.get("name", ""), "yield": str(node.get("recipeYield", "")),
                        "ingredients": list(node.get("recipeIngredient", []))}})
    return out


def recipes_from_microdata(parser):
    return []


def extract(page):
    parser = _PageParser()
    parser.feed(page)
    parser.close()
    for recipe in recipes_from_jsonld(parser.jsonld):
        if recipe["ingredients"]:
            return {{**recipe, "name": recipe["name"] or clean(parser.title), "method": "jsonld"}}
    return None
'''


def fake_extractor(directory: Path, *, max_bytes: int = 5 * 1024 * 1024,
                   timeout_s: float = 20, slow_s: float = 0, version: str = "1.0.0") -> Path:
    path = directory / "extract_recipe.py"
    slow = f"import time; time.sleep({slow_s})" if slow_s else "pass"
    path.write_text(FAKE_EXTRACTOR.format(max_bytes=max_bytes, timeout_s=timeout_s, slow=slow,
                                          version=version), encoding="utf-8")
    return path


def recipe_page(name: str, lines: list[str], recipe_yield: str = "4 servings",
                pad: int = 0) -> str:
    node = {"@context": "https://schema.org", "@type": "Recipe", "name": name,
            "recipeYield": recipe_yield, "recipeIngredient": lines,
            "recipeInstructions": [{"@type": "HowToStep", "text": "Cook it all."}]}
    filler = "<p>" + "Lorem ipsum dolor sit amet. " * (pad // 28 + 1) + "</p>" if pad else ""
    return (f"<html><head><title>{name} | A Blog</title></head><body>{filler}"
            f'<script type="application/ld+json">{json.dumps(node)}</script></body></html>')


class Peer:
    """The socket under a response: what the peer check reads."""

    def __init__(self, ip: str) -> None:
        self.ip = ip

    def get_extra_info(self, key: str) -> Any:
        return (self.ip, 443) if key == "server_addr" else None


UNIT = r"(g|kg|ml|l|tbsp|tsp|cups?|cloves?|cans?|lb|oz)"
LINE = re.compile(rf"^(\d+(?:\.\d+)?)\s*{UNIT}?\s+(.+)$", re.IGNORECASE)


def parse_line(text: str) -> dict[str, Any]:
    """pantry's lineparse in miniature: "400 g spaghetti, broken" -> 400, g, spaghetti, note."""
    body = text.strip().lstrip("-*• ").strip()
    head, _, note = body.partition(",")
    m = LINE.match(head)
    if m:
        unit = (m.group(2) or "").lower()
        return {"quantity": float(m.group(1)), "unit": unit or "each",
                "name": m.group(3).strip(), "note": note.strip()}
    return {"quantity": None, "unit": "", "name": head.strip(), "note": note.strip()}


class Net:
    """Every host the import talks to. Pages are served by (Host header, path); a pinned request
    goes to the IP in its URL with the name in Host, as the hub sends it."""

    def __init__(self) -> None:
        self.pages: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {}
        self.dns: dict[str, list[list[str]]] = {}
        self.resolved: list[str] = []
        self.requests: list[httpx.Request] = []
        self.peer: dict[str, str] = {}               # host -> the peer the socket reports
        self.parsed: list[dict[str, Any]] = []       # parse-lines bodies
        self.youtube: dict[str, dict[str, Any]] = {}  # video id -> videos.list item
        self.youtube_status = 200                    # 403: a refused key or a spent quota
        self.gemini: list[httpx.Request] = []
        self.gemini_answer: Callable[[httpx.Request], httpx.Response] | None = None

    # --- set-up ----------------------------------------------------------------------------------

    def serve(self, url: str, body: str | bytes = "", status: int = 200,
              headers: dict[str, str] | None = None, ip: str = PUBLIC,
              content_type: str = "text/html; charset=utf-8") -> None:
        u = httpx.URL(url)
        self.dns.setdefault(u.host, [[ip]])
        content = body.encode() if isinstance(body, str) else body
        hdrs = {"content-type": content_type, **(headers or {})}
        self.pages[(u.host, u.path)] = lambda _r: httpx.Response(status, content=content,
                                                                 headers=hdrs)

    def video(self, vid: str, title: str = "Weeknight Dal", channel: str = "Home Cook",
              description: str = "", duration: str = "PT12M30S", oembed: int = 200) -> None:
        body = json.dumps({"title": title, "author_name": channel,
                           "author_url": "https://www.youtube.com/@homecook",
                           "thumbnail_url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"})
        self.dns.setdefault("www.youtube.com", [[PUBLIC_2]])
        self.pages[("www.youtube.com", "/oembed")] = (
            lambda r: httpx.Response(oembed, content=body.encode() if oembed == 200
                                     else b"Not Found",
                                     headers={"content-type": "application/json"}))
        self.youtube[vid] = {"snippet": {"title": title, "channelTitle": channel,
                                         "description": description},
                             "contentDetails": {"duration": duration}}

    # --- the network -----------------------------------------------------------------------------

    async def resolve(self, host: str, port: int) -> list[str]:
        self.resolved.append(host)
        answers = self.dns.get(host)
        if not answers:
            raise OSError(f"no such host {host}")
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.headers.get("host", request.url.host).split(":")[0]
        path = request.url.path
        if host == "pantry.test" and path == "/recipes/parse-lines":
            return self._parse_lines(request)
        if host == "www.googleapis.com" and path == "/youtube/v3/videos":
            if self.youtube_status != 200:
                return httpx.Response(self.youtube_status, json={"error": {
                    "code": self.youtube_status, "message": "The request cannot be completed "
                    "because you have exceeded your quota."}})
            vid = request.url.params.get("id")
            items = [self.youtube[vid]] if vid in self.youtube else []
            return httpx.Response(200, json={"items": items})
        if host == "generativelanguage.googleapis.com":
            self.gemini.append(request)
            assert self.gemini_answer is not None, "Gemini was called"
            return self.gemini_answer(request)
        serve = self.pages.get((host, path))
        if serve is None:
            response = httpx.Response(404, content=b"not here")
        else:
            response = serve(request)
        # the socket's peer: the address the request was sent to, unless a test says otherwise
        peer = self.peer.get(host, request.url.host)
        response.extensions["network_stream"] = Peer(peer)
        return response

    def _parse_lines(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.parsed.append(body)
        basis = "stated_by_source" if body.get("origin") == "page" else "parsed_from_your_paste"
        lines, warnings = [], []
        for text in body["lines"]:
            if not text.strip().lstrip("-*• ").strip():
                continue
            p = parse_line(text)
            lines.append({"line_no": len(lines) + 1, "text": text.strip(), **p,
                          "amount_basis": basis})
            if p["quantity"] is None:
                warnings.append(f"line {len(lines)} ({p['name']}) states no amount")
        m = re.search(r"(\d+)", f"{body.get('yield_text') or ''}")
        servings = int(m.group(1)) if m and re.search(r"serv|^\s*\d+\s*$",
                                                      body.get("yield_text") or "") else None
        if servings is None:
            warnings.insert(0, "servings not stated")
        return httpx.Response(200, json={"servings": servings,
                                         "servings_stated": servings is not None,
                                         "lines": lines, "warnings": warnings})


def make_importer(tmp_path: Path, net: Net, extractor: Path | None = None,
                  **settings: Any) -> Importer:
    s = Settings(pantry_api_url="http://pantry.test",
                 recipe_extractor=str(extractor or fake_extractor(tmp_path)),
                 usage_dir=str(tmp_path / "usage"), **settings)
    return Importer(s, transport=httpx.MockTransport(net.handler), resolver=net.resolve,
                    usage=VideoUsage(tmp_path / "usage", s.video_import_daily_seconds))
