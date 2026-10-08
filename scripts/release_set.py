"""The release set: which released version of each component the platform pins, and the checks
that keep release-set.json, the submodule pins, pantry-gitops and demo/Dockerfile in agreement.

train.py writes release-set.json; verify_release_set.py checks it on every pull request. The
format is documented in RELEASING.md ("The release set"). Stdlib only, like the rest of the
release tooling.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

SCHEMA = 1
OWNER = "pjvjay"
# The components released by tag. The key is also the submodule path.
COMPONENTS: dict[str, dict[str, str]] = {
    "pantry-api": {"repo": f"{OWNER}/pantry-api", "image": f"ghcr.io/{OWNER}/pantry-api"},
    "pantry-db": {"repo": f"{OWNER}/pantry-db", "image": f"ghcr.io/{OWNER}/pantry-db-migrate"},
    "pantry-frontend": {"repo": f"{OWNER}/pantry-frontend",
                        "image": f"ghcr.io/{OWNER}/pantry-frontend"},
}
# Deploy repos are not released; the set pins them by commit.
DEPLOY: dict[str, str] = {"pantry-gitops": f"{OWNER}/pantry-gitops",
                          "pantry-infra": f"{OWNER}/pantry-infra"}
KUSTOMIZATION = "apps/kustomization.yaml"
DOCKERFILE = "demo/Dockerfile"
# pantry-api carries a copy of pantry-db's JSON seeds for SQLite; a set ships them identical.
SEED_FILES = ("seeds/products.json", "seeds/recipes.json")

VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
NOTES_DIGEST_RE = re.compile(r"^Digest: `(sha256:[0-9a-f]{64})`", re.MULTILINE)


def release_url(repo: str, tag: str) -> str:
    return f"https://github.com/{repo}/releases/tag/{tag}"


def validate(data: Any) -> list[str]:
    """Every way `data` is not a well-formed release set; empty when it is."""
    if not isinstance(data, dict):
        return ["release-set.json is not a JSON object"]
    problems = []
    if data.get("schema") != SCHEMA:
        problems.append(f"schema must be {SCHEMA}")
    components = data.get("components")
    if not isinstance(components, dict) or set(components) != set(COMPONENTS):
        problems.append(f"components must be exactly {', '.join(sorted(COMPONENTS))}")
        components = components if isinstance(components, dict) else {}
    for name, entry in components.items():
        expected = COMPONENTS.get(name)
        if expected is None or not isinstance(entry, dict):
            continue
        version = str(entry.get("version", ""))
        checks = [
            (VERSION_RE.match(version), f"version {version!r} is not X.Y.Z"),
            (entry.get("tag") == f"v{version}", f"tag must be v{version}"),
            (entry.get("repo") == expected["repo"], f"repo must be {expected['repo']}"),
            (entry.get("image") == expected["image"], f"image must be {expected['image']}"),
            (SHA_RE.match(str(entry.get("commit", ""))), "commit must be 40 hex"),
            (DIGEST_RE.match(str(entry.get("digest", ""))), "digest must be sha256:<64 hex>"),
            (entry.get("release_url") == release_url(expected["repo"], f"v{version}"),
             "release_url must be the tag's release page"),
        ]
        if name == "pantry-db":
            checks.append((isinstance(entry.get("schema_head"), str) and entry["schema_head"],
                           "schema_head must name the last migration"))
        problems += [f"{name}: {message}" for ok, message in checks if not ok]
    deploy = data.get("deploy")
    if not isinstance(deploy, dict) or set(deploy) != set(DEPLOY):
        problems.append(f"deploy must be exactly {', '.join(sorted(DEPLOY))}")
        deploy = deploy if isinstance(deploy, dict) else {}
    for name, entry in deploy.items():
        if name in DEPLOY and not (isinstance(entry, dict) and entry.get("repo") == DEPLOY[name]
                                   and SHA_RE.match(str(entry.get("commit", "")))):
            problems.append(f"{name}: needs repo {DEPLOY[name]} and a 40-hex commit")
    stack = data.get("local_stack", [])
    if not isinstance(stack, list):
        problems.append("local_stack must be a list")
        stack = []
    for item in stack:
        if not isinstance(item, dict) or not item.get("name") or \
                not isinstance(item.get("pinned"), bool):
            problems.append(f"local_stack entry {item!r} needs a name and pinned true/false")
        elif item["pinned"] and not SHA_RE.match(str(item.get("commit", ""))):
            problems.append(f"local_stack {item['name']}: a pinned entry needs a 40-hex commit")
    return problems


def dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2) + "\n"


# --- pantry-gitops -------------------------------------------------------------------------------


def deployed_images(kustomization: str) -> dict[str, dict[str, str | None]]:
    """The images block of apps/kustomization.yaml, line by line like bump_image_tag.py: name ->
    {tag, digest, comment}. A digest line may carry the version as a comment
    ('digest: sha256:... # 0.2.0'), which is how pantry-gitops pins by digest."""
    images: dict[str, dict[str, str | None]] = {}
    current: dict[str, str | None] | None = None
    for line in kustomization.splitlines():
        stripped = line.strip()
        if stripped.startswith("- name:"):
            current = {"tag": None, "digest": None, "comment": None}
            images[stripped.split(":", 1)[1].strip()] = current
        elif current is not None and stripped.startswith("newTag:"):
            current["tag"] = stripped.split(":", 1)[1].split("#", 1)[0].strip().strip("'\"")
        elif current is not None and stripped.startswith("digest:"):
            value, _, comment = stripped.split(":", 1)[1].partition("#")
            current["digest"] = value.strip().strip("'\"")
            current["comment"] = comment.strip() or None
    return images


def deploys(entry: dict[str, str | None] | None, version: str, digest: str) -> bool:
    if not entry:
        return False
    if entry.get("digest"):
        return entry["digest"] == digest
    return entry.get("tag") == version


# --- demo/Dockerfile -----------------------------------------------------------------------------

FROM_RE = re.compile(r"^(?P<lead>FROM\s+)(?P<image>[^\s:@]+(?::\d+)?/[^\s:@]+)"
                     r"(?::(?P<tag>[^\s@]+))?(?:@(?P<digest>sha256:[0-9a-f]{64}))?(?P<rest>.*)$",
                     re.IGNORECASE)


def from_lines(dockerfile: str) -> dict[str, dict[str, str | None]]:
    """image -> {tag, digest} for each FROM line naming a registry image."""
    found = {}
    for line in dockerfile.splitlines():
        match = FROM_RE.match(line.strip())
        if match:
            found[match.group("image")] = {"tag": match.group("tag"),
                                           "digest": match.group("digest")}
    return found


def pin_dockerfile(dockerfile: str, components: dict[str, dict[str, Any]]) -> str:
    """Rewrite each FROM line that names a component image to image:X.Y.Z@sha256:..., keeping
    any 'AS name' and every other line, comment and blank line as it was."""
    by_image = {c["image"]: c for c in components.values()}
    out = []
    for line in dockerfile.splitlines(keepends=True):
        match = FROM_RE.match(line.strip())
        component = by_image.get(match.group("image")) if match else None
        if match and component:
            newline = "\n" if line.endswith("\n") else ""
            line = (f"{match.group('lead')}{component['image']}:{component['version']}"
                    f"@{component['digest']}{match.group('rest')}{newline}")
        out.append(line)
    return "".join(out)


# --- GHCR, read anonymously ----------------------------------------------------------------------

MANIFEST_TYPES = ("application/vnd.oci.image.index.v1+json, "
                  "application/vnd.docker.distribution.manifest.list.v2+json, "
                  "application/vnd.oci.image.manifest.v1+json, "
                  "application/vnd.docker.distribution.manifest.v2+json")


class Registry:
    """Digests and labels of public GHCR images, through the registry API with an anonymous pull
    token. No docker, no login, no write access."""

    def __init__(self, host: str = "ghcr.io", timeout: float = 20) -> None:
        self.host, self.timeout = host, timeout
        self._tokens: dict[str, str] = {}

    def _name(self, image: str) -> str:
        prefix = self.host + "/"
        if not image.startswith(prefix):
            raise ValueError(f"{image} is not on {self.host}")
        return image[len(prefix):]

    def _token(self, name: str) -> str:
        if name not in self._tokens:
            url = f"https://{self.host}/token?scope=repository:{name}:pull&service={self.host}"
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                self._tokens[name] = json.loads(resp.read())["token"]
        return self._tokens[name]

    def _get(self, name: str, path: str, accept: str, method: str = "GET") -> tuple[bytes, Any]:
        req = urllib.request.Request(f"https://{self.host}/v2/{name}/{path}", method=method)
        req.add_header("Authorization", f"Bearer {self._token(name)}")
        req.add_header("Accept", accept)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return (resp.read() if method == "GET" else b""), resp.headers

    def digest(self, image: str, tag: str) -> str | None:
        """The digest a tag points at now, or None when the tag does not exist."""
        try:
            _, headers = self._get(self._name(image), f"manifests/{tag}", MANIFEST_TYPES, "HEAD")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        return headers.get("Docker-Content-Digest")

    def revision(self, image: str, digest: str) -> str | None:
        """The org.opencontainers.image.revision label of the linux/amd64 image, or None when the
        image has no such label (images built before versioning) or it cannot be read."""
        name = self._name(image)
        try:
            body, _ = self._get(name, f"manifests/{digest}", MANIFEST_TYPES)
            manifest = json.loads(body)
            if "manifests" in manifest:
                chosen = next((m for m in manifest["manifests"]
                               if (m.get("platform") or {}).get("architecture") == "amd64"
                               and (m.get("platform") or {}).get("os") == "linux"), None)
                if chosen is None:
                    return None
                body, _ = self._get(name, f"manifests/{chosen['digest']}", MANIFEST_TYPES)
                manifest = json.loads(body)
            config = manifest["config"]["digest"]
            body, _ = self._get(name, f"blobs/{config}", "application/octet-stream")
            labels = (json.loads(body).get("config") or {}).get("Labels") or {}
        except (urllib.error.URLError, ValueError, KeyError):
            return None
        return labels.get("org.opencontainers.image.revision")
