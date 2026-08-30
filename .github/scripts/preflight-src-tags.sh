#!/usr/bin/env bash
# .github/scripts/preflight-src-tags.sh <TAG> -- ticket #184
#
# `stamp`-job pre-flight for the orphan-tag release flow. Four ordered
# checks, each `::error::` + `exit 1` *before any side effect* of the
# release pipeline:
#
#   (a) HEAD must equal main's tip (read via `gh api`, never a local ref --
#       this is the precondition for check (d)'s push being accepted by the
#       workflow-modification guard, so it is verified here rather than
#       discovered later as a failed push in `assemble`).
#   (b) `src/<TAG>` must not already exist (the marker is immutable; a
#       burned version is retried under a new version number, never by
#       deleting a marker). Fails closed: only a genuine 404 is treated as
#       "absent, proceed"; any other `gh api` failure (rate-limit, 5xx,
#       network blip) hard-stops rather than assuming absence.
#   (c) if a predecessor release resolves (via prev-release-tag.sh), its
#       `src/<PREV_TAG>` marker must already exist -- otherwise print the
#       one-time bootstrap commands with a literal `<head_sha>` placeholder.
#   (d) no predecessor at all (first-ever release) is allowed.
#
# All remote state is read through `gh api`, never through local git refs --
# matching the existing "Fail if tag already exists" check in release.yml
# and removing any dependence on how complete the checkout's tag fetch
# happened to be.
#
# On success: prints `prev_tag=<...>` (possibly empty on a first-ever
# release) to stdout, appended to $GITHUB_OUTPUT.
#
# Env: REPO (required), GITHUB_REF (optional, used only in the check-(a)
# error message), GITHUB_OUTPUT (required in real CI use; falls back to
# /dev/null so this script stays runnable standalone).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREV_RELEASE_TAG_SCRIPT="${SCRIPT_DIR}/prev-release-tag.sh"

TAG="${1:?usage: preflight-src-tags.sh <TAG>}"
PLUGIN="${TAG%%--v*}"
REPO="${REPO:?REPO must be set}"
GITHUB_REF="${GITHUB_REF:-<unset>}"
GITHUB_OUTPUT="${GITHUB_OUTPUT:-/dev/null}"

# --- (a) dispatched from main's tip -------------------------------------
HEAD_SHA="$(git rev-parse HEAD)"
MAIN_SHA="$(gh api "repos/${REPO}/git/refs/heads/main" --jq '.object.sha')"
if [ "$HEAD_SHA" != "$MAIN_SHA" ]; then
  echo "::error::This run is on ${HEAD_SHA} (${GITHUB_REF}), but main's tip is ${MAIN_SHA}. Re-run release.yml from main's current tip." >&2
  exit 1
fi

# --- (b) src/<TAG> must not already exist --------------------------------
# Fails closed: a genuine 404 ("HTTP 404" in gh's stderr) means the tag is
# absent and it is safe to proceed; any other failure (rate-limit, 5xx,
# network blip) is ambiguous and must hard-stop rather than be treated as
# "tag absent" -- same fail-closed posture as check (c) below, whose
# "predecessor marker must exist" branch already hard-stops on any error.
SRC_TAG_STDERR="$(mktemp)"
if gh api "repos/${REPO}/git/refs/tags/src/${TAG}" >/dev/null 2>"$SRC_TAG_STDERR"; then
  echo "::error::Marker tag src/${TAG} already exists; version ${TAG} is burned. Pick a new version number and re-run." >&2
  echo "Do not delete or move the marker tag; leftover markers stay inert." >&2
  rm -f "$SRC_TAG_STDERR"
  exit 1
elif ! grep -q "HTTP 404" "$SRC_TAG_STDERR"; then
  echo "::error::Could not determine whether marker tag src/${TAG} exists -- gh api failed for a reason other than a 404 Not Found (rate-limit, transient 5xx, or network error). Refusing to assume the marker is absent; re-run once the underlying gh api error is resolved." >&2
  cat "$SRC_TAG_STDERR" >&2
  rm -f "$SRC_TAG_STDERR"
  exit 1
fi
rm -f "$SRC_TAG_STDERR"

# --- (c)/(d) predecessor marker must exist, if a predecessor resolves ----
CANDIDATES="$(gh api "repos/${REPO}/git/matching-refs/tags/${PLUGIN}--v" --paginate --jq '.[].ref')"
PREV_TAG="$(printf '%s\n' "$CANDIDATES" | "$BASH" "$PREV_RELEASE_TAG_SCRIPT" "$TAG")"

if [ -n "$PREV_TAG" ]; then
  if ! gh api "repos/${REPO}/git/refs/tags/src/${PREV_TAG}" >/dev/null 2>&1; then
    {
      echo "::error::Missing marker tag src/${PREV_TAG} for the previous release ${PREV_TAG}."
      echo "Read the head SHA from the Actions run that published ${PREV_TAG} and run:"
      echo "  git tag src/${PREV_TAG} <head_sha>"
      echo "  git push origin src/${PREV_TAG}"
    } >&2
    exit 1
  fi
fi

echo "prev_tag=${PREV_TAG}"
echo "prev_tag=${PREV_TAG}" >> "$GITHUB_OUTPUT"
