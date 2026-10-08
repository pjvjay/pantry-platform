"""Gemini watching a video for its ingredient lines (recipe_import/gemini_video.py, usage.py)
and the import routes, with Gemini, YouTube and pantry behind a MockTransport: Gemini is never
called for real."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from demo_hub import pricing
from demo_hub.recipe_import import Importer, ImportFailure
from demo_hub.recipe_import.gemini_video import checked_lines, request_body
from demo_hub.recipe_import.usage import VideoUsage
from demo_hub.recipe_import.web import RecipeDoc
from demo_hub.settings import Settings
from tests.import_fakes import Net, make_importer

VID = "dQw4w9WgXcQ"
KEY = "gm-secret-key-123"
USAGE = {"promptTokenCount": 75_000, "candidatesTokenCount": 400, "thoughtsTokenCount": 100,
         "totalTokenCount": 75_500,
         "promptTokensDetails": [{"modality": "VIDEO", "tokenCount": 70_000},
                                 {"modality": "AUDIO", "tokenCount": 4_800},
                                 {"modality": "TEXT", "tokenCount": 200}]}


def gemini_says(lines: list[dict[str, Any]], servings: int | None = 4,
                usage: dict[str, Any] | None = None):
    def answer(request: httpx.Request) -> httpx.Response:
        text = json.dumps({"servings": servings, "lines": lines})
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
            "usageMetadata": usage or USAGE})
    return answer


LINES = [{"text": "200 g red lentils", "at": "0:42"}, {"text": "1 onion", "at": "1:05"},
         {"text": "2 cloves garlic", "at": ""}, {"text": "1 tsp turmeric", "at": "99:00"},
         {"text": "400 ml coconut milk", "at": "3:10"}]


def importer(tmp_path: Path, net: Net, **settings: Any) -> Importer:
    defaults: dict[str, Any] = {"video_import_enabled": True, "gemini_api_key": KEY,
                                "youtube_api_key": "yt-key"}
    return make_importer(tmp_path, net, **{**defaults, **settings})


def watched(imp: Importer, **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(imp.import_video(VID, **kwargs))


def refused(imp: Importer, **kwargs: Any) -> ImportFailure:
    with pytest.raises(ImportFailure) as err:
        watched(imp, **kwargs)
    return err.value


def test_the_request_and_the_key_header(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    net = Net()
    net.video(VID)
    net.gemini_answer = gemini_says(LINES)
    watched(importer(tmp_path, net))
    [sent] = net.gemini
    assert sent.url.path == "/v1beta/models/gemini-3-flash-preview:generateContent"
    assert sent.headers["x-goog-api-key"] == KEY
    assert KEY not in str(sent.url) and "key=" not in str(sent.url)
    body = json.loads(sent.content)
    assert body == request_body(VID)
    assert body["contents"][0]["parts"][0] == {
        "file_data": {"file_uri": f"https://www.youtube.com/watch?v={VID}"}}
    assert body["generationConfig"]["mediaResolution"] == "MEDIA_RESOLUTION_LOW"
    assert body["generationConfig"]["temperature"] == 0
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert "Estimate nothing" in body["contents"][0]["parts"][1]["text"]
    assert KEY not in caplog.text and "yt-key" not in caplog.text
    assert "video import" in caplog.text                          # it did log, without them


def test_lines_are_checked_unconfirmed_and_timestamped(tmp_path: Path) -> None:
    net = Net()
    net.video(VID, duration="PT12M30S")
    net.gemini_answer = gemini_says(LINES)
    result = watched(importer(tmp_path, net))
    doc = RecipeDoc.model_validate(result["doc"])
    assert result["needs"] == "confirm_lines" and result["method"] == "gemini_video"
    # no time, and past the 12:30 video's end: dropped
    assert [ln.text for ln in doc.lines] == ["200 g red lentils", "1 onion",
                                             "400 ml coconut milk"]
    assert [ln.evidence.at for ln in doc.lines if ln.evidence] == ["0:42", "1:05", "3:10"]
    assert all(not ln.confirmed and ln.amount_basis == "transcribed_confirmed_by_you"
               for ln in doc.lines)
    assert doc.servings == 4 and doc.source.model == "gemini-3-flash-preview"
    assert doc.source.label == "transcribed by Gemini: check every line"
    assert "2 line(s) without a valid time" in result["warnings"][0]
    assert net.parsed[-1]["lines"] == [ln.text for ln in doc.lines]


def test_checked_lines() -> None:
    raw = [{"text": "a", "at": "0:01"}, {"text": "", "at": "0:02"}, {"text": "b", "at": "1:60"},
           {"text": "c"}, "junk", {"text": "d " * 200, "at": "10:00"}]
    kept, dropped = checked_lines(raw, duration_s=None)
    assert kept[0]["text"] == "a" and len(kept[1]["text"]) <= 300
    assert dropped == 4
    kept, dropped = checked_lines([{"text": f"{i} g x", "at": "0:01"} for i in range(70)], 600)
    assert len(kept) == 60 and dropped == 10
    assert checked_lines(None, None) == ([], 0)


def test_usage_is_priced_as_a_video_import() -> None:
    cost = pricing.video_import("gemini-3-flash-preview", USAGE, (0.0, 0.0))
    assert cost == {"kind": "video_import", "model": "gemini-3-flash-preview",
                    "prompt_tokens": 75_000, "output_tokens": 500, "total_tokens": 75_500,
                    "by_modality": {"video": 70_000, "audio": 4_800, "text": 200},
                    "llm_cost_usd": 0.0, "pricing": "preview pricing"}
    assert pricing.video_import("m", USAGE, (1.0, 2.0))["llm_cost_usd"] == 0.076


def test_the_daily_cap(tmp_path: Path) -> None:
    net = Net()
    net.video(VID, duration="PT2H")
    net.gemini_answer = gemini_says(LINES[:2])
    imp = importer(tmp_path, net, video_import_daily_seconds=3 * 3600)
    watched(imp)
    assert imp.usage.read()["seconds"] == 7200 and imp.usage.read()["calls"] == 1
    err = refused(imp)                                    # 2 h more would pass the 3 h cap
    assert (err.status, err.code) == (429, "daily_video_limit")
    assert err.body()["seconds_used"] == 7200 and err.body()["limit_s"] == 10800
    assert len(net.gemini) == 1                            # refused before Gemini was called
    # without a key the length is unknown: the shopper's estimate is checked, and the tokens
    # (/ 100 a second) are what is counted
    net2 = Net()
    net2.video(VID)
    net2.gemini_answer = gemini_says(LINES[:2])
    (tmp_path / "b").mkdir()
    imp2 = importer(tmp_path / "b", net2, youtube_api_key="", video_import_daily_seconds=1000)
    assert refused(imp2, duration_estimate_s=1200).code == "daily_video_limit"
    watched(imp2, duration_estimate_s=600)
    assert imp2.usage.read()["seconds"] == 755.0           # 75,500 tokens / 100
    # the count is per UTC day and survives a restart (it is a file)
    again = VideoUsage(tmp_path / "b" / "usage", 1000)
    assert again.read()["seconds"] == 755.0


def test_no_gemini_key_and_switched_off_are_409(tmp_path: Path) -> None:
    net = Net()
    net.video(VID)
    err = refused(importer(tmp_path, net, gemini_api_key=""))
    assert (err.status, err.code) == (409, "no_gemini_key")
    assert "local models only" in err.message
    err = refused(importer(tmp_path, net, video_import_enabled=False))
    assert (err.status, err.code) == (409, "video_import_disabled")
    assert net.gemini == [] and net.requests == []
    status = importer(tmp_path, net, gemini_api_key="").video_status()
    assert status == {"enabled": False, "reason": "Needs a Gemini API key; not available with "
                      "local models only."}


def test_gemini_errors(tmp_path: Path) -> None:
    net = Net()
    net.video(VID)
    imp = importer(tmp_path, net)
    for status, message, code in ((400, "The video is private.", "not_public"),
                                  (429, "Quota exceeded", "gemini_quota"),
                                  (500, "boom", "gemini_error")):
        net.gemini_answer = lambda r, s=status, m=message: httpx.Response(
            s, json={"error": {"message": m}})
        assert refused(imp).code == code
    net.gemini_answer = lambda r: httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": "not json"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"totalTokenCount": 5000}})
    before = imp.usage.read()["seconds"]
    assert refused(imp).code == "gemini_error"
    assert imp.usage.read()["seconds"] > before          # it read the video: that still counts
    net.gemini_answer = gemini_says([])
    empty = watched(imp)
    assert empty["doc"] is None and empty["needs"] == "choose_method"
    assert "no ingredient lines" in empty["warnings"][-1]


def test_settings_from_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    key_file = tmp_path / "youtube_api_key"
    key_file.write_text("yt-from-file\n")
    for name in ("YOUTUBE_API_KEY", "RECIPE_EXTRACTOR", "DEMO_VIDEO_IMPORT",
                 "DEMO_VIDEO_IMPORT_PRICE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("YOUTUBE_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("RECIPE_SHOPPER_SKILL", "/skills/recipe-shopper/SKILL.md")
    s = Settings.from_env()
    assert s.youtube_api_key == "yt-from-file" and "yt-from-file" not in repr(s)
    assert s.recipe_extractor == "/skills/recipe-shopper/scripts/extract_recipe.py"
    assert s.video_import_enabled is False and s.video_import_daily_seconds == 6 * 3600
    assert s.video_import_price == (0.0, 0.0)
    monkeypatch.setenv("DEMO_VIDEO_IMPORT", "1")
    monkeypatch.setenv("DEMO_VIDEO_IMPORT_PRICE", "0.5,3")
    monkeypatch.setenv("YOUTUBE_API_KEY", "yt-env")
    monkeypatch.setenv("RECIPE_EXTRACTOR", "/x/extract_recipe.py")
    s = Settings.from_env()
    assert s.video_import_enabled and s.video_import_price == (0.5, 3.0)
    assert s.youtube_api_key == "yt-env" and s.recipe_extractor == "/x/extract_recipe.py"
    monkeypatch.setenv("YOUTUBE_API_KEY_FILE", str(tmp_path / "missing"))
    monkeypatch.delenv("YOUTUBE_API_KEY")
    assert Settings.from_env().youtube_api_key == ""
