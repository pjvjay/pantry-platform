#!/usr/bin/env python3
"""Create the four release labels in each pantry repo, from .github/labels.json.

By default this only prints the gh commands, so you can read exactly what would change. Labels
are repository settings, so nothing is created until you pass --apply yourself:

    python3 scripts/labels.py                      # print the commands for the four repos
    python3 scripts/labels.py --repo pjvjay/pantry-api
    python3 scripts/labels.py --apply              # run them with your own gh login

`gh label create --force` updates a label that already exists, so --apply is safe to rerun.
Stdlib only; needs gh (https://cli.github.com) for --apply.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LABELS = ROOT / ".github" / "labels.json"
# Every repo whose PRs carry a release label. gitops and infra are identified by commit in the
# release set (RELEASING.md, "What is not versioned"), so their PRs need no label.
REPOS = ("pjvjay/pantry-api", "pjvjay/pantry-db", "pjvjay/pantry-frontend",
         "pjvjay/pantry-platform")


def load(path: Path) -> list[dict[str, str]]:
    labels = json.loads(path.read_text(encoding="utf-8"))
    for label in labels:
        missing = {"name", "color", "description"} - set(label)
        if missing:
            raise ValueError(f"{path}: {label.get('name', '?')} lacks {', '.join(sorted(missing))}")
        if len(label["description"]) > 100:
            raise ValueError(f"{label['name']}: GitHub allows 100 characters of description")
    return labels


def commands(labels: list[dict[str, str]], repos: list[str]) -> list[list[str]]:
    return [["gh", "label", "create", label["name"], "--repo", repo, "--color", label["color"],
             "--description", label["description"], "--force"]
            for repo in repos for label in labels]


def main(argv: list[str] | None = None,
         run: Callable[[list[str]], Any] = lambda cmd: subprocess.run(cmd, check=True)) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", action="append",
                        help="owner/name; repeatable (default: all four)")
    parser.add_argument("--labels", type=Path, default=LABELS)
    parser.add_argument("--apply", action="store_true", help="run the commands (needs gh auth)")
    args = parser.parse_args(argv)
    cmds = commands(load(args.labels), args.repo or list(REPOS))
    for cmd in cmds:
        print(shlex.join(cmd))
        if args.apply:
            run(cmd)
    if not args.apply:
        print(f"# {len(cmds)} commands printed; nothing was changed. Pass --apply to run them.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
