#!/usr/bin/env bash
# install.sh -- rebuild this desktop from the repo.
# Direction: repo  ->  machine.  Safe to re-run; it backs up whatever it replaces.
#
#   ./install.sh                 everything
#   ./install.sh --dry-run       print what would happen, touch nothing
#   ./install.sh --only configs  one section (packages|configs|system|repos|wallpapers|units)
#   ./install.sh --skip packages
#   ./install.sh --yes           no prompts
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="$REPO/manifest.conf"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$HOME/.config-backup-$STAMP"
WALLPAPER_DST="$HOME/Pictures/Wallpapers"

DRY=0; YES=0; ONLY=""; declare -a SKIP=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --yes|-y)  YES=1 ;;
    --only)    ONLY="${2:-}"; shift ;;
    --skip)    SKIP+=("${2:-}"); shift ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac; shift
done

c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_err=$'\e[31m'; c_dim=$'\e[2m'; c_off=$'\e[0m'
ok()   { printf '%s  ok %s %s\n' "$c_ok"   "$c_off" "$*"; }
warn() { printf '%swarn %s %s\n' "$c_warn" "$c_off" "$*"; }
err()  { printf '%s err %s %s\n' "$c_err"  "$c_off" "$*" >&2; }
step() { printf '\n%s==>%s %s\n' $'\e[1;34m' "$c_off" "$*"; }
run()  { if [ "$DRY" = 1 ]; then printf '%s   would: %s%s\n' "$c_dim" "$*" "$c_off"; else "$@"; fi; }

want() {  # section gating
  local s="$1"
  [ -n "$ONLY" ] && [ "$ONLY" != "$s" ] && return 1
  for x in ${SKIP+"${SKIP[@]}"}; do [ "$x" = "$s" ] && return 1; done
  return 0
}
confirm() {
  [ "$YES" = 1 ] && return 0
  [ "$DRY" = 1 ] && return 0
  read -rp "$1 [y/N] " a; [[ "$a" =~ ^[Yy] ]]
}

[ -f "$MANIFEST" ] || { err "manifest.conf missing"; exit 1; }
[ "$DRY" = 1 ] && warn "DRY RUN -- nothing will be modified"

# Stash anything we are about to overwrite, so a bad restore is always undoable.
stash() {
  local live="$1" rel="$2"
  [ -e "$live" ] || return 0
  [ -L "$live" ] && { run rm -f "$live"; return 0; }
  run mkdir -p "$BACKUP/$(dirname "$rel")"
  run cp -a "$live" "$BACKUP/$rel"
}

# ============================================================ 1. packages
if want packages; then
  step "packages"
  if ! command -v pacman >/dev/null; then
    warn "not an Arch system -- skipping packages, configs still apply"
  else
    if [ -s "$REPO/packages/pacman-official.txt" ]; then
      n=$(wc -l < "$REPO/packages/pacman-official.txt")
      if confirm "install $n official packages?"; then
        run sudo pacman -S --needed --noconfirm - < "$REPO/packages/pacman-official.txt" \
          || warn "some official packages failed (renamed or dropped from repos)"
      fi
    fi
    if [ -s "$REPO/packages/pacman-aur.txt" ]; then
      if ! command -v yay >/dev/null; then
        warn "yay not installed -- AUR packages need it"
        if confirm "bootstrap yay from the AUR now?"; then
          run sudo pacman -S --needed --noconfirm git base-devel
          run git clone https://aur.archlinux.org/yay-bin.git /tmp/yay-bin-$STAMP
          if [ "$DRY" = 0 ]; then (cd /tmp/yay-bin-$STAMP && makepkg -si --noconfirm); fi
        fi
      fi
      if command -v yay >/dev/null || [ "$DRY" = 1 ]; then
        n=$(wc -l < "$REPO/packages/pacman-aur.txt")
        confirm "install $n AUR packages?" && \
          run yay -S --needed --noconfirm - < "$REPO/packages/pacman-aur.txt" \
            || warn "some AUR packages failed"
      fi
    fi
    if [ -s "$REPO/packages/flatpak.txt" ] && command -v flatpak >/dev/null; then
      if confirm "install $(wc -l < "$REPO/packages/flatpak.txt") flatpaks?"; then
        while read -r app origin; do
          [ -n "$app" ] || continue
          run flatpak install -y "${origin:-flathub}" "$app" || warn "flatpak $app failed"
        done < "$REPO/packages/flatpak.txt"
      fi
    fi
  fi
fi

# ============================================================ 2. home configs
if want configs; then
  step "home configs  (replaced files are stashed in $BACKUP)"
  while read -r kind path; do
    [ "$kind" = home ] || continue
    src="$REPO/home/$path"; dst="$HOME/$path"
    [ -e "$src" ] || { warn "$path (not in repo)"; continue; }
    stash "$dst" "$path"
    run mkdir -p "$(dirname "$dst")"
    if [ -d "$src" ]; then run rsync -a --delete "$src/" "$dst/"
    else run rsync -a "$src" "$dst"; fi
    ok "$path"
  done < <(grep -vE '^\s*#|^\s*$' "$MANIFEST")
fi

# ============================================================ 3. git repos from bundles
# Restored AFTER plain configs so history lands under the files, then the captured
# working tree (including uncommitted edits) is laid back on top.
if want repos; then
  step "git history"
  while read -r kind path; do
    [ "$kind" = repo ] || continue
    name="$(echo "$path" | tr '/' '-' | sed 's/^\.//')"
    bundle="$REPO/repos/$name.bundle"
    dst="$HOME/$path"
    [ -f "$bundle" ] || { warn "$name.bundle missing"; continue; }
    if [ -d "$dst/.git" ]; then ok "$path already a git repo, left alone"; continue; fi
    tmp="$(mktemp -d)"
    if [ "$DRY" = 1 ]; then
      printf '%s   would: clone %s -> %s%s\n' "$c_dim" "$name.bundle" "$path" "$c_off"; rm -rf "$tmp"; continue
    fi
    if git clone -q "$bundle" "$tmp/r" 2>/dev/null; then
      mkdir -p "$dst"
      mv "$tmp/r/.git" "$dst/.git"
      git -C "$dst" checkout -q -- . 2>/dev/null
      rsync -a --exclude='.git/' "$REPO/home/$path/" "$dst/"   # re-apply captured worktree
      if [ -f "$REPO/repos/$name.remotes.txt" ]; then
        while read -r rname rurl _; do
          [ -n "$rname" ] && git -C "$dst" remote add "$rname" "$rurl" 2>/dev/null
        done < <(sort -u "$REPO/repos/$name.remotes.txt")
      fi
      ok "$path ($(git -C "$dst" rev-list --count HEAD) commits restored)"
    else
      err "$name.bundle failed to clone"
    fi
    rm -rf "$tmp"
  done < <(grep -vE '^\s*#|^\s*$' "$MANIFEST")
fi

# ============================================================ 4. system files
if want system; then
  step "system files  (sudo)"
  while read -r kind path; do
    [ "$kind" = system ] || continue
    src="$REPO/system$path"
    [ -e "$src" ] || { warn "$path (not in repo)"; continue; }
    run sudo mkdir -p "$(dirname "$path")"
    run sudo cp -a "$src" "$path"
    case "$path" in /usr/local/bin/*) run sudo chmod +x "$path" ;; esac
    ok "$path"
  done < <(grep -vE '^\s*#|^\s*$' "$MANIFEST")
  run sudo systemctl daemon-reload
fi

# ============================================================ 5. wallpapers
if want wallpapers; then
  step "wallpapers"
  if [ -d "$REPO/wallpapers" ]; then
    run mkdir -p "$WALLPAPER_DST"
    run rsync -a --exclude='.current' "$REPO/wallpapers/" "$WALLPAPER_DST/"
    ok "$(find "$REPO/wallpapers" -type f ! -name .current | wc -l) files -> $WALLPAPER_DST"
    # the 300 upstream-provided ones are fetched, not stored -- see wallpapers-meta/
    UP="$REPO/wallpapers-meta/upstream-provided.txt"
    if [ -f "$UP" ]; then
      info_n=$(wc -l < "$UP")
      if [ "$DRY" = 1 ]; then
        printf '%s  would: clone SleepyCatHey/Ultimate-Win11-Setup and copy %s wallpapers%s\n' "$c_dim" "$info_n" "$c_off"
      elif command -v git >/dev/null; then
        tmp="$(mktemp -d)"
        if git clone --depth 1 -q https://github.com/SleepyCatHey/Ultimate-Win11-Setup.git "$tmp/u" 2>/dev/null; then
          n=0
          while read -r w; do
            [ -n "$w" ] || continue
            [ -f "$tmp/u/Wallpapers/$w" ] && cp -n "$tmp/u/Wallpapers/$w" "$WALLPAPER_DST/" && n=$((n+1))
          done < "$UP"
          ok "fetched $n/$info_n wallpapers from upstream"
          [ "$n" -lt "$info_n" ] && warn "$((info_n-n)) upstream wallpapers no longer exist there"
        else
          warn "could not clone the upstream wallpaper repo -- $info_n wallpapers missing"
          warn "they are recoverable from /mnt/backup or this repo's pre-dedupe history"
        fi
        rm -rf "$tmp"
      fi
    fi
    if [ -f "$REPO/wallpapers/.current" ] && command -v caelestia >/dev/null; then
      w="$WALLPAPER_DST/$(cat "$REPO/wallpapers/.current")"
      [ -f "$w" ] && run caelestia wallpaper -f "$w" && ok "set wallpaper: $(basename "$w")"
    fi
  fi
fi

# ============================================================ 6. services
if want units; then
  step "services"
  for u in $(cat "$REPO/packages/systemd-system-enabled.txt" 2>/dev/null); do
    [ -n "$u" ] || continue
    systemctl list-unit-files "$u" >/dev/null 2>&1 || continue
    systemctl is-enabled "$u" >/dev/null 2>&1 && continue
    run sudo systemctl enable "$u" && ok "enabled $u"
  done
  for u in $(cat "$REPO/packages/systemd-user-enabled.txt" 2>/dev/null); do
    [ -n "$u" ] || continue
    systemctl --user list-unit-files "$u" >/dev/null 2>&1 || continue
    systemctl --user is-enabled "$u" >/dev/null 2>&1 && continue
    run systemctl --user enable "$u" && ok "enabled --user $u"
  done
fi

step "done"
[ "$DRY" = 0 ] && [ -d "$BACKUP" ] && echo "replaced files stashed at: $BACKUP"
cat <<'NEXT'
Remaining manual steps (things a repo cannot restore):
  * log into Discord / Steam / browsers
  * re-pair Bluetooth devices
  * check docs/fstab.reference against the new machine's UUIDs before touching /etc/fstab
NEXT
