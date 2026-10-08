"""Tests for semver_labels: the 0.x bump table, promote_major, the PR rules R1-R4 (carried-PR
discovery on pantry-api #27's real titles, bases and 19 commit subjects), plan on a temporary git
repository, the notes and the atomic tag reservation. GitHub is a fake; nothing goes online.

Run: python3 -m unittest discover -s .github/actions/semver-labels/tests
"""

from __future__ import annotations

import copy
import importlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sl = importlib.import_module("semver_labels")
Config, GitHubError, Version = sl.Config, sl.GitHubError, sl.Version
bump, parse_version = sl.bump, sl.parse_version

FIXTURES = HERE / "fixtures"
PR27 = json.loads((FIXTURES / "pantry-api-27.json").read_text(encoding="utf-8"))

# What each app repo will declare as shipped: what goes into its image. tests/** and the
# workflow file trigger a build but change nothing a user runs, so they are not shipped.
SHIPPED = {
    "api": ["Dockerfile", "pyproject.toml", "pantry_planner/**", "seeds/**"],
    "db": ["Dockerfile", "migrations/**", "seeds/**", "scripts/**"],
    "frontend": ["Dockerfile", "nginx.conf", "package*.json", "vite.config.ts", "tsconfig.json",
                 "index.html", "src/**"],
}


def config(component: str = "pantry-api", shipped: list[str] | None = None,
           workflow: str | None = ".github/workflows/build.yml") -> Config:
    return Config(component=component, image=f"ghcr.io/pjvjay/{component}",
                  baseline=Version(0, 1, 0), shipped_paths=tuple(shipped or SHIPPED["api"]),
                  build_workflow=workflow)


class FakeGitHub:
    """The REST paths semver_labels calls, served from dicts."""

    def __init__(self, pulls: dict[str, Any] | None = None, repo: str = "pjvjay/pantry-api"):
        self.repo = repo
        self.pulls: dict[str, Any] = copy.deepcopy(pulls or {})
        self.files: dict[int, list[dict[str, Any]]] = {}
        self.commits: dict[int, list[dict[str, Any]]] = {}
        self.refs: dict[str, dict[str, Any]] = {}
        self.tag_objects: dict[str, str] = {}
        self.posts: list[tuple[str, Any]] = []
        self.fail_post: int | None = None

    def label(self, number: int, *names: str) -> None:
        self.pulls[str(number)]["labels"] = [{"name": n} for n in names]

    def get(self, path: str, **params: Any) -> Any:
        parts = path.split("/")
        if parts[0] == "pulls" and len(parts) == 2:
            if parts[1] not in self.pulls:
                raise GitHubError(404, "Not Found")
            return self.pulls[parts[1]]
        if parts[0] == "commits" and parts[2:] == ["pulls"]:
            return []
        if path.startswith("git/ref/tags/"):
            tag = path.rsplit("/", 1)[1]
            if tag not in self.refs:
                raise GitHubError(404, "Not Found")
            return {"object": self.refs[tag]}
        if path.startswith("git/tags/"):
            return {"object": {"type": "commit", "sha": self.tag_objects[path.rsplit("/", 1)[1]]}}
        if path == "git/matching-refs/tags/v":
            return [{"ref": f"refs/tags/{t}"} for t in self.refs]
        raise AssertionError(f"unexpected GET {path} {params}")

    def paginate(self, path: str, max_pages: int = 10, **params: Any) -> list[Any]:
        parts = path.split("/")
        if path == "pulls":
            assert params.get("state") == "closed"
            return [p for p in self.pulls.values()
                    if p["base"]["ref"] == params["base"] and p["state"] == "closed"]
        if parts[0] == "pulls" and parts[2] == "files":
            return self.files.get(int(parts[1]), [])
        if parts[0] == "pulls" and parts[2] == "commits":
            return self.commits.get(int(parts[1]), [])
        raise AssertionError(f"unexpected list {path}")

    def post(self, path: str, body: Any) -> Any:
        self.posts.append((path, body))
        if self.fail_post:
            raise GitHubError(self.fail_post, "Reference already exists")
        self.refs[body["ref"].rsplit("/", 1)[1]] = {"type": "commit", "sha": body["sha"]}
        return {"ref": body["ref"]}


def pr27_github() -> FakeGitHub:
    gh = FakeGitHub(PR27["pulls"])
    gh.commits[27] = PR27["commits_27"]
    gh.files[27] = [{"filename": "pantry_planner/flow.py"}, {"filename": "tests/test_flow.py"}]
    return gh


# --- versions ------------------------------------------------------------------------------------


class VersionTests(unittest.TestCase):
    def test_parse_accepts_a_v_prefix_and_refuses_suffixes(self) -> None:
        self.assertEqual(parse_version("v0.4.2"), Version(0, 4, 2))
        self.assertEqual(str(parse_version("10.0.1")), "10.0.1")
        for bad in ["1.0", "1.0.0-rc1", "01.2.3", "v1.2.3.4", "", "latest"]:
            with self.assertRaises(ValueError, msg=bad):
                parse_version(bad)

    def test_the_0x_table(self) -> None:
        # RELEASING.md's worked examples: below 1.0.0, major bumps the minor.
        current = Version(0, 4, 2)
        self.assertEqual(bump(current, "major"), Version(0, 5, 0))
        self.assertEqual(bump(current, "minor"), Version(0, 5, 0))
        self.assertEqual(bump(current, "patch"), Version(0, 4, 3))
        self.assertIsNone(bump(current, "none"))
        # No label sequence ever reaches 1.0.0 on its own.
        v = Version(0, 9, 9)
        for _ in range(5):
            v = bump(v, "major")  # type: ignore[assignment]
        self.assertEqual(v.major, 0)

    def test_promote_major_is_the_only_way_to_1_0_0(self) -> None:
        self.assertEqual(bump(Version(0, 9, 3), "none", promote_major=Version(1, 0, 0)),
                         Version(1, 0, 0))
        self.assertEqual(bump(Version(0, 9, 3), "patch", promote_major=Version(1, 0, 0)),
                         Version(1, 0, 0))
        for bad in [Version(2, 0, 0), Version(1, 1, 0), Version(0, 10, 0)]:
            with self.assertRaises(ValueError, msg=str(bad)):
                bump(Version(0, 9, 3), "major", promote_major=bad)
        # From 1.x, promote_major may name the next major; a label does the same.
        self.assertEqual(bump(Version(1, 4, 0), "none", promote_major=Version(2, 0, 0)),
                         Version(2, 0, 0))

    def test_plain_semver_from_1_0_0(self) -> None:
        self.assertEqual(bump(Version(1, 2, 3), "major"), Version(2, 0, 0))
        self.assertEqual(bump(Version(1, 2, 3), "minor"), Version(1, 3, 0))
        self.assertEqual(bump(Version(1, 2, 3), "patch"), Version(1, 2, 4))
        self.assertIsNone(bump(Version(1, 2, 3), "none"))

    def test_the_first_release_is_the_baseline(self) -> None:
        self.assertEqual(bump(None, "patch"), Version(0, 1, 0))
        self.assertEqual(bump(None, "major"), Version(0, 1, 0))
        self.assertIsNone(bump(None, "none"))

    def test_level_from_labels(self) -> None:
        self.assertEqual(sl.level_from_labels(["bug", "release:minor"]), ("minor", None))
        level, problem = sl.level_from_labels(["bug"])
        self.assertIsNone(level)
        self.assertIn("no release label", problem or "")
        level, problem = sl.level_from_labels(["release:patch", "release:major"])
        self.assertEqual(level, "major")
        self.assertIn("2 release labels", problem or "")
        level, problem = sl.level_from_labels(["release:huge"])
        self.assertIn("unknown release label release:huge", problem or "")


# --- paths (R2, R4) ------------------------------------------------------------------------------


class PathTests(unittest.TestCase):
    def test_push_paths_of_the_three_real_build_files(self) -> None:
        expected = {
            "api": ["Dockerfile", "pyproject.toml", "pantry_planner/**", "seeds/**", "tests/**",
                    ".github/workflows/build.yml"],
            "db": ["Dockerfile", "migrations/**", "seeds/**", "scripts/**",
                   ".github/workflows/build.yml"],
            "frontend": ["Dockerfile", "nginx.conf", "package*.json", "vite.config.ts",
                         "tsconfig.json", "index.html", "src/**", ".github/workflows/build.yml"],
        }
        for repo, paths in expected.items():
            text = (FIXTURES / f"build-{repo}.yml").read_text(encoding="utf-8")
            self.assertEqual(sl.push_paths(text), paths, repo)

    def test_r4_shipped_paths_are_covered_by_each_real_build(self) -> None:
        for repo, shipped in SHIPPED.items():
            pushed = sl.push_paths((FIXTURES / f"build-{repo}.yml").read_text(encoding="utf-8"))
            self.assertEqual(sl.uncovered(shipped, pushed or []), [], repo)
        api = sl.push_paths((FIXTURES / "build-api.yml").read_text(encoding="utf-8")) or []
        self.assertEqual(sl.uncovered(["pantry_planner/mealplan/**", "skills/**"], api),
                         ["skills/**"])

    def test_flow_form_and_missing_paths(self) -> None:
        text = 'on:\n  push:\n    branches: [main]\n    paths: ["demo/**", render.yaml]\n'
        self.assertEqual(sl.push_paths(text), ["demo/**", "render.yaml"])
        self.assertIsNone(sl.push_paths("on:\n  pull_request:\n    paths: [a]\n"))
        self.assertIsNone(sl.push_paths("on: [push]\n"))

    def test_glob_semantics_match_github_path_filters(self) -> None:
        self.assertTrue(sl.matches_any("src/a/b.tsx", ["src/**"]))
        self.assertTrue(sl.matches_any("package-lock.json", ["package*.json"]))
        self.assertFalse(sl.matches_any("demo/Dockerfile", ["Dockerfile"]))
        self.assertFalse(sl.matches_any("src/a/b.ts", ["src/*"]))
        self.assertTrue(sl.matches_any("pantry-api", ["pantry-api"]))  # a submodule gitlink
        self.assertFalse(sl.matches_any("tests/test_x.py", SHIPPED["api"]))


# --- R3: carried PRs on pantry-api #27 -----------------------------------------------------------


class CarriedTests(unittest.TestCase):
    def test_the_19_real_subjects_alone_find_nothing(self) -> None:
        commits = PR27["commits_27"]
        self.assertEqual(len(commits), 19)
        self.assertFalse(any(c["commit"]["message"].startswith("Merge pull request")
                             for c in commits))
        # "Merge main (#20 squash-merged) into ..." is not a '(#N)' suffix.
        self.assertEqual(sl.subject_numbers(commits), [])
        self.assertEqual(sl.subject_numbers([{"sha": "abc1234", "commit": {
            "message": "Seeds: yellow onions in bags (#13)\n\nbody (#99)"}}]), [(13, "abc1234")])

    def test_the_title_yields_24_and_26(self) -> None:
        self.assertEqual(sl.title_numbers(PR27["pulls"]["27"]["title"]), [24, 26])
        self.assertEqual(sl.title_numbers("Land #3, #5 and #8"), [3, 5, 8])
        self.assertEqual(sl.title_numbers("Landing page tweaks for #4"), [])

    def test_the_lands_line_yields_24_and_26(self) -> None:
        self.assertEqual(sl.lands_numbers("Some text\n\nLands: #24, #26\n"), [24, 26])
        self.assertEqual(sl.lands_numbers("- **Lands:** #24 and #26"), [24, 26])
        template = "<!-- Lands: #24, #26 for a land PR -->\nLands: \n"
        self.assertEqual(sl.lands_numbers(template), [])
        self.assertEqual(sl.lands_numbers("This lands: #9 eventually"), [])

    def test_27_today_carries_24_and_26_by_its_title(self) -> None:
        gh = pr27_github()
        carried = sl.discover_carried(gh, gh.pulls["27"], PR27["commits_27"])
        self.assertEqual([c.number for c in carried], [24, 26])
        self.assertEqual(carried[0].sources, ["the title"])
        # #26 merged into feat/recipe-links 27 s after #24 left it, so #24 did not carry it;
        # only the title (or a Lands: line) does. #28 is still open, so it carries nothing yet.
        self.assertEqual(carried[1].sources, ["the title"])
        self.assertTrue(all(c.level is None and not c.sets_floor for c in carried))

    def test_lands_line_then_base_branch_recursion_when_the_stack_lands_in_order(self) -> None:
        gh = pr27_github()
        gh.pulls["27"]["title"] = "Recipe links, Gemini models and store-aware plans"
        gh.pulls["27"]["body"] = "Lands: #24, #26\n"
        # #29 merges into feat/observability, then #28 into the land branch.
        gh.pulls["29"].update(state="closed", merged_at="2026-10-09T10:00:00Z")
        gh.pulls["28"].update(state="closed", merged_at="2026-10-09T11:00:00Z")
        carried = sl.discover_carried(gh, gh.pulls["27"], PR27["commits_27"])
        self.assertEqual([c.number for c in carried], [24, 26, 28, 29])
        self.assertEqual(carried[2].sources, ["merged into land/recipe-links-gemini"])
        self.assertEqual(carried[3].sources, ["merged into feat/observability (carried by #28)"])

    def test_a_pr_merged_into_a_carried_branch_after_it_merged_is_not_carried(self) -> None:
        gh = pr27_github()
        gh.pulls["28"].update(state="closed", merged_at="2026-10-09T11:00:00Z")
        gh.pulls["29"].update(state="closed", merged_at="2026-10-09T12:00:00Z")
        numbers = [c.number for c in sl.discover_carried(gh, gh.pulls["27"], [])]
        self.assertEqual(numbers, [24, 26, 28])

    def test_a_carried_pr_already_on_main_sets_no_floor(self) -> None:
        gh = pr27_github()
        gh.pulls["27"]["body"] = "Lands: #20, #404"
        gh.label(20, "release:major")
        carried = {c.number: c for c in sl.discover_carried(gh, gh.pulls["27"], [])}
        self.assertTrue(carried[20].on_default_branch)
        self.assertFalse(carried[20].sets_floor)
        self.assertTrue(carried[404].missing)
        self.assertIn("already merged into main", carried[20].describe())


class CheckPrTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gh = pr27_github()
        self.workflow = (FIXTURES / "build-api.yml").read_text(encoding="utf-8")

    def check(self, number: int = 27) -> sl.PrReport:
        return sl.check_pr(self.gh, number, config(), self.workflow)

    def rules(self, report: sl.PrReport) -> dict[str, sl.Check]:
        return {c.rule: c for c in report.checks}

    def test_r3_holds_27_to_the_highest_carried_label(self) -> None:
        self.gh.label(24, "release:minor")
        self.gh.label(26, "release:patch")
        self.gh.label(27, "release:patch")
        report = self.check()
        r3 = self.rules(report)["R3"]
        self.assertFalse(r3.ok)
        self.assertIn("release:patch is below release:minor carried by #24", r3.message)
        self.gh.label(27, "release:minor")
        report = self.check()
        self.assertTrue(report.ok, report.lines())

    def test_unlabelled_carried_prs_are_listed_and_set_no_floor(self) -> None:
        self.gh.label(27, "release:patch")
        r3 = self.rules(self.check())["R3"]
        self.assertTrue(r3.ok)
        self.assertIn("#24 (no release label; sets no floor; from the title)", r3.message)

    def test_r1_needs_exactly_one_label(self) -> None:
        report = self.check()
        rules = self.rules(report)
        self.assertFalse(rules["R1"].ok)
        self.assertTrue(rules["R2"].ok and "skipped" in rules["R2"].message)
        self.gh.label(27, "release:minor", "release:patch")
        self.assertFalse(self.rules(self.check())["R1"].ok)

    def test_r2_none_if_and_only_if_nothing_shipped(self) -> None:
        self.gh.label(27, "release:none")
        r2 = self.rules(self.check())["R2"]
        self.assertFalse(r2.ok)
        self.assertIn("pantry_planner/flow.py", r2.message)
        self.gh.files[27] = [{"filename": "README.md"}, {"filename": "tests/test_flow.py"}]
        self.assertTrue(self.rules(self.check())["R2"].ok)
        self.gh.label(27, "release:minor")
        r2 = self.rules(self.check())["R2"]
        self.assertFalse(r2.ok)
        self.assertIn("use release:none", r2.message)
        # A rename out of a shipped directory counts as touching it.
        self.gh.label(27, "release:none")
        self.gh.files[27] = [{"filename": "docs/old.py",
                              "previous_filename": "pantry_planner/old.py"}]
        self.assertFalse(self.rules(self.check())["R2"].ok)

    def test_r3_is_skipped_for_a_stacked_pr(self) -> None:
        self.gh.label(28, "release:minor")
        self.gh.files[28] = [{"filename": "pantry_planner/timings.py"}]
        r3 = self.rules(self.check(28))["R3"]
        self.assertTrue(r3.ok)
        self.assertIn("the base is land/recipe-links-gemini", r3.message)

    def test_r4_reports_a_shipped_path_the_build_ignores(self) -> None:
        self.gh.label(27, "release:minor")
        report = sl.check_pr(self.gh, 27, config(shipped=SHIPPED["api"] + ["skills/**"]),
                             self.workflow)
        r4 = self.rules(report)["R4"]
        self.assertFalse(r4.ok)
        self.assertIn("skills/**", r4.message)
        report = sl.check_pr(self.gh, 27, config(workflow=None), None)
        self.assertIn("skipped", self.rules(report)["R4"].message)

    def test_the_cli_is_advisory_only_when_asked(self) -> None:
        tmp = tempfile.mkdtemp()
        cfg = Path(tmp, "versioning.json")
        cfg.write_text(json.dumps({"component": "pantry-api", "baseline": "0.1.0",
                                   "shipped_paths": SHIPPED["api"], "build_workflow": None}))
        with mock.patch.object(sl, "_github", return_value=self.gh), \
                mock.patch.dict(os.environ, {"GITHUB_OUTPUT": "", "GITHUB_STEP_SUMMARY": ""}):
            out = io.StringIO()
            with redirect_stdout(out):
                strict = sl.main(["check-pr", "--pr", "27", "--config", str(cfg)])
                advisory = sl.main(["check-pr", "--pr", "27", "--config", str(cfg), "--advisory"])
        self.assertEqual((strict, advisory), (1, 0))
        self.assertIn("::error title=release-label R1::", out.getvalue())
        self.assertIn("::warning title=release-label R1::", out.getvalue())

    def test_the_prediction_counts_this_label_against_the_highest_tag(self) -> None:
        self.gh.label(27, "release:minor")
        self.gh.refs = {"v0.4.2": {}, "v0.10.0": {}, "vNext": {}}
        line = sl.prediction(self.gh, config(), "minor", None)  # type: ignore[arg-type]
        self.assertEqual(line, "pantry-api: v0.10.0 -> v0.11.0 if this merges next (minor) "
                               "(PRs merged since the last tag are not counted here)")
        self.gh.refs = {}
        self.assertEqual(sl.prediction(self.gh, config(), "none", None),  # type: ignore[arg-type]
                         "pantry-api: no tag yet, nothing to release if this merges next")

    def test_usage_errors_exit_2(self) -> None:
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "", "GITHUB_EVENT_PATH": ""}), \
                redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(sl.main(["check-pr", "--pr", "1"]), 2)
            self.assertEqual(sl.main(["nonsense"]), 2)


# --- plan on a real git repository ---------------------------------------------------------------


class Repo:
    """A throwaway repository whose commits read like squash merges ('Subject (#N)')."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
                               *args], cwd=self.dir, capture_output=True, text=True,
                              check=True).stdout.strip()

    def commit(self, subject: str, *paths: str) -> str:
        for p in paths:
            f = self.dir / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f.read_text() + "x\n" if f.exists() else "x\n")
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", subject)
        return self.git("rev-parse", "HEAD")

    def tag(self, name: str, ref: str = "HEAD") -> None:
        self.git("tag", name, ref)


GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = Repo()
        self.gh = FakeGitHub()

    def pr(self, number: int, title: str, *labels: str) -> None:
        self.gh.pulls[str(number)] = {
            "number": number, "title": title, "state": "closed",
            "merged_at": "2026-10-08T00:00:00Z",
            "base": {"ref": "main"}, "head": {"ref": f"feat/{number}"},
            "labels": [{"name": n} for n in labels]}

    def merge(self, number: int, title: str, paths: list[str], *labels: str) -> str:
        self.pr(number, title, *labels)
        return self.repo.commit(f"{title} (#{number})", *paths)

    def plan(self, **kwargs: Any) -> dict[str, Any]:
        return sl.plan_release(sl.Git(self.repo.dir), self.gh, config(), repo="pjvjay/pantry-api",
                               **kwargs)

    def test_the_first_release_is_the_baseline(self) -> None:
        self.merge(1, "MCP server", ["pantry_planner/mcp.py"], "release:major")
        plan = self.plan()
        self.assertEqual((plan["version"], plan["previous"], plan["mode"]),
                         ("0.1.0", None, "release"))

    def test_highest_label_wins_and_a_replaced_run_is_included(self) -> None:
        self.repo.commit("Start", "pantry_planner/a.py")
        self.repo.tag("v0.4.2")
        self.merge(5, "Fix the trip total", ["pantry_planner/a.py"], "release:patch")
        # The run for #5 is still pending when #6 merges and replaces it; #6's plan covers both.
        self.merge(6, "Meal plan endpoints", ["pantry_planner/mealplan.py"], "release:minor")
        self.merge(7, "README: releases", ["README.md"], "release:none")
        plan = self.plan()
        self.assertEqual((plan["previous"], plan["version"], plan["level"]),
                         ("0.4.2", "0.5.0", "minor"))
        self.assertEqual([p["number"] for p in plan["prs"]], [5, 6, 7])
        self.assertEqual([p["shipped"] for p in plan["prs"]], [True, True, False])

    def test_covered_by_a_tag_skips(self) -> None:
        self.merge(5, "Fix", ["pantry_planner/a.py"], "release:patch")
        self.repo.tag("v0.1.0")
        plan = self.plan()
        self.assertEqual((plan["mode"], plan["skip_reason"]), ("skip", "covered by v0.1.0"))
        older = self.repo.git("rev-parse", "HEAD")
        self.merge(6, "Feature", ["pantry_planner/b.py"], "release:minor")
        self.repo.tag("v0.2.0")
        self.assertEqual(self.plan(ref=older)["skip_reason"], "covered by v0.1.0")

    def test_an_unlabelled_pr_counts_as_patch_and_says_so(self) -> None:
        self.repo.commit("Start", "seeds/products.json")
        self.repo.tag("v0.3.0")
        self.merge(8, "Seeds: onions", ["seeds/products.json"])
        self.merge(9, "Mislabelled", ["Dockerfile"], "release:none")
        self.repo.commit("Direct push", "pantry_planner/x.py")
        plan = self.plan()
        self.assertEqual((plan["version"], plan["level"]), ("0.3.1", "patch"))
        notes = [p["note"] for p in plan["prs"]]
        self.assertEqual(notes, [
            "no release label; counted as patch",
            "labelled release:none but changed shipped paths; counted as patch",
            "no PR found (a direct push); counted as patch"])

    def test_nothing_shipped_releases_nothing(self) -> None:
        self.repo.commit("Start", "pantry_planner/a.py")
        self.repo.tag("v0.3.0")
        self.merge(10, "CI tweak", [".github/workflows/build.yml"], "release:minor")
        plan = self.plan()
        self.assertEqual((plan["mode"], plan["skip_reason"]),
                         ("skip", "nothing shipped since v0.3.0"))
        self.assertIn("nothing shipped changed", plan["prs"][0]["note"])

    def test_a_dispatch_override_is_recorded(self) -> None:
        self.repo.commit("Start", "pantry_planner/a.py")
        self.repo.tag("v0.3.0")
        self.merge(11, "Fix", ["pantry_planner/a.py"], "release:patch")
        plan = self.plan(level_override="minor", override_by="@pjvjay in run 42")
        self.assertEqual(plan["version"], "0.4.0")
        self.assertEqual(plan["level_source"], "dispatch by @pjvjay in run 42 (labels said patch)")

    def test_a_major_below_1_bumps_the_minor_and_is_breaking(self) -> None:
        self.repo.commit("Start", "pantry_planner/a.py")
        self.repo.tag("v0.4.2")
        self.merge(12, "Remove plan_from_text's verbose flag", ["pantry_planner/mcp.py"],
                   "release:major")
        plan = self.plan()
        self.assertEqual((plan["version"], plan["breaking"]), ("0.5.0", True))

    def test_promote_major_and_promote_version(self) -> None:
        self.repo.commit("Start", "pantry_planner/a.py")
        self.repo.tag("v0.9.3")
        first = self.repo.git("rev-parse", "HEAD")
        plan = self.plan(promote_major=Version(1, 0, 0), override_by="@pjvjay")
        self.assertEqual((plan["version"], plan["mode"], plan["level_source"]),
                         ("1.0.0", "release", "promote_major by @pjvjay"))
        self.repo.tag("v1.0.0")
        with self.assertRaises(sl.UsageError):
            self.plan(promote_major=Version(1, 0, 0))
        self.merge(13, "Fix", ["pantry_planner/a.py"], "release:patch")
        plan = self.plan(promote_version=Version(0, 9, 3))
        self.assertEqual((plan["mode"], plan["tag"], plan["ref"]), ("promote", "v0.9.3", first))
        with self.assertRaises(sl.UsageError):
            self.plan(promote_version=Version(0, 9, 4))

    def test_a_shallow_clone_is_refused(self) -> None:
        self.merge(15, "Fix", ["pantry_planner/a.py"], "release:patch")
        self.merge(16, "Fix", ["pantry_planner/a.py"], "release:patch")
        shallow = Path(tempfile.mkdtemp()) / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{self.repo.dir}",
                        str(shallow)], check=True, capture_output=True)
        with self.assertRaises(sl.UsageError) as caught:
            sl.plan_release(sl.Git(shallow), self.gh, config())
        self.assertIn("fetch-depth: 0", str(caught.exception))

    def test_a_floor_raises_the_level(self) -> None:
        self.repo.commit("Start", "pantry_planner/a.py")
        self.repo.tag("v0.2.0")
        self.merge(14, "Fix", ["pantry_planner/a.py"], "release:patch")
        plan = self.plan(floor="minor")
        self.assertEqual((plan["version"], plan["level"]), ("0.3.0", "minor"))
        self.assertIn("floor from the component versions", plan["level_source"])


# --- notes ---------------------------------------------------------------------------------------

DIGEST = "sha256:" + "ab" * 32


class NotesTests(unittest.TestCase):
    def plan(self, **changes: Any) -> dict[str, Any]:
        plan = {"component": "pantry-api", "repo": "pjvjay/pantry-api", "image":
                "ghcr.io/pjvjay/pantry-api", "ref": "f" * 40, "previous": "0.4.2",
                "previous_tag": "v0.4.2", "version": "0.5.0", "tag": "v0.5.0", "level": "major",
                "level_source": "labels", "mode": "release", "skip_reason": None,
                "breaking": True, "prs": [
                    {"number": 12, "title": "Remove a tool", "sha": "1" * 40, "label":
                     "release:major", "level": "major", "shipped": True, "note": None},
                    {"number": 13, "title": "Fix totals", "sha": "2" * 40, "label": None,
                     "level": "patch", "shipped": True,
                     "note": "no release label; counted as patch"},
                    {"number": 14, "title": "Docs", "sha": "3" * 40, "label": "release:none",
                     "level": "none", "shipped": False, "note": None}]}
        plan.update(changes)
        return plan

    def test_notes_cite_prs_shas_the_run_and_the_digest(self) -> None:
        text = sl.notes(self.plan(), repo="pjvjay/pantry-api",
                        run_url="https://github.com/pjvjay/pantry-api/actions/runs/7",
                        image="ghcr.io/pjvjay/pantry-api", digest=DIGEST)
        self.assertIn(f"Digest: `{DIGEST}`", text)
        self.assertIn("Build run: https://github.com/pjvjay/pantry-api/actions/runs/7", text)
        self.assertIn("compare/v0.4.2...v0.5.0", text)
        self.assertIn("- Remove a tool (#12, 1111111)", text)
        self.assertIn("- #13: no release label; counted as patch", text)
        headings = [line for line in text.splitlines() if line.startswith("### ")]
        self.assertEqual(headings, ["### Breaking", "### Fixes", "### Not shipped", "### Notes"])
        self.assertIn("0.x policy", text)

    def test_a_dispatched_major_still_gets_the_breaking_heading(self) -> None:
        plan = self.plan(level_source="dispatch by @pjvjay (labels said patch)",
                         prs=self.plan()["prs"][1:])
        text = sl.notes(plan, repo="pjvjay/pantry-api")
        self.assertIn("### Breaking\nBelow 1.0.0", text)
        self.assertIn("- Major by dispatch by @pjvjay", text)

    def test_a_skip_has_no_notes(self) -> None:
        with self.assertRaises(sl.UsageError):
            sl.notes(self.plan(mode="skip", version=None), repo="pjvjay/pantry-api")


# --- platform-level ------------------------------------------------------------------------------


def release_set(api: str, db: str, gitops: str = "a" * 40) -> dict[str, Any]:
    return {"components": {
        "pantry-api": {"version": api, "tag": f"v{api}", "digest": DIGEST},
        "pantry-db": {"version": db, "tag": f"v{db}", "digest": DIGEST}},
        "deploy": {"pantry-gitops": {"commit": gitops}}}


class PlatformLevelTests(unittest.TestCase):
    def test_version_distance(self) -> None:
        self.assertEqual(sl.version_distance("0.4.2", "0.4.2"), "none")
        self.assertEqual(sl.version_distance("0.4.2", "0.4.3"), "patch")
        self.assertEqual(sl.version_distance("0.4.2", "0.5.0"), "minor")
        self.assertEqual(sl.version_distance("0.9.3", "1.0.0"), "major")
        self.assertEqual(sl.version_distance("0.5.0", "0.4.2"), "patch")  # a rollback
        self.assertEqual(sl.version_distance(None, "0.1.0"), "minor")
        self.assertEqual(sl.version_distance("0.1.0", None), "major")

    def test_the_train_floor_and_rows(self) -> None:
        level, rows = sl.platform_level(release_set("0.4.2", "0.3.1"),
                                        release_set("0.5.0", "0.3.1", gitops="b" * 40))
        self.assertEqual(level, "minor")
        by_name = {r["component"]: r for r in rows}
        self.assertEqual(by_name["pantry-api"]["change"], "0.4.2 → 0.5.0 (minor)")
        self.assertEqual(by_name["pantry-db"]["change"], "unchanged since v0.3.1")
        self.assertEqual(by_name["pantry-gitops"]["change"], "from aaaaaaa")
        level, _ = sl.platform_level(None, release_set("0.1.0", "0.1.0"))
        self.assertEqual(level, "minor")
        level, _ = sl.platform_level(release_set("0.1.0", "0.1.0"), release_set("0.1.0", "0.1.0"))
        self.assertEqual(level, "none")


# --- reserve-tag ---------------------------------------------------------------------------------


class ReserveTagTests(unittest.TestCase):
    SHA = "c" * 40

    def test_created_then_a_rerun_on_the_same_commit_continues(self) -> None:
        gh = FakeGitHub()
        self.assertIn("created v0.2.0", sl.reserve_tag(gh, "v0.2.0", self.SHA))
        self.assertEqual(gh.posts, [("git/refs", {"ref": "refs/tags/v0.2.0", "sha": self.SHA})])
        gh.fail_post = 422
        self.assertIn("a rerun", sl.reserve_tag(gh, "v0.2.0", self.SHA))

    def test_a_422_on_another_commit_fails(self) -> None:
        gh = FakeGitHub()
        gh.refs["v0.2.0"] = {"type": "commit", "sha": "d" * 40}
        gh.fail_post = 422
        with self.assertRaises(sl.TagConflict):
            sl.reserve_tag(gh, "v0.2.0", self.SHA)
        # An annotated tag is followed to its commit.
        gh.refs["v0.2.0"] = {"type": "tag", "sha": "e" * 40}
        gh.tag_objects["e" * 40] = self.SHA
        self.assertIn("a rerun", sl.reserve_tag(gh, "v0.2.0", self.SHA))

    def test_other_errors_and_bad_input(self) -> None:
        gh = FakeGitHub()
        gh.fail_post = 403
        with self.assertRaises(GitHubError):
            sl.reserve_tag(gh, "v0.2.0", self.SHA)
        for tag, sha in [("0.2.0", self.SHA), ("v0.2", self.SHA), ("v0.2.0", "abc")]:
            with self.assertRaises(sl.UsageError):
                sl.reserve_tag(FakeGitHub(), tag, sha)


class ActionTests(unittest.TestCase):
    """action.yml's shell step, run with bash the way a runner runs a composite step."""

    def setUp(self) -> None:
        self.text = (HERE.parent / "action.yml").read_text(encoding="utf-8")
        self.tmp = Path(tempfile.mkdtemp())

    def run_step(self, cwd: Path, **inputs: str) -> tuple[subprocess.CompletedProcess[str],
                                                          dict[str, str]]:
        block = self.text.split("      run: |\n", 1)[1]
        script = "\n".join(line[8:] for line in block.splitlines())
        outputs = self.tmp / "github-output"
        outputs.write_text("")
        # The runner sets every SL_ variable, empty when the input is; so does this harness.
        env = {**os.environ, **{name: "" for name in re.findall(r"(SL_[A-Z_]+):", self.text)},
               **GIT_ENV, "GITHUB_ACTION_PATH": str(HERE.parent),
               "GITHUB_OUTPUT": str(outputs), "GITHUB_REPOSITORY": "", "GITHUB_TOKEN": "",
               "SL_REF": "HEAD", "SL_CONFIG": ".github/versioning.json",
               "SL_PLAN_FILE": str(self.tmp / "plan.json"),
               "SL_NOTES_FILE": str(self.tmp / "notes.md")}
        env.update({f"SL_{k.upper()}": v for k, v in inputs.items()})
        proc = subprocess.run(["bash", "-c", script], cwd=cwd, env=env, capture_output=True,
                              text=True, check=False)
        values = dict(line.split("=", 1) for line in outputs.read_text().splitlines() if line)
        return proc, values

    def test_every_input_reaches_the_step_and_nothing_else_does(self) -> None:
        inputs_block = self.text.split("\ninputs:\n", 1)[1].split("\noutputs:\n", 1)[0]
        declared = set(re.findall(r"^  ([a-z-]+):$", inputs_block, re.MULTILINE))
        used = set(re.findall(r"\$\{\{ inputs\.([a-z-]+) \}\}", self.text))
        self.assertEqual(declared, used)
        self.assertNotIn("${{ inputs", self.text.split("      run: |", 1)[1])

    def test_platform_level_and_plan_write_their_outputs(self) -> None:
        old, new = self.tmp / "old.json", self.tmp / "new.json"
        old.write_text(json.dumps(release_set("0.4.2", "0.3.1")))
        new.write_text(json.dumps(release_set("0.5.0", "0.3.1")))
        proc, values = self.run_step(self.tmp, command="platform-level", release_set=str(new),
                                     previous_set=str(old))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(values["level"], "minor")

        with mock.patch.dict(os.environ, GIT_ENV):
            repo = Repo()
            (repo.dir / ".github").mkdir()
            (repo.dir / ".github" / "versioning.json").write_text(json.dumps(
                {"component": "pantry-api", "baseline": "0.1.0", "shipped_paths": ["src/**"]}))
            repo.commit("First (#1)", "src/a.py")
        proc, values = self.run_step(repo.dir, command="plan", floor="minor", by="@pjvjay")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((values["version"], values["tag"], values["mode"], values["skip"]),
                         ("0.1.0", "v0.1.0", "release", "false"))
        self.assertEqual(json.loads((self.tmp / "plan.json").read_text())["version"], "0.1.0")

    def test_an_unknown_command_fails(self) -> None:
        proc, _ = self.run_step(self.tmp, command="publish")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unknown command 'publish'", proc.stdout)


class ConfigTests(unittest.TestCase):
    def test_the_platforms_versioning_json_loads(self) -> None:
        path = HERE.parents[3] / ".github" / "versioning.json"
        if not path.exists():
            self.skipTest("not inside pantry-platform")
        cfg = Config.load(path)
        self.assertEqual(cfg.component, "pantry-platform")
        self.assertIsNone(cfg.build_workflow)
        self.assertTrue(sl.matches_any("demo-hub/demo_hub/app.py", cfg.shipped_paths))
        self.assertTrue(sl.matches_any("release-set.json", cfg.shipped_paths))
        self.assertFalse(sl.matches_any("RELEASING.md", cfg.shipped_paths))

    def test_the_pr_templates_lands_line_carries_nothing_until_filled_in(self) -> None:
        path = HERE.parents[3] / ".github" / "pull_request_template.md"
        if not path.exists():
            self.skipTest("not inside pantry-platform")
        template = path.read_text(encoding="utf-8")
        self.assertIn("\nLands: ", template)
        self.assertEqual(sl.lands_numbers(template), [])
        filled = template.replace("Lands: <!--", "Lands: #24, #26 <!--")
        self.assertEqual(sl.lands_numbers(filled), [24, 26])

    def test_bad_configs_are_usage_errors(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        for raw in [{}, {"component": "x", "baseline": "0.1", "shipped_paths": ["a"]},
                    {"component": "x", "baseline": "0.1.0", "shipped_paths": []}]:
            (tmp / "v.json").write_text(json.dumps(raw))
            with self.assertRaises(sl.UsageError, msg=raw):
                Config.load(tmp / "v.json")


if __name__ == "__main__":
    unittest.main()
