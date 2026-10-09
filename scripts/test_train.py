"""Tests for train.py: what it reads, what it refuses, the dry run, and --open-pr against a local
bare repository through a stand-in gh. GitHub and GHCR are fakes; nothing goes online.

Run: python3 -m unittest discover -s scripts
"""

from __future__ import annotations

import copy
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

import release_set as rs
import train
from test_release_set import (
    COMMITS,
    DEMO_DOCKERFILE,
    DIGESTS,
    GIT_ENV,
    VERSIONS,
    digest,
    git,
    good_set,
    kustomization,
    sha,
)


class FakeSources:
    def __init__(self) -> None:
        self.releases = {meta["repo"]: {
            "tag_name": f"v{VERSIONS[name]}",
            "html_url": rs.release_url(meta["repo"], f"v{VERSIONS[name]}"),
            "body": f"## {name} {VERSIONS[name]}\n\nDigest: `{DIGESTS[name]}`\n"}
            for name, meta in rs.COMPONENTS.items()}
        self.revisions: dict[str, str | None] = dict(COMMITS)
        self.served = dict(DIGESTS)
        self.gitops_text = kustomization()
        self.previous: dict[str, Any] | None = None

    def _name(self, repo: str) -> str:
        return repo.split("/")[1]

    def latest_release(self, repo: str) -> dict[str, Any] | None:
        return self.releases.get(repo)

    def tag_commit(self, repo: str, tag: str) -> str | None:
        return COMMITS[self._name(repo)]

    def branch_head(self, repo: str, branch: str = "main") -> str:
        return COMMITS[self._name(repo)]

    def file_at(self, repo: str, commit: str, path: str) -> str | None:
        assert (repo, commit, path) == ("pjvjay/pantry-gitops", sha("d"), rs.KUSTOMIZATION)
        return self.gitops_text

    def schema_head(self, repo: str, commit: str) -> str | None:
        return "0006_origin_submissions"

    def registry_digest(self, image: str, version: str) -> str | None:
        name = next(n for n, m in rs.COMPONENTS.items() if m["image"] == image)
        return self.served.get(name) if version == VERSIONS[name] else None

    def image_revision(self, image: str, digest: str) -> str | None:
        name = next(n for n, m in rs.COMPONENTS.items() if m["image"] == image)
        return self.revisions.get(name)

    def local_stack(self) -> list[dict[str, Any]]:
        return [{"name": "mcp-sim", "pinned": True, "commit": sha("f")},
                {"name": "contextforge", "pinned": False, "note": "not recorded"}]

    def previous_set(self) -> dict[str, Any] | None:
        return self.previous



# --- train.py ------------------------------------------------------------------------------------


class TrainTests(unittest.TestCase):
    def refusal(self, src: FakeSources) -> str:
        with self.assertRaises(train.Refused) as caught:
            train.build_set(src)
        return str(caught.exception)

    def test_the_set_records_versions_tags_commits_and_digests(self) -> None:
        data, warnings = train.build_set(FakeSources())
        api = data["components"]["pantry-api"]
        self.assertEqual((api["version"], api["tag"], api["commit"], api["digest"]),
                         ("0.2.0", "v0.2.0", sha("a"), digest("1")))
        self.assertEqual(data["components"]["pantry-db"]["schema_head"], "0006_origin_submissions")
        self.assertEqual(data["deploy"]["pantry-gitops"]["commit"], sha("d"))
        self.assertEqual(warnings, [])

    def test_refuses_a_version_released_but_not_deployed(self) -> None:
        src = FakeSources()
        src.gitops_text = kustomization(**{"pantry-api": "dev-fa76277"})
        message = self.refusal(src)
        self.assertIn("pantry-api: 0.2.0 is released but pantry-gitops main (ddddddd) deploys "
                      "dev-fa76277", message)
        self.assertIn("promote_version=0.2.0", message)
        self.assertNotIn("pantry-db", message)

    def test_refuses_a_digest_mismatch(self) -> None:
        src = FakeSources()
        src.served["pantry-frontend"] = digest("9")
        self.assertIn("pantry-frontend: digest mismatch: the v0.3.0 release recorded",
                      self.refusal(src))
        src = FakeSources()
        src.gitops_text = kustomization().replace(
            "  newTag: 0.2.0", f"  digest: {digest('8')} # 0.2.0")
        self.assertIn("pantry-api: digest mismatch: gitops deploys", self.refusal(src))

    def test_refuses_an_image_built_from_another_commit(self) -> None:
        src = FakeSources()
        src.revisions["pantry-db"] = sha("9")
        self.assertIn("pantry-db: ghcr.io/pjvjay/pantry-db-migrate:0.1.1 was built from 9999999",
                      self.refusal(src))

    def test_refuses_before_the_bootstrap(self) -> None:
        src = FakeSources()
        src.releases.clear()
        message = self.refusal(src)
        for name in rs.COMPONENTS:
            self.assertIn(f"{name}: no release yet (bootstrap v0.1.0 first", message)

    def test_missing_digest_and_revision_are_warnings(self) -> None:
        src = FakeSources()
        src.releases["pjvjay/pantry-api"]["body"] = "baseline"
        src.revisions["pantry-api"] = None
        _, warnings = train.build_set(src)
        self.assertEqual(warnings, [
            "pantry-api: the v0.2.0 release notes record no digest; using GHCR's",
            "pantry-api: ghcr.io/pjvjay/pantry-api:0.2.0 has no revision label; not checked"])

    def test_dry_run_prints_the_set_and_changes_nothing(self) -> None:
        out_file = Path(tempfile.mkdtemp()) / "set.json"
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = train.main(["--out", str(out_file)], src=FakeSources())
        self.assertEqual(code, 0)
        self.assertIn("Pin the release set: api 0.2.0, db 0.1.1, frontend 0.3.0", out.getvalue())
        self.assertIn("Release label: `release:minor`", out.getvalue())
        self.assertIn("dry run: nothing was written", err.getvalue())
        self.assertEqual(json.loads(out_file.read_text()), good_set())

    def test_an_unchanged_set_is_not_trained_and_a_refusal_exits_1(self) -> None:
        src = FakeSources()
        src.previous = good_set()
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(train.main([], src=src), 0)
        self.assertIn("nothing to train", err.getvalue())
        src.releases.clear()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(train.main([], src=src), 1)

    def test_summary_level_follows_the_largest_move(self) -> None:
        previous = good_set()
        current = copy.deepcopy(previous)
        current["components"]["pantry-db"]["version"] = "0.1.2"
        level, _, body = train.summary(current, previous)
        self.assertEqual(level, "patch")
        self.assertIn("| pantry-api | 0.2.0 | unchanged since v0.2.0 |", body)
        self.assertIn("mcp-sim fffffff, contextforge (not pinned)", body)


class OpenPrTests(unittest.TestCase):
    """--open-pr against a local bare 'origin' and a stand-in gh, to show it leaves the user's
    checkout alone and pushes exactly the set, the pins and the Dockerfile."""

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = Path(tempfile.mkdtemp())
        seed = self.tmp / "seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        (seed / "demo").mkdir()
        (seed / "demo" / "Dockerfile").write_text(DEMO_DOCKERFILE)
        git(seed, "add", "-A")
        for path in [*rs.COMPONENTS, *rs.DEPLOY]:
            git(seed, "update-index", "--add", "--cacheinfo", f"160000,{sha('1')},{path}")
        git(seed, "commit", "-q", "-m", "Start")
        git(self.tmp, "clone", "-q", "--bare", str(seed), "origin.git")
        git(self.tmp, "clone", "-q", str(self.tmp / "origin.git"), "checkout")
        self.checkout = self.tmp / "checkout"
        git(self.checkout, "switch", "-q", "-c", "feat/elsewhere")
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fake_gh = bin_dir / "gh"
        fake_gh.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{self.tmp}/gh-args"\n'
                           'echo https://github.com/pjvjay/pantry-platform/pull/99\n')
        fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IEXEC)
        patcher = mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_open_pr_pushes_a_train_branch_from_a_temporary_worktree(self) -> None:
        data = good_set()
        level, title, body = train.summary(data, None)
        url = train.open_pr(data, title, body, level, root=self.checkout)
        self.assertEqual(url, "https://github.com/pjvjay/pantry-platform/pull/99")
        branch = "train/api-0.2.0-db-0.1.1-frontend-0.3.0"
        origin = self.tmp / "origin.git"
        pushed = json.loads(git(origin, "show", f"{branch}:release-set.json"))
        self.assertEqual(pushed, data)
        for path, commit in COMMITS.items():
            self.assertEqual(git(origin, "rev-parse", f"{branch}:{path}"), commit)
        self.assertIn(f"pantry-api:0.2.0@{digest('1')}",
                      git(origin, "show", f"{branch}:demo/Dockerfile"))
        self.assertEqual(git(origin, "log", "-1", "--format=%s", branch), title)
        args = (self.tmp / "gh-args").read_text().splitlines()
        self.assertEqual(args[:2], ["pr", "create"])
        self.assertIn("release:minor", args)
        # The user's checkout is untouched: same branch, clean, and no worktree left behind.
        self.assertEqual(git(self.checkout, "branch", "--show-current"), "feat/elsewhere")
        self.assertEqual(git(self.checkout, "status", "--porcelain"), "")
        self.assertEqual(len(git(self.checkout, "worktree", "list").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
