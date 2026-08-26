#!/usr/bin/env bash
# display-rescue — bring a physical monitor back after a DisplayPort link failure.
#
# Symptom this exists for: the panel drops to "no signal" and never recovers,
# with the kernel logging
#
#   amdgpu ... [drm] *ERROR* dpcd_set_link_settings: core_link_write_dpcd (...) failed
#   amdgpu ... [drm] enabling link 2 failed: 15
#
# That is DP link training failing on a re-enable. The session is usually still
# alive and healthy behind the black screen -- only the link is down -- so a
# forced modeset can recover it without a reboot. Bind this to something you can
# hit blind (see hyprland/keybinds.lua) and mark the bind `locked = true` so it
# also works from the lock screen.
#
# Strategy, in order of increasing disruption:
#   1. DPMS on, in case the output is merely blanked.
#   2. Re-enable any physical output Hyprland has marked disabled.
#   3. Re-apply the mode at a conservative refresh. Link training margin is what
#      fails first, and 60Hz asks far less of the link than 170Hz, so a mode the
#      panel can definitely train is the one most likely to bring a picture
#      back. `restore` puts the preferred mode back once you can see again.
#
# hyprctl keyword/dispatch are no-ops under this machine's Lua config parser, so
# every monitor change goes through `hyprctl eval` + hl.monitor.

set -uo pipefail

SAFE_MODE="1920x1080@60"
PREFERRED_MODE="1920x1080@170"

log() { printf '[display-rescue] %s\n' "$*" >&2; }

notify() {
  command -v notify-send >/dev/null 2>&1 || return 0
  notify-send -a "Display" -i video-display "$1" "${2:-}" >/dev/null 2>&1
}

if [ -z "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]; then
  d="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/hypr"
  [ -d "$d" ] && export HYPRLAND_INSTANCE_SIGNATURE="$(
    find "$d" -maxdepth 1 -mindepth 1 -type d -printf '%T@ %f\n' 2>/dev/null |
      sort -rn | head -1 | cut -d' ' -f2)"
fi
hyprctl version >/dev/null 2>&1 || { log "cannot reach Hyprland"; exit 1; }

# `monitors all` includes outputs Hyprland has disabled; `monitors` does not.
physical_all() {
  hyprctl monitors all -j | python3 -c \
    'import sys,json;[print(m["name"]) for m in json.load(sys.stdin) if not m["name"].startswith("HEADLESS-")]'
}
is_enabled() {
  hyprctl monitors -j | python3 -c "
import sys, json
sys.exit(0 if any(m['name'] == '$1' for m in json.load(sys.stdin)) else 1)"
}

apply() {  # $1=output $2=mode
  hyprctl eval "hl.monitor({output=\"$1\", mode=\"$2\", position=\"0x0\", scale=1})" >/dev/null 2>&1
}

cmd_rescue() {
  local mode="${1:-$SAFE_MODE}" n=0

  hyprctl dispatch dpms on >/dev/null 2>&1

  while read -r m; do
    [ -z "$m" ] && continue
    n=$((n + 1))
    if ! is_enabled "$m"; then
      log "$m is disabled -- re-enabling"
      hyprctl eval "hl.monitor({output=\"$m\", disabled=false})" >/dev/null 2>&1
      sleep 0.5
    fi
    log "forcing $m to $mode"
    apply "$m" "$mode"
    sleep 0.5
    if is_enabled "$m"; then
      log "$m is up"
    else
      log "$m still down after modeset"
    fi
  done < <(physical_all)

  [ "$n" -eq 0 ] && { log "no physical outputs found"; return 1; }
  notify "Display rescue" "Forced $mode. Use 'restore' once you can see."
}

cmd_restore() {
  while read -r m; do
    [ -z "$m" ] && continue
    log "restoring $m to $PREFERRED_MODE"
    apply "$m" "$PREFERRED_MODE"
  done < <(physical_all)
  notify "Display restored" "$PREFERRED_MODE"
}

cmd_status() {
  hyprctl monitors all -j | python3 -c '
import sys, json
for m in json.load(sys.stdin):
    print("{:<12} {}x{}@{:.0f} at {},{}  dpms={} disabled={}".format(
        m["name"], m["width"], m["height"], m["refreshRate"],
        m["x"], m["y"], m.get("dpmsStatus"), m.get("disabled")))'
}

case "${1:-rescue}" in
  rescue)  cmd_rescue "${2:-$SAFE_MODE}" ;;
  restore) cmd_restore ;;
  status)  cmd_status ;;
  *) echo "usage: $(basename "$0") {rescue [WxH@R]|restore|status}" >&2; exit 2 ;;
esac
