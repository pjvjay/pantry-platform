#!/usr/bin/env python3
"""Release labels to version numbers for the pantry repos.

Every PR into main carries one release label: release:major, release:minor, release:patch or
release:none. This file turns those labels into versions:

    check-pr        rules R1-R4 on a pull request, plus a version prediction
    plan            the next vX.Y.Z from the last tag and the labels of the PRs merged since
    notes           release notes that cite only PRs, commits, the build run and the digest
    platform-level  how far a release set moved from the previous one (the train's floor)
    reserve-tag     create the git tag through the API; a rerun on the same commit is fine

It is stdlib only on purpose. The release jobs hold write credentials (packages, contents and
the gitops PAT), so nothing is installed into them. RELEASING.md at the root of
pjvjay/pantry-platform is the process this implements; the rules and the 0.x policy are
described there in prose.

    python3 semver_labels.py check-pr --pr 27 --config .github/versioning.json
    python3 semver_labels.py plan --config .github/versioning.json --out plan.json
    python3 semver_labels.py notes --plan plan.json --image ghcr.io/pjvjay/pantry-api \\
        --digest sha256:... --run-url https://github.com/.../actions/runs/1
    python3 semver_labels.py platform-level --previous-set old.json --set release-set.json
    python3 semver_labels.py reserve-tag --tag v0.2.0 --sha <40-hex>

Exit codes: 0 ok, 1 a rule was broken or GitHub refused, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LEVELS = ("none", "patch", "minor", "major")
LABEL_PREFIX = "release:"
RELEASE_LABELS = tuple(LABEL_PREFIX + level for level in LEVELS)
VERSION_RE = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class UsageError(Exception):
    """Bad input from the caller (exit 2)."""


# --- versions and levels -------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @property
    def tag(self) -> str:
        return f"v{self}"


BASELINE = Version(0, 1, 0)


def parse_version(text: str) -> Version:
    """'0.4.2' or 'v0.4.2'. Pre-release and build suffixes are refused: these repos never cut
    them, and a tag like v1.0.0-rc1 sorting below v1.0.0 is a trap nobody needs."""
    match = VERSION_RE.match(text.strip())
    if not match:
        raise ValueError(f"not a version: {text!r} (expected X.Y.Z)")
    return Version(*(int(part) for part in match.groups()))


def rank(level: str | None) -> int:
    return LEVELS.index(level) if level in LEVELS else -1


def highest(levels: Iterable[str | None]) -> str:
    best = "none"
    for level in levels:
        if rank(level) > rank(best):
            best = level  # type: ignore[assignment]
    return best


def bump(current: Version | None, level: str, *, baseline: Version | None = None,
         promote_major: Version | None = None) -> Version | None:
    """The next version, or None when nothing is released.

    The 0.x policy: below 1.0.0 a major change bumps the minor (0.4.2 -> 0.5.0) and the notes put
    a Breaking heading first, so a label can never make 1.0.0 by accident. Reaching 1.0.0 takes
    promote_major, which the owner passes by hand on a dispatch. From 1.0.0 on it is plain semver.
    The first release of a component with no tag is its baseline, whatever the level.
    """
    if level not in LEVELS:
        raise ValueError(f"unknown level {level!r}")
    if promote_major is not None:
        if promote_major.minor or promote_major.patch or promote_major.major < 1:
            raise ValueError(f"promote_major must be N.0.0 with N >= 1, not {promote_major}")
        expected = current.major + 1 if current is not None else promote_major.major
        if promote_major.major != expected:
            raise ValueError(f"promote_major {promote_major} does not follow {current}: "
                             f"the next major is {expected}.0.0")
        return promote_major
    if level == "none":
        return None
    if current is None:
        return baseline or BASELINE
    if current.major == 0:
        if level in ("major", "minor"):
            return Version(0, current.minor + 1, 0)
        return Version(0, current.minor, current.patch + 1)
    if level == "major":
        return Version(current.major + 1, 0, 0)
    if level == "minor":
        return Version(current.major, current.minor + 1, 0)
    return Version(current.major, current.minor, current.patch + 1)


def level_from_labels(names: Iterable[str]) -> tuple[str | None, str | None]:
    """(level, problem). R1: exactly one known release label. With two, the level is the highest
    so a prediction can still be shown, but the problem is reported."""
    release = sorted({n for n in names if n.startswith(LABEL_PREFIX)})
    unknown = [n for n in release if n not in RELEASE_LABELS]
    known = [n[len(LABEL_PREFIX):] for n in release if n in RELEASE_LABELS]
    if unknown:
        problem = (f"unknown release label {', '.join(unknown)} "
                   f"(use one of {', '.join(RELEASE_LABELS)})")
        return (highest(known) if known else None), problem
    if not known:
        return None, f"no release label (add one of {', '.join(RELEASE_LABELS)})"
    if len(known) > 1:
        return highest(known), f"{len(known)} release labels ({', '.join(release)}); keep one"
    return known[0], None


# --- configuration and paths ---------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """A repo's .github/versioning.json."""

    component: str
    image: str | None
    baseline: Version
    shipped_paths: tuple[str, ...]
    # The workflow whose push.paths must cover shipped_paths (R4). None skips R4, for a repo
    # that is released by something other than a push-triggered build (the platform train).
    build_workflow: str | None = ".github/workflows/build.yml"

    @staticmethod
    def load(path: str | Path) -> Config:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise UsageError(f"cannot read {path}: {exc}") from exc
        missing = [k for k in ("component", "baseline", "shipped_paths") if k not in raw]
        if missing:
            raise UsageError(f"{path} is missing {', '.join(missing)}")
        paths = raw["shipped_paths"]
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p
                                                                for p in paths):
            raise UsageError(f"{path}: shipped_paths must be a non-empty list of paths")
        try:
            baseline = parse_version(raw["baseline"])
        except ValueError as exc:
            raise UsageError(f"{path}: {exc}") from exc
        return Config(component=raw["component"], image=raw.get("image"), baseline=baseline,
                      shipped_paths=tuple(paths),
                      build_workflow=raw.get("build_workflow", Config.build_workflow))


def glob_regex(pattern: str) -> re.Pattern[str]:
    """GitHub Actions path-filter semantics: ** crosses directories, * and ? do not."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(glob_regex(p).fullmatch(path) for p in patterns if not p.startswith("!"))


def push_paths(workflow_text: str) -> list[str] | None:
    """on.push.paths of a workflow file, without a YAML library. Handles the block list these
    repos use and the flow form (paths: ["a", "b"]). None when there is no push.paths at all."""
    lines = workflow_text.splitlines()

    def indent(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def key(line: str) -> str:
        return line.split("#", 1)[0].strip()

    def child_block(start: int, name: str, parent_indent: int) -> int | None:
        for j in range(start, len(lines)):
            text = key(lines[j])
            if not text:
                continue
            if indent(lines[j]) <= parent_indent:
                return None
            if ":" in text and text.split(":", 1)[0].strip().strip("'\"") == name:
                return j
        return None

    on_line = next((i for i, line in enumerate(lines)
                    if indent(line) == 0 and ":" in key(line)
                    and key(line).split(":", 1)[0].strip("'\"") in ("on", "true")), None)
    if on_line is None:
        return None
    push = child_block(on_line + 1, "push", 0)
    if push is None:
        return None
    paths = child_block(push + 1, "paths", indent(lines[push]))
    if paths is None:
        return None
    inline = key(lines[paths]).split(":", 1)[1].strip()
    if inline.startswith("["):
        return [item.strip().strip("'\"") for item in inline.strip("[]").split(",") if item.strip()]
    found = []
    for line in lines[paths + 1:]:
        text = key(line)
        if not text:
            continue
        if not text.startswith("- ") or indent(line) < indent(lines[paths]):
            break
        found.append(text[2:].strip().strip("'\""))
    return found


def uncovered(shipped: Iterable[str], pushed: Iterable[str]) -> list[str]:
    """R4: every shipped path must also trigger the build, or a release could be planned for a
    change that never built an image. A shipped pattern is covered when a push pattern equals it
    or matches it as a path (src/** covers src/foo/** and src/main.ts)."""
    pushed = [p for p in pushed if not p.startswith("!")]
    return [s for s in shipped if not any(p == s or glob_regex(p).fullmatch(s) for p in pushed)]


# --- GitHub --------------------------------------------------------------------------------------


class GitHubError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


class GitHub:
    """The few REST calls the rules need, over urllib. Paths without a leading slash are relative
    to /repos/<repo>/."""

    def __init__(self, repo: str, token: str, api: str = "https://api.github.com") -> None:
        self.repo, self.token, self.api = repo, token, api.rstrip("/")

    def _url(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = path if path.startswith("http") else (
            f"{self.api}{path}" if path.startswith("/") else f"{self.api}/repos/{self.repo}/{path}")
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        return url

    def request(self, method: str, path: str, body: Any = None,
                params: dict[str, Any] | None = None) -> tuple[Any, dict[str, str]]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self._url(path, params), data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "pantry-semver-labels")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return (json.loads(raw) if raw else None), dict(resp.headers.items())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("message", detail)
            except ValueError:
                pass
            raise GitHubError(exc.code, str(detail)[:300]) from None

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params or None)[0]

    def post(self, path: str, body: Any) -> Any:
        return self.request("POST", path, body=body)[0]

    def paginate(self, path: str, max_pages: int = 10, **params: Any) -> list[Any]:
        params.setdefault("per_page", 100)
        url: str | None = self._url(path, params)
        items: list[Any] = []
        for _ in range(max_pages):
            if url is None:
                break
            page, headers = self.request("GET", url)
            items.extend(page or [])
            url = _next_link(headers.get("Link") or headers.get("link") or "")
        return items


def _next_link(link_header: str) -> str | None:
    for part in link_header.split(","):
        if 'rel="next"' in part:
            return part.split(";", 1)[0].strip().strip("<>")
    return None


# --- R3: the PRs a pull request carries ----------------------------------------------------------

HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
TITLE_LAND_RE = re.compile(r"^\s*land\s+(#\d+(?:\s*(?:,\s*and|,|and|&|\+)\s*#\d+)*)",
                           re.IGNORECASE)
SUBJECT_PR_RE = re.compile(r"\(#(\d+)\)\s*$")


def lands_numbers(body: str | None) -> list[int]:
    """PR numbers on a 'Lands: #24, #26' line. Template comments are dropped first, so the PR
    template's own example never counts; markdown emphasis and list bullets are allowed."""
    found: list[int] = []
    for line in HTML_COMMENT_RE.sub("", body or "").splitlines():
        text = re.sub(r"[*_`]", "", line).strip().lstrip("->").strip()
        if text.lower().startswith("lands:"):
            found += [int(n) for n in re.findall(r"#(\d+)", text[len("lands:"):])]
    return _unique(found)


def title_numbers(title: str | None) -> list[int]:
    """'Land #24 and #26 on main: ...' -> [24, 26]."""
    match = TITLE_LAND_RE.match(title or "")
    return _unique(int(n) for n in re.findall(r"#(\d+)", match.group(1))) if match else []


def subject_numbers(commits: Iterable[dict[str, Any]]) -> list[tuple[int, str]]:
    """(number, sha) for each commit subject ending in '(#N)', the squash-merge form. A merge
    commit's 'Merge pull request #N' is deliberately not read: the repos squash-merge, and on a
    land branch like pantry-api #27 no such subject exists anyway."""
    found = []
    for commit in commits:
        subject = (commit.get("commit", {}).get("message") or "").splitlines()[:1]
        match = SUBJECT_PR_RE.search(subject[0]) if subject else None
        if match:
            found.append((int(match.group(1)), commit.get("sha", "")))
    return found


def _unique(numbers: Iterable[int]) -> list[int]:
    seen: list[int] = []
    for n in numbers:
        if n not in seen:
            seen.append(n)
    return seen


@dataclass
class Carried:
    number: int
    sources: list[str]
    title: str = ""
    level: str | None = None
    label_problem: str | None = None
    merged_at: str | None = None
    base: str = ""
    head: str = ""
    on_default_branch: bool = False
    missing: bool = False

    @property
    def sets_floor(self) -> bool:
        return not self.missing and not self.on_default_branch and self.level is not None

    def describe(self) -> str:
        if self.missing:
            state = "not a pull request in this repo; ignored"
        elif self.on_default_branch:
            state = f"already merged into {self.base}; sets no floor"
        elif self.level is None:
            state = "no release label; sets no floor"
        else:
            state = f"release:{self.level}"
        if not self.missing and not self.merged_at:
            state = "open, " + state
        return f"#{self.number} ({state}; from {', '.join(self.sources)})"


def discover_carried(gh: Any, pr: dict[str, Any], commits: list[dict[str, Any]],
                     max_refs: int = 40) -> list[Carried]:
    """Every PR whose changes this PR brings into its base, in the order they were found.

    Seeds come from the 'Lands:' line, a 'Land #N and #M' title and '(#N)' commit subjects.
    Then, recursively, any merged PR whose base is the head branch of this PR or of a carried PR.
    For a carried PR that has itself merged, only PRs merged into its head before it merged count:
    anything merged into that branch later never reached its merge. (That is why pantry-api #26,
    merged into feat/recipe-links 27 s after #24 left it, is carried by #27's Lands line and not
    by #24.)
    """
    own = pr["number"]
    default = (pr.get("base", {}).get("repo") or {}).get("default_branch") or "main"
    carried: dict[int, Carried] = {}
    queue: list[tuple[str, str | None, str]] = []
    # Base-branch links only mean something inside this repository: a fork's branch names (often
    # its own main) say nothing about which of this repo's PRs it carries.
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    base_repo = ((pr.get("base") or {}).get("repo") or {}).get("full_name")
    if head_repo == base_repo and pr["head"]["ref"] != default:
        queue.append((pr["head"]["ref"], None, "this PR's branch"))

    def add(number: int, source: str, data: dict[str, Any] | None) -> Carried | None:
        if number == own:
            return None
        if number in carried:
            if source not in carried[number].sources:
                carried[number].sources.append(source)
            return None
        if data is None:
            try:
                data = gh.get(f"pulls/{number}")
            except GitHubError as exc:
                if exc.status != 404:
                    raise
                carried[number] = Carried(number, [source], missing=True)
                return None
        level, problem = level_from_labels(lbl["name"] for lbl in data.get("labels") or [])
        if problem and level is None:
            problem = None  # an unlabelled carried PR is listed, not reported as a rule break
        entry = Carried(number, [source], title=data.get("title", ""), level=level,
                        label_problem=problem, merged_at=data.get("merged_at"),
                        base=data["base"]["ref"], head=data["head"]["ref"])
        entry.on_default_branch = bool(entry.merged_at) and entry.base == default
        carried[number] = entry
        if not entry.on_default_branch and entry.head != default:
            queue.append((entry.head, entry.merged_at, f"#{number}"))
        return entry

    for n in lands_numbers(pr.get("body")):
        add(n, "the Lands: line", None)
    for n in title_numbers(pr.get("title")):
        add(n, "the title", None)
    for n, sha in subject_numbers(commits):
        add(n, f"commit {sha[:7]}", None)

    seen: set[tuple[str, str | None]] = set()
    while queue and len(seen) < max_refs:
        ref, cutoff, via = queue.pop(0)
        if (ref, cutoff) in seen:
            continue
        seen.add((ref, cutoff))
        for data in gh.paginate("pulls", state="closed", base=ref):
            merged_at = data.get("merged_at")
            if not merged_at or (cutoff is not None and merged_at > cutoff):
                continue
            source = f"merged into {ref}"
            if via != "this PR's branch":
                source += f" (carried by {via})"
            add(data["number"], source, data)
    return list(carried.values())


# --- check-pr ------------------------------------------------------------------------------------


@dataclass
class Check:
    rule: str
    ok: bool
    message: str


@dataclass
class PrReport:
    number: int
    base: str
    head: str
    level: str | None
    checks: list[Check] = field(default_factory=list)
    carried: list[Carried] = field(default_factory=list)
    prediction: str = ""

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def lines(self) -> list[str]:
        out = [f"release-label check for #{self.number} ({self.base} <- {self.head})"]
        out += [f"  {c.rule} {'ok  ' if c.ok else 'FAIL'}  {c.message}" for c in self.checks]
        if self.prediction:
            out.append(f"  prediction  {self.prediction}")
        return out


def check_pr(gh: Any, number: int, config: Config, workflow_text: str | None,
             predict: Any = None) -> PrReport:
    """R1-R4 for one pull request. `predict(level)` returns a prediction line or ""."""
    pr = gh.get(f"pulls/{number}")
    base, head = pr["base"]["ref"], pr["head"]["ref"]
    default = (pr["base"].get("repo") or {}).get("default_branch") or "main"
    level, problem = level_from_labels(lbl["name"] for lbl in pr.get("labels") or [])
    report = PrReport(number, base, head, level)

    # R1: exactly one release label.
    report.checks.append(Check("R1", problem is None,
                               problem or f"one release label: release:{level}"))

    # R2: release:none if and only if nothing shipped changed.
    files = gh.paginate(f"pulls/{number}/files", max_pages=30)
    paths = []
    for f in files:
        paths.append(f["filename"])
        if f.get("previous_filename"):
            paths.append(f["previous_filename"])
    shipped = [p for p in paths if matches_any(p, config.shipped_paths)]
    if level is None:
        report.checks.append(Check("R2", True, "skipped until there is one release label (R1); "
                                               + (f"it changes shipped paths ({_few(shipped)})"
                                                  if shipped else "it changes no shipped path")))
    elif level == "none" and shipped:
        report.checks.append(Check("R2", False, f"release:none, but it changes shipped paths: "
                                                f"{_few(shipped)}"))
    elif level != "none" and not shipped:
        report.checks.append(Check("R2", False, f"release:{level}, but it changes no shipped path "
                                                f"({len(paths)} files); use release:none"))
    else:
        report.checks.append(Check("R2", True, f"changes shipped paths ({_few(shipped)})"
                                   if shipped else "changes no shipped path"))

    # R3: into the default branch, at least the highest label among the carried PRs.
    if base != default:
        report.checks.append(Check("R3", True, f"skipped: the base is {base}, not {default}; "
                                               f"the land PR into {default} carries this one"))
    else:
        commits = gh.paginate(f"pulls/{number}/commits", max_pages=3)
        report.carried = discover_carried(gh, pr, commits)
        floor_prs = [c for c in report.carried if c.sets_floor]
        floor = highest(c.level for c in floor_prs)
        listed = "; ".join(c.describe() for c in report.carried) or "none found"
        if level is None:
            report.checks.append(Check("R3", True, "skipped until there is one release label "
                                                   f"(R1); the floor is release:{floor}. "
                                                   f"Carried: {listed}"))
        elif floor_prs and rank(level) < rank(floor):
            top = [f"#{c.number}" for c in floor_prs if c.level == floor]
            report.checks.append(Check("R3", False, f"release:{level} is below release:{floor} "
                                                    f"carried by {', '.join(top)}. "
                                                    f"Carried: {listed}"))
        else:
            report.checks.append(Check("R3", True, f"carried: {listed}"))

    # R4: shipped_paths is a subset of the build's push.paths.
    if config.build_workflow is None:
        report.checks.append(Check("R4", True, "skipped: versioning.json names no build workflow "
                                               "(this repo is released by its train)"))
    elif workflow_text is None:
        report.checks.append(Check("R4", False, f"cannot read {config.build_workflow}"))
    else:
        pushed = push_paths(workflow_text)
        if pushed is None:
            report.checks.append(Check("R4", False,
                                       f"{config.build_workflow} has no on.push.paths"))
        else:
            missing = uncovered(config.shipped_paths, pushed)
            where = f"{config.build_workflow} push.paths"
            report.checks.append(Check("R4", not missing,
                                       f"shipped_paths not in {where}: {', '.join(missing)}"
                                       if missing else f"shipped_paths are all in {where}"))

    if predict is not None and base == default and level is not None:
        report.prediction = predict(level)
    return report


def _few(paths: list[str], limit: int = 4) -> str:
    more = f" and {len(paths) - limit} more" if len(paths) > limit else ""
    return ", ".join(paths[:limit]) + more


# --- plan ----------------------------------------------------------------------------------------


class Git:
    def __init__(self, cwd: str | Path = ".") -> None:
        self.cwd = str(cwd)

    def run(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.cwd, capture_output=True, text=True,
                              check=True).stdout

    def rev_parse(self, ref: str) -> str:
        return self.run("rev-parse", f"{ref}^{{commit}}").strip()

    def is_shallow(self) -> bool:
        return self.run("rev-parse", "--is-shallow-repository").strip() == "true"

    def version_tags(self, *selector: str) -> list[tuple[Version, str]]:
        tags = []
        for name in self.run("tag", *selector, "--list", "v*").split():
            if VERSION_RE.match(name):
                tags.append((parse_version(name), name))
        return sorted(tags)

    def first_parent(self, since: str | None, ref: str) -> list[tuple[str, str]]:
        span = f"{since}..{ref}" if since else ref
        out = self.run("log", "--first-parent", "--reverse", "--format=%H%x00%s", span)
        pairs = [line.split("\0", 1) for line in out.splitlines() if line]
        return [(sha, subject) for sha, subject in pairs]

    def changed_paths(self, sha: str) -> list[str]:
        parents = self.run("rev-list", "--parents", "-n", "1", sha).split()[1:]
        if not parents:
            return self.run("ls-tree", "-r", "--name-only", sha).splitlines()
        return self.run("diff", "--name-only", "--no-renames", parents[0], sha).splitlines()


def pr_for_commit(gh: Any, sha: str, subject: str, default: str) -> dict[str, Any] | None:
    """The PR a first-parent commit came from: GitHub's commit-to-PR link, else the '(#N)' that a
    squash merge leaves on the subject."""
    if gh is None:
        return None
    try:
        pulls = gh.get(f"commits/{sha}/pulls") or []
    except GitHubError:
        pulls = []
    for pull in pulls:
        if pull.get("merge_commit_sha") == sha:
            return pull
    for pull in pulls:
        if pull.get("merged_at") and pull["base"]["ref"] == default:
            return pull
    match = SUBJECT_PR_RE.search(subject)
    if match:
        try:
            return gh.get(f"pulls/{match.group(1)}")
        except GitHubError:
            return None
    return None


def plan_release(git: Git, gh: Any, config: Config, *, ref: str = "HEAD",
                 default_branch: str = "main", level_override: str | None = None,
                 override_by: str = "", promote_major: Version | None = None,
                 promote_version: Version | None = None, floor: str = "none",
                 repo: str = "") -> dict[str, Any]:
    """What to release at `ref`. The version is decided here, before anything is built.

    It covers every first-parent commit since the highest tag merged into `ref`, so when a newer
    run replaces a pending one (the release concurrency group keeps only the newest), nothing is
    lost: the newer run's plan includes the older merge.
    """
    # A shallow clone has no tags and no history, so every plan would be the baseline again and
    # the tag reservation would fail on another commit. Refuse up front instead.
    if git.is_shallow():
        raise UsageError("plan needs the full history and tags (actions/checkout fetch-depth: 0)")
    head = git.rev_parse(ref)
    merged = git.version_tags("--merged", head)
    previous = merged[-1] if merged else None
    plan: dict[str, Any] = {
        "component": config.component, "repo": repo, "image": config.image, "ref": head,
        "previous": str(previous[0]) if previous else None,
        "previous_tag": previous[1] if previous else None,
        "version": None, "tag": None, "level": "none", "level_source": "labels",
        "mode": "skip", "skip_reason": None, "breaking": False, "prs": [],
    }

    if promote_version is not None:
        all_tags = {name for _, name in git.version_tags()}
        if promote_version.tag not in all_tags:
            raise UsageError(f"promote_version {promote_version}: tag {promote_version.tag} "
                             f"does not exist")
        plan.update(version=str(promote_version), tag=promote_version.tag, mode="promote",
                    level="none", level_source=f"promote_version{_by(override_by)}",
                    ref=git.rev_parse(promote_version.tag))
        return plan

    if promote_major is not None:
        newest = git.version_tags()
        if newest and newest[-1][0] >= promote_major:
            raise UsageError(f"promote_major {promote_major}: {newest[-1][1]} already exists")
    containing = [name for _, name in git.version_tags("--contains", head)]
    if containing and promote_major is None:
        plan["skip_reason"] = f"covered by {containing[0]}"
        return plan

    level = "none"
    for sha, subject in git.first_parent(previous[1] if previous else None, head):
        touched = git.changed_paths(sha)
        shipped = [p for p in touched if matches_any(p, config.shipped_paths)]
        pull = pr_for_commit(gh, sha, subject, default_branch)
        entry: dict[str, Any] = {"number": pull["number"] if pull else None,
                                 "title": pull["title"] if pull else subject, "sha": sha,
                                 "label": None, "level": "none", "shipped": bool(shipped),
                                 "note": None}
        if pull:
            labelled, problem = level_from_labels(lbl["name"] for lbl in pull.get("labels") or [])
            entry["label"] = f"release:{labelled}" if labelled else None
            if problem and labelled:
                entry["note"] = problem
        else:
            labelled = None
        if shipped:
            if labelled is None:
                entry["level"] = "patch"
                entry["note"] = ("no release label; counted as patch" if pull is not None
                                 else "no PR found (a direct push); counted as patch" if gh
                                 else "PR unknown (no API access); counted as patch")
            elif labelled == "none":
                entry["level"] = "patch"
                entry["note"] = "labelled release:none but changed shipped paths; counted as patch"
            else:
                entry["level"] = labelled
        elif labelled not in (None, "none"):
            entry["note"] = f"release:{labelled}, but nothing shipped changed; not counted"
        level = highest([level, entry["level"]])
        plan["prs"].append(entry)

    plan["breaking"] = any(p["level"] == "major" for p in plan["prs"])
    if level_override:
        if level_override not in LEVELS:
            raise UsageError(f"level must be one of {', '.join(LEVELS)}, not {level_override!r}")
        level = level_override
        plan["level_source"] = f"dispatch{_by(override_by)}; labels said {plan_level(plan)}"
        plan["breaking"] = plan["breaking"] or level == "major"
    if rank(floor) > rank(level):
        plan["level_source"] = f"the component versions' floor; labels said {level}"
        level = floor
        plan["breaking"] = plan["breaking"] or floor == "major"
    plan["level"] = level

    try:
        version = bump(previous[0] if previous else None, level, baseline=config.baseline,
                       promote_major=promote_major)
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    if promote_major is not None:
        plan["level_source"] = f"promote_major{_by(override_by)}"
    if version is None:
        plan["skip_reason"] = (f"nothing shipped since {previous[1]}" if previous
                               else "nothing shipped yet")
        return plan
    plan.update(version=str(version), tag=version.tag, mode="release")
    return plan


def plan_level(plan: dict[str, Any]) -> str:
    return highest(p["level"] for p in plan["prs"])


def _by(who: str) -> str:
    return f" by {who}" if who else ""


# --- notes ---------------------------------------------------------------------------------------

SECTIONS = (("major", "Breaking"), ("minor", "Features"), ("patch", "Fixes"),
            ("none", "Not shipped"))


def notes(plan: dict[str, Any], *, repo: str, run_url: str = "", image: str | None = None,
          digest: str | None = None, release_set: dict[str, Any] | None = None,
          previous_set: dict[str, Any] | None = None, server: str = "https://github.com") -> str:
    """Release notes built by code from the plan. Every line cites a PR, a commit, the run or the
    digest; nothing is summarised. train.py reads the 'Digest:' line back, so keep its form."""
    if plan.get("mode") not in ("release", "promote") or not plan.get("version"):
        raise UsageError("the plan releases nothing; there are no notes to write")
    version, tag = plan["version"], plan["tag"]
    name = plan.get("component") or repo
    out = [f"## {name} {version}", ""]
    commit = plan.get("ref", "")
    if commit:
        out.append(f"Commit: [`{commit[:7]}`]({server}/{repo}/commit/{commit})")
    if run_url:
        out.append(f"Build run: {run_url}")
    if image and digest:
        out.append(f"Image: `{image}:{version}`")
        out.append(f"Digest: `{digest}`")
    if plan.get("previous_tag"):
        out.append(f"Previous: [{plan['previous_tag']}]({server}/{repo}/compare/"
                   f"{plan['previous_tag']}...{tag})")
    out.append(f"Level: {plan['level']} ({plan['level_source']})")
    if plan.get("mode") == "promote":
        out.append("")
        out.append(f"Promoted again from the existing tag {tag}; nothing was rebuilt.")
    below_one = parse_version(version).major == 0
    by_level: dict[str, list[dict[str, Any]]] = {}
    for pr in plan.get("prs") or []:
        by_level.setdefault(pr["level"] if pr["shipped"] else "none", []).append(pr)
    for level, heading in SECTIONS:
        items = by_level.get(level) or []
        # A major set by a dispatch or a train's floor has no labelled PR, but still gets the
        # Breaking heading first, saying where the level came from.
        forced = level == "major" and plan.get("breaking") and not items
        if not items and not forced:
            continue
        out += ["", f"### {heading}"]
        if level == "major" and below_one:
            out.append("Below 1.0.0, release:major bumps the minor "
                       "(the 0.x policy in RELEASING.md).")
        if forced:
            out.append(f"- Level major from {plan['level_source']}")
        for pr in items:
            cite = f"#{pr['number']}, " if pr.get("number") else ""
            out.append(f"- {pr['title']} ({cite}{pr['sha'][:7]})")
    flagged = [p for p in plan.get("prs") or [] if p.get("note")]
    if flagged:
        out += ["", "### Notes"]
        out += [f"- {('#' + str(p['number'])) if p.get('number') else p['sha'][:7]}: {p['note']}"
                for p in flagged]
    if release_set is not None:
        out += ["", "### Release set", ""] + component_table(previous_set, release_set)
    out.append("")
    return "\n".join(out)


def component_table(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[str]:
    _, rows = platform_level(previous, current)
    table = ["| Component | Version | Change | Digest |", "|---|---|---|---|"]
    for row in rows:
        table.append(f"| {row['component']} | {row['version']} | {row['change']} | "
                     f"{('`' + row['digest'][:19] + '…`') if row.get('digest') else ''} |")
    return table


# --- platform-level ------------------------------------------------------------------------------


def version_distance(old: str | None, new: str | None) -> str:
    """The level of a component's move in a train: the highest semver position that changed.
    New components count as minor, removed ones as major, a move down (a rollback) as patch."""
    if old == new:
        return "none"
    if old is None:
        return "minor"
    if new is None:
        return "major"
    a, b = parse_version(old), parse_version(new)
    if b < a:
        return "patch"
    if b.major != a.major:
        return "major"
    if b.minor != a.minor:
        return "minor"
    return "patch"


def platform_level(previous: dict[str, Any] | None,
                   current: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """The train's floor: the largest move of any component since the previous release set, plus
    a row per component for the notes. Deploy repos (gitops, infra) are pinned by commit, so a
    moved commit is a patch."""
    old_components = (previous or {}).get("components") or {}
    rows, levels = [], []
    for name in sorted(set(old_components) | set(current.get("components") or {})):
        old = old_components.get(name) or {}
        new = (current.get("components") or {}).get(name) or {}
        level = version_distance(old.get("version"), new.get("version"))
        levels.append(level)
        if not new:
            change = f"removed (was {old.get('version')})"
        elif not old:
            change = "new in this set"
        elif level == "none":
            change = f"unchanged since {new.get('tag') or 'v' + str(new.get('version'))}"
        else:
            change = f"{old.get('version')} → {new.get('version')} ({level})"
        rows.append({"component": name, "version": new.get("version", "—"), "change": change,
                     "level": level, "digest": new.get("digest")})
    old_deploy = (previous or {}).get("deploy") or {}
    for name, new in sorted((current.get("deploy") or {}).items()):
        before = (old_deploy.get(name) or {}).get("commit")
        moved = before != new.get("commit")
        levels.append("patch" if moved and before else "none")
        rows.append({"component": name, "version": f"commit {str(new.get('commit'))[:7]}",
                     "change": ("new in this set" if not before else
                                f"from {before[:7]}" if moved else "unchanged"),
                     "level": "patch" if moved and before else "none", "digest": None})
    return highest(levels), rows


# --- reserve-tag ---------------------------------------------------------------------------------


class TagConflict(Exception):
    pass


def reserve_tag(gh: Any, tag: str, sha: str) -> str:
    """Create refs/tags/<tag> at <sha> with one API call, which either creates it or fails with
    422 if it exists. That is the lock between two release runs: whoever creates the tag owns the
    version. A 422 on the same commit is a rerun and continues; on another commit it fails, since
    tags never move."""
    if not VERSION_RE.match(tag) or not tag.startswith("v"):
        raise UsageError(f"refusing to create an implausible tag {tag!r}")
    if not SHA_RE.match(sha):
        raise UsageError(f"--sha must be a full 40-hex commit, not {sha!r}")
    try:
        gh.post("git/refs", {"ref": f"refs/tags/{tag}", "sha": sha})
        return f"created {tag} at {sha[:12]}"
    except GitHubError as exc:
        if exc.status != 422:
            raise
    obj = gh.get(f"git/ref/tags/{tag}")["object"]
    target = obj["sha"]
    if obj.get("type") == "tag":
        target = gh.get(f"git/tags/{target}")["object"]["sha"]
    if target == sha:
        return f"{tag} already at {sha[:12]} (a rerun); continuing"
    raise TagConflict(f"{tag} already points at {target[:12]}, not {sha[:12]}. Tags never move: "
                      f"fix forward with the next release")


# --- command line --------------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _github(args: argparse.Namespace) -> GitHub:
    repo = args.repo or _env("GITHUB_REPOSITORY")
    if not repo:
        raise UsageError("--repo or GITHUB_REPOSITORY is required")
    return GitHub(repo, _env("GITHUB_TOKEN") or _env("GH_TOKEN"),
                  _env("GITHUB_API_URL", "https://api.github.com"))


def _write_outputs(values: dict[str, Any]) -> None:
    path = _env("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in values.items():
            if value is None:
                text = ""
            elif isinstance(value, bool):
                text = str(value).lower()
            else:
                text = str(value)
            fh.write(f"{key}={text}\n")


def _summary(lines: list[str]) -> None:
    path = _env("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("```\n" + "\n".join(lines) + "\n```\n")


def _run_url() -> str:
    if _env("GITHUB_RUN_ID") and _env("GITHUB_REPOSITORY"):
        return (f"{_env('GITHUB_SERVER_URL', 'https://github.com')}/{_env('GITHUB_REPOSITORY')}"
                f"/actions/runs/{_env('GITHUB_RUN_ID')}")
    return ""


def _optional_version(text: str | None, flag: str) -> Version | None:
    if not text:
        return None
    try:
        return parse_version(text)
    except ValueError as exc:
        raise UsageError(f"{flag}: {exc}") from exc


def cmd_check_pr(args: argparse.Namespace) -> int:
    gh = _github(args)
    number = args.pr
    if number is None and _env("GITHUB_EVENT_PATH"):
        event = json.loads(Path(_env("GITHUB_EVENT_PATH")).read_text(encoding="utf-8"))
        number = (event.get("pull_request") or {}).get("number")
    if not number:
        raise UsageError("--pr or a pull_request event is required")
    config = Config.load(args.config)
    workflow_text = None
    if config.build_workflow:
        try:
            workflow_text = Path(config.build_workflow).read_text(encoding="utf-8")
        except OSError:
            workflow_text = None

    def predict(level: str) -> str:
        return prediction(gh, config, level, args.predict_ref)

    report = check_pr(gh, int(number), config, workflow_text, predict)
    lines = report.lines()
    print("\n".join(lines))
    _summary(lines)
    kind = "warning" if args.advisory else "error"
    for check in report.checks:
        if not check.ok:
            print(f"::{kind} title=release-label {check.rule}::{check.message}")
    if not report.ok and args.advisory:
        print("advisory mode: reported, not failed (RELEASING.md says when this becomes required)")
    return 0 if report.ok or args.advisory else 1


def prediction(gh: GitHub, config: Config, level: str, predict_ref: str | None) -> str:
    """What merging this PR next would release. With a full-history checkout of the base branch
    (predict_ref), PRs merged since the last tag count too; otherwise only this PR's label does,
    against the highest tag the API lists."""
    pending, previous = "none", None
    if predict_ref:
        try:
            plan = plan_release(Git("."), gh, config, ref=predict_ref, repo=gh.repo)
            pending = plan_level(plan)
            previous = _optional_version(plan["previous"], "previous")
        except (subprocess.CalledProcessError, OSError, UsageError):
            predict_ref = None
    if not predict_ref:
        refs = gh.get("git/matching-refs/tags/v") or []
        versions = sorted(parse_version(r["ref"].rsplit("/", 1)[1]) for r in refs
                          if VERSION_RE.match(r["ref"].rsplit("/", 1)[1]))
        previous = versions[-1] if versions else None
    combined = highest([level, pending])
    nxt = bump(previous, combined, baseline=config.baseline)
    start = previous.tag if previous else "no tag yet"
    if nxt is None:
        return f"{config.component}: {start}, nothing to release if this merges next"
    extra = "" if predict_ref else " (PRs merged since the last tag are not counted here)"
    return f"{config.component}: {start} -> {nxt.tag} if this merges next ({combined}){extra}"


def cmd_plan(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    gh = _github(args) if (args.repo or _env("GITHUB_REPOSITORY")) else None
    plan = plan_release(Git(args.git_dir), gh, config, ref=args.ref,
                        default_branch=args.default_branch, level_override=args.level or None,
                        override_by=args.by or "",
                        promote_major=_optional_version(args.promote_major, "--promote-major"),
                        promote_version=_optional_version(args.promote_version,
                                                          "--promote-version"),
                        floor=args.floor or "none", repo=gh.repo if gh else "")
    text = json.dumps(plan, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text, end="")
    _write_outputs({"version": plan["version"], "previous": plan["previous"],
                    "level": plan["level"], "tag": plan["tag"], "mode": plan["mode"],
                    "skip": plan["mode"] == "skip", "skip-reason": plan["skip_reason"]})
    return 0


def cmd_notes(args: argparse.Namespace) -> int:
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    if args.digest and not DIGEST_RE.match(args.digest):
        raise UsageError(f"--digest must be sha256:<64 hex>, not {args.digest!r}")
    current = json.loads(Path(args.release_set).read_text(encoding="utf-8")) \
        if args.release_set else None
    previous = _read_optional_json(args.previous_set)
    text = notes(plan, repo=args.repo or plan.get("repo") or _env("GITHUB_REPOSITORY"),
                 run_url=args.run_url or _run_url(), image=args.image or plan.get("image"),
                 digest=args.digest or None, release_set=current, previous_set=previous)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text, end="")
    _write_outputs({"notes-file": args.out or ""})
    return 0


def _read_optional_json(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return json.loads(text) if text else None


def cmd_platform_level(args: argparse.Namespace) -> int:
    current = json.loads(Path(args.set).read_text(encoding="utf-8"))
    level, rows = platform_level(_read_optional_json(args.previous_set), current)
    for row in rows:
        print(f"{row['component']:<16} {row['version']:<16} {row['change']}")
    print(f"level: {level}")
    _write_outputs({"level": level})
    return 0


def cmd_reserve_tag(args: argparse.Namespace) -> int:
    print(reserve_tag(_github(args), args.tag, args.sha))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="semver_labels.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", help="owner/name (default: GITHUB_REPOSITORY)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check-pr", help="rules R1-R4 on a pull request")
    p.add_argument("--pr", type=int, help="PR number (default: the pull_request event's)")
    p.add_argument("--config", default=".github/versioning.json")
    p.add_argument("--advisory", action="store_true", help="report violations without failing")
    p.add_argument("--predict-ref", help="a full-history ref of the base branch, e.g. origin/main")
    p.set_defaults(func=cmd_check_pr)

    p = sub.add_parser("plan", help="the next version at a commit")
    p.add_argument("--config", default=".github/versioning.json")
    p.add_argument("--ref", default="HEAD")
    p.add_argument("--git-dir", default=".")
    p.add_argument("--default-branch", default="main")
    p.add_argument("--level", choices=LEVELS, help="a dispatch override of the labels")
    p.add_argument("--by", help="who set the override, recorded in the plan (actor and run)")
    p.add_argument("--floor", choices=LEVELS, help="the lowest level to release at")
    p.add_argument("--promote-major", help="N.0.0: the only way to reach 1.0.0")
    p.add_argument("--promote-version", help="X.Y.Z: release and deploy an existing tag again")
    p.add_argument("--out", help="write plan.json here")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("notes", help="release notes from a plan")
    p.add_argument("--plan", required=True)
    p.add_argument("--run-url", help="default: this Actions run")
    p.add_argument("--image")
    p.add_argument("--digest")
    p.add_argument("--release-set", help="the platform's release-set.json, for the component table")
    p.add_argument("--previous-set", help="the previous release-set.json")
    p.add_argument("--out")
    p.set_defaults(func=cmd_notes)

    p = sub.add_parser("platform-level", help="the train's level from two release sets")
    p.add_argument("--previous-set", help="the release set at the previous platform tag")
    p.add_argument("--set", required=True)
    p.set_defaults(func=cmd_platform_level)

    p = sub.add_parser("reserve-tag", help="create a tag through the API, atomically")
    p.add_argument("--tag", required=True)
    p.add_argument("--sha", required=True)
    p.set_defaults(func=cmd_reserve_tag)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        return args.func(args)
    except UsageError as exc:
        print(f"usage error: {exc}", file=sys.stderr)
        return 2
    except (GitHubError, TagConflict) as exc:
        print(f"::error::{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
