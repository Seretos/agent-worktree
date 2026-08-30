#!/usr/bin/env bash
# .github/scripts/prev-release-tag.sh -- ticket #184
#
# Prints the strict-semver-highest `<plugin>--v<semver>` tag other than the
# tag currently being created, given candidate tags on stdin (one per line,
# each optionally `refs/tags/`-prefixed). `src/*` marker tags, foreign-
# plugin tags, and malformed tags are filtered out by the same strict-
# semver grammar used everywhere else in this repo's release pipeline.
# Prints nothing (empty stdout, exit 0) when no candidate qualifies.
#
# Ordering uses a fixed-width sort key compared with `LC_ALL=C sort`, never
# `sort -V` -- `-V`'s prerelease handling differs between ubuntu-22.04
# coreutils and Git-for-Windows, and this script runs under both (see
# tests/test_release_scripts.py).
#
# Usage:
#   prev-release-tag.sh <tag-being-created>     # candidate tags on stdin
#   prev-release-tag.sh --check-version <VERSION>  # exit 0/1, same grammar
#
# Pure text processing -- no git, no network, no `gh`.
set -euo pipefail

# Strict semver: MAJOR.MINOR.PATCH (no leading zeros), optional
# dot-separated prerelease identifiers, no build metadata (`+...`).
SEMVER_CORE='(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
SEMVER_PRERELEASE='(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?'
SEMVER_RE="^${SEMVER_CORE}${SEMVER_PRERELEASE}\$"

if [ "${1:-}" = "--check-version" ]; then
  version="${2:-}"
  if [[ "$version" =~ $SEMVER_RE ]]; then
    exit 0
  else
    exit 1
  fi
fi

tag="${1:?usage: prev-release-tag.sh <tag-being-created>  (candidates on stdin)}"
plugin="${tag%%--v*}"
tag_re="^${plugin}--v${SEMVER_CORE}${SEMVER_PRERELEASE}\$"

# Build "<sort-key><TAB><candidate-tag>" lines for every valid, non-self
# candidate; `LC_ALL=C sort | tail -1` then picks the highest.
keyed_candidates=""
while IFS= read -r line || [ -n "$line" ]; do
  # Strip a trailing CR: on Windows, Python's `text=True` subprocess input
  # translates `\n` to `\r\n`, so every candidate line arrives CR-terminated
  # when this script is driven from tests/test_release_scripts.py.
  line="${line%$'\r'}"
  [ -z "$line" ] && continue
  candidate="${line#refs/tags/}"
  [ "$candidate" = "$tag" ] && continue
  [[ "$candidate" =~ $tag_re ]] || continue

  version="${candidate#"${plugin}--v"}"
  core="${version%%-*}"
  major="${core%%.*}"
  rest="${core#*.}"
  minor="${rest%%.*}"
  patch="${rest#*.}"

  if [[ "$version" == *-* ]]; then
    prerelease="${version#*-}"
    release_flag=0
  else
    prerelease=""
    release_flag=1
  fi

  key=$(printf '%010d%010d%010d%d' "$((10#$major))" "$((10#$minor))" "$((10#$patch))" "$release_flag")

  if [ -n "$prerelease" ]; then
    id_key=""
    IFS='.' read -ra ids <<<"$prerelease"
    for id in "${ids[@]}"; do
      if [[ "$id" =~ ^[0-9]+$ ]]; then
        token=$(printf '0%020d' "$((10#$id))")
      else
        token="1${id}"
      fi
      if [ -z "$id_key" ]; then
        id_key="$token"
      else
        id_key="${id_key}.${token}"
      fi
    done
    key="${key}.${id_key}"
  fi

  keyed_candidates="${keyed_candidates}${key}"$'\t'"${candidate}"$'\n'
done

if [ -z "$keyed_candidates" ]; then
  exit 0
fi

printf '%s' "$keyed_candidates" | LC_ALL=C sort | tail -1 | cut -f2-
