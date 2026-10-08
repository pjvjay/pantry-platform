"""The platform version and the /hub/status release block: the environment wins, then git
describe, then "unknown"; a missing or broken release-set.json gives nulls and never raises."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from demo_hub import version

GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


@pytest.fixture(autouse=True)
def plain_git(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("PANTRY_PLATFORM_VERSION", raising=False)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


def repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("x\n")
    git(path, "add", "README.md")
    git(path, "commit", "-q", "-m", "Start")
    return path


def release_set(**versions: str) -> dict[str, Any]:
    return {"schema": 1, "components": {
        name: {"version": v, "release_url": f"https://github.com/pjvjay/{name}/releases/tag/v{v}"}
        for name, v in versions.items()},
        "local_stack": [{"name": "mcp-sim", "pinned": True, "commit": "a" * 40},
                        {"name": "contextforge", "pinned": False}]}


def test_the_environment_wins(tmp_path: Path) -> None:
    root = repo(tmp_path / "platform")
    git(root, "tag", "v0.2.0")
    assert version.platform_version(root, {"PANTRY_PLATFORM_VERSION": " 0.3.0 "}) == ("0.3.0", "env")
    assert version.platform_version(root, {}) == ("0.2.0", "git")


def test_git_describe_counts_commits_past_the_tag_and_dirt(tmp_path: Path) -> None:
    root = repo(tmp_path / "platform")
    git(root, "tag", "v0.2.0")
    (root / "README.md").write_text("y\n")
    git(root, "commit", "-q", "-am", "Docs")
    sha = git(root, "rev-parse", "--short", "HEAD")
    assert version.platform_version(root, {}) == (f"0.2.0-1-g{sha}", "git")
    block = version.release_block(root=root, env={})["platform"]
    assert block["describe"] == f"v0.2.0-1-g{sha}" and block["dirty"] is False
    (root / "README.md").write_text("z\n")
    block = version.release_block(root=root, env={})["platform"]
    assert block["dirty"] is True and block["describe"].endswith("-dirty")


def test_no_tag_or_no_git_is_unknown_not_an_error(tmp_path: Path) -> None:
    root = repo(tmp_path / "platform")
    git(root, "tag", "not-a-version")
    assert version.platform_version(root, {}) == ("unknown", "unknown")
    assert version.release_block(root=root, env={})["platform"]["commit"] == git(root, "rev-parse",
                                                                                  "HEAD")
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    block = version.release_block(root=bare, env={})
    assert block["platform"] == {"version": "unknown", "source": "unknown", "describe": None,
                                 "commit": None, "dirty": None}


def test_without_a_release_set_every_component_is_null(tmp_path: Path) -> None:
    block = version.release_block(root=tmp_path, workspace=tmp_path, env={},
                                  pantry_health={"status": "ok"})
    assert block["release_set"] == {"present": False, "error": None}
    assert [c["name"] for c in block["components"]] == ["pantry-api", "pantry-db",
                                                         "pantry-frontend"]
    assert all(c["pinned"] is None and c["running"] is None and c["match"] is None
               for c in block["components"])
    assert [s["name"] for s in block["local_stack"]] == ["mcp-sim", "pantry-gateway"]
    assert all(s["checkout"] is None and s["match"] is None for s in block["local_stack"])


def test_pinned_against_running(tmp_path: Path) -> None:
    root = tmp_path / "platform"
    root.mkdir()
    (root / "release-set.json").write_text(json.dumps(release_set(
        **{"pantry-api": "0.2.0", "pantry-db": "0.1.1", "pantry-frontend": "0.3.0"})))
    spa = tmp_path / "dist"
    spa.mkdir()
    (spa / "version.json").write_text(json.dumps({"version": "0.3.1", "revision": "abc"}))
    workspace = tmp_path / "workspace"
    mcp_sim = repo(workspace / "mcp-sim")
    (workspace / "pantry-gateway").mkdir()      # a directory, not a repository
    block = version.release_block(root=root, workspace=workspace, env={}, spa_dist=str(spa),
                                  pantry_health={"status": "ok", "version": "0.2.0"})
    by_name = {c["name"]: c for c in block["components"]}
    assert by_name["pantry-api"] == {"name": "pantry-api", "pinned": "0.2.0", "running": "0.2.0",
                                     "match": True, "release_url":
                                     "https://github.com/pjvjay/pantry-api/releases/tag/v0.2.0"}
    assert (by_name["pantry-frontend"]["running"], by_name["pantry-frontend"]["match"]) == \
        ("0.3.1", False)
    assert (by_name["pantry-db"]["pinned"], by_name["pantry-db"]["running"],
            by_name["pantry-db"]["match"]) == ("0.1.1", None, None)
    stack = {s["name"]: s for s in block["local_stack"]}
    assert stack["mcp-sim"] == {"name": "mcp-sim", "checkout": git(mcp_sim, "rev-parse", "HEAD"),
                                "release_set": "a" * 40, "pinned": True, "match": False}
    assert stack["pantry-gateway"]["checkout"] is None and stack["pantry-gateway"]["pinned"] is None
    assert stack["contextforge"] == {"name": "contextforge", "checkout": None, "release_set": None,
                                     "pinned": False, "match": None}


def test_an_api_reporting_unknown_counts_as_unknown(tmp_path: Path) -> None:
    (tmp_path / "release-set.json").write_text(json.dumps(release_set(**{"pantry-api": "0.2.0"})))
    block = version.release_block(root=tmp_path, workspace=tmp_path, env={},
                                  pantry_health={"version": "unknown"})
    api = block["components"][0]
    assert (api["pinned"], api["running"], api["match"]) == ("0.2.0", None, None)


def test_a_broken_release_set_is_reported_not_raised(tmp_path: Path) -> None:
    (tmp_path / "release-set.json").write_text("{not json")
    block = version.release_block(root=tmp_path, workspace=tmp_path, env={}, pantry_health=None)
    assert block["release_set"] == {"present": False,
                                    "error": "release-set.json is unreadable: JSONDecodeError"}
    (tmp_path / "release-set.json").write_text("[]")
    block = version.release_block(root=tmp_path, workspace=tmp_path, env={})
    assert block["release_set"]["error"] == "release-set.json is not a JSON object"
    (tmp_path / "release-set.json").write_text(json.dumps({"components": {"pantry-api": "0.2.0"},
                                                           "local_stack": ["mcp-sim"]}))
    block = version.release_block(root=tmp_path, workspace=tmp_path, env={})
    assert block["components"][0]["pinned"] is None
