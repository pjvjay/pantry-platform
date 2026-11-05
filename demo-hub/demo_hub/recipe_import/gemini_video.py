"""Gemini watching a public YouTube video for its ingredient lines, each with the time it is said
or shown, which the shopper then checks against the video one by one.

A native ``generateContent`` client, apart from ``llm.py``: Gemini's OpenAI-compatible endpoint,
which the Assistant uses, documents no video input. The request names the video by its
YouTube URL (``file_data.file_uri``, public videos only, a preview feature), asks for JSON in a
fixed schema at low media resolution and temperature 0, and sends the key in the
``x-goog-api-key`` header: never in a URL, a log line or a trace.

What comes back is checked in code: a line without a valid ``mm:ss``, or one past the video's
end, is dropped; at most 60 are kept; every one is unconfirmed until the shopper ticks it.
Gemini's ``usageMetadata`` is returned for pricing (``pricing.video_import``) and the daily cap
(``usage.py``).

This module never decides whether to call Gemini: the hub's video route does, and only on the
shopper's click with consent, with video import switched on (off by default) and a Gemini key
(none in Ollama-only mode).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from demo_hub.recipe_import.fetch import ImportFailure
from demo_hub.recipe_import.youtube import watch_url

log = logging.getLogger(__name__)

MAX_LINES = 60
MAX_LINE = 300
TIMEOUT_S = 300.0          # a long video takes Gemini minutes to read
AT = re.compile(r"^(\d{1,3}):([0-5]\d)$")
PROMPT = """\
Read this cooking video for its ingredient list and nothing else.
Return one line per ingredient as it is said aloud or shown on screen (amount, unit and
ingredient, for example "400 g spaghetti"), with "at": the time in mm:ss where it is first said
or shown. Estimate nothing: when an amount is neither said nor shown, write the ingredient
without one. Leave out steps, equipment, commentary and sponsor messages. "servings" is the
number of servings only when the video states it, otherwise null. If this is not a recipe, or
you are unsure what the ingredients are, return an empty list."""
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "servings": {"type": "INTEGER", "nullable": True},
        "lines": {"type": "ARRAY", "items": {
            "type": "OBJECT",
            "properties": {"text": {"type": "STRING"}, "at": {"type": "STRING"}},
            "required": ["text", "at"]}}},
    "required": ["servings", "lines"],
}
NOT_PUBLIC = re.compile(r"public|private|not found|unavailable|permission|does not exist",
                        re.IGNORECASE)


def request_body(video_id: str) -> dict[str, Any]:
    return {"contents": [{"parts": [{"file_data": {"file_uri": watch_url(video_id)}},
                                    {"text": PROMPT}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "responseSchema": RESPONSE_SCHEMA,
                                 "mediaResolution": "MEDIA_RESOLUTION_LOW", "temperature": 0}}


def seconds_at(at: Any) -> int | None:
    m = AT.match(str(at or "").strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def checked_lines(raw: Any, duration_s: int | None) -> tuple[list[dict[str, str]], int]:
    """(the lines kept, how many were dropped): each kept line has text (at most 300
    characters) and a valid ``at`` within the video; at most 60."""
    kept: list[dict[str, str]] = []
    dropped = 0
    for item in raw if isinstance(raw, list) else []:
        text = " ".join(str((item or {}).get("text") or "").split()) \
            if isinstance(item, dict) else ""
        at = str(item.get("at") or "").strip() if isinstance(item, dict) else ""
        sec = seconds_at(at)
        if not text or sec is None or (duration_s is not None and sec > duration_s):
            dropped += 1
            continue
        kept.append({"text": text[:MAX_LINE].rstrip(), "at": at})
    dropped += max(0, len(kept) - MAX_LINES)
    return kept[:MAX_LINES], dropped


def _error(r: httpx.Response, model: str) -> ImportFailure:
    try:
        message = str(((r.json() or {}).get("error") or {}).get("message") or "")
    except ValueError:
        message = ""
    message = message[:200]
    if r.status_code in (400, 403, 404) and NOT_PUBLIC.search(message):
        return ImportFailure(422, "not_public", "Gemini could not watch this video (only "
                             f"public videos can be read): {message}")
    if r.status_code in (401, 403):
        return ImportFailure(502, "gemini_key", "Gemini refused the hub's key.")
    if r.status_code == 429:
        return ImportFailure(502, "gemini_quota", f"Gemini's quota for {model} is used up for "
                             "now; try later or paste the ingredient list.")
    return ImportFailure(502, "gemini_error", f"Gemini answered HTTP {r.status_code}"
                         + (f": {message}" if message else "."))


async def transcribe(video_id: str, *, api_key: str, model: str, base_url: str,
                     duration_s: int | None,
                     transport: httpx.AsyncBaseTransport | None = None,
                     timeout_s: float = TIMEOUT_S) -> dict[str, Any]:
    """{servings, lines: [{text, at}], dropped, usage (Gemini's usageMetadata)}."""
    url = f"{base_url.rstrip('/')}/models/{model}:generateContent"
    try:
        async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=timeout_s,
                                     follow_redirects=False) as client:
            r = await client.post(url, json=request_body(video_id),
                                  headers={"x-goog-api-key": api_key})
    except httpx.TimeoutException as exc:
        raise ImportFailure(504, "timeout", f"Gemini took longer than {timeout_s:g} s.") from exc
    except httpx.HTTPError as exc:
        raise ImportFailure(502, "unreachable", "Gemini could not be reached "
                            f"({type(exc).__name__}).") from exc
    if r.status_code != 200:
        raise _error(r, model)
    body = r.json() or {}
    usage = body.get("usageMetadata") or {}
    candidates = body.get("candidates") or []
    parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
    text = "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))
    try:
        answer = json.loads(text) if text.strip() else None
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        reason = candidates[0].get("finishReason") if candidates else "no candidates"
        raise ImportFailure(502, "gemini_error", f"Gemini gave no readable answer ({reason}).",
                            usage=usage)
    lines, dropped = checked_lines(answer.get("lines"), duration_s)
    servings = answer.get("servings")
    servings = servings if isinstance(servings, int) and 1 <= servings <= 100 else None
    log.info("video import %s with %s: %s lines kept, %s dropped, %s tokens", video_id, model,
             len(lines), dropped, usage.get("totalTokenCount"))
    return {"servings": servings, "lines": lines, "dropped": dropped, "usage": usage}
