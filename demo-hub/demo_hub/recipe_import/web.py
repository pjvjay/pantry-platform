"""A recipe page read into a RecipeDoc: the schema.org Recipe found by the skill's extractor
(JSON-LD first, then microdata), its ingredient lines read by pantry's ``POST
/recipes/parse-lines`` (no LLM), and the source kept for the link back.

Only ingredient lines and the facts needed to cite the page are kept: never the method text,
the page's prose or its images (the copyright posture in docs/recipe-import.md). Extraction
runs in a worker thread: ``html.parser`` on a page of several megabytes takes long enough to
stall every other request the hub is serving.

The models below mirror pantry-api's RecipeDoc (``pantry_planner/models.py``, PLAN.md 3.2) with
the same bounds, so a doc the hub builds or receives is one pantry plans as it stands.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import ModuleType
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, model_validator

from demo_hub.recipe_import.fetch import ImportFailure, Page

MAX_DOC_LINES = 60
MAX_LINE_TEXT = 300
MAX_LINE_NAME = 200
MAX_SERVINGS = 100
PAGE_TYPES = {"", "text/html", "application/xhtml+xml", "application/xml", "text/xml"}

AmountBasis = Literal["stated_by_source", "demo_house_amounts", "parsed_from_your_paste",
                      "transcribed_confirmed_by_you", "written_by_assistant"]


class LineEvidence(BaseModel):
    quote: str | None = Field(default=None, max_length=300)
    at: str | None = Field(default=None, pattern=r"^\d{1,3}:\d{2}$")


class RecipeLine(BaseModel):
    line_no: int = Field(ge=1)
    text: str = Field(max_length=MAX_LINE_TEXT)
    name: str = Field(min_length=1, max_length=MAX_LINE_NAME)
    quantity: float | None = Field(default=None, ge=0)
    unit: str = Field(default="", max_length=40)
    note: str = Field(default="", max_length=300)
    evidence: LineEvidence | None = None
    confirmed: bool = True
    amount_basis: AmountBasis


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


def doc_from_parsed(key: str, title: str, yield_text: str, parsed: dict[str, Any],
                    source: RecipeSource, warnings: list[str],
                    evidence: list[LineEvidence | None] | None = None,
                    amount_basis: AmountBasis | None = None,
                    confirmed: bool = True) -> RecipeDoc:
    """The RecipeDoc for parse-lines' answer. ``evidence``, ``amount_basis`` and ``confirmed``
    override each line's (a video's timestamps, its lines unticked)."""
    lines = []
    for i, ln in enumerate(parsed.get("lines") or []):
        lines.append(RecipeLine(
            line_no=int(ln["line_no"]), text=str(ln.get("text") or "")[:MAX_LINE_TEXT],
            name=str(ln["name"])[:MAX_LINE_NAME], quantity=ln.get("quantity"),
            unit=str(ln.get("unit") or ""), note=str(ln.get("note") or "")[:300],
            evidence=evidence[i] if evidence and i < len(evidence) else None,
            confirmed=confirmed, amount_basis=amount_basis or ln.get("amount_basis")
            or "stated_by_source"))
    servings = parsed.get("servings")
    all_warnings = [*warnings, *(str(w) for w in parsed.get("warnings") or [])]
    return RecipeDoc(key=key, title=(title or source.site or "Recipe")[:200], servings=servings,
                     servings_stated=servings is not None,
                     servings_basis="source" if servings is not None else None,
                     yield_text=yield_text[:200], lines=lines, source=source,
                     warnings=all_warnings[:20])
