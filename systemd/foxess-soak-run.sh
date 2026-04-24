#!/bin/bash
# Invoked by foxess-soak.service. Kept out of the unit file because
# inlined ExecStart= scripts suffer from systemd's own %- and $-
# substitution rules, which previously swallowed ${TS}_${SHORT} and
# $$ — making every run reuse the same output directory and lose
# prior-night artefacts.

set -euo pipefail

REPO="$HOME/git/fox"
TAG=$(git -C "$REPO" describe --tags --abbrev=0)
COMMIT=$(git -C "$REPO" rev-parse "$TAG")
SHORT=$(git -C "$REPO" rev-parse --short "$TAG")
BRANCH="$TAG"
TS=$(date +%Y%m%d_%H%M%S)
RUN_DIR="$REPO/test-artifacts/soak/runs/${TS}_${SHORT}"
WORKTREE="$REPO/test-artifacts/soak/.worktree-$$"

mkdir -p "$RUN_DIR"

{
    echo "commit: $COMMIT"
    echo "branch: $BRANCH"
    echo "mode: real-time"
    echo "started: $(date -Iseconds)"
} > "$RUN_DIR/meta.txt"

git -C "$REPO" worktree add --detach "$WORKTREE" "$TAG"
trap 'git -C "$REPO" worktree remove --force "$WORKTREE" 2>/dev/null || true' EXIT

cd "$WORKTREE"
export SOAK_ARTIFACT_DIR="$RUN_DIR"

EC=0
/usr/bin/python3 -m pytest tests/soak/ -m soak \
    --tb=long -v --override-ini="addopts=-n auto" \
    > "$RUN_DIR/pytest.log" 2>&1 || EC=$?

{
    echo "exit_code=$EC"
} >> "$RUN_DIR/pytest.log"
{
    echo "finished: $(date -Iseconds)"
    echo "exit_code: $EC"
} >> "$RUN_DIR/meta.txt"

exit "$EC"
