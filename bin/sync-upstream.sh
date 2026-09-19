#!/usr/bin/env bash
# Syncs the self-hosted-fork's master with PostHog/posthog and rebases
# the self-hosted branch of local patches on top. See
# docs/internal/self-hosted-fork.md for what this does and why.
set -euo pipefail

UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-upstream}"
UPSTREAM_URL="${UPSTREAM_URL:-https://github.com/PostHog/posthog.git}"
ORIGIN_REMOTE="${ORIGIN_REMOTE:-origin}"
BASE_BRANCH="${BASE_BRANCH:-master}"
FORK_BRANCH="${FORK_BRANCH:-self-hosted}"

log() { printf '\n\033[1m%s\033[0m\n' "$*"; }

if git rev-parse --is-shallow-repository >/dev/null 2>&1 && [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
  log "Repo is a shallow clone; unshallowing (needed for a reliable rebase)..."
  git fetch --unshallow "$ORIGIN_REMOTE"
fi

if ! git remote get-url "$UPSTREAM_REMOTE" >/dev/null 2>&1; then
  log "Adding '$UPSTREAM_REMOTE' remote ($UPSTREAM_URL)"
  git remote add "$UPSTREAM_REMOTE" "$UPSTREAM_URL"
fi

log "Fetching $UPSTREAM_REMOTE/$BASE_BRANCH"
git fetch "$UPSTREAM_REMOTE" "$BASE_BRANCH"

if [ -n "$(git status --porcelain)" ]; then
  echo "Working tree is dirty. Commit or stash before syncing." >&2
  exit 1
fi

git checkout "$BASE_BRANCH"
git fetch "$ORIGIN_REMOTE" "$BASE_BRANCH"
git reset --hard "$ORIGIN_REMOTE/$BASE_BRANCH"

if ! git merge-base --is-ancestor "$BASE_BRANCH" "$UPSTREAM_REMOTE/$BASE_BRANCH" && \
   [ "$(git rev-parse "$BASE_BRANCH")" != "$(git rev-parse "$UPSTREAM_REMOTE/$BASE_BRANCH")" ]; then
  echo "'$BASE_BRANCH' has diverged from '$UPSTREAM_REMOTE/$BASE_BRANCH'." >&2
  echo "That means a fork-only commit landed on $BASE_BRANCH — it belongs on $FORK_BRANCH instead." >&2
  echo "Fix that manually before running this script again." >&2
  exit 1
fi

log "Fast-forwarding $BASE_BRANCH to $UPSTREAM_REMOTE/$BASE_BRANCH"
git merge --ff-only "$UPSTREAM_REMOTE/$BASE_BRANCH"
git push "$ORIGIN_REMOTE" "$BASE_BRANCH"

log "Rebasing $FORK_BRANCH onto $BASE_BRANCH"
git checkout "$FORK_BRANCH"
git fetch "$ORIGIN_REMOTE" "$FORK_BRANCH"
git reset --hard "$ORIGIN_REMOTE/$FORK_BRANCH"

if git rebase "$BASE_BRANCH"; then
  log "Clean rebase. Pushing $FORK_BRANCH."
  git push "$ORIGIN_REMOTE" "$FORK_BRANCH" --force-with-lease
  log "Done. $FORK_BRANCH is now $BASE_BRANCH + must-have patches (docs/internal/self-hosted-fork.md)."
else
  cat >&2 <<'EOF'

Rebase stopped on a conflict.

Cross-reference the conflicting file(s) against the must-have patch table in
docs/internal/self-hosted-fork.md before resolving — the goal is to preserve
the *behavior* each patch describes, not necessarily the exact old diff.

Once resolved:
  git add <files>
  git rebase --continue

Then re-run this script, or finish manually with:
  git push origin self-hosted --force-with-lease
EOF
  exit 1
fi
