#!/usr/bin/env bash
# sync-upstream — pull the newly-installed packaged shell in as a new upstream
# commit, so local changes can be replayed on top of it.
#
# Run this AFTER pacman upgrades caelestia-shell. It does not touch your working
# tree; it only advances refs/remotes/upstream/main to a snapshot of whatever
# /etc/xdg/quickshell/caelestia now contains. You then merge or rebase.
#
#   ~/.config/quickshell/caelestia/sync-upstream.sh
#   git -C ~/.config/quickshell/caelestia merge upstream/main   # or: rebase upstream/main
#   # resolve conflicts, then restart the shell
#
# Why a snapshot rather than `git fetch upstream`: the AUR package installs a
# processed tree (no build files, extra assets), so it does not correspond to any
# single commit in the real repo. Snapshotting what is actually installed is what
# makes `git diff upstream/main` mean "my changes" and nothing else.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STOCK=/etc/xdg/quickshell/caelestia

[ -d "$REPO/.git" ] || { echo "not a git repo: $REPO" >&2; exit 1; }
[ -d "$STOCK" ]     || { echo "packaged shell not found at $STOCK" >&2; exit 1; }

ver=$(pacman -Q caelestia-shell 2>/dev/null || echo "caelestia-shell unknown")
pinned=$(cat "$REPO/.upstream-version" 2>/dev/null || echo "")

if [ "$ver" = "$pinned" ]; then
  echo "Already synced: $ver"
  echo "(nothing installed since the last sync)"
  exit 0
fi

echo "Packaged: $ver"
echo "Pinned:   ${pinned:-none}"

# Build the snapshot through a throwaway index so the working tree is untouched.
TMPIDX=$(mktemp -u /tmp/caelestia-sync-idx.XXXXXX)
trap 'rm -f "$TMPIDX"' EXIT
# Exclude pacman's .pacnew/.pacsave siblings: `add -A` would otherwise sweep them
# into the snapshot and they would then read as "upstream content".
GIT_INDEX_FILE="$TMPIDX" git -C "$REPO" --work-tree="$STOCK" add -A -- \
  ':!*.pacnew' ':!*.pacsave' ':!*.pacorig' 
TREE=$(GIT_INDEX_FILE="$TMPIDX" git -C "$REPO" write-tree)
PARENT=$(git -C "$REPO" rev-parse refs/remotes/upstream/main)

if [ "$TREE" = "$(git -C "$REPO" rev-parse "$PARENT^{tree}")" ]; then
  echo "Packaged tree is byte-identical to the current upstream/main; only bumping the version marker."
else
  NEW=$(git -C "$REPO" commit-tree "$TREE" -p "$PARENT" -m "$ver as packaged")
  git -C "$REPO" update-ref refs/remotes/upstream/main "$NEW"
  echo "upstream/main -> $(git -C "$REPO" rev-parse --short "$NEW")"
  echo
  echo "What upstream changed:"
  git -C "$REPO" --no-pager diff --stat "$PARENT" "$NEW" | sed 's/^/    /'
  echo
  echo "Now replay your changes:"
  echo "    git -C $REPO merge upstream/main"
  echo "  or"
  echo "    git -C $REPO rebase upstream/main"
fi

printf '%s\n' "$ver" > "$REPO/.upstream-version"
