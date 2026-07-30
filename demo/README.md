---
title: Pantry Planner
emoji: 🥫
colorFrom: green
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: Recipe → best-value store picks via a staged SQL query plan
---

# 🥫 Pantry Planner — live demo

Paste a recipe (with notes like *"under $30, no dairy, only stores within
10km"*) and watch the **staged query plan** execute: existence check →
store options → brand statistics → substitute lookups → split-trip
optimizer, each step with its SQL, row counts, and abort gates. Or plan a
whole week — the menu optimizer rewards ingredient overlap exactly and
prices the merged basket across stores.

**This Space runs in demo mode**: the two Claude call sites (recipe parse,
product selection) are replaced by deterministic stand-ins so the demo is
free and abuse-proof. The full LLM pipeline — structured tool-use parsing
and a cost-based model router (Haiku ↔ Sonnet) — runs when you deploy with
your own `ANTHROPIC_API_KEY`.

The image is composed from the **same GHCR artifacts** the Kubernetes
deployment uses; the production path ships via GitHub Actions → Argo CD
(GitOps). Source, architecture docs, and the full pipeline:
**[github.com/pjvjay/pantry-platform](https://github.com/pjvjay/pantry-platform)**
