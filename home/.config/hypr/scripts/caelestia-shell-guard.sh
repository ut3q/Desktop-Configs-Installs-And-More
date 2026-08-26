#!/usr/bin/env bash
# caelestia-shell-guard — never be left without a bar because of an upgrade.
#
# The shell QML is forked to ~/.config/quickshell/caelestia and pinned to a
# specific caelestia-shell version, but the COMPILED QML PLUGINS it imports
# (Caelestia.Config, .Services, .Internal, .Models, .Blobs, .Components,
# M3Shapes) live in /usr/lib/qt6/qml and ARE upgraded by pacman. If a future
# release renames or removes something the pinned QML uses, the fork stops
# loading -- and since it shadows /etc/xdg, you get no shell at all.
#
# This checks, some seconds after login, that a shell is actually up. If not, it
# starts the PACKAGED one (which always matches the installed plugins) and tells
# you loudly. You keep a desktop; you fix the fork when convenient.
#
#   check   verify a shell is running; fall back to stock if not  (use this at login)
#   stock   force the packaged shell now
#   local   force the forked shell now
#   status  what is running, and whether the pin is stale

set -uo pipefail

FORK="$HOME/.config/quickshell/caelestia"
STOCK=/etc/xdg/quickshell/caelestia
WAIT=25

log() { printf '[shell-guard] %s\n' "$*" >&2; }
notify() {
  command -v notify-send >/dev/null 2>&1 || return 0
  notify-send -a "Caelestia" -u "${3:-normal}" -i preferences-desktop "$1" "$2" >/dev/null 2>&1
}

# Match the SHELL specifically. `pgrep -x qs` also matches `qs log -f`,
# `qs ipc call`, `qs kill` and friends -- a stray log follower would look like a
# healthy shell and suppress every restart. Match the argv instead.
shell_pid() { pgrep -f '^qs (-c caelestia|-p /etc/xdg/quickshell/caelestia)( |$)' 2>/dev/null | head -1; }
running() { [ -n "$(shell_pid)" ]; }

start() {  # $1 = "local" | "stock"
  if [ "$1" = stock ]; then
    hyprctl eval "hl.exec_cmd(\"qs -p $STOCK -n -d\")" >/dev/null 2>&1
  else
    hyprctl eval 'hl.exec_cmd("qs -c caelestia -n -d")' >/dev/null 2>&1
  fi
}

which_is_running() {
  local d
  d=$(ls -td /run/user/1000/quickshell/by-id/*/ 2>/dev/null | head -1) || return 1
  grep -m1 'Launching config' "$d/log.log" 2>/dev/null | sed 's/.*Launching config: //'
}

cmd_check() {
  local i=0
  while [ $i -lt $WAIT ]; do
    running && { log "shell is up ($(which_is_running))"; return 0; }
    i=$((i+1)); sleep 1
  done

  log "no shell after ${WAIT}s -- falling back to the packaged build"
  start stock
  sleep 6
  if running; then
    notify "Forked shell failed to start" \
      "Running the packaged shell instead. Check: caelestia shell --log, then ~/.config/quickshell/caelestia" \
      critical
    log "packaged shell started"
  else
    notify "No shell could be started" "Neither the forked nor the packaged shell came up." critical
    log "packaged shell ALSO failed -- this is not a fork problem"
    return 1
  fi
}

cmd_status() {
  echo "running     : $(running && which_is_running || echo 'nothing')"
  echo "fork        : $FORK"
  echo "pinned      : $(cat "$FORK/.upstream-version" 2>/dev/null || echo 'none')"
  echo "installed   : $(pacman -Q caelestia-shell 2>/dev/null || echo '?')"
  if [ "$(cat "$FORK/.upstream-version" 2>/dev/null)" != "$(pacman -Q caelestia-shell 2>/dev/null)" ]; then
    echo "  -> STALE: run $FORK/sync-upstream.sh, then merge"
  else
    echo "  -> in sync"
  fi
}

case "${1:-status}" in
  check)  cmd_check ;;
  stock)  pkill -x qs 2>/dev/null; sleep 1; start stock ;;
  local)  pkill -x qs 2>/dev/null; sleep 1; start local ;;
  status) cmd_status ;;
  *) echo "usage: $(basename "$0") {check|stock|local|status}" >&2; exit 2 ;;
esac
