"""The recipe-import routes (guarded, like every route that changes something): a link, a
video on the shopper's click with consent, ChatBody.recipe_doc, /hub/status, and the import
trace each route keeps. Every upstream is faked."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from demo_hub import app as app_module
from demo_hub.settings import Settings
from demo_hub.telemetry import compute_metrics
from tests.conftest import console_client
from tests.import_fakes import Net, fake_extractor, recipe_page
from tests.test_import_chat import reviewed
from tests.test_video_import import KEY, LINES, VID, gemini_says


def route_client(tmp_path: Path, net: Net, **settings: Any) -> tuple[Any, Any]:
    s = Settings(pantry_api_url="http://pantry.test",
                 recipe_extractor=str(fake_extractor(tmp_path)), usage_dir=str(tmp_path / "u"),
                 traces_dir=str(tmp_path / "traces"), images_dir=str(tmp_path / "images"),
                 **{"video_import_enabled": True, "gemini_api_key": KEY,
                    "youtube_api_key": "yt-key", **settings})
    app = app_module.create_app(s)
    imp = app.state.importer
    imp.transport, imp.resolver = httpx.MockTransport(net.handler), net.resolve
    return console_client(app), app


def test_the_video_route_needs_consent(tmp_path: Path) -> None:
    net = Net()
    net.video(VID)
    net.gemini_answer = gemini_says(LINES)
    client, _ = route_client(tmp_path, net)
    for body in ({"video_id": VID}, {"video_id": VID, "consent": False},
                 {"video_id": VID, "consent": "yes"}, {"video_id": "bad", "consent": True}):
        assert client.post("/hub/recipes/import/video", json=body).status_code == 422, body
    assert net.gemini == []                                  # never called without consent
    r = client.post("/hub/recipes/import/video", json={"video_id": VID, "consent": True})
    assert r.status_code == 200 and r.json()["needs"] == "confirm_lines"
    assert len(net.gemini) == 1
    (tmp_path / "off").mkdir()
    off, _ = route_client(tmp_path / "off", net, gemini_api_key="")
    r = off.post("/hub/recipes/import/video", json={"video_id": VID, "consent": True})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_gemini_key"


def test_a_video_import_is_a_trace_with_its_tokens_and_cost(tmp_path: Path) -> None:
    net = Net()
    net.video(VID)
    net.gemini_answer = gemini_says(LINES)
    client, app = route_client(tmp_path, net, video_import_price=(1.0, 2.0))
    r = client.post("/hub/recipes/import/video", json={"video_id": VID, "consent": True})
    assert r.status_code == 200 and r.json()["usage"]["llm_cost_usd"] == 0.076
    [trace] = app.state.traces.list(5)
    stored = app.state.traces.get(trace["id"])
    assert stored["source"] == "import" and stored["cost_usd"] == 0.076
    span = stored["spans"][0]
    assert span["kind"] == "import" and span["attrs"]["total_tokens"] == 75_500
    assert span["attrs"]["method"] == "gemini_video" and span["attrs"]["pricing"] == \
        "preview pricing"
    imports = compute_metrics([stored])["imports"]
    assert imports["count"] == 1 and imports["video_tokens"] == 75_500
    assert imports["video_cost_usd"] == 0.076 and imports["by_method"] == {"gemini_video": 1}
    assert compute_metrics([stored])["models"] == []          # an import is not a model turn


def test_the_link_route_and_status(tmp_path: Path) -> None:
    net = Net()
    net.serve("https://blog.example/dal?token=abc", recipe_page("Dal", ["200 g red lentils"]))
    client, app = route_client(tmp_path, net)
    r = client.post("/hub/recipes/import", json={"url": "https://blog.example/dal?token=abc"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"doc", "method", "linked_pages", "video", "needs", "warnings"}
    assert body["doc"]["key"] == "imp:draft" and body["needs"] == "none"
    stored = app.state.traces.get(app.state.traces.list(1)[0]["id"])
    assert stored["spans"][0]["attrs"]["url"] == "https://blog.example/dal"       # no query
    assert stored["spans"][0]["attrs"]["host"] == "blog.example"
    assert "abc" not in json.dumps(stored)
    r = client.post("/hub/recipes/import", json={"url": "http://127.0.0.1:8090/hub/status"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_public"
    r = client.post("/hub/recipes/import", json={"url": "https://nothing.example/"})
    assert r.status_code == 502
    status = client.get("/hub/status").json()
    assert status["recipe_import"] == {"links": True, "youtube_description": True}
    assert status["video_import"]["enabled"] is True and status["keys"]["youtube"] is True
    assert "gm-secret" not in json.dumps(status) and "yt-key" not in json.dumps(status)


def test_the_chat_route_checks_the_reviewed_doc(monkeypatch: pytest.MonkeyPatch,
                                                tmp_path: Path) -> None:
    app = app_module.create_app(Settings(traces_dir=str(tmp_path / "t"),
                                         images_dir=str(tmp_path / "i"),
                                         recipe_extractor=str(fake_extractor(tmp_path))))
    seen: list[Any] = []

    async def fake_run(conv: Any, message: str, recipe_doc: Any = None) -> Any:
        seen.append(recipe_doc)
        yield {"type": "done", "steps": 0, "stop": "answered", "seconds": 0,
               "input_tokens": 0, "output_tokens": 0}

    monkeypatch.setattr(app.state.agent, "run", fake_run)
    real = httpx.AsyncClient          # the store names lookup: no real pantry is contacted
    monkeypatch.setattr(app_module.httpx, "AsyncClient", lambda *a, **k: real(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    client = console_client(app)

    def post(doc: dict[str, Any]) -> httpx.Response:
        return client.post("/hub/agent/chat", json={"message": "plan", "model": "gemini:m",
                                                    "target": "pantry", "recipe_doc": doc})

    assert post(reviewed()).status_code == 200 and seen[-1].title == "Weeknight Dal"
    r = post(reviewed(confirmed=False))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "unconfirmed_lines"
    assert r.json()["detail"]["lines"] == [1, 2]
    big = reviewed()
    big["warnings"] = ["x" * 70_000]
    assert post(big).status_code == 413
    bad = reviewed()
    bad["lines"][0]["quantity"] = -1
    assert post(bad).status_code == 422
    assert post({**reviewed(), "lines": []}).json()["detail"]["code"] == "no_lines"
    assert len(seen) == 1
