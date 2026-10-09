#!/usr/bin/env python3
"""The platform release train: pin the latest release of each component as one tested set.

    python3 scripts/train.py                 # --dry-run: print the release set; change nothing
    python3 scripts/train.py --out set.json  # also write it to a file to read or diff
    python3 scripts/train.py --open-pr       # open 'Pin the release set: ...' with your gh login

For pantry-api, pantry-db and pantry-frontend it reads the latest GitHub Release, the commit its
tag points at and the image digest GHCR holds for that version. It then checks that pantry-gitops
main deploys exactly those versions and that each digest is the one the release recorded. If
any of that fails it refuses and names the component; it never pins a release that is not
deployed. pantry-gitops and pantry-infra are pinned by their main commits, and the local stack
(mcp-sim, pantry-gateway) by the commits checked out next to this repo.

--open-pr works in a temporary worktree of origin/main, so this checkout, its branch and its
submodule working trees are never touched. In that worktree it writes release-set.json, moves
the five submodule pins with `git update-index` (no submodule checkout), pins demo/Dockerfile's
FROM lines to tag@digest, commits, pushes a train/ branch and opens the PR with a release label
from the version distance. Merging it runs release.yml, which tags the platform, and Render
rebuilds the demo from the pinned digests.

Stdlib only; reads GitHub through `gh api` and GHCR anonymously. Exit codes: 0 ok, 1 refused,
2 usage.
"""

from __future__ import annotations

import argparse
import base64
import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import release_set as rs

ROOT = Path(__file__).resolve().parent.parent
# The version arithmetic lives in the semver-labels action, so the train and the release
# workflow can never disagree about what a level means.
sys.path.insert(0, str(ROOT / ".github" / "actions" / "semver-labels"))
sl = importlib.import_module("semver_labels")

RELEASE_SET = "release-set.json"


class Refused(Exception):
    """The set cannot be pinned; the message names each component and why."""


class Sources:
    """Everything the train reads, through the user's own gh login and anonymous GHCR reads.
    Tests replace this with a fake."""

    def __init__(self, root: Path = ROOT, workspace: Path | None = None) -> None:
        self.root = root
        self.workspace = workspace or Path(os.environ.get("WORKSPACE", root.parent))
        self.registry = rs.Registry()

    def _gh(self, path: str) -> Any:
        proc = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            if "HTTP 404" in proc.stderr:
                return None
            raise Refused(f"gh api {path} failed: {proc.stderr.strip()[:300]}")
        return json.loads(proc.stdout)

    def latest_release(self, repo: str) -> dict[str, Any] | None:
        return self._gh(f"repos/{repo}/releases/latest")

    def tag_commit(self, repo: str, tag: str) -> str | None:
        ref = self._gh(f"repos/{repo}/git/ref/tags/{tag}")
        if ref is None:
            return None
        obj = ref["object"]
        if obj["type"] == "tag":
            obj = self._gh(f"repos/{repo}/git/tags/{obj['sha']}")["object"]
        return obj["sha"]

    def branch_head(self, repo: str, branch: str = "main") -> str:
        return self._gh(f"repos/{repo}/commits/{branch}")["sha"]

    def file_at(self, repo: str, commit: str, path: str) -> str | None:
        data = self._gh(f"repos/{repo}/contents/{path}?ref={commit}")
        return base64.b64decode(data["content"]).decode("utf-8") if data else None

    def schema_head(self, repo: str, commit: str) -> str | None:
        listing = self._gh(f"repos/{repo}/contents/migrations?ref={commit}") or []
        names = sorted(item["name"] for item in listing if item["name"].endswith(".sql"))
        return names[-1][: -len(".sql")] if names else None

    def registry_digest(self, image: str, version: str) -> str | None:
        return self.registry.digest(image, version)

    def image_revision(self, image: str, digest: str) -> str | None:
        return self.registry.revision(image, digest)

    def local_stack(self) -> list[dict[str, Any]]:
        stack = []
        for name in ("mcp-sim", "pantry-gateway"):
            path = self.workspace / name
            try:
                commit = _git(path, "rev-parse", "HEAD")
                dirty = bool(_git(path, "status", "--porcelain", "--untracked-files=no"))
            except (subprocess.CalledProcessError, OSError):
                stack.append({"name": name, "pinned": False, "note": f"no checkout at {path}"})
                continue
            entry: dict[str, Any] = {"name": name, "pinned": not dirty, "commit": commit}
            if dirty:
                entry["note"] = "the checkout had uncommitted changes"
            stack.append(entry)
        stack.append({"name": "mcp-sim-local", "pinned": False,
                      "note": "a local skill directory, not a repository"})
        stack.append({"name": "contextforge", "pinned": False,
                      "note": "installed into its own virtualenv; version not recorded"})
        return stack

    def previous_set(self) -> dict[str, Any] | None:
        try:
            _git(self.root, "fetch", "-q", "origin", "main")
            return json.loads(_git(self.root, "show", f"origin/main:{RELEASE_SET}"))
        except (subprocess.CalledProcessError, ValueError):
            return None


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def build_set(src: Any) -> tuple[dict[str, Any], list[str]]:
    """(release set, warnings). Raises Refused listing every component that cannot be pinned."""
    problems: list[str] = []
    warnings: list[str] = []
    components: dict[str, Any] = {}
    for name, meta in rs.COMPONENTS.items():
        repo, image = meta["repo"], meta["image"]
        release = src.latest_release(repo)
        if not release:
            problems.append(f"{name}: no release yet (bootstrap v0.1.0 first; RELEASING.md)")
            continue
        tag = release["tag_name"]
        version = tag.removeprefix("v")
        if not rs.VERSION_RE.match(version):
            problems.append(f"{name}: the latest release {tag} is not a vX.Y.Z tag")
            continue
        commit = src.tag_commit(repo, tag)
        digest = src.registry_digest(image, version)
        if commit is None or digest is None:
            problems.append(f"{name}: {tag} has " + ("no git tag" if commit is None else
                                                     f"no image {image}:{version} on GHCR"))
            continue
        recorded = rs.NOTES_DIGEST_RE.search(release.get("body") or "")
        if recorded and recorded.group(1) != digest:
            problems.append(f"{name}: digest mismatch: the {tag} release recorded "
                            f"{recorded.group(1)[:19]}, GHCR serves {digest[:19]} for {version}")
            continue
        if not recorded:
            warnings.append(f"{name}: the {tag} release notes record no digest; using GHCR's")
        revision = src.image_revision(image, digest)
        if revision and revision != commit:
            problems.append(f"{name}: {image}:{version} was built from {revision[:7]}, "
                            f"not {tag}'s commit {commit[:7]}")
            continue
        if not revision:
            warnings.append(f"{name}: {image}:{version} has no revision label; not checked")
        entry = {"repo": repo, "version": version, "tag": tag, "commit": commit, "image": image,
                 "digest": digest, "release_url": release.get("html_url")
                 or rs.release_url(repo, tag)}
        if name == "pantry-db":
            entry["schema_head"] = src.schema_head(repo, commit)
        components[name] = entry

    deploy = {name: {"repo": repo, "commit": src.branch_head(repo)}
              for name, repo in rs.DEPLOY.items()}
    gitops = deploy["pantry-gitops"]
    text = src.file_at(gitops["repo"], gitops["commit"], rs.KUSTOMIZATION) or ""
    deployed = rs.deployed_images(text)
    for name, entry in components.items():
        found = deployed.get(entry["image"])
        if found and found.get("digest") and found["digest"] != entry["digest"]:
            problems.append(f"{name}: digest mismatch: gitops deploys {found['digest'][:19]}, "
                            f"{entry['tag']} is {entry['digest'][:19]}")
        elif not rs.deploys(found, entry["version"], entry["digest"]):
            now = (found or {}).get("tag") or (found or {}).get("digest") or "nothing"
            problems.append(f"{name}: {entry['version']} is released but pantry-gitops main "
                            f"({gitops['commit'][:7]}) deploys {now}. Wait for the deploy job, "
                            f"or run build.yml with promote_version={entry['version']}")
    if problems:
        raise Refused("\n".join(problems))
    release_set = {"schema": rs.SCHEMA, "components": components, "deploy": deploy,
                   "local_stack": src.local_stack()}
    invalid = rs.validate(release_set)
    if invalid:
        raise Refused("\n".join(invalid))
    return release_set, warnings


def summary(release_set: dict[str, Any], previous: dict[str, Any] | None) -> tuple[str, str, str]:
    """(level, PR title, PR body) built by code from the set."""
    level, rows = sl.platform_level(previous, release_set)
    c = release_set["components"]
    title = (f"Pin the release set: api {c['pantry-api']['version']}, "
             f"db {c['pantry-db']['version']}, frontend {c['pantry-frontend']['version']}")
    intro = ("Generated by `scripts/train.py`. Every row below comes from a GitHub Release, its "
             "tag and the GHCR digest; verify-pins checks it in strict mode.")
    body = [intro, "", "| Component | Version | Change | Release |", "|---|---|---|---|"]
    for row in rows:
        entry = c.get(row["component"]) or {}
        link = f"[{entry['tag']}]({entry['release_url']})" if entry else ""
        body.append(f"| {row['component']} | {row['version']} | {row['change']} | {link} |")
    stack = ", ".join(f"{s['name']} {s['commit'][:7]}" if s.get("pinned") else
                      f"{s['name']} (not pinned)" for s in release_set["local_stack"])
    label = (f"Release label: `release:{_label(level)}` (the largest component move; "
             "RELEASING.md, the train).")
    merging = ("Merging tags the platform (release.yml) and Render rebuilds the demo from the "
               "pinned digests.")
    body += ["", f"Local stack: {stack}.", "", label, "", merging, ""]
    return level, title, "\n".join(body)


def _label(level: str) -> str:
    # A train always ships something, so it is never release:none (R2).
    return level if level != "none" else "patch"


def open_pr(release_set: dict[str, Any], title: str, body: str, level: str,
            root: Path = ROOT) -> str:
    """Commit the set in a temporary worktree of origin/main and open the PR. Returns its URL."""
    branch = "train/" + "-".join(f"{n.split('-')[1]}-{c['version']}"
                                 for n, c in release_set["components"].items())
    _git(root, "fetch", "-q", "origin", "main")
    tmp = Path(tempfile.mkdtemp(prefix="pantry-train-"))
    worktree = tmp / "platform"
    _git(root, "worktree", "add", "-q", "--detach", str(worktree), "origin/main")
    try:
        (worktree / RELEASE_SET).write_text(rs.dumps(release_set), encoding="utf-8")
        dockerfile = worktree / rs.DOCKERFILE
        dockerfile.write_text(rs.pin_dockerfile(dockerfile.read_text(encoding="utf-8"),
                                                release_set["components"]), encoding="utf-8")
        pins = {**release_set["components"], **release_set["deploy"]}
        for path, entry in pins.items():
            _git(worktree, "update-index", "--cacheinfo", f"160000,{entry['commit']},{path}")
        _git(worktree, "add", RELEASE_SET, rs.DOCKERFILE)
        _git(worktree, "switch", "-q", "-c", branch)
        _git(worktree, "commit", "-q", "-m", title, "-m", body)
        _git(worktree, "push", "-q", "-u", "origin", branch)
        proc = subprocess.run(["gh", "pr", "create", "--base", "main", "--head", branch,
                               "--title", title, "--body", body, "--label",
                               f"release:{_label(level)}"], cwd=worktree, capture_output=True,
                              text=True, check=True)
        return proc.stdout.strip()
    finally:
        _git(root, "worktree", "remove", "--force", str(worktree))


def main(argv: list[str] | None = None, src: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="print the release set and change nothing (the default)")
    mode.add_argument("--open-pr", action="store_true",
                      help="push a train/ branch and open the PR with your gh login")
    parser.add_argument("--out", type=Path, help="also write the release set to this file")
    args = parser.parse_args(argv)
    src = src or Sources()
    try:
        release_set, warnings = build_set(src)
    except Refused as exc:
        print("refused: the release set cannot be pinned", file=sys.stderr)
        for line in str(exc).splitlines():
            print(f"  {line}", file=sys.stderr)
        return 1
    previous = src.previous_set()
    level, title, body = summary(release_set, previous)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(title)
    print(body)
    print(rs.dumps(release_set), end="")
    if args.out:
        args.out.write_text(rs.dumps(release_set), encoding="utf-8")
    if previous is not None and level == "none" and \
            previous.get("components") == release_set["components"]:
        print("nothing to train: every component is at the version already pinned",
              file=sys.stderr)
        return 0
    if args.open_pr:
        try:
            print(open_pr(release_set, title, body, level))
        except subprocess.CalledProcessError as exc:
            print(f"failed: {' '.join(exc.cmd[:3])}: {(exc.stderr or '').strip()[:500]}",
                  file=sys.stderr)
            return 1
    else:
        print("dry run: nothing was written to git and no PR was opened", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
