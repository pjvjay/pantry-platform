"""Which platform release this hub is, and whether the stack it runs is the pinned set.

The demo hub shares the platform's version (RELEASING.md). That version is
``PANTRY_PLATFORM_VERSION`` when the environment sets it, else ``git describe`` of the platform
checkout the hub runs from (``v0.2.0-3-gabc1234`` reads as 0.2.0-3-gabc1234: three commits past
the v0.2.0 train), else "unknown". Nothing is guessed: every value this module cannot read is
null, and a missing or broken release-set.json is reported, never raised.

``release_block`` is the ``release`` part of /hub/status:

* platform: the version and where it came from, the describe output, the commit and whether
  tracked files (submodule pins included) differ from it;
* components: per released component, the version release-set.json pins, the version running
  (pantry-api's /health, the console's dist/version.json; the hub cannot see which migrate image
  ran, so pantry-db is null) and whether they match;
* local_stack: mcp-sim and pantry-gateway's checked-out commits against the set's, and the parts
  the set does not pin.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PLATFORM_ROOT = Path(__file__).resolve().parents[2]
RELEASE_SET = "release-set.json"
UNKNOWN = "unknown"
COMPONENTS = ("pantry-api", "pantry-db", "pantry-frontend")
LOCAL_STACK = ("mcp-sim", "pantry-gateway")


def _git(root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              timeout=3, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip()


def describe(root: Path | None = None) -> str | None:
    """``git describe`` against vX.Y.Z tags, or None when there is no such tag or no git."""
    return _git(root or PLATFORM_ROOT, "describe", "--tags", "--dirty",
                "--match", "v[0-9]*.[0-9]*.[0-9]*") or None


def platform_version(root: Path | None = None,
                     env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """(version, source): source is "env", "git" or "unknown"."""
    value = (env if env is not None else os.environ).get("PANTRY_PLATFORM_VERSION", "").strip()
    if value:
        return value, "env"
    described = describe(root)
    if described:
        return described.removeprefix("v"), "git"
    return UNKNOWN, "unknown"


def _platform(root: Path, env: Mapping[str, str]) -> dict[str, Any]:
    version, source = platform_version(root, env)
    commit = _git(root, "rev-parse", "HEAD") or None
    status = _git(root, "status", "--porcelain", "--untracked-files=no",
                  "--ignore-submodules=dirty") if commit else None
    return {"version": version, "source": source, "describe": describe(root), "commit": commit,
            "dirty": None if status is None else bool(status)}


def _load_set(root: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = root / RELEASE_SET
    if not path.is_file():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{RELEASE_SET} is unreadable: {type(exc).__name__}"
    if not isinstance(data, dict):
        return None, f"{RELEASE_SET} is not a JSON object"
    return data, None


def _known(value: Any) -> str | None:
    return value if isinstance(value, str) and value and value != UNKNOWN else None


def _spa_version(spa_dist: str) -> str | None:
    if not spa_dist:
        return None
    try:
        data = json.loads((Path(spa_dist) / "version.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _known(data.get("version")) if isinstance(data, dict) else None


def release_block(*, root: Path | None = None, pantry_health: Any = None, spa_dist: str = "",
                  workspace: Path | None = None,
                  env: Mapping[str, str] | None = None) -> dict[str, Any]:
    root = root or PLATFORM_ROOT
    env = env if env is not None else os.environ
    workspace = workspace or Path(env.get("WORKSPACE") or root.parent)
    data, error = _load_set(root)
    pinned_components = (data or {}).get("components") or {}
    health = pantry_health if isinstance(pantry_health, dict) else {}
    running = {"pantry-api": _known(health.get("version")), "pantry-db": None,
               "pantry-frontend": _spa_version(spa_dist)}
    components = []
    for name in COMPONENTS:
        entry = pinned_components.get(name) if isinstance(pinned_components, dict) else None
        entry = entry if isinstance(entry, dict) else {}
        pinned = _known(entry.get("version"))
        components.append({"name": name, "pinned": pinned, "running": running[name],
                           "match": pinned == running[name] if pinned and running[name] else None,
                           "release_url": entry.get("release_url")})

    stack_entries = {item.get("name"): item for item in (data or {}).get("local_stack") or []
                     if isinstance(item, dict)}
    names = list(LOCAL_STACK) + [n for n in stack_entries if n and n not in LOCAL_STACK]
    local_stack = []
    for name in names:
        item = stack_entries.get(name) or {}
        # Only a directory that is itself a repository: git -C would otherwise find a parent's.
        path = workspace / name
        checkout = _git(path, "rev-parse", "HEAD") if (path / ".git").exists() else None
        recorded = item.get("commit") if item.get("pinned") else None
        local_stack.append({"name": name, "checkout": checkout or None, "release_set": recorded,
                            "pinned": item.get("pinned") if item else None,
                            "match": checkout == recorded if checkout and recorded else None})
    return {"platform": _platform(root, env),
            "release_set": {"present": data is not None, "error": error},
            "components": components, "local_stack": local_stack}
