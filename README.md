# 🥫 pantry-platform

A complete, small-scale **GitOps reference implementation**: a grocery-matching
app (recipes → best-value store picks) — React + FastAPI running a Claude-based
agentic workflow that **generates NL2SQL** to query a Postgres grocery database
for the most relevant data meeting natural-language search constraints, then
uses a **model router** to cost-effectively select the model based on the size
of the data and the evaluated complexity; on AKS via GitOps (GitHub Actions,
Argo CD), where **a git commit is the only deployment mechanism**.

This umbrella repo pins all five components as git submodules, carries the
architecture docs, and ships a one-command local run.

**▶ Live demo:** **[pantry-planner-demo.onrender.com/pantry/](https://pantry-planner-demo.onrender.com/pantry/)** —
a single-container build of the same GHCR images in
[demo mode](https://github.com/pjvjay/pantry-api#demo-mode) (deterministic
stand-ins at the two LLM call sites; the query-plan SQL, abort gates, and
trip/week optimizers run for real). Free-tier hosting: if it's been idle,
give it ~a minute to wake. Deploy config: [`demo/`](demo/) +
[`render.yaml`](render.yaml).

**GitOps deployment:** the production path deploys to AKS via the full
pipeline (see the verified run in
[pantry-gitops's commit history](https://github.com/pjvjay/pantry-gitops/commits/main) —
every `bump … to dev-<sha>` commit is CI deploying). The dev cluster runs
**on demand** to keep idle cost at zero; reproduce the whole stack locally in
one command (below).

## The repos

| Repo (submodule) | Layer | CI output |
|---|---|---|
| [pantry-frontend](https://github.com/pjvjay/pantry-frontend) | React + Vite SPA behind nginx | `ghcr.io/pjvjay/pantry-frontend` |
| [pantry-api](https://github.com/pjvjay/pantry-api) | FastAPI + Claude model-router pipeline (Burr state machine) | `ghcr.io/pjvjay/pantry-api` |
| [pantry-db](https://github.com/pjvjay/pantry-db) | Schema migrations + seeds, psql runner | `ghcr.io/pjvjay/pantry-db-migrate` |
| [pantry-gitops](https://github.com/pjvjay/pantry-gitops) | ArgoCD app-of-apps + Kustomize manifests | — (watched by ArgoCD) |
| [pantry-infra](https://github.com/pjvjay/pantry-infra) | Terraform bootstrap: Key Vault secret + ArgoCD root app | — |

## How a change ships

```mermaid
flowchart LR
    push["git push<br/>app repo"] --> ci["GitHub Actions<br/>test · build · push"]
    ci --> ghcr[("GHCR")]
    ci -->|"kustomize edit set image<br/>+ commit"| gitops["pantry-gitops"]
    gitops --> argo["ArgoCD"]
    argo -->|"reconcile"| aks["AKS"]
    aks -.->|pull| ghcr
```

No `kubectl apply` from a laptop, no CI credentials against the cluster —
CI's only cluster-facing permission is **write access to one git repo**.
Rollback = `git revert`.

## Runtime architecture

```mermaid
flowchart TB
    user["Browser"] -->|HTTPS| ing["ingress-nginx<br/>(shared AKS ingress + Let's Encrypt)"]
    agent["MCP client<br/>Claude Desktop · Claude Code"] -->|"HTTPS or local stdio"| ing
    ing -->|"/pantry"| fe["pantry-frontend<br/>nginx + React"]
    ing -->|"/pantry/api (rewrite)"| api["pantry-api<br/>FastAPI :8000<br/>REST + MCP /mcp"]
    api -->|SQL| pg[("CNPG Postgres 17<br/>ns pantry-db")]
    api -->|"Haiku ↔ Sonnet<br/>model router"| claude["Anthropic API"]
    job["migrate Job<br/>(ArgoCD PreSync)"] -->|"DDL + seeds"| pg
    eso["External Secrets Operator"] -.->|"projects"| sec["K8s Secrets<br/>db creds · API key"]
    kv[("Azure Key Vault")] -.-> eso
    sec -.-> api
```

## GitOps practices demonstrated

- **App-of-Apps** — one root Application fans out to an `AppProject` +
  child Applications; adding a service is a git commit, not an ArgoCD change
- **Scoped AppProject** — the demo can only deploy from its own repo into
  its own namespaces; Namespace is the only cluster-scoped kind allowed
- **Sync waves + PreSync hooks** — namespaces → secrets → database →
  migrations → workloads, ordered and enforced
- **Separation of concerns** — schema (pantry-db) ≠ app (pantry-api) ≠
  desired state (pantry-gitops) ≠ bootstrap (pantry-infra)
- **No secrets in git** — Key Vault → External Secrets Operator via Workload
  Identity; the gitops repo holds only references
- **Immutable, multi-arch images** — every deploy pins `dev-<sha>`; `latest`
  exists only for local pulls
- **Self-heal + prune** — manual cluster drift reverts automatically

## Run it locally

```bash
git clone --recurse-submodules https://github.com/pjvjay/pantry-platform
cd pantry-platform
export ANTHROPIC_API_KEY=sk-ant-...   # optional — browsing works without it
docker compose up --build
# → http://localhost:8080/pantry/
```

Same containers, same migration flow as the cluster — compose plays the role
of ArgoCD + ingress.

## Use it from an MCP client

The same pipeline is exposed over the
[Model Context Protocol](https://modelcontextprotocol.io), so Claude Desktop,
Claude Code, or any agent can browse the catalog, resolve where products come
from, and run the planners as tools. Two transports, one server definition:
a `pantry-mcp` console script (stdio, for locally-launched clients) and a
Streamable HTTP endpoint mounted on the API itself at `/pantry/api/mcp`.

```bash
claude mcp add --transport http pantry http://localhost:8080/pantry/api/mcp
```

Setup, the full tool list, and the no-auth caveat for the HTTP endpoint:
[pantry-api § MCP server](https://github.com/pjvjay/pantry-api#mcp-server).

## Deploy it

```bash
cd pantry-infra
terraform apply        # seeds Key Vault + the ArgoCD root Application
```

Everything else converges from git. Details in
[pantry-infra](https://github.com/pjvjay/pantry-infra) and
[pantry-gitops](https://github.com/pjvjay/pantry-gitops).

## Working with the submodules

Submodule pins mark a **known-good set** across the five repos — a
platform-level release marker.

```bash
git submodule update --remote --merge   # pull every repo to latest main
git commit -am "pin: <what changed>"    # record the new known-good set
```

## Checks on pull requests into main

[`.github/workflows/verify.yml`](.github/workflows/verify.yml) runs four jobs
on the pinned set for every pull request into `main` (and, from the Actions
tab, by hand on any branch):

| Job | What it checks |
|---|---|
| `verify-pins` | Every pin resolves and is a merged commit on its repo's `main`; `docker compose config` parses. |
| `hub-tests` | demo-hub's tests pass on Python 3.12, with no network and no keys. |
| `shared-seed` | `seeds/products.json` and `seeds/recipes.json` are byte-identical in pantry-api and pantry-db. pantry-api's tests seed from its copy; production's `seed.sql` is rendered from pantry-db's. |
| `schema-parity` | pantry-db's migrations and pantry-api's SQLAlchemy models build the same schema on Postgres 17. |

Only `verify-pins` is a required check in the ruleset on `main`; the other
three report on the pull request but do not block a merge until the ruleset
lists them too.

The jobs check the pinned submodules, so a pantry-db migration or a
pantry-api model change gets its `shared-seed` and `schema-parity` check
here, when a pull request moves its pin, not when it merges in its own repo.
Run the parity check locally (below) before merging such a change there, or
the drift turns up in the next pin bump. A pull request stacked on another
branch runs no checks until it targets `main`.

### Reading a schema-parity failure

The job builds two databases from the pinned submodules: `mig` with
pantry-db's migrate image (`run-migrations.sh`, as in the cluster) and `orm`
with pantry-api's `Base.metadata.create_all`.
[`scripts/schema_parity.py`](scripts/schema_parity.py) then prints one line
per difference, always `mig` first and `orm` second:

```text
Not allowed. Change the model or add a migration so the two agree; ...
  "column recipe_line_amounts.unit: nullable mig no, orm yes",
```

That line says the migration declares `recipe_line_amounts.unit` NOT NULL
and the model lets it be NULL. The other kinds of line:

| Line | Meaning |
|---|---|
| `table T: only in mig` (or `orm`) | Only one side has the table. `only in orm` usually means a model whose migration is missing. |
| `column T.C: only in mig` (or `orm`) | Only one side has the column. |
| `column T.C: type mig A, orm B` | The types differ after normalising (`String` and `text`, `Float` and `double precision` count as the same). |
| `column T.C: nullable mig no, orm yes` | NOT NULL on one side only. |
| `column T.C: default mig A, orm B` | The server defaults differ. `none` is no DEFAULT, and `serial` and `identity` are auto-numbered keys. A model's Python-side `default=` is not a server default. |
| `table T: primary key ...`, `foreign key ...`, `check ...` | The constraint differs, or only one side has it. |

The migrations are the source of truth for the deployed schema (as
`0001_init.sql` says), so usually the model changes to match. If the
database has to change, add a new migration: `run-migrations.sh` never re-runs
one that is already applied, so editing it changes nothing in the cluster.
If the difference is intended, add the line exactly as printed to
[`scripts/schema_parity_allow.json`](scripts/schema_parity_allow.json), in a
group whose `reason` says why it is safe. Lines under "allowed differences
not found here" do not fail the job: they are for tables the pinned commits
do not have yet, or for differences fixed since, which can then be removed.

To run it locally against any Postgres, with pantry-api installed
(`pip install ./pantry-api`):

```bash
psql -c 'CREATE DATABASE mig' -c 'CREATE DATABASE orm'
docker build -t pantry-db-migrate pantry-db
# On Docker Desktop, drop --network host and set DB_HOST=host.docker.internal
docker run --rm --network host -e DB_HOST=127.0.0.1 -e DB_USER=pantry \
  -e DB_PASSWORD=pantry -e DB_NAME=mig pantry-db-migrate
python -c "import sqlalchemy as sa; from pantry_planner.db import Base; \
  Base.metadata.create_all(sa.create_engine('postgresql+psycopg://pantry:pantry@127.0.0.1:5432/orm'))"
python scripts/schema_parity.py --verbose \
  --mig postgresql+psycopg://pantry:pantry@127.0.0.1:5432/mig \
  --orm postgresql+psycopg://pantry:pantry@127.0.0.1:5432/orm
```

`--verbose` also prints the allowed differences, grouped under their reasons.

## Origin

The app itself (the Claude model-router pipeline) predates the platform —
it was a standalone interview artifact. This umbrella wraps it in a
production-grade GitOps polyrepo architecture, scaled down to be readable
in an afternoon.

## License

[MIT](LICENSE).

This repo carries the architecture docs, the compose stack and the submodule
pins. Each component repo is its own project — `pantry-api` already declares
MIT in its `pyproject.toml`; the others state no licence of their own yet, so
this file covers what lives here rather than speaking for them.
