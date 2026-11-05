#!/usr/bin/env bash
# Usage: checkout_refs.sh <submodule> <ref> [<submodule> <ref> ...]
#
# Moves each submodule from its pin to <ref>, a branch name or a full commit
# SHA in that submodule's own repo; an empty <ref> leaves the pin. verify.yml
# runs this on a manual run, so a pantry-db migration and the pantry-api
# model that mirrors it can be checked together before either one merges.
set -euo pipefail

while [ $# -ge 2 ]; do
  sub=$1 ref=$2
  shift 2
  if [ -z "$ref" ]; then
    echo "$sub stays at its pin $(git -C "$sub" rev-parse --short HEAD)"
    continue
  fi
  # The ref reaches git as an argument, and one that starts with "-" would
  # be read as an option.
  case $ref in
    -* | *[!A-Za-z0-9._/-]*)
      echo "::error::$sub: '$ref' is not a branch name or a commit SHA"
      exit 1 ;;
  esac
  # The submodule checkout is shallow, so the ref is fetched by name; GitHub
  # serves a commit by its full SHA but not by a short one.
  if ! git -C "$sub" fetch -q --depth 1 origin "$ref"; then
    echo "::error::$sub: could not fetch '$ref'; give a branch name or a full 40-character commit SHA"
    exit 1
  fi
  git -C "$sub" checkout -q --detach FETCH_HEAD
  echo "$sub at $(git -C "$sub" rev-parse --short HEAD) ($ref)"
done
