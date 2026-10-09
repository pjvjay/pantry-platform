"""Recipe import: a recipe page or a YouTube video read into a RecipeDoc the shopper reviews,
and planned exactly as reviewed (``plan_from_lines``). PLAN.md 4.4; docs/recipe-import.md.

Methods, in the order a link is tried:

- a web page: its schema.org Recipe (JSON-LD, then microdata) through the skill's extractor
  (``web.py``), read by the guarded fetch (``fetch.py``);
- a YouTube video: its title and channel (oEmbed), then, with a YouTube Data API key, the
  description's ingredient list and the recipe pages it links (``youtube.py``,
  ``description.py``); without a key, when the API refuses the key, or with no list there,
  the shopper chooses how to go on
  (``needs: choose_method``): a linked page, a paste, or, on a click, Gemini watching the video
  (``gemini_video.py``, capped per day by ``usage.py``).

Every result is an ImportResult: ``{doc, method, linked_pages, video, needs, warnings}`` where
``needs`` is ``none``, ``confirm_lines`` (a video transcription: tick every line),
``choose_method`` (no lines yet; ``doc`` is null) or ``needs_servings`` (the source states no
servings count). Every line keeps its verbatim text, its parsed amount and its source.
"""

from __future__ import annotations

import tempfile
from collections import OrderedDict
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx

from demo_hub import pricing
from demo_hub.recipe_import import description, gemini_video, web, youtube
from demo_hub.recipe_import.fetch import (
    ImportFailure,
    Limits,
    Resolver,
    check_url,
    fetch_page,
    limits_of,
    load_extractor,
    resolve,
    without_query,
)
from demo_hub.recipe_import.usage import VideoUsage
from demo_hub.recipe_import.web import LineEvidence, RecipeDoc, RecipeSource
from demo_hub.settings import Settings

__all__ = ["DRAFT_KEY", "ImportFailure", "Importer", "RecipeDoc", "import_note", "needs_of",
           "without_query"]

# The key of a doc imported outside a conversation (the console's import sheet). Chat keeps its
# own docs as imp:1, imp:2, ... and re-keys a doc the console sends (ChatBody.recipe_doc).
DRAFT_KEY = "imp:draft"
# the first extract_recipe.py that says which markup held the recipe (JSON-LD or microdata),
# which web.extract needs; an older one loads, and then refuses every page
MIN_EXTRACTOR = (1, 0, 0)
MAX_NOTE = 8_000
LINKED_MEMORY = 64


def needs_of(doc: RecipeDoc | None) -> str:
    if doc is None or not doc.lines:
        return "choose_method"
    if any(not ln.confirmed for ln in doc.lines):
        return "confirm_lines"
    if doc.servings is None:
        return "needs_servings"
    return "none"


def result(doc: RecipeDoc | None, *, linked_pages: list[dict[str, str]] | None = None,
           video: dict[str, Any] | None = None, warnings: list[str] | None = None,
           **extra: Any) -> dict[str, Any]:
    return {"doc": doc.model_dump() if doc else None,
            "method": doc.source.method if doc and doc.lines else None,
            "linked_pages": list(linked_pages or []), "video": video, "needs": needs_of(doc),
            "warnings": list(warnings if warnings is not None
                             else (doc.warnings if doc else [])), **extra}


def _amount(line: dict[str, Any]) -> str:
    q = line.get("quantity")
    if q is None:
        return ""
    unit = line.get("unit") or ""
    return f"{q:g} {unit} " if unit else f"{q:g} "


def import_note(doc_key: str, doc: dict[str, Any], limit: int = MAX_NOTE) -> str:
    """What the model reads of an imported recipe, before the shopper's words:

        [import] <title> (serves N | serves N, your answer; the recipe does not say |
        servings not stated), from <site or channel>, K lines, doc_key imp:N:
        - 400 g spaghetti

    A servings count the shopper gave in the import sheet (servings_basis your_setting) is
    said to be theirs, so the model never reports it as the recipe's own.

    Only the parsed lines (each at most 300 characters, at most 60) and the title, never the
    page or the description. At most ``limit`` characters: lines that do not fit are counted,
    and planning still uses all of them (the hub fills them in from the doc)."""
    source = doc.get("source") or {}
    where = source.get("channel") or source.get("site") or "the shopper's paste"
    servings = "servings not stated"
    if doc.get("servings"):
        servings = f"serves {doc['servings']}" + (
            ", your answer; the recipe does not say"
            if doc.get("servings_basis") == "your_setting" else "")
    lines = doc.get("lines") or []
    head = (f"[import] {doc.get('title')} ({servings}), from {where}, {len(lines)} lines, "
            f"doc_key {doc_key}:")
    out = [head]
    size = len(head)
    for i, ln in enumerate(lines):
        row = f"- {_amount(ln)}{ln.get('name')}" + (f", {ln['note']}" if ln.get("note") else "")
        if size + len(row) + 1 > limit - 80:
            out.append(f"- ... {len(lines) - i} more line(s), planned with the rest")
            break
        out.append(row)
        size += len(row) + 1
    return "\n".join(out)


class Importer:
    """Reads links into ImportResults for the hub's routes and the chat's ``_pre_import``.
    ``transport`` and ``resolver`` replace the network in tests (every outbound request: the
    page, oEmbed, the YouTube Data API, Gemini and pantry's parse-lines)."""

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None,
                 resolver: Resolver = resolve, usage: VideoUsage | None = None) -> None:
        self.settings = settings
        self.transport = transport
        self.resolver = resolver
        usage_dir = settings.usage_dir or str(Path(tempfile.gettempdir()) / "pantry-demo-usage")
        self.usage = usage or VideoUsage(usage_dir, settings.video_import_daily_seconds)
        # recipe pages a video's description linked (page URL -> (video id, channel)): reading
        # one of them is a youtube_linked_page import
        self._linked: OrderedDict[str, tuple[str, str]] = OrderedDict()

    # --- availability ----------------------------------------------------------------------------

    def extractor(self) -> ModuleType:
        """The skill's extractor, 1.0.0 or later; 503 import_unavailable otherwise, so link
        import is off (and the chat reads links as before) rather than failing on each page."""
        module = load_extractor(self.settings.recipe_extractor)
        if _version(module) < MIN_EXTRACTOR:
            raise ImportFailure(503, "import_unavailable", "This extract_recipe.py predates "
                                "1.0.0 and does not say which markup held the recipe; update "
                                "pantry-api's recipe-shopper skill.")
        return module

    def available(self) -> tuple[bool, str]:
        try:
            self.extractor()
        except ImportFailure as exc:
            return False, exc.message
        return True, ""

    def video_status(self) -> dict[str, Any]:
        if not self.settings.video_import_enabled:
            return {"enabled": False, "reason": "Video import is off (DEMO_VIDEO_IMPORT=1 turns "
                    "it on)."}
        if not self.settings.gemini_api_key:
            return {"enabled": False, "reason": "Needs a Gemini API key; not available with "
                    "local models only."}
        return {"enabled": True, "reason": "", "model": self.settings.video_import_model,
                "daily": self.usage.read()}

    def status(self) -> dict[str, Any]:
        ok, reason = self.available()
        return {"links": ok, "youtube_description": ok and bool(self.settings.youtube_api_key),
                **({"reason": reason} if reason else {})}

    # --- a link ----------------------------------------------------------------------------------

    async def import_url(self, url: str, key: str = DRAFT_KEY) -> dict[str, Any]:
        """A recipe page or a YouTube video as an ImportResult. Raises ImportFailure."""
        extractor = self.extractor()
        url = url.strip()
        check_url(url)
        limits = limits_of(extractor)
        vid = youtube.video_id(url)
        if vid:
            return await self._youtube(vid, key, limits)
        return await self._web(url, key, extractor, limits)

    async def _web(self, url: str, key: str, extractor: ModuleType,
                   limits: Limits) -> dict[str, Any]:
        page = await fetch_page(url, limits=limits, resolver=self.resolver,
                                transport=self.transport)
        found = await web.read_recipe(extractor, page)
        lines, warnings = web.bounded(found["ingredients"])
        parsed = await web.parse_lines(self.settings.pantry_api_url, lines, title=found["name"],
                                       yield_text=found["yield"], origin="page",
                                       transport=self.transport)
        linked = self._linked.get(page.url) or self._linked.get(url)
        source = RecipeSource(
            kind="web", method="youtube_linked_page" if linked else found["method"],
            url=page.url, site=web.site_of(page.url), page_title=found["name"][:300] or None,
            channel=(linked[1] or None) if linked else None, retrieved_at=web.now_iso(),
            extractor=web.extractor_label(extractor))
        doc = web.doc_from_parsed(key, found["name"], found["yield"], parsed, source, warnings)
        return result(doc, structured_data=found["method"])

    async def _youtube(self, vid: str, key: str, limits: Limits) -> dict[str, Any]:
        meta = await youtube.oembed(vid, limits=limits, resolver=self.resolver,
                                    transport=self.transport)
        video = {"id": vid, "url": youtube.watch_url(vid), **meta, "duration_s": None,
                 "description_read": False, "transcribe": self.video_status()}
        warnings: list[str] = []
        lines: list[str] = []
        links: list[dict[str, str]] = []
        details = await self._details(vid, limits, warnings, "the description was not read")
        if details is not None:
            video.update(duration_s=details["duration_s"], description_read=True,
                         title=video["title"] or details["title"],
                         channel=video["channel"] or details["channel"])
            lines = description.ingredient_lines(details["description"])
            links = description.recipe_links(details["description"])
            for link in links:
                self._remember(link["url"], vid, str(video["channel"]))
            if not lines:            # description.py decides what counts as a list
                warnings.append("The description has no ingredient list.")
        if not lines:
            return result(None, linked_pages=links, video=video, warnings=warnings)
        bounded, cut = web.bounded(lines)
        parsed = await web.parse_lines(self.settings.pantry_api_url, bounded,
                                       title=str(video["title"]), origin="page",
                                       transport=self.transport)
        source = RecipeSource(kind="youtube", method="youtube_description",
                              url=youtube.watch_url(vid), site="youtube.com",
                              page_title=str(video["title"])[:300] or None,
                              channel=str(video["channel"])[:200] or None,
                              retrieved_at=web.now_iso())
        doc = web.doc_from_parsed(key, str(video["title"]), "", parsed, source, cut)
        return result(doc, linked_pages=links, video=video)

    async def _details(self, vid: str, limits: Limits, warnings: list[str],
                       unread: str) -> dict[str, Any] | None:
        """videos.list for ``vid``, or None when there is no key, or the YouTube Data API
        refuses it or cannot be reached (a bad key, a spent quota, the API not enabled for the
        key). The import then goes on as it does without a key, with the title and channel
        oEmbed already gave, and ``warnings`` says why (``unread``: what that leaves out), as
        the recipe-shopper skill does. A video the API says is not public is still 422."""
        if not self.settings.youtube_api_key:
            warnings.append(f"No YouTube Data API key is set, so {unread}.")
            return None
        try:
            return await youtube.video_details(vid, self.settings.youtube_api_key,
                                               timeout_s=limits.timeout_s,
                                               transport=self.transport)
        except ImportFailure as exc:
            if exc.code == "not_public":
                raise
            warnings.append(f"{exc.message.rstrip('.')}, so {unread}.")
            return None

    def _remember(self, url: str, vid: str, channel: str) -> None:
        self._linked[url] = (vid, channel)
        self._linked.move_to_end(url)
        while len(self._linked) > LINKED_MEMORY:
            self._linked.popitem(last=False)

    # --- a video, watched by Gemini --------------------------------------------------------------

    async def import_video(self, vid: str, *, duration_estimate_s: int | None = None,
                           key: str = DRAFT_KEY) -> dict[str, Any]:
        """Gemini's transcription of the video's ingredient lines, every one unconfirmed.
        Only the video route calls this, on the shopper's click with consent."""
        s = self.settings
        if not s.video_import_enabled:
            raise ImportFailure(409, "video_import_disabled", "Video import is off on this hub "
                                "(DEMO_VIDEO_IMPORT=1 turns it on).")
        if not s.gemini_api_key:
            raise ImportFailure(409, "no_gemini_key", "Needs a Gemini API key; not available "
                                "with local models only.")
        if not youtube.ID.match(vid):
            raise ImportFailure(400, "bad_video_id", "That is not a YouTube video id.")
        # only the fetch limits: watching a video reads no page
        limits = limits_of(load_extractor(s.recipe_extractor))
        meta = await youtube.oembed(vid, limits=limits, resolver=self.resolver,
                                    transport=self.transport)
        notes: list[str] = []          # without a key the estimate is the only length there is
        details = (await self._details(vid, limits, notes, "the video's length was not read "
                                       "and your estimate was used")
                   if s.youtube_api_key else None)
        duration = details["duration_s"] if details else None
        length = duration or duration_estimate_s
        if not length:
            # the daily cap is counted in seconds of video, checked before Gemini watches it
            raise ImportFailure(422, "needs_duration", "Say about how long the video is: the "
                                "hub cannot read its length, and video imports are capped by "
                                "the seconds of video watched each day.")
        ticket = await self.usage.reserve(length)
        try:
            try:
                answer = await gemini_video.transcribe(
                    vid, api_key=s.gemini_api_key, model=s.video_import_model,
                    base_url=s.gemini_native_url, duration_s=duration, transport=self.transport)
            except ImportFailure as exc:
                spent = exc.detail.pop("usage", None)
                if spent:          # Gemini read the video and then failed: it still counts
                    await self.usage.settle(ticket, duration,
                                            int(spent.get("totalTokenCount") or 0))
                raise
            cost = pricing.video_import(s.video_import_model, answer["usage"],
                                        s.video_import_price)
            daily = await self.usage.settle(ticket, duration, cost["total_tokens"])
        finally:
            self.usage.release(ticket)     # a call that failed before Gemini read the video
        video = {"id": vid, "url": youtube.watch_url(vid), **meta, "duration_s": duration,
                 "description_read": duration is not None, "transcribe": self.video_status(),
                 "daily": daily}
        dropped = (f"{answer['dropped']} line(s) without a valid time in the video were left "
                   "out")
        warnings = [*notes, *([dropped] if answer["dropped"] else [])]
        if not answer["lines"]:
            return result(None, video=video, usage=cost,
                          warnings=[*warnings, "Gemini found no ingredient lines in the video."])
        texts = [ln["text"] for ln in answer["lines"]]
        parsed = await web.parse_lines(s.pantry_api_url, texts, title=str(meta["title"]),
                                       origin="paste", transport=self.transport)
        source = RecipeSource(kind="youtube", method="gemini_video", url=youtube.watch_url(vid),
                              site="youtube.com", page_title=str(meta["title"])[:300] or None,
                              channel=str(meta["channel"])[:200] or None,
                              retrieved_at=web.now_iso(), model=s.video_import_model,
                              label="transcribed by Gemini: check every line")
        if answer["servings"] is not None:
            parsed = {**parsed, "servings": answer["servings"]}
        doc = web.doc_from_parsed(key, str(meta["title"]), "", parsed, source, warnings,
                                  evidence=_evidence(answer["lines"], parsed),
                                  amount_basis="transcribed_confirmed_by_you", confirmed=False)
        return result(doc, video=video, usage=cost)


def _version(module: ModuleType) -> tuple[int, ...]:
    """``__version__`` as numbers ("1.0.0" -> (1, 0, 0)); (0,) when it has none or another
    shape."""
    try:
        return tuple(int(p) for p in str(getattr(module, "__version__", "")).split(".")[:3])
    except ValueError:
        return (0,)


def _evidence(lines: list[dict[str, str]], parsed: dict[str, Any]) -> list[LineEvidence | None]:
    """Each parsed line's timestamp. parse-lines drops lines with nothing to buy and renumbers
    the rest, so lines are matched by their text, in order, never by position."""
    out: list[LineEvidence | None] = []
    j = 0
    for row in parsed.get("lines") or []:
        text = str(row.get("text") or "").strip()
        k = j
        while k < len(lines) and lines[k]["text"].strip() != text:
            k += 1
        if k < len(lines):
            out.append(LineEvidence(quote=lines[k]["text"][:300], at=lines[k]["at"]))
            j = k + 1
        else:
            out.append(None)
    return out
