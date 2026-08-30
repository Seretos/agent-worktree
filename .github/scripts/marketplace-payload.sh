#!/usr/bin/env bash
# .github/scripts/marketplace-payload.sh -- ticket #184
#
# Builds the `{event_type, client_payload}` repository_dispatch JSON body
# for the agent-marketplace dispatch. Shared between release.yml's own
# "Dispatch to agent-marketplace" step and dispatch.yml's manual-retry
# equivalent of the same step -- this used to be a ~70-line inline jq
# program duplicated verbatim in both workflow files.
#
# Required env: REPO, TAG, VERSION, PLUGIN_JSON, MAX_CHANGELOG_LEN.
# GH_TOKEN is consumed by the `gh` binary itself, not read here directly.
# Prints the JSON payload on stdout; callers pipe it straight into `curl`.
#
# Changelog source: the *published* release body (`gh release view --json
# body`), never a fresh notes-generation API call made directly against the
# orphan tag -- the orphan release commit has no merge-base with any prior
# tag, so a freshly generated set of notes for it always comes back empty;
# that emptiness is the bug this ticket fixes. The notes generated (by the
# "Create tag and GitHub Release" step, elsewhere) against the parallel
# `src/<TAG>` marker tag -- which DOES have real ancestry -- land in the
# published release body and are read back here unmodified.
#
# Failure split:
#   - `gh release view` itself failing (release not found, API/network
#     error) is FATAL: under `set -euo pipefail`, the `&&`-chained
#     assignment below inherits that non-zero status and the script aborts,
#     printing no payload on stdout, so the caller's `| curl ...` pipe
#     sends nothing. The release we just published must be fetchable.
#   - An empty / whitespace-only / literal-"null" *body* is not a pipeline
#     failure (a real release can simply have no notes yet): warn via
#     `::warning::` and omit the `changelog` key, exit 0.
set -euo pipefail

REPO="${REPO:?REPO must be set}"
TAG="${TAG:?TAG must be set}"
VERSION="${VERSION:?VERSION must be set}"
PLUGIN_JSON="${PLUGIN_JSON:?PLUGIN_JSON must be set}"
MAX_CHANGELOG_LEN="${MAX_CHANGELOG_LEN:?MAX_CHANGELOG_LEN must be set}"

NAME=$(jq -r '.name' "$PLUGIN_JSON")
DESC=$(jq -r '.description' "$PLUGIN_JSON")

RELEASE_URL="https://github.com/${REPO}/releases/tag/${TAG}"
ICON_URL="https://raw.githubusercontent.com/${REPO}/${TAG}/assets/icon.png"
DESCRIPTION_URL="https://raw.githubusercontent.com/${REPO}/${TAG}/description.md"

# `&& printf x` plus the two suffix strips below preserve the changelog
# body's own trailing whitespace/newlines exactly, minus the one trailing
# newline `gh --jq` itself appends -- plain `$(...)` command substitution
# would otherwise silently strip ALL trailing newlines, corrupting a body
# that legitimately ends in one. A failing `gh release view` aborts here
# (fatal -- see module comment above).
CHANGELOG=$(gh release view "$TAG" --repo "$REPO" --json body --jq '.body' && printf x)
CHANGELOG="${CHANGELOG%x}"
CHANGELOG="${CHANGELOG%$'\n'}"

# Trim leading/trailing whitespace via jq's regex engine, not a bash
# extglob pattern -- `${VAR##+([[:space:]])}` is catastrophically slow
# (multi-second-plus) on changelog bodies in the tens of thousands of
# characters. Used only to decide HAVE_CHANGELOG; the untrimmed $CHANGELOG
# above (not this trimmed copy) is what actually goes into the payload.
TRIMMED_CHANGELOG=$(printf '%s' "$CHANGELOG" | jq -Rsj 'gsub("^[[:space:]]+|[[:space:]]+$"; "")')
if [ -z "$TRIMMED_CHANGELOG" ] || [ "$TRIMMED_CHANGELOG" = "null" ]; then
  echo "::warning::Release notes body for tag ${TAG} is empty/whitespace-only/null; omitting changelog from the marketplace dispatch payload." >&2
  HAVE_CHANGELOG=false
else
  HAVE_CHANGELOG=true
fi

# The changelog body is read from a temp file via --rawfile, not passed as
# a --arg -- a large-enough release-notes body (tens of thousands of chars)
# as a literal argv value overflows the OS argument-list limit ("Argument
# list too long"), which --rawfile (reads the content from disk) never hits.
CHANGELOG_FILE=$(mktemp)
trap 'rm -f "$CHANGELOG_FILE"' EXIT
printf '%s' "$CHANGELOG" > "$CHANGELOG_FILE"

# GitHub's repository_dispatch client_payload allows at most 10 top-level
# properties; changelog is the 10th (name, description, repo, category,
# version, ref, icon, description_url, tags = 9 already present). Any
# future field must nest under an existing object rather than adding an
# 11th key.
jq -n -c \
  --arg name "$NAME" \
  --arg description "$DESC" \
  --arg repo "$REPO" \
  --arg version "$VERSION" \
  --arg ref "$TAG" \
  --arg icon "$ICON_URL" \
  --arg description_url "$DESCRIPTION_URL" \
  --rawfile changelog "$CHANGELOG_FILE" \
  --argjson have_changelog "$HAVE_CHANGELOG" \
  --argjson max_changelog_len "$MAX_CHANGELOG_LEN" \
  --arg release_url "$RELEASE_URL" \
  '
  def truncated_changelog:
    if (. | length) > $max_changelog_len then
      .[0:$max_changelog_len] + "\n\n...truncated — see the full release notes: " + $release_url
    else
      .
    end;
  {
    event_type: "plugin-release",
    client_payload: (
      {
        name: $name,
        description: $description,
        repo: $repo,
        category: "mcp",
        version: $version,
        ref: $ref,
        icon: $icon,
        description_url: $description_url,
        tags: ["git", "environment"]
      }
      + (if $have_changelog then { changelog: ($changelog | truncated_changelog) } else {} end)
    )
  }
  '
