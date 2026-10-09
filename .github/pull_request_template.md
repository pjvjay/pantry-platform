<!--
Title: sentence case, saying what changed ("Seeds: yellow onions in bags").
The process behind every section below is in RELEASING.md at the root of pjvjay/pantry-platform.
-->

## What and why

<!-- What changes for whoever uses this, and why. Link the issue or plan section. -->

## Release label

Add exactly one label; the release-label check reads it (RELEASING.md, "Labels"):

- [ ] `release:major`: breaks something already in use (RELEASING.md lists what that means for this repo). Below 1.0.0 it bumps the minor, and the notes open with "Breaking".
- [ ] `release:minor`: new behaviour that nothing already using it notices (an endpoint, tool, field, tab or migration).
- [ ] `release:patch`: a fix or internal change to something that ships.
- [ ] `release:none`: changes nothing that ships (docs, tests, CI, tooling).

A PR that lands stacked PRs on main names them here, so its label is at least theirs:

Lands: <!-- e.g. #24, #26; leave empty when this PR carries no other PR -->

## Data sources

- [ ] No new data source, or each new one is listed here with its licence, size and who approved the download.
- [ ] Every price, store, origin, freshness time and nutrition value shown comes from a tool result or a cited row, or is shown as unknown.
- [ ] Synthetic data (store prices, stock, reviews, starter recipes, house amounts) is labelled as such.
- [ ] No key, token or personal data is committed, logged or put in a URL.

## Gateway refresh

- [ ] This PR does not change MCP tools or their arguments.
- [ ] It does: the rollout notes say to run `REFRESH_PANTRY=true scripts/register_fetch.sh` (pantry-gateway) and `make_scenarios.py` after deploy, so ContextForge stops serving the old schemas.

## Live smoke

Run against `demo-hub/scripts/up.sh` in DEMO_MODE in a real browser; record pass or fail per step and attach a screenshot of the final state. Write "Not applicable: <reason>" when nothing a person sees changed.

1.
2.
3.

Result:

## Tests

<!-- The commands run and their results, e.g. `pytest -q`: 123 passed. -->
