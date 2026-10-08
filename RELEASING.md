# Releasing

How a change to any pantry repo becomes a version number, a published release and a deployment.
This file is the one description of the process for all of them: pantry-api, pantry-db,
pantry-frontend and pantry-platform (with the demo hub). Each repo's README should link here.

- [The model](#the-model)
- [Where things stand](#where-things-stand)
- [Labels](#labels)
- [The 0.x policy](#the-0x-policy)
- [Contributor steps](#contributor-steps)
- [What happens on merge](#what-happens-on-merge)
- [The train](#the-train)
- [The release set](#the-release-set)
- [Rollback](#rollback)
- [Reruns](#reruns)
- [Bootstrap: v0.1.0](#bootstrap-v010) (needs the owner's go-ahead)
- [Credentials](#credentials)
- [What is not versioned](#what-is-not-versioned)
- [Troubleshooting](#troubleshooting)
- [Tooling reference](#tooling-reference)

## The model

There are three layers, and each one feeds the next:

1. **Labels.** Every PR into `main` carries exactly one release label: `release:major`,
   `release:minor`, `release:patch` or `release:none`. The author picks it; a check reads it.
2. **Semver per component.** pantry-api, pantry-db and pantry-frontend each have their own
   `vX.Y.Z`. On every merge to `main` that changes something shipped, the repo's `build.yml`
   works out the next version from the previous tag and the labels of every PR merged since, and
   releases it: a git tag, the built image retagged `X.Y.Z` (never rebuilt), a GitHub Release
   and a pantry-gitops commit that deploys `X.Y.Z`.
3. **The platform train.** pantry-platform pins one tested set of released components. The owner
   runs `scripts/train.py`, which opens a "Pin the release set: ..." PR. Merging it tags the
   platform `vX.Y.Z` and redeploys the public demo from the pinned image digests. The demo hub
   shares the platform's version.

Version numbers are decided before anything is built, and a tag never moves. A mistake is fixed
by the next release, never by rewriting one.

## Where things stand

| Piece | State |
|---|---|
| The semver-labels action, `labels.json`, `scripts/labels.py` | In pantry-platform (`.github/actions/semver-labels`, `scripts/`) |
| The release-label check | `labels.yml` in pantry-platform; each app repo gets the same job, pinned to this action, on its `feat/release-versioning` branch (planned). **Advisory**: it warns and is not a required check |
| The labels themselves | **Not created yet.** `python3 scripts/labels.py` prints the commands; the owner runs `--apply` |
| App `build.yml` (plan, test, build, release, deploy) | Each app repo's `feat/release-versioning` branch; until it merges, builds deploy `dev-<sha>` as before |
| v0.1.0 tags | **Not created.** See [Bootstrap](#bootstrap-v010); needs the owner's go-ahead |
| `scripts/train.py`, `verify_release_set.py`, `release.yml` | In pantry-platform. `train.py` refuses until v0.1.0 exists |
| `release-set.json` | Does not exist yet; the first train writes it |
| `/hub/status` `release` block | In demo-hub; all nulls until a release set and versioned images exist |

## Labels

| Label | Meaning | The version moves (from 1.0.0) |
|---|---|---|
| `release:major` | Breaks something already in use (see the table below) | `X+1.0.0` |
| `release:minor` | New behaviour that nothing already using it notices | `X.Y+1.0` |
| `release:patch` | A fix or internal change to something that ships | `X.Y.Z+1` |
| `release:none` | Changes nothing that ships: docs, tests, CI, tooling | no release |

Below 1.0.0 the [0.x policy](#the-0x-policy) changes the major row.

What ships is listed in each repo's `.github/versioning.json` as `shipped_paths` (for pantry-api,
the code, `pyproject.toml`, the seeds and the Dockerfile; tests and workflows do not ship). A PR
labelled `release:none` must not touch them, and a PR that touches them must not be
`release:none`.

**What counts as major, per component:**

| Component | Major means |
|---|---|
| pantry-api | A REST endpoint, MCP tool, tool argument or response field is removed or renamed, or changes meaning or type. A new required setting or secret with no default. Needing a database schema that the deployed pantry-db does not have (release pantry-db first). |
| pantry-db | A destructive migration: dropping or renaming a table or column, narrowing a type, adding NOT NULL without a default. Removing or renaming seed products or recipes that the api, the demo or the evals refer to. A change to the migrate runner's environment contract (`DB_*`, `MIGRATIONS_DIR`). |
| pantry-frontend | Removing a tab or a hash route people bookmark (`#/planner`, `#/mealplan`). Changing a browser storage key or format (`pantry.mealplan.v1`, `pantry.recipes.v1`) without migrating saved data. Requiring a pantry-api version that is not deployed. |
| pantry-platform and demo-hub | Removing or renaming a `/hub/*` route or a field the console reads. Changing `up.sh`'s ports or environment variables, or the compose contract. A train that carries a component's major (the train sets this floor itself). |

Minor examples: a new endpoint, MCP tool, optional field, tab, migration that only adds, or a new
hub route. Patch examples: a bug fix, a performance change, a refactor, a dependency bump with no
behaviour change, seed corrections. None examples: README and docs, tests, CI workflows,
`scripts/` tooling that is not in the image.

## The 0.x policy

While a component is below 1.0.0:

- `release:major` bumps the **minor** (0.4.2 → 0.5.0), and its Release notes open with a
  **Breaking** heading;
- `release:minor` bumps the minor;
- `release:patch` bumps the patch.

So no combination of labels can produce 1.0.0 by accident. Reaching 1.0.0 takes the dispatch
input `promote_major: 1.0.0` on the repo's `build.yml` (or the platform's `release.yml`), run by
the owner by hand. From 1.0.0 on, standard semver applies. The platform train follows the same
rule.

When several PRs merged since the last tag, the highest level wins and they ship as one release.

| Previous tag | Merged since | Next version | Why |
|---|---|---|---|
| none | anything that ships | 0.1.0 | the baseline in `versioning.json` |
| v0.4.2 | `release:patch` | 0.4.3 | |
| v0.4.2 | `release:minor` | 0.5.0 | |
| v0.4.2 | `release:major` | 0.5.0, notes open with "Breaking" | 0.x policy |
| v0.4.2 | patch, minor, patch | 0.5.0 | the highest label wins; one release |
| v0.4.2 | a shipped change with no label | 0.4.3 | unlabelled counts as patch; the notes say so |
| v0.4.2 | `release:none` only | no release | nothing shipped |
| v0.9.3 | `promote_major: 1.0.0` (dispatch) | 1.0.0 | the only way to 1.0.0 |
| v1.2.3 | `release:major` | 2.0.0 | standard semver |
| v1.2.3 | `release:minor` | 1.3.0 | |
| v1.2.3 | `release:patch` | 1.2.4 | |

The table is a unit test (`test_the_0x_table`, `test_promote_major_is_the_only_way_to_1_0_0` and
`test_plain_semver_from_1_0_0` in `.github/actions/semver-labels/tests`).

## Contributor steps

1. Open the PR with the template filled in.
2. Add **one** release label from the table above.
3. If the PR lands stacked PRs on `main` (a "land" PR), name them on the template's line:
   `Lands: #24, #26`. A title such as "Land #24 and #26 on main: ..." also works.
4. Read the `release-label` job's output. It checks:
   - **R1**: exactly one release label;
   - **R2**: `release:none` if and only if no shipped path changed;
   - **R3**: on a PR into `main`, the label is at least the highest label among the PRs it
     carries. Carried PRs are found from the `Lands:` line, a `Land #N and #M` title, merged PRs
     whose base is this PR's branch (followed recursively, counting only PRs merged into a
     branch before that branch's own PR merged) and `(#N)` commit subjects. Carried PRs without a
     label are listed but set no floor;
   - **R4**: every shipped path also triggers the build (`shipped_paths` ⊆ `build.yml`
     `on.push.paths`). The platform is released by its train, so R4 is skipped there.

   It also prints a prediction, for example `pantry-api: v0.4.2 -> v0.5.0 if this merges next
   (minor)`.

The check is **advisory** for now: a broken rule shows as a warning and does not block the
merge. Making `release-label` a required check is a ruleset change for the owner, planned after
a few weeks of labelled PRs.

Stacked PRs (base is not `main`) still need a label for R1 and R2; R3 is checked on the land PR.

## What happens on merge

This describes each app repo's `build.yml` once its `feat/release-versioning` rework merges.
Until then a merge deploys `dev-<sha>` exactly as before.

1. **plan.** The previous version is the highest `vX.Y.Z` tag merged into `HEAD`. If a tag
   already contains `HEAD`, the run stops ("covered by vX"). Otherwise every first-parent commit
   since that tag is mapped to its PR (GitHub's commit-to-PR link, else the `(#N)` squash
   suffix), and the level is the highest label among those that changed a shipped path. The
   version comes from the 0.x policy. The plan is recorded as `plan.json`.
2. **test** (pantry-api).
3. **build-push**, only when there is something to release: the image is built with the version,
   commit and build time baked in, and pushed as `dev-<short sha>` only.
4. **release**: the tag `vX.Y.Z` is reserved with one API call (whoever creates it owns the
   version); then the built digest is retagged `X.Y.Z`, `X.Y` and `latest` with no rebuild, the
   digest is read back, and a GitHub Release is published whose notes cite only PRs, commits,
   the run and the digest.
5. **deploy**: `bump_image_tag.py` sets `X.Y.Z` in pantry-gitops `apps/kustomization.yaml` and
   pushes "Deploy <image> X.Y.Z (pjvjay/<repo>@<short>)", retrying on a race. Argo CD rolls it out.

The order is always git tag, then image tag, then Release, then deploy. Merges labelled
`release:none` skip build and deploy. Runs share the concurrency group `release-<repo>` and are
never cancelled: GitHub keeps one pending run and replaces older pending ones, which loses
nothing because the plan covers every commit since the last tag.

`build.yml` also takes `workflow_dispatch` inputs: `level` (override the labels; recorded with
who ran it), `dry_run`, `promote_version` and `promote_major`.

## The train

The train pins one released set of components in pantry-platform. The owner runs it:

```bash
python3 scripts/train.py            # dry run (the default): prints the set; changes nothing
python3 scripts/train.py --open-pr  # opens "Pin the release set: api X, db Y, frontend Z"
```

`train.py` uses the owner's `gh` login and anonymous GHCR reads. For each component it takes the
latest GitHub Release, the commit its tag points at and the digest GHCR serves for `X.Y.Z`. It
**refuses**, naming the component, when:

- a component has no release yet;
- a version is released but pantry-gitops `main` does not deploy it (wait for the deploy job, or
  dispatch `promote_version`);
- the digest GHCR serves differs from the one the Release recorded, or from the one pantry-gitops
  pins;
- the image's `org.opencontainers.image.revision` label names another commit (images without the
  label, built before versioning, are reported and not checked).

`--open-pr` works in a temporary worktree of `origin/main`, so no checkout, branch or submodule
working tree of yours is touched. It writes `release-set.json`, moves the five submodule pins,
pins `demo/Dockerfile`'s `FROM` lines to `image:X.Y.Z@sha256:...`, pushes a `train/...` branch
and opens the PR with the label of the largest component move.

On that PR, `verify-pins` runs in **strict** mode (below). Merging it runs `release.yml`, which
checks the set strictly again, takes the largest component move since the previous platform tag
as the floor for the platform's own labels, reserves `vX.Y.Z` and publishes a Release with a
component table ("unchanged since vX" for components that did not move). Render rebuilds the
public demo by itself, because the train changed `demo/Dockerfile`.

**verify-pins modes** (`scripts/verify_release_set.py`, in `verify.yml`):

| Mode | When | Checks |
|---|---|---|
| lenient | any PR that leaves `release-set.json` alone (every PR today) | the existing ancestor and compose checks, unchanged; `release-set.json`, if present, is well formed; pins that moved away from it are noted, not failed |
| strict | a PR that changes `release-set.json` (a train) | `pins-are-tags`, `set-is-the-pins`, `gitops-deploys`, `digests` (GHCR), `demo-dockerfile`, `seeds` (pantry-api and pantry-db `seeds/*.json` byte-identical) |

Gitlink-only PRs (the hand-written pin PRs of today) stay lenient. Once trains are the only way
pins move, the owner can make gitlink changes strict too.

## The release set

`release-set.json` at the platform root is written by `train.py` and never by hand.

```json
{
  "schema": 1,
  "components": {
    "pantry-api": {
      "repo": "pjvjay/pantry-api",
      "version": "0.2.0",
      "tag": "v0.2.0",
      "commit": "<40-hex commit the tag points at>",
      "image": "ghcr.io/pjvjay/pantry-api",
      "digest": "sha256:<64 hex>",
      "release_url": "https://github.com/pjvjay/pantry-api/releases/tag/v0.2.0"
    },
    "pantry-db": { "...": "the same fields", "schema_head": "0006_origin_submissions" },
    "pantry-frontend": { "...": "the same fields" }
  },
  "deploy": {
    "pantry-gitops": { "repo": "pjvjay/pantry-gitops", "commit": "<40 hex>" },
    "pantry-infra": { "repo": "pjvjay/pantry-infra", "commit": "<40 hex>" }
  },
  "local_stack": [
    { "name": "mcp-sim", "pinned": true, "commit": "<40 hex>" },
    { "name": "pantry-gateway", "pinned": true, "commit": "<40 hex>" },
    { "name": "mcp-sim-local", "pinned": false, "note": "a local skill directory, not a repository" },
    { "name": "contextforge", "pinned": false, "note": "installed into its own virtualenv; version not recorded" }
  ]
}
```

| Field | Meaning |
|---|---|
| `schema` | Format version, 1 |
| `components.<name>` | Exactly pantry-api, pantry-db and pantry-frontend; the name is also the submodule path |
| `version`, `tag` | `X.Y.Z` and `vX.Y.Z` |
| `commit` | The commit the tag points at; the submodule pin must equal it |
| `image`, `digest` | The image and the digest GHCR served for `X.Y.Z` when the train ran; `demo/Dockerfile` pins the same digest |
| `release_url` | The component's GitHub Release |
| `schema_head` | pantry-db only: the last migration in that release |
| `deploy.<name>` | pantry-gitops and pantry-infra, by commit; the pinned pantry-gitops commit must deploy every version |
| `local_stack` | What the local demo stack ran with: `pinned: true` entries carry the checked-out commit, the others say why they are not pinned |

`/hub/status` reads it to show, per component, the pinned version against the running one.

## Rollback

Tags never move. To roll back:

- **Quickest:** `git revert` the deploy commit in pantry-gitops. Argo CD rolls the previous version
  back out. The next train refuses until gitops deploys a released set again, which is what you
  want.
- **By version:** dispatch the component's `build.yml` with `promote_version: X.Y.Z` (an existing
  tag). It republishes that release if needed and deploys it, without a rebuild.
- **Then fix forward:** merge the fix with a label; it becomes the next version.

For the platform, revert the train's commit in a PR. That restores the previous
`release-set.json`, verify-pins checks it strictly, and merging it releases the earlier set as the
next platform version (a move down counts as a patch).

## Reruns

Every step is safe to run twice:

- the tag reservation continues when the tag already points at the same commit and fails when it
  points at another (`reserve-tag`);
- retagging an image to the same digest changes nothing; the digest is read back and recorded;
- the GitHub Release step skips a Release that exists;
- the gitops bump is idempotent and retries on a push race;
- a rerun of an older commit that a newer tag already contains stops with "covered by vX".

A run replaced while pending is not lost: the next run's plan includes its commits. If a release
succeeded but its deploy failed, rerun the job or dispatch `promote_version`.

## Bootstrap: v0.1.0

> **Needs the owner's go-ahead (decision D6).** Nothing in this section has been run. Publishing
> tags and labels changes the repositories; run each step yourself, or approve each one.

1. Re-read the live `main` of each app repo and pantry-gitops' `apps/kustomization.yaml`: local
   refs may be stale. When this was written (2026-10-08) the deployed set was:

   | Repo | Deployed image | Commit to tag v0.1.0 |
   |---|---|---|
   | pantry-api | `dev-fa76277` | `fa76277e1a08fcec78f452daf94ec817cf384bb3` |
   | pantry-db | `dev-236d827` | `236d827ad0f49a0c9b0038b7f49f482759be3427` |
   | pantry-frontend | `dev-ff846e7` | `ff846e7a8d54499932b240d1d4b8ec66d5e84ae0` |

2. Create the labels: `python3 scripts/labels.py` to read the commands, then
   `python3 scripts/labels.py --apply`.
3. Tag each deployed commit, for example:
   `gh api repos/pjvjay/pantry-api/git/refs -f ref=refs/tags/v0.1.0 -f sha=fa76277e1a08fcec78f452daf94ec817cf384bb3`.
4. Once each repo's `build.yml` rework has merged, dispatch it with `promote_version: 0.1.0`. It
   retags the deployed `dev-<sha>` digest as `0.1.0`, publishes the Release and deploys `0.1.0`,
   which is the same image: a rollout with no change.
5. These images were built before version injection, so they report "unknown" until each
   component's next release.
6. Run `python3 scripts/train.py`, then `--open-pr`. Merging the first train creates platform
   v0.1.0.

The open stacks need nothing more than a label each before they land, plus `Lands: #24, #26` on
pantry-api #27.

## Credentials

No new credential is needed.

| Credential | Used by | Scope |
|---|---|---|
| `GITHUB_TOKEN` | every workflow | Per job: read-only for the label check; `contents: write` and `packages: write` only in the release jobs |
| `GITOPS_PAT` | app `build.yml` deploy step | A fine-grained PAT with contents write on pjvjay/pantry-gitops only. **Its expiry date is not recorded here**: check it at github.com/settings/personal-access-tokens. When it expires, deploys fail at the push; rotate the secret in each app repo and dispatch `promote_version` for the stuck versions |
| `HF_TOKEN` | `deploy-demo.yml` (optional) | Write to the Hugging Face Space; the job skips without it |
| The owner's `gh` login | `train.py --open-pr`, `labels.py --apply`, the bootstrap | Run by the owner only |
| none | GHCR reads by `train.py` and verify-pins | Anonymous pulls of the public images |

## What is not versioned

- **pantry-gitops and pantry-infra:** identified by commit in `release-set.json`.
- **The local stack:** mcp-sim and pantry-gateway are recorded by the commit checked out next to
  the platform; mcp-sim-local and contextforge are listed as not pinned.
- **Models:** Ollama models and the Gemini or Anthropic model versions the APIs serve.
- **`dev-<sha>` images:** still pushed on every build, for debugging; they are not releases.
- **Seed data** is part of pantry-db's version (and pantry-api's copy must match it in a train).
- **Compatibility between components** is not encoded in the numbers. The train is the tested
  combination; a component major that needs another component says so in its notes.
- **The cluster's Postgres** (CloudNativePG 17.5 in pantry-gitops) moves with pantry-gitops commits.
- **The Hugging Face Space** is a copy of `demo/` pushed by `deploy-demo.yml`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| R1: "no release label" or "2 release labels" | Missing or extra label | Keep exactly one |
| R2: "release:none, but it changes shipped paths" | The PR ships something | Use patch, minor or major |
| R2: "changes no shipped path; use release:none" | Docs, tests or CI only | Use `release:none` |
| R3: "release:patch is below release:minor carried by #24" | A carried PR has a higher label | Raise this PR's label, or fix the carried PR's label if it was wrong |
| R3 lists carried PRs with "no release label" | Older PRs from before labels | Label them if you want them to set a floor; otherwise nothing to do |
| A land PR shows "carried: none found" | No `Lands:` line and no link through base branches | Add `Lands: #N, #M` to the body |
| R4: "shipped_paths not in build.yml push.paths" | A shipped path would never trigger a build | Add it to `on.push.paths`, or drop it from `shipped_paths` |
| plan: "covered by vX" | A newer tag already contains this commit | Nothing; that release covers it |
| plan: "nothing shipped since vX" | Only `release:none` changes | Nothing; dispatch with `level` to force a release |
| reserve-tag: "vX already points at ..., not ..." | Another commit owns that version | Never move the tag; merge a fix and let the next version ship |
| Deploy push keeps failing | A gitops race beyond the retries, or an expired `GITOPS_PAT` | Rerun the job; rotate the PAT if needed |
| train: "is released but pantry-gitops main deploys ..." | The deploy has not landed or failed | Wait, rerun the deploy, or dispatch `promote_version` |
| train: "digest mismatch" | `X.Y.Z` was re-pushed after release, or gitops pins another digest | Investigate before pinning; never pin a mismatch |
| train: "no release yet (bootstrap v0.1.0 first)" | No tags yet | [Bootstrap](#bootstrap-v010) |
| verify-pins strict: `seeds: seeds/products.json differs` | pantry-api's copy of the seeds lags pantry-db's | Release pantry-api with the matching seeds first. On 2026-10-08 the two mains' `products.json` differed |
| verify-pins strict: `demo-dockerfile` | `demo/Dockerfile` edited by hand | Rerun `train.py --open-pr` or restore its `FROM` lines |
| `/hub/status` release block is all null | No `release-set.json` yet, or unversioned images | Expected before the first train and the first releases |

## Tooling reference

The tools are stdlib Python and run anywhere `python3` does. Tests:

```bash
python3 -m unittest discover -s .github/actions/semver-labels/tests
python3 -m unittest discover -s scripts
```

**The semver-labels action** (`.github/actions/semver-labels`) is a composite action around
`semver_labels.py`. App repos pin it by full commit SHA, never by branch:

```yaml
- uses: pjvjay/pantry-platform/.github/actions/semver-labels@<40-hex sha on pantry-platform main>
  with:
    command: plan
```

| Command | Inputs | Outputs |
|---|---|---|
| `check-pr` | `config`, `advisory`, `predict-ref`, `token` | annotations; exit 1 on a broken rule unless advisory |
| `plan` | `config`, `ref`, `level`, `by`, `floor`, `promote-major`, `promote-version`, `plan-file` | `version`, `previous`, `tag`, `level`, `mode` (release, promote or skip), `skip`, `skip-reason` |
| `notes` | `plan-file`, `notes-file`, `run-url`, `image`, `digest`, `release-set`, `previous-set` | `notes-file` |
| `platform-level` | `release-set`, `previous-set` | `level` |
| `reserve-tag` | `tag`, `sha`, `token` (contents write) | exit 1 when the tag exists on another commit |

`plan` and `check-pr`'s `predict-ref` read tags and first-parent history, so the job checks out
with `actions/checkout` `fetch-depth: 0`; `plan` refuses a shallow clone rather than plan the
baseline again. The same commands run locally:
`python3 .github/actions/semver-labels/semver_labels.py --help`. Exit codes are 0 ok, 1 a rule
broken or GitHub refused, 2 usage.

**`.github/versioning.json`**, one per repo:

```json
{
  "component": "pantry-api",
  "image": "ghcr.io/pjvjay/pantry-api",
  "baseline": "0.1.0",
  "shipped_paths": ["Dockerfile", "pyproject.toml", "pantry_planner/**", "seeds/**"],
  "build_workflow": ".github/workflows/build.yml"
}
```

`shipped_paths` use GitHub's path-filter syntax (`**` crosses directories). `build_workflow`
defaults to `.github/workflows/build.yml`; `null` skips R4, as on the platform.

**Platform scripts:** `scripts/labels.py` (prints the label commands; `--apply` runs them),
`scripts/train.py`, `scripts/verify_release_set.py` and `scripts/release_set.py` (the format and
its parsers).
