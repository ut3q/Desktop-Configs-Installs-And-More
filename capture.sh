#!/usr/bin/env bash
# capture.sh -- pull the LIVE system state into this repo.
# Direction: machine  ->  repo.  Run it, review `git diff`, commit, push.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="$REPO/manifest.conf"
WALLPAPER_SRC="$HOME/Pictures/Wallpapers"

c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_err=$'\e[31m'; c_dim=$'\e[2m'; c_off=$'\e[0m'
ok()   { printf '%s  ok %s %s\n'   "$c_ok"   "$c_off" "$*"; }
warn() { printf '%swarn %s %s\n'   "$c_warn" "$c_off" "$*"; }
err()  { printf '%s err %s %s\n'   "$c_err"  "$c_off" "$*" >&2; }
step() { printf '\n%s==>%s %s\n' $'\e[1;34m' "$c_off" "$*"; }

[ -f "$MANIFEST" ] || { err "manifest.conf missing next to capture.sh"; exit 1; }

# Junk that must never enter the working tree, regardless of .gitignore.
RSYNC_EX=(
  --exclude='__pycache__/' --exclude='*.pyc'
  --exclude='node_modules/' --exclude='.venv/'
  --exclude='*.bak' --exclude='*.bak.*' --exclude='*.bak-*'
  --exclude='*.orig' --exclude='*~' --exclude='*.original-backup'
  --exclude='Cache/' --exclude='GPUCache/' --exclude='Crashpad/'
  --exclude='*.log' --exclude='*.sock' --exclude='*.pid'
  --exclude='*.sqlite-wal' --exclude='*.sqlite-shm'
  --exclude='*cookies*' --exclude='*.cookies'
  --exclude='dist/' --exclude='equicord.asar' --exclude='build/'
  --exclude='.git/'
)

# ----------------------------------------------------------------- home + system
step "configs"
while read -r kind path; do
  case "$kind" in
    home)
      src="$HOME/$path"; dst="$REPO/home/$path"
      [ -e "$src" ] || { warn "$path (not on this machine, skipped)"; continue; }
      mkdir -p "$(dirname "$dst")"
      if [ -d "$src" ]; then
        rsync -a --delete "${RSYNC_EX[@]}" "$src/" "$dst/" && ok "$path"
      else
        rsync -a "$src" "$dst" && ok "$path"
      fi
      ;;
    system)
      src="$path"; dst="$REPO/system${path}"
      [ -e "$src" ] || { warn "$path (not on this machine, skipped)"; continue; }
      mkdir -p "$(dirname "$dst")"
      if cp -a "$src" "$dst" 2>/dev/null; then ok "$path"
      else sudo cat "$src" > "$dst" 2>/dev/null && ok "$path (via sudo)" || warn "$path (unreadable)"; fi
      ;;
  esac
done < <(grep -vE '^\s*#|^\s*$' "$MANIFEST")

# ------------------------------------------------------------------- git bundles
# A bundle is a single file holding a repo's ENTIRE history. `git clone x.bundle`
# reconstructs the repo, commits and all. This is how local-only history survives.
step "git history bundles"
while read -r kind path; do
  [ "$kind" = repo ] || continue
  src="$HOME/$path"
  [ -d "$src/.git" ] || { warn "$path (not a git repo, skipped)"; continue; }
  name="$(echo "$path" | tr '/' '-' | sed 's/^\.//')"
  if [ -n "$(git -C "$src" status --porcelain 2>/dev/null)" ]; then
    warn "$path has UNCOMMITTED changes -- bundling committed history only"
  fi
  if git -C "$src" bundle create "$REPO/repos/$name.bundle" --all >/dev/null 2>&1; then
    ok "$name.bundle ($(git -C "$src" rev-list --count --all) commits, $(du -h "$REPO/repos/$name.bundle" | cut -f1))"
    git -C "$src" remote -v > "$REPO/repos/$name.remotes.txt" 2>/dev/null
  else
    err "$path bundle FAILED"
  fi
done < <(grep -vE '^\s*#|^\s*$' "$MANIFEST")

# ----------------------------------------------------------------- package lists
step "package lists"
if command -v pacman >/dev/null; then
  pacman -Qqen > "$REPO/packages/pacman-official.txt" && ok "pacman-official.txt ($(wc -l < "$REPO/packages/pacman-official.txt"))"
  pacman -Qqem > "$REPO/packages/pacman-aur.txt"      && ok "pacman-aur.txt ($(wc -l < "$REPO/packages/pacman-aur.txt"))"
fi
if command -v flatpak >/dev/null; then
  flatpak list --app --columns=application,origin > "$REPO/packages/flatpak.txt" && ok "flatpak.txt ($(wc -l < "$REPO/packages/flatpak.txt"))"
fi
systemctl list-unit-files --state=enabled --no-legend --no-pager 2>/dev/null \
  | awk '{print $1}' > "$REPO/packages/systemd-system-enabled.txt" && ok "systemd-system-enabled.txt"
systemctl --user list-unit-files --state=enabled --no-legend --no-pager 2>/dev/null \
  | awk '{print $1}' > "$REPO/packages/systemd-user-enabled.txt" && ok "systemd-user-enabled.txt"

# --------------------------------------------------------------------- wallpapers
step "wallpapers"
if [ -d "$WALLPAPER_SRC" ]; then
  rsync -a --delete --exclude='.cache/' "$WALLPAPER_SRC/" "$REPO/wallpapers/" \
    && ok "$(find "$REPO/wallpapers" -type f | wc -l) files, $(du -sh "$REPO/wallpapers" | cut -f1)"
  cur="$HOME/.local/state/caelestia/wallpaper/path.txt"
  [ -f "$cur" ] && basename "$(cat "$cur")" > "$REPO/wallpapers/.current" && ok "current: $(cat "$REPO/wallpapers/.current")"
else
  warn "no $WALLPAPER_SRC"
fi

# ------------------------------------------------------------------- reference docs
step "reference"
cp /etc/fstab "$REPO/docs/fstab.reference" 2>/dev/null && ok "fstab.reference"
{ echo "# Captured $(date -Iseconds) on $(uname -n)"; echo
  echo "## Kernel";      uname -a
  echo; echo "## Shell"; getent passwd "$USER" | cut -d: -f7
  echo; echo "## Disks"; lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT 2>/dev/null
} > "$REPO/docs/system-snapshot.txt" && ok "system-snapshot.txt"

step "done"
echo "Review, then commit:"
echo "  cd $REPO && git status && git add -A && git commit -m 'capture' && git push"
