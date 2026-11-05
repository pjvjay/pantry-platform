#!/usr/bin/env python3
"""Check release-set.json against the submodule pins, pantry-gitops and demo/Dockerfile.

Two modes, so ordinary pull requests keep passing exactly as before:

* lenient (any PR that leaves release-set.json alone): validates release-set.json when it
  exists, and warns, without failing, when the pins have moved away from it. The ancestor check
  in verify.yml (every pin is a merged commit on its repo's main) still runs as it always has.
* strict (a release-train PR, i.e. one that changes release-set.json): every check below must
  pass. A train pins released tags, so anything less is refused.

    pins-are-tags      each component's pin is the commit its vX.Y.Z tag points at
    set-is-the-pins    release-set.json names exactly the five commits pinned as submodules
    gitops-deploys     the pinned pantry-gitops commit deploys each version (tag or digest)
    digests            the digests GHCR serves for each version are the recorded ones (--registry)
    demo-dockerfile    demo/Dockerfile's FROM lines are image:X.Y.Z@digest for api and frontend
    seeds              pantry-api's and pantry-db's seeds/*.json are byte-identical

    python3 scripts/verify_release_set.py --mode auto --base origin/main --fetch --registry

Stdlib only. Exit codes: 0 ok, 1 a check failed, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import release_set as rs

ROOT = Path(__file__).resolve().parent.parent


class Repo:
    """Reads the platform checkout and its submodules through git."""

    def __init__(self, root: Path = ROOT) -> None:
        self.root = root

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(["git", *args], cwd=cwd or self.root, capture_output=True,
                              text=True, check=True).stdout

    def pin(self, path: str) -> str | None:
        """The commit a gitlink at HEAD points at."""
        out = self.git("ls-tree", "HEAD", path).split()
        return out[2] if len(out) >= 3 and out[1] == "commit" else None

    def changed(self, base: str) -> list[str]:
        return self.git("diff", "--name-only", f"{base}...HEAD").splitlines()

    def read(self, path: str) -> str | None:
        try:
            return self.git("show", f"HEAD:{path}")
        except subprocess.CalledProcessError:
            return None

    def fetch_tags(self, path: str) -> None:
        self.git("fetch", "-q", "--tags", "origin", cwd=self.root / path)

    def tag_commit(self, path: str, tag: str) -> str | None:
        try:
            return self.git("rev-parse", f"{tag}^{{commit}}", cwd=self.root / path).strip()
        except subprocess.CalledProcessError:
            return None

    def show(self, path: str, commit: str, file: str) -> bytes | None:
        try:
            return subprocess.run(["git", "show", f"{commit}:{file}"], cwd=self.root / path,
                                  capture_output=True, check=True).stdout
        except subprocess.CalledProcessError:
            return None


def lenient(repo: Repo) -> tuple[list[str], list[str]]:
    """(failures, notes) for an ordinary PR."""
    text = repo.read("release-set.json")
    if text is None:
        return [], ["no release-set.json yet (it arrives with the first train)"]
    try:
        data = json.loads(text)
    except ValueError as exc:
        return [f"release-set.json is not JSON: {exc}"], []
    failures = rs.validate(data)
    notes = []
    if not failures:
        pinned = {**data["components"], **data["deploy"]}
        moved = [name for name, entry in pinned.items() if repo.pin(name) != entry["commit"]]
        if moved:
            notes.append(f"pins moved since the last train: {', '.join(moved)}; the next train "
                         "re-pins them (not a failure on an ordinary PR)")
        else:
            notes.append("release-set.json is valid and matches the pins")
    return failures, notes


def strict(repo: Repo, *, fetch: bool = False, registry: Any = None) -> tuple[list[str], list[str]]:
    """(failures, notes) for a release-train PR. Every failure names its check."""
    text = repo.read("release-set.json")
    if text is None:
        return ["schema: release-set.json is missing"], []
    try:
        data = json.loads(text)
    except ValueError as exc:
        return [f"schema: release-set.json is not JSON: {exc}"], []
    problems = rs.validate(data)
    if problems:
        return [f"schema: {p}" for p in problems], []
    failures: list[str] = []
    notes: list[str] = []
    components, deploy = data["components"], data["deploy"]

    for name, entry in components.items():
        if fetch:
            repo.fetch_tags(name)
        pin = repo.pin(name)
        tagged = repo.tag_commit(name, entry["tag"])
        if tagged is None:
            failures.append(f"pins-are-tags: {name} has no tag {entry['tag']} (fetch tags?)")
        elif not (pin == tagged == entry["commit"]):
            failures.append(f"pins-are-tags: {name} is pinned at {str(pin)[:7]}, {entry['tag']} "
                            f"is {tagged[:7]}, the set says {entry['commit'][:7]}")
    for name, entry in deploy.items():
        pin = repo.pin(name)
        if pin != entry["commit"]:
            failures.append(f"set-is-the-pins: {name} is pinned at {str(pin)[:7]}, the set says "
                            f"{entry['commit'][:7]}")

    gitops = deploy["pantry-gitops"]["commit"]
    kustomization = repo.show("pantry-gitops", gitops, rs.KUSTOMIZATION)
    if kustomization is None:
        failures.append(f"gitops-deploys: cannot read {rs.KUSTOMIZATION} at pantry-gitops "
                        f"{gitops[:7]}")
    else:
        deployed = rs.deployed_images(kustomization.decode("utf-8"))
        for name, entry in components.items():
            found = deployed.get(entry["image"])
            if not rs.deploys(found, entry["version"], entry["digest"]):
                now = (found or {}).get("digest") or (found or {}).get("tag") or "nothing"
                failures.append(f"gitops-deploys: pantry-gitops {gitops[:7]} deploys {now} for "
                                f"{name}, not {entry['version']}")

    if registry is not None:
        for entry in components.values():
            try:
                served = registry.digest(entry["image"], entry["version"])
            except OSError as exc:
                failures.append(f"digests: cannot read {entry['image']}:{entry['version']}: {exc}")
                continue
            if served != entry["digest"]:
                failures.append(f"digests: GHCR serves {str(served)[:19]} for {entry['image']}:"
                                f"{entry['version']}, the set says {entry['digest'][:19]}")
    else:
        notes.append("digests: not compared with GHCR (pass --registry)")

    dockerfile = repo.read(rs.DOCKERFILE) or ""
    lines = rs.from_lines(dockerfile)
    for name in ("pantry-api", "pantry-frontend"):
        entry = components[name]
        found = lines.get(entry["image"])
        want = {"tag": entry["version"], "digest": entry["digest"]}
        if found != want:
            if not found:
                got = " no FROM line"
            else:
                got = f":{found['tag']}" + (f"@{found['digest'][:19]}…" if found["digest"]
                                            else " (no digest)")
            failures.append(f"demo-dockerfile: {rs.DOCKERFILE} has {entry['image']}{got}, want "
                            f"{entry['version']}@{entry['digest'][:19]}…")

    for file in rs.SEED_FILES:
        api = repo.show("pantry-api", components["pantry-api"]["commit"], file)
        db = repo.show("pantry-db", components["pantry-db"]["commit"], file)
        if api is None or db is None or api != db:
            failures.append(f"seeds: {file} differs between pantry-api "
                            f"{components['pantry-api']['tag']} and pantry-db "
                            f"{components['pantry-db']['tag']}")
    return failures, notes


def main(argv: list[str] | None = None, repo: Repo | None = None, registry: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mode", choices=("auto", "lenient", "strict"), default="auto")
    parser.add_argument("--base", default="origin/main",
                        help="auto mode: the base the PR is compared with")
    parser.add_argument("--fetch", action="store_true", help="strict: fetch submodule tags first")
    parser.add_argument("--registry", action="store_true",
                        help="strict: compare digests with GHCR (anonymous reads)")
    args = parser.parse_args(argv)
    repo = repo or Repo()
    mode = args.mode
    if mode == "auto":
        try:
            changed = repo.changed(args.base)
        except subprocess.CalledProcessError as exc:
            print(f"::error::cannot diff against {args.base}: {exc.stderr.strip()}")
            return 2
        mode = "strict" if "release-set.json" in changed else "lenient"
        why = "it changes release-set.json" if mode == "strict" else \
            "it leaves release-set.json alone"
        print(f"mode: {mode} ({why})")
    if mode == "strict":
        if registry is None and args.registry:
            registry = rs.Registry()
        failures, notes = strict(repo, fetch=args.fetch, registry=registry)
    else:
        failures, notes = lenient(repo)
    for note in notes:
        print(f"note  {note}")
    for failure in failures:
        print(f"::error::{failure}")
    if not failures:
        print(f"ok    release set ({mode})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
