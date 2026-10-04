#!/usr/bin/env bash
# Squash-merge PR $PR_NUMBER if it is safe to: open, not a draft, labelled
# `automerge`, targets main, comes from this repo (not a fork), its head is
# still $HEAD_SHA (when given), and every check in $REQUIRED_CHECKS has
# succeeded on that head. A PR that isn't ready is a quiet skip (exit 0);
# a failed GitHub lookup is an error (non-zero), never mistaken for a skip.
#
# Env: GH_TOKEN, GITHUB_REPOSITORY, PR_NUMBER, HEAD_SHA (optional),
#      REQUIRED_CHECKS (space-separated job names), DRY_RUN=1 to only report.
# Needs only the gh CLI (its built-in --jq). Never checks out or runs PR code.
set -euo pipefail

: "${PR_NUMBER:?}" "${GITHUB_REPOSITORY:?}" "${REQUIRED_CHECKS:?}"
repo="$GITHUB_REPOSITORY"
skip() { echo "skip PR #$PR_NUMBER: $*"; exit 0; }

# One line, tab-separated, so a failed lookup aborts here (set -e) instead of
# leaving empty fields that would read as "not ready".
info=$(gh pr view "$PR_NUMBER" --repo "$repo" \
  --json state,isDraft,baseRefName,isCrossRepository,headRefOid,labels \
  --jq '[.state, .isDraft, .baseRefName, .isCrossRepository, .headRefOid,
         (.labels | any(.name == "automerge"))] | map(tostring) | join("\t")')
IFS=$'\t' read -r state draft base cross sha labelled <<<"$info"
[ -n "$sha" ] || { echo "error: no head SHA for PR #$PR_NUMBER" >&2; exit 1; }

[ "$state" = "OPEN" ]      || skip "state is $state"
[ "$draft" = "false" ]     || skip "draft"
[ "$base" = "main" ]       || skip "base is $base"
[ "$cross" = "false" ]     || skip "opened from a fork"
[ "$labelled" = "true" ]   || skip "no automerge label"
if [ -n "${HEAD_SHA:-}" ] && [ "$HEAD_SHA" != "$sha" ]; then
  skip "head moved to ${sha:0:7} after CI ran on ${HEAD_SHA:0:7}"
fi

# "<id>\t<name>\t<conclusion or status>" for every check run on the head.
runs=$(gh api --paginate "repos/$repo/commits/$sha/check-runs?per_page=100" \
  --jq '.check_runs[] | [(.id | tostring), .name, (.conclusion // .status)] | join("\t")')
for name in $REQUIRED_CHECKS; do
  # Latest attempt of each job wins (a re-run supersedes an earlier failure).
  verdict=$(awk -F'\t' -v n="$name" '$2 == n && $1 + 0 > best { best = $1 + 0; v = $3 }
                                      END { print (v == "" ? "missing" : v) }' <<<"$runs")
  [ "$verdict" = "success" ] || skip "check '$name' is $verdict on ${sha:0:7}"
done

if [ "${DRY_RUN:-}" = "1" ]; then
  echo "would merge PR #$PR_NUMBER at ${sha:0:7}"
  exit 0
fi
# --match-head-commit: refuse if a commit lands between the check above and the merge.
gh pr merge "$PR_NUMBER" --repo "$repo" --squash --match-head-commit "$sha"
echo "merged PR #$PR_NUMBER at ${sha:0:7}"
