"""Tests for release_set.py and verify_release_set.py: the release-set format, the gitops and
Dockerfile parsers, and verify-pins' lenient and strict modes. GHCR is a fake; one test builds a
throwaway git repository. Nothing goes online.

Run: python3 -m unittest discover -s scripts
"""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

import release_set as rs
import verify_release_set as vrs

ROOT = Path(__file__).resolve().parent.parent
DEMO_DOCKERFILE = (ROOT / "demo" / "Dockerfile").read_text(encoding="utf-8")
# pantry-gitops apps/kustomization.yaml as deployed on 2026-10-08 (the images block, verbatim).
KUSTOMIZATION = """\
# The Kustomize root ArgoCD renders for the pantry-apps Application.
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: pantry-app
resources:
- migrate-job.yaml
images:
- name: ghcr.io/pjvjay/pantry-api
  newName: ghcr.io/pjvjay/pantry-api
  newTag: dev-fa76277
- name: ghcr.io/pjvjay/pantry-db-migrate
  newName: ghcr.io/pjvjay/pantry-db-migrate
  newTag: dev-236d827
- name: ghcr.io/pjvjay/pantry-frontend
  newName: ghcr.io/pjvjay/pantry-frontend
  newTag: dev-ff846e7
"""
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def digest(c: str) -> str:
    return "sha256:" + c * 64


def sha(c: str) -> str:
    return c * 40


VERSIONS = {"pantry-api": "0.2.0", "pantry-db": "0.1.1", "pantry-frontend": "0.3.0"}
COMMITS = {"pantry-api": sha("a"), "pantry-db": sha("b"), "pantry-frontend": sha("c"),
           "pantry-gitops": sha("d"), "pantry-infra": sha("e")}
DIGESTS = {"pantry-api": digest("1"), "pantry-db": digest("2"), "pantry-frontend": digest("3")}


def kustomization(**tags: str) -> str:
    text = KUSTOMIZATION
    for old, name in [("dev-fa76277", "pantry-api"), ("dev-236d827", "pantry-db"),
                      ("dev-ff846e7", "pantry-frontend")]:
        text = text.replace(old, tags.get(name, VERSIONS[name]))
    return text


def good_set() -> dict[str, Any]:
    """The set train.py should build from FakeSources in test_train.py, written out by hand."""
    components: dict[str, Any] = {}
    for name, meta in rs.COMPONENTS.items():
        tag = f"v{VERSIONS[name]}"
        components[name] = {"repo": meta["repo"], "version": VERSIONS[name], "tag": tag,
                            "commit": COMMITS[name], "image": meta["image"],
                            "digest": DIGESTS[name],
                            "release_url": rs.release_url(meta["repo"], tag)}
    components["pantry-db"]["schema_head"] = "0006_origin_submissions"
    return {"schema": rs.SCHEMA, "components": components,
            "deploy": {name: {"repo": repo, "commit": COMMITS[name]}
                       for name, repo in rs.DEPLOY.items()},
            "local_stack": [{"name": "mcp-sim", "pinned": True, "commit": sha("f")},
                            {"name": "contextforge", "pinned": False, "note": "not recorded"}]}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


# --- release_set.py ------------------------------------------------------------------------------


class ReleaseSetTests(unittest.TestCase):
    def test_a_built_set_validates_and_each_break_is_named(self) -> None:
        data = good_set()
        self.assertEqual(rs.validate(data), [])
        broken = copy.deepcopy(data)
        broken["components"]["pantry-api"]["tag"] = "v9.9.9"
        broken["components"]["pantry-db"].pop("schema_head")
        broken["deploy"]["pantry-infra"]["commit"] = "main"
        broken["local_stack"].append({"name": "x", "pinned": True})
        problems = rs.validate(broken)
        self.assertIn("pantry-api: tag must be v0.2.0", problems)
        self.assertIn("pantry-db: schema_head must name the last migration", problems)
        self.assertIn("pantry-infra: needs repo pjvjay/pantry-infra and a 40-hex commit", problems)
        self.assertIn("local_stack x: a pinned entry needs a 40-hex commit", problems)
        self.assertEqual(rs.validate([]), ["release-set.json is not a JSON object"])

    def test_deployed_images_reads_tags_and_digest_pins(self) -> None:
        images = rs.deployed_images(KUSTOMIZATION)
        self.assertEqual(images["ghcr.io/pjvjay/pantry-api"]["tag"], "dev-fa76277")
        pinned = KUSTOMIZATION.replace("  newTag: dev-fa76277",
                                       f"  digest: {digest('1')} # 0.2.0")
        entry = rs.deployed_images(pinned)["ghcr.io/pjvjay/pantry-api"]
        self.assertEqual((entry["digest"], entry["comment"]), (digest("1"), "0.2.0"))
        self.assertTrue(rs.deploys(entry, "0.2.0", digest("1")))
        self.assertFalse(rs.deploys(entry, "0.2.0", digest("9")))
        self.assertFalse(rs.deploys(None, "0.2.0", digest("1")))

    def test_pin_dockerfile_rewrites_only_the_component_from_lines(self) -> None:
        self.assertEqual(rs.from_lines(DEMO_DOCKERFILE)["ghcr.io/pjvjay/pantry-api"],
                         {"tag": "latest", "digest": None})
        pinned = rs.pin_dockerfile(DEMO_DOCKERFILE, good_set()["components"])
        self.assertIn(f"FROM ghcr.io/pjvjay/pantry-frontend:0.3.0@{digest('3')} AS spa\n", pinned)
        self.assertIn(f"FROM ghcr.io/pjvjay/pantry-api:0.2.0@{digest('1')}\n", pinned)
        self.assertEqual(len(pinned.splitlines()), len(DEMO_DOCKERFILE.splitlines()))
        self.assertEqual(rs.from_lines(pinned)["ghcr.io/pjvjay/pantry-frontend"],
                         {"tag": "0.3.0", "digest": digest("3")})
        # Idempotent: pinning a pinned file changes nothing.
        self.assertEqual(rs.pin_dockerfile(pinned, good_set()["components"]), pinned)


# --- verify_release_set.py -----------------------------------------------------------------------


class FakeRepo:
    def __init__(self, data: dict[str, Any] | None) -> None:
        pinned = rs.pin_dockerfile(DEMO_DOCKERFILE, good_set()["components"])
        self.files = {"demo/Dockerfile": pinned}
        if data is not None:
            self.files["release-set.json"] = rs.dumps(data)
        self.pins = dict(COMMITS)
        self.tags = {name: {f"v{v}": COMMITS[name]} for name, v in VERSIONS.items()}
        seeds = {f: b"[]\n" for f in rs.SEED_FILES}
        self.blobs: dict[tuple[str, str], bytes] = {
            ("pantry-gitops", rs.KUSTOMIZATION): kustomization().encode(),
            **{("pantry-api", f): b for f, b in seeds.items()},
            **{("pantry-db", f): b for f, b in seeds.items()}}
        self.changed_files: list[str] = []
        self.fetched: list[str] = []

    def pin(self, path: str) -> str | None:
        return self.pins.get(path)

    def changed(self, base: str) -> list[str]:
        return self.changed_files

    def read(self, path: str) -> str | None:
        return self.files.get(path)

    def fetch_tags(self, path: str) -> None:
        self.fetched.append(path)

    def tag_commit(self, path: str, tag: str) -> str | None:
        return self.tags.get(path, {}).get(tag)

    def show(self, path: str, commit: str, file: str) -> bytes | None:
        return self.blobs.get((path, file))


class FakeRegistry:
    def __init__(self, served: dict[str, str]) -> None:
        self.served = served

    def digest(self, image: str, tag: str) -> str | None:
        return self.served.get(image)


class VerifyTests(unittest.TestCase):
    def run_main(self, repo: FakeRepo, *argv: str, registry: Any = None) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = vrs.main(list(argv), repo=repo, registry=registry)  # type: ignore[arg-type]
        return code, out.getvalue()

    def test_lenient_accepts_todays_untagged_pins(self) -> None:
        # Today: no release-set.json, pins on main but untagged, Dockerfile on :latest.
        repo = FakeRepo(None)
        repo.files["demo/Dockerfile"] = DEMO_DOCKERFILE
        repo.tags = {}
        code, out = self.run_main(repo)
        self.assertEqual(code, 0)
        self.assertIn("mode: lenient (it leaves release-set.json alone)", out)
        self.assertIn("no release-set.json yet", out)

    def test_lenient_validates_an_existing_set_and_only_notes_moved_pins(self) -> None:
        repo = FakeRepo(good_set())
        repo.pins["pantry-api"] = sha("7")
        code, out = self.run_main(repo, "--mode", "lenient")
        self.assertEqual(code, 0)
        self.assertIn("pins moved since the last train: pantry-api", out)
        repo.files["release-set.json"] = "{"
        self.assertEqual(self.run_main(repo, "--mode", "lenient")[0], 1)

    def test_strict_passes_a_coherent_train(self) -> None:
        repo = FakeRepo(good_set())
        repo.changed_files = ["release-set.json", "pantry-api"]
        registry = FakeRegistry({rs.COMPONENTS[n]["image"]: d for n, d in DIGESTS.items()})
        code, out = self.run_main(repo, "--fetch", registry=registry)
        self.assertEqual(code, 0, out)
        self.assertIn("mode: strict (it changes release-set.json)", out)
        self.assertEqual(repo.fetched, list(rs.COMPONENTS))

    def test_strict_fails_each_check(self) -> None:
        def failures(mutate: Any, registry: Any = None) -> list[str]:
            repo = FakeRepo(good_set())
            mutate(repo)
            found, _ = vrs.strict(repo, registry=registry)  # type: ignore[arg-type]
            return found

        def untagged(r: FakeRepo) -> None:
            r.tags["pantry-api"] = {}

        def off_tag(r: FakeRepo) -> None:
            r.pins["pantry-frontend"] = sha("7")

        def gitops_moved(r: FakeRepo) -> None:
            r.pins["pantry-gitops"] = sha("7")

        def not_deployed(r: FakeRepo) -> None:
            r.blobs[("pantry-gitops", rs.KUSTOMIZATION)] = KUSTOMIZATION.encode()

        def latest_dockerfile(r: FakeRepo) -> None:
            r.files["demo/Dockerfile"] = DEMO_DOCKERFILE

        def seeds_differ(r: FakeRepo) -> None:
            r.blobs[("pantry-db", "seeds/products.json")] = b"[1]\n"

        def bad_schema(r: FakeRepo) -> None:
            r.files["release-set.json"] = json.dumps({"schema": 2})

        cases = [(untagged, "pins-are-tags: pantry-api has no tag v0.2.0"),
                 (off_tag, "pins-are-tags: pantry-frontend is pinned at 7777777"),
                 (gitops_moved, "set-is-the-pins: pantry-gitops is pinned at 7777777"),
                 (not_deployed, ("gitops-deploys: pantry-gitops ddddddd deploys dev-fa76277 "
                                 "for pantry-api, not 0.2.0")),
                 (latest_dockerfile, ("demo-dockerfile: demo/Dockerfile has "
                                      "ghcr.io/pjvjay/pantry-api:latest (no digest)")),
                 (seeds_differ, "seeds: seeds/products.json differs"),
                 (bad_schema, "schema: schema must be 1")]
        for mutate, expected in cases:
            found = failures(mutate)
            self.assertTrue(any(f.startswith(expected) for f in found),
                            f"{mutate.__name__}: {found}")
        stale = FakeRegistry({rs.COMPONENTS[n]["image"]: digest("9") for n in DIGESTS})
        found = failures(lambda r: None, registry=stale)
        self.assertEqual(len([f for f in found if f.startswith("digests: GHCR serves")]), 3)

    def test_auto_mode_against_real_git(self) -> None:
        with mock.patch.dict(os.environ, GIT_ENV):
            root = Path(tempfile.mkdtemp())
            git(root, "init", "-q", "-b", "main")
            (root / "README.md").write_text("x\n")
            git(root, "add", "-A")
            git(root, "update-index", "--add", "--cacheinfo", f"160000,{sha('a')},pantry-api")
            git(root, "commit", "-q", "-m", "Start")
            git(root, "switch", "-q", "-c", "pr")
            (root / "README.md").write_text("y\n")
            # Not commit -a: the gitlink has no checkout here, so -a would record its deletion.
            git(root, "add", "README.md")
            git(root, "commit", "-q", "-m", "Docs")
            repo = vrs.Repo(root)
            self.assertEqual(repo.pin("pantry-api"), sha("a"))
            self.assertEqual(self.run_main(repo, "--base", "main")[0], 0)
            (root / "release-set.json").write_text(rs.dumps({"schema": 1}))
            git(root, "add", "release-set.json")
            git(root, "commit", "-q", "-m", "Train")
            code, out = self.run_main(repo, "--base", "main")
            self.assertEqual(code, 1)
            self.assertIn("mode: strict", out)
            self.assertEqual(self.run_main(repo, "--base", "no-such-ref")[0], 2)


if __name__ == "__main__":
    unittest.main()
