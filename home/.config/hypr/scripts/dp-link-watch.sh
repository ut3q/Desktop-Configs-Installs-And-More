#!/usr/bin/env bash
# dp-link-watch — record (and optionally recover from) DisplayPort link failures.
#
# WHY THIS EXISTS
# On 2026-08-16, -21, -22 (x2) and -23 (x2) this machine's journal simply stops
# mid-line: no shutdown sequence, no kernel oops, nothing. That is the signature
# of a hard reset, not a clean reboot. In the same window the kernel logged, on
# three occasions:
#
#   amdgpu [drm] *ERROR* dpcd_set_link_settings: core_link_write_dpcd (...) failed
#   amdgpu [drm] enabling link 2 failed: 15
#
# i.e. DisplayPort link training failed. When it does not recover, the screen
# goes dark while the session behind it is still running -- and the only way out
# looks like the power button, which is exactly what the journal shows.
#
# Nothing survives a hard reset except what was fsync'd, so this appends each
# event to a file and fsyncs it immediately. That way the NEXT occurrence leaves
# evidence: what happened, when, and what the display state was just before.
#
# WHAT IT DOES
#   watch    follow the kernel log; on a link-enable failure, fsync a record.
#            With --recover, also force a modeset (see display-rescue.sh) at a
#            refresh the link can definitely train, rate-limited to once a
#            minute. Without it, purely passive.
#   report   print what has been recorded so far.
#
# ENABLE IT (opt-in -- it is not started automatically)
# add to ~/.config/caelestia/hypr-user.lua, inside the hyprland.start handler:
#   hl.exec_cmd(os.getenv("HOME") .. "/.config/hypr/scripts/dp-link-watch.sh watch --recover")

set -uo pipefail

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/dp-link-watch"
LOG="$STATE_DIR/events.log"
RESCUE="$HOME/.config/hypr/scripts/display-rescue.sh"
COOLDOWN=60
RECOVER=0

mkdir -p "$STATE_DIR"

# Append and flush to stable storage. A hard reset loses anything still in the
# page cache, which is the whole reason this script exists.
record() {
  { printf '%s\n' "$*" >> "$LOG"; } 2>/dev/null
  python3 - "$LOG" <<'PY' 2>/dev/null || true
import os, sys
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_APPEND)
os.fsync(fd)
os.close(fd)
PY
}

snapshot() {
  hyprctl monitors all -j 2>/dev/null | python3 -c '
import sys, json
try:
    for m in json.load(sys.stdin):
        print("    {} {}x{}@{:.0f} dpms={} disabled={}".format(
            m["name"], m["width"], m["height"], m["refreshRate"],
            m.get("dpmsStatus"), m.get("disabled")))
except Exception as e:
    print("    (could not read monitors: %s)" % e)' 2>/dev/null
}

cmd_watch() {
  record "=== watch started $(date -Iseconds) recover=$RECOVER ==="
  local last=0 now
  # -kf follows the kernel log. --line-buffered so matches are not held in a pipe.
  journalctl -kf -n0 -o cat 2>/dev/null |
    grep --line-buffered -E 'enabling link [0-9]+ failed|core_link_write_dpcd .* failed' |
    while IFS= read -r line; do
      case "$line" in
        *"enabling link"*) ;;                 # the terminal failure; act on it
        *) record "$(date -Iseconds) dpcd: $line"; continue ;;   # noise; just note it
      esac

      record "$(date -Iseconds) LINK FAILURE: $line"
      record "$(snapshot)"

      [ "$RECOVER" = "1" ] || continue
      now=$(date +%s)
      if [ $((now - last)) -lt $COOLDOWN ]; then
        record "    (recovery skipped: within ${COOLDOWN}s cooldown)"
        continue
      fi
      last=$now
      sleep 2                                  # let the driver finish retrying
      record "    running display-rescue"
      [ -x "$RESCUE" ] && "$RESCUE" rescue >/dev/null 2>&1
      record "$(snapshot)"
    done
}

cmd_report() {
  [ -f "$LOG" ] || { echo "no events recorded"; return 0; }
  cat "$LOG"
}

case "${1:-report}" in
  watch)  shift; [ "${1:-}" = "--recover" ] && RECOVER=1; cmd_watch ;;
  report) cmd_report ;;
  *) echo "usage: $(basename "$0") {watch [--recover]|report}" >&2; exit 2 ;;
esac
