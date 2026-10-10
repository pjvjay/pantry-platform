"""A recipe page read into a RecipeDoc: the schema.org Recipe found by the skill's extractor
(JSON-LD first, then microdata), its ingredient lines read by pantry's ``POST
/recipes/parse-lines`` (no LLM), and the source kept for the link back.

Only ingredient lines and the facts needed to cite the page are kept, with the link to the
page's own preview image (``preview_image``: the Recipe's image, else its og:image), which the
console shows through the hub's guarded image proxy, credited to the site and linked back, as a
link preview does. The method text and the page's prose are never stored or shown, and the
image itself is never stored in the doc (the copyright posture in docs/recipe-import.md).
Extraction runs in a worker thread: ``html.parser`` on a page of several megabytes takes long
enough to stall every other request the hub is serving.

The models below mirror pantry-api's RecipeDoc (``pantry_planner/models.py``, PLAN.md 3.2) with
the same bounds, so a doc the hub builds or receives is one pantry plans as it stands.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from types import ModuleType
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import AfterValidator, BaseModel, Field, ValidationError, model_validator

from demo_hub.recipe_import.fetch import ImportFailure, Page

MAX_DOC_LINES = 60
MAX_LINE_TEXT = 300
MAX_LINE_NAME = 200
MAX_SERVINGS = 100
# pantry's bound on a line's amount (models.MAX_LINE_QUANTITY): a doc the hub holds must be one
# pantry plans, or the shopper's "Plan this now" would fail only at the plan call
MAX_LINE_QUANTITY = 1_000_000
PAGE_TYPES = {"", "text/html", "application/xhtml+xml", "application/xml", "text/xml"}
MAX_URL = 2000
OG_IMAGE = frozenset({"og:image", "og:image:url", "og:image:secure_url"})

AmountBasis = Literal["stated_by_source", "demo_house_amounts", "parsed_from_your_paste",
                      "transcribed_confirmed_by_you", "written_by_assistant"]


class LineEvidence(BaseModel):
    quote: str | None = Field(default=None, max_length=300)
    at: str | None = Field(default=None, pattern=r"^\d{1,3}:\d{2}$")


class RecipeLine(BaseModel):
    line_no: int = Field(ge=1)
    text: str = Field(max_length=MAX_LINE_TEXT)
    name: str = Field(min_length=1, max_length=MAX_LINE_NAME)
    quantity: float | None = Field(default=None, ge=0, le=MAX_LINE_QUANTITY,
                                   allow_inf_nan=False)
    unit: str = Field(default="", max_length=40)
    note: str = Field(default="", max_length=300)
    evidence: LineEvidence | None = None
    confirmed: bool = True
    amount_basis: AmountBasis


def http_url(value: Any) -> str | None:
    """``value`` as a link a RecipeSource holds, as pantry checks it: an absolute http(s) URL
    with a host, at most 2,000 characters, no space or control character; else None."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if (parts.scheme.lower() not in ("http", "https") or not host or len(url) > MAX_URL
            or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url)):
        return None
    return url


def _image_url(value: str) -> str:
    if http_url(value) != value:
        raise ValueError("must be an absolute http(s) URL")
    return value


class RecipeSource(BaseModel):
    kind: Literal["library", "starter", "pasted", "web", "youtube", "assistant"]
    method: Literal["db", "seed", "paste", "jsonld", "microdata", "youtube_description",
                    "youtube_linked_page", "gemini_video", "agent_written"]
    url: str | None = Field(default=None, max_length=2000)
    site: str | None = Field(default=None, max_length=200)
    page_title: str | None = Field(default=None, max_length=300)
    author: str | None = Field(default=None, max_length=200)
    channel: str | None = Field(default=None, max_length=200)
    retrieved_at: str | None = Field(default=None, max_length=40)
    extractor: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=100)
    label: str | None = Field(default=None, max_length=100)
    # the page's own preview image, as a link: never the image
    image_url: Annotated[str, Field(max_length=MAX_URL), AfterValidator(_image_url)] | None = None


class RecipeDoc(BaseModel):
    v: Literal[1] = 1
    key: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    servings: int | None = Field(default=None, ge=1, le=MAX_SERVINGS)
    servings_stated: bool = False
    servings_basis: Literal["source", "your_setting"] | None = None
    yield_text: str = Field(default="", max_length=200)
    lines: list[RecipeLine] = Field(default_factory=list, max_length=MAX_DOC_LINES)
    source: RecipeSource
    warnings: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _numbered_in_order(self) -> RecipeDoc:
        got = [ln.line_no for ln in self.lines]
        if got != list(range(1, len(got) + 1)):
            raise ValueError(f"lines must be numbered 1..{len(got)} in order, got {got}")
        return self


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def site_of(url: str) -> str:
    try:
        host = httpx.URL(url).host
    except httpx.InvalidURL:
        return ""
    return host.removeprefix("www.")


# --- extraction ----------------------------------------------------------------------------------

def extract(extractor: ModuleType, page: str) -> dict[str, Any] | None:
    """The extractor's own ``extract(page)``: {name, yield, ingredients, method: jsonld |
    microdata}, or None when the page has no Recipe with ingredient lines. Only the extractor
    knows which markup it read, so one that does not say (an extract_recipe.py before 1.0.0)
    is refused rather than guessed at. Blocking: call it in a thread."""
    found = extractor.extract(page)
    if found is None:
        return None
    if found.get("method") not in ("jsonld", "microdata"):
        raise ImportFailure(503, "import_unavailable", "This extract_recipe.py does not say "
                            "which markup held the recipe; update pantry-api's skill.")
    return found


def extractor_label(extractor: ModuleType) -> str:
    """source.extractor: the script and its version, so a doc says which reading of the page
    produced its lines."""
    version = getattr(extractor, "__version__", "")
    return f"extract_recipe.py {version}".strip()[:100]


async def read_recipe(extractor: ModuleType, page: Page) -> dict[str, Any]:
    """The page's recipe, extracted in a worker thread; 422 no_recipe_found when it has none
    (the chat then reads the page with the fetch tool, as before)."""
    if page.content_type not in PAGE_TYPES:
        raise ImportFailure(422, "no_recipe_found", f"The link is a {page.content_type} file, "
                            "not a recipe page.")
    found = await asyncio.to_thread(extract, extractor, page.text)
    if found is None:
        raise ImportFailure(422, "no_recipe_found", "The page has no structured recipe "
                            "(schema.org JSON-LD or microdata). Paste the ingredient list "
                            "instead.")
    return found


# --- the page's preview image ---------------------------------------------------------------------

def _is_recipe(types: Any) -> bool:
    """A schema.org type, a list of them or a microdata itemtype, that names Recipe."""
    names = types if isinstance(types, list) else str(types or "").split()
    return any(isinstance(t, str) and re.split(r"[/:#]", t.strip())[-1].lower() == "recipe"
               for t in names)


class _PreviewParser(HTMLParser):
    """What a page says its preview image is: its ld+json blocks, its og:image, and the image of
    a microdata Recipe (an itemprop="image" whose nearest item is the Recipe: a meta's content,
    an img's src or a link's href)."""

    VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
                      "source", "track", "wbr"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.jsonld: list[str] = []
        self.og_image: list[str] = []
        self.microdata: list[str] = []
        self._script: list[str] | None = None
        self._open: list[tuple[str, bool | None]] = []  # (tag, whether its item is a Recipe)

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {k.lower(): (v or "") for k, v in attrs_list}
        if tag == "script":
            kind = attrs.get("type", "").split(";")[0].strip().lower()
            self._script = [] if kind == "application/ld+json" else None
            return
        if tag == "meta" and (attrs.get("property") or attrs.get("name", "")).strip().lower() \
                in OG_IMAGE:
            self.og_image.append(attrs.get("content", ""))
        item = "itemscope" in attrs
        if not item and "image" in attrs.get("itemprop", "").split() and self._in_recipe():
            self.microdata.append(attrs.get("content") or attrs.get("src")
                                  or attrs.get("href", ""))
        if tag not in self.VOID:
            self._open.append((tag, _is_recipe(attrs.get("itemtype")) if item else None))

    def _in_recipe(self) -> bool:
        return next((recipe for _tag, recipe in reversed(self._open) if recipe is not None),
                    False)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            if self._script is not None:
                self.jsonld.append("".join(self._script))
            self._script = None
            return
        for i in range(len(self._open) - 1, -1, -1):
            if self._open[i][0] == tag:
                del self._open[i:]
                return

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)

    def close(self) -> None:
        super().close()
        if self._script is not None:             # an unterminated ld+json block
            self.jsonld.append("".join(self._script))
            self._script = None


def _jsonld_nodes(blocks: list[str]) -> list[dict[str, Any]]:
    """Every object in the page's JSON-LD, in document order (the extractor's tolerance for
    comment and CDATA wrappers)."""
    found: list[dict[str, Any]] = []
    for block in blocks:
        text = re.sub(r"^\s*(<!--|//\s*<!\[CDATA\[|<!\[CDATA\[)", "", block.strip())
        text = re.sub(r"(-->|//\s*\]\]>|\]\]>)\s*$", "", text).strip()
        try:
            stack: list[Any] = [json.loads(text, strict=False)] if text else []
        except (ValueError, RecursionError):
            continue
        while stack:                              # depth first, in document order
            node = stack.pop()
            if isinstance(node, dict):
                found.append(node)
                stack.extend(v for k, v in reversed(node.items()) if k != "@context")
            elif isinstance(node, list):
                stack.extend(reversed(node))
    return found


def _image_values(value: Any, by_id: dict[str, dict[str, Any]]) -> list[Any]:
    """The URLs a schema.org image names, in order: a URL, an ImageObject's url or contentUrl
    (or those of the node its @id names), or a list of these."""
    out = []
    for item in value if isinstance(value, list) else [value]:
        if isinstance(item, dict):
            node = item if ("url" in item or "contentUrl" in item) else by_id.get(
                str(item.get("@id")), {})
            item = node.get("url") or node.get("contentUrl")
        out.append(item)
    return out


def preview_image(page: str, method: str) -> str | None:
    """The link to the page's own preview image: the image of the schema.org Recipe the extractor
    read (``method`` jsonld: the first JSON-LD Recipe with ingredient lines; microdata: a
    microdata Recipe's), else the page's og:image; the first that is an absolute http(s) URL
    (``http_url``), or None. Blocking: call it in a thread."""
    parser = _PreviewParser()
    parser.feed(page)
    parser.close()
    candidates: list[Any] = []
    if method == "jsonld":
        nodes = _jsonld_nodes(parser.jsonld)
        by_id: dict[str, dict[str, Any]] = {}
        for n in nodes:                           # a node's definition, not a {"@id"} pointing at it
            if isinstance(n.get("@id"), str) and set(n) != {"@id"}:
                by_id.setdefault(n["@id"], n)
        recipe = next((n for n in nodes if _is_recipe(n.get("@type"))
                       and n.get("recipeIngredient", n.get("ingredients"))), None)
        if recipe is not None:
            candidates += _image_values(recipe.get("image"), by_id)
    elif method == "microdata":
        candidates += parser.microdata
    for value in [*candidates, *parser.og_image]:
        url = http_url(value)
        if url:
            return url
    return None


# --- lines through pantry's parser ----------------------------------------------------------------

def bounded(lines: list[str]) -> tuple[list[str], list[str]]:
    """At most MAX_DOC_LINES lines of at most MAX_LINE_TEXT characters, as parse-lines and a
    RecipeDoc take them, and the warnings for what was cut."""
    warnings = []
    kept = [ln.strip() for ln in lines if ln and ln.strip()]
    if len(kept) > MAX_DOC_LINES:
        warnings.append(f"{len(kept) - MAX_DOC_LINES} ingredient line(s) past the first "
                        f"{MAX_DOC_LINES} were left out")
        kept = kept[:MAX_DOC_LINES]
    out = []
    for n, ln in enumerate(kept, start=1):
        if len(ln) > MAX_LINE_TEXT:
            warnings.append(f"line {n} was longer than {MAX_LINE_TEXT} characters and was cut")
            ln = ln[:MAX_LINE_TEXT].rstrip()
        out.append(ln)
    return out, warnings


async def parse_lines(pantry_url: str, lines: list[str], *, title: str = "",
                      yield_text: str = "", origin: str = "page",
                      transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """pantry's POST /recipes/parse-lines: {servings, servings_stated, lines, warnings}."""
    body = {"title": title[:200] or None, "yield_text": yield_text[:200] or None,
            "lines": lines, "origin": origin}
    try:
        async with httpx.AsyncClient(base_url=pantry_url, transport=transport, timeout=20,
                                     trust_env=False) as client:
            r = await client.post("/recipes/parse-lines", json=body)
    except httpx.HTTPError as exc:
        raise ImportFailure(502, "pantry_unreachable", "pantry could not read the lines "
                            f"({type(exc).__name__}).") from exc
    if r.status_code != 200:
        raise ImportFailure(502, "pantry_error", f"pantry's parse-lines answered HTTP "
                            f"{r.status_code}.")
    return r.json()


def problem(error: Any) -> str:
    """One pydantic error as a phrase a shopper can find: "line 2 quantity: Input should be
    less than or equal to 1000000" (a doc's lines are numbered from 1, as the sheet shows)."""
    loc = list(error.get("loc") or ())
    if len(loc) >= 2 and loc[0] == "lines" and isinstance(loc[1], int):
        loc = [f"line {loc[1] + 1}", *loc[2:]]
    where = " ".join(str(p) for p in loc) or "value"
    return f"{where}: {error.get('msg', 'not valid')}"


def doc_from_parsed(key: str, title: str, yield_text: str, parsed: dict[str, Any],
                    source: RecipeSource, warnings: list[str],
                    evidence: list[LineEvidence | None] | None = None,
                    amount_basis: AmountBasis | None = None,
                    confirmed: bool = True) -> RecipeDoc:
    """The RecipeDoc for parse-lines' answer. ``evidence``, ``amount_basis`` and ``confirmed``
    override each line's (a video's timestamps, its lines unticked).

    A line pantry read into something no RecipeDoc holds is 502 bad_upstream naming that line
    (a pantry-api without the 1,000,000 quantity bound reads "2000000 g flour" as 2000000.0):
    the hub never changes an amount it was given, and an import never answers 500."""
    lines = []
    for i, ln in enumerate(parsed.get("lines") or []):
        text = str(ln.get("text") or "")[:MAX_LINE_TEXT]
        try:
            lines.append(RecipeLine(
                line_no=int(ln["line_no"]), text=text, name=str(ln["name"])[:MAX_LINE_NAME],
                quantity=ln.get("quantity"), unit=str(ln.get("unit") or ""),
                note=str(ln.get("note") or "")[:300],
                evidence=evidence[i] if evidence and i < len(evidence) else None,
                confirmed=confirmed, amount_basis=amount_basis or ln.get("amount_basis")
                or "stated_by_source"))
        except ValidationError as exc:
            raise ImportFailure(
                502, "bad_upstream", f"pantry read line {i + 1} ({text[:80]}) into something "
                f"a recipe cannot hold ({problem(exc.errors()[0])}). Paste the list with that "
                "line corrected, or update pantry-api.", line_no=i + 1) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise ImportFailure(502, "bad_upstream", "pantry's parse-lines answered line "
                                f"{i + 1} without a name or a line number.",
                                line_no=i + 1) from exc
    servings = parsed.get("servings")
    all_warnings = [*warnings, *(str(w) for w in parsed.get("warnings") or [])]
    try:
        return RecipeDoc(key=key, title=(title or source.site or "Recipe")[:200],
                         servings=servings, servings_stated=servings is not None,
                         servings_basis="source" if servings is not None else None,
                         yield_text=yield_text[:200], lines=lines, source=source,
                         warnings=all_warnings[:20])
    except ValidationError as exc:
        raise ImportFailure(502, "bad_upstream", "pantry's parse-lines answer is not a recipe "
                            f"the hub can hold ({problem(exc.errors()[0])}).") from exc
