#!/usr/bin/env bash
# caelestia-shell-supervisor — keep the bar alive, and record why it ever isn't.
#
# The shell is a single process with no supervision: anything that kills it
# (an OOM, a compositor fault, a stray pkill, a bad restart) leaves the desktop
# with no bar until a human notices. This watches it and brings it back.
#
# Deliberate stops are respected. The kill keybind writes a stop file; this
# honours it and does nothing until a restart clears it. So:
#   CTRL+SUPER+SHIFT+R  -> stays down (as it always did)
#   CTRL+SUPER+ALT+R    -> restarts, supervisor stays armed
#   anything unexpected -> back within a few seconds
#
# Repeated rapid failures back off exponentially and then fall back to the
# packaged shell rather than spinning, because a fork that cannot start is
# better replaced than retried forever.
#
#   run      supervise (this is what login starts)
#   status   is it supervising, and what has it seen
#   off/on   pause / resume restarts

set -uo pipefail

STATE="${XDG_STATE_HOME:-$HOME/.local/state}/caelestia-fork"
STOP="$STATE/supervisor.off"
LOG="$STATE/supervisor.log"
LOCK="$STATE/supervisor.lock"
FORK="$HOME/.config/quickshell/caelestia"
STOCK=/etc/xdg/quickshell/caelestia

POLL=2
GRACE=5              # seconds gone before we act (lets a manual restart win)
STABLE_AFTER=120     # seconds up before the failure counter resets
MAX_FAST_FAILS=4     # consecutive quick failures before falling back to stock

mkdir -p "$STATE"

# Append and flush: a hard reset must not lose the record of what happened.
say() {
  # Bound the log: this runs for the life of the session and writes on every
  # death plus a health line every 5 minutes.
  if [ -f "$LOG" ] && [ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt 3000 ]; then
    tail -1500 "$LOG" > "$LOG.tmp" 2>/dev/null && mv -f "$LOG.tmp" "$LOG"
  fi
  printf '%s %s\n' "$(date -Iseconds)" "$*" >> "$LOG"
  # Was a python3 one-liner purely to reach fsync. Starting an interpreter cost
  # ~8.8ms a line; coreutils `sync --data` does the same fdatasync in ~0.7ms.
  sync --data "$LOG" 2>/dev/null || true
}
notify() {
  command -v notify-send >/dev/null 2>&1 || return 0
  notify-send -a "Caelestia" -u "${3:-normal}" -i preferences-desktop "$1" "$2" 2>/dev/null || true
}
# Match the SHELL specifically. `pgrep -x qs` also matches `qs log -f`,
# `qs ipc call`, `qs kill` and friends -- a stray log follower would look like a
# healthy shell and suppress every restart. Match the argv instead.
# Is this pid the shell proper? The argv match also catches every SUBCOMMAND:
# `qs -c caelestia ipc call lock isLocked` (which this script itself runs every
# 15s), `qs -c caelestia log`, `... kill`, `... msg`. Those live <100ms so they
# rarely win the race, but when one does the supervisor reads a transient pid as
# the shell: it suppresses a restart, or samples health off the wrong process.
# The shell proper takes no subcommand, so filter them out by name.
is_shell_pid() {
  [ -n "${1:-}" ] && [ -d "/proc/$1" ] || return 1
  local args=" $(tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null) "
  case "$args" in
    " qs -c caelestia "*|" qs -p /etc/xdg/quickshell/caelestia "*) ;;
    *) return 1 ;;
  esac
  case "$args" in
    *" ipc "*|*" log "*|*" kill "*|*" msg "*|*" list "*|*" show "*) return 1 ;;
  esac
  return 0
}

# `pgrep -f` reads the cmdline of every process on the system -- ~15ms of CPU,
# and at a 2s poll that alone was most of what this script cost. The shell it
# finds is the same one it found last time in almost every poll, so check the
# remembered pid first and fall back to the scan only when that pid is gone.
# A replacement still cannot hide: the old pid has to die for a new shell to
# take over, and that is exactly what drops us into the scan.
#
# NOTE: callers invoke this in $(...), a subshell, so it cannot update the cache
# itself -- the run loop assigns SHELL_PID_CACHE from the value it gets back.
SHELL_PID_CACHE=
shell_pid() {
  if is_shell_pid "${SHELL_PID_CACHE:-}"; then
    printf '%s\n' "$SHELL_PID_CACHE"
    return 0
  fi
  local p
  for p in $(pgrep -f '^qs (-c caelestia|-p /etc/xdg/quickshell/caelestia)( |$)' 2>/dev/null); do
    is_shell_pid "$p" || continue
    printf '%s\n' "$p"
    return 0
  done
  return 1
}
running() { [ -n "$(shell_pid)" ]; }

hypr_env() {
  [ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ] && return 0
  local d="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/hypr"
  [ -d "$d" ] && export HYPRLAND_INSTANCE_SIGNATURE="$(
    find "$d" -maxdepth 1 -mindepth 1 -type d -printf '%T@ %f\n' 2>/dev/null |
      sort -rn | head -1 | cut -d' ' -f2)"
}

# --- session lock ------------------------------------------------------------
# If the shell dies while the session is LOCKED, Hyprland keeps the session
# secure with its own fallback -- but that fallback has no password field, so
# there is no way to get back in. That is very likely what "the lockscreen would
# die" meant. misc.lua already sets allow_session_lock_restore = true, so a
# restarted client is allowed to retake the lock; it just has to be told to.
#
# Sample the state while the shell is alive, and re-lock after an unexpected
# death only if the sample is RECENT -- a stale "locked" must never lock a
# desktop the user is actively working on.
LOCKSTATE="$STATE/was-locked"
sample_lock_state() {
  local v
  v=$(timeout 3 qs -c caelestia ipc call lock isLocked 2>/dev/null | tr -d '[:space:]')
  case "$v" in
    true)  printf '%s\n' "$(date +%s)" > "$LOCKSTATE" ;;
    false) rm -f "$LOCKSTATE" ;;
    *)     : ;;   # no answer (shell busy/gone) -- leave the last sample alone
  esac
}
was_locked_recently() {
  [ -f "$LOCKSTATE" ] || return 1
  local t now
  t=$(cat "$LOCKSTATE" 2>/dev/null) || return 1
  now=$(date +%s)
  [ $((now - t)) -le 60 ]
}
restore_lock() {
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if [ "$(timeout 3 qs -c caelestia ipc call lock isLocked 2>/dev/null | tr -d '[:space:]')" = "false" ]; then
      timeout 5 qs -c caelestia ipc call lock lock >/dev/null 2>&1
      say "  session was locked before the death -- re-locked so a password can be entered"
      return 0
    fi
    sleep 1
  done
  say "  WARNING: could not restore the lock; press SUPER+ALT+L"
  return 1
}

start_shell() {  # $1 = local|stock
  hypr_env
  if [ "$1" = stock ]; then
    hyprctl eval "hl.exec_cmd(\"qs -p $STOCK -n -d\")" >/dev/null 2>&1
  else
    hyprctl eval 'hl.exec_cmd("qs -c caelestia -n -d")' >/dev/null 2>&1
  fi
}

# Everything we can still learn about a death, captured at the moment it happens.
forensics() {
  local d
  d=$(ls -td /run/user/1000/quickshell/by-id/*/ 2>/dev/null | head -1)
  say "  last instance: $(basename "${d:-none}")"
  [ -n "$d" ] && [ -f "$d/log.log" ] && {
    say "  config: $(grep -m1 'Launching config' "$d/log.log" 2>/dev/null | sed 's/.*config: //')"
    # grep -c exits 1 on zero matches, so `|| echo 0` would append a SECOND
    # line to the captured output. Take the count and normalise separately.
    local errs; errs=$(grep -cE 'ERROR' "$d/log.log" 2>/dev/null); errs=${errs:-0}
    say "  errors in its log: $errs"
    [ "$errs" -gt 0 ] && grep -E 'ERROR' "$d/log.log" | tail -5 | while read -r l; do say "    $l"; done
    say "  last log line: $(tail -1 "$d/log.log" 2>/dev/null)"
  }
  # A signal death leaves a coredump; an external kill does not. This is the
  # single most useful discriminator between "it crashed" and "something killed it".
  local cd; cd=$(coredumpctl list --since "-2min" --no-pager 2>/dev/null | grep -ciE '/usr/bin/(qs|quickshell)' || true)
  say "  coredumps for qs in the last 2min: ${cd:-0}  ($([ "${cd:-0}" -gt 0 ] && echo 'CRASHED' || echo 'killed externally, or clean exit'))"
  say "  memory: $(free -m | awk '/^Mem:/{print $3"/"$2" MB used"}')  swap: $(free -m | awk '/^Swap:/{print $3"/"$2" MB"}')"
  dump_ring
}

# A 2s poll can miss a `qs kill` that lives for 30ms. Sample faster in the
# background and keep a short ring, so a death can be attributed to whatever ran
# immediately before it rather than guessed at.
RING="$STATE/ring"
# The sampler used to run for the whole session. A full `ps -eo` scan costs
# ~17ms of CPU and it did two a second, so simply being armed burned ~3.5% of
# a core forever -- to catch an event that happens maybe twice a day. Arm it
# only after something has actually gone wrong, when a repeat is likely, and
# let it disarm itself once things have been quiet. The first death in a quiet
# session is dumped with a cold ring; every death after it is fully recorded.
SAMPLER_WINDOW=900
SAMPLER_UNTIL=0
arm_sampler() {
  SAMPLER_UNTIL=$((SECONDS + SAMPLER_WINDOW))
  [ -n "${SAMPLER:-}" ] && kill -0 "$SAMPLER" 2>/dev/null && return 0
  start_sampler
  say "  ring sampler armed for ${SAMPLER_WINDOW}s"
}
disarm_sampler() {
  [ -n "${SAMPLER:-}" ] || return 0
  kill "$SAMPLER" 2>/dev/null
  SAMPLER=
  say "  ring sampler disarmed (quiet for ${SAMPLER_WINDOW}s)"
}
start_sampler() {
  mkdir -p "$RING"; rm -f "$RING"/* 2>/dev/null
  (
    i=0
    while true; do
      # Capture by AGE, not by name. A keyword filter (qs|kill|caelestia|...)
      # misses the common case: a script with an unrelated name using bash's
      # `kill` BUILTIN, which never appears as a process at all. Anything that
      # can kill the shell has to run, and anything that just ran is young --
      # so grab every process younger than 30s and let the dump dedupe it.
      ps -eo pid,ppid,etimes,comm,args --no-headers 2>/dev/null \
        | awk '$3+0 <= 30' \
        > "$RING/$(printf '%03d' $((i % 40)))" 2>/dev/null
      i=$((i + 1))
      sleep 0.5
    done
  ) &
  SAMPLER=$!
}
dump_ring() {
  # The ring holds ~20s of every process younger than 30s. Dedupe by pid,
  # samples, and any agent tool-call whose command line happens to contain
  # "kill" or "caelestia" dumps a screenful. What actually identifies a killer
  # is a SHORT-LIVED process that appeared just before the death, so: dedupe by
  # pid, drop anything older than a minute, drop our own plumbing, and truncate.
  local tmp; tmp=$(mktemp)
  cat "$RING"/* 2>/dev/null |
    awk '{
      pid=$1; ppid=$2; et=$3; comm=$4;
      $1=$2=$3=$4="";
      sub(/^ +/, "");
      if (et+0 > 60) next;                      # long-lived: was already there
      args=$0;
      if (index(args, "shell-snapshots/snapshot-bash") > 0) next;   # agent tool wrapper
      if (comm == "grep" || comm == "awk" || comm == "ps") next;
      if (index(args, "shell-supervi" "sor") > 0) next;
      if (index(args, "qs -c caelestia -n -d") > 0) next;           # the shell itself
      if (seen[pid]++) next;
      if (length(args) > 130) args = substr(args, 1, 130) "...";
      printf "%s %s %ss %s %s\n", pid, ppid, et, comm, args;
    }' | sort -u > "$tmp"

  if [ -s "$tmp" ]; then
    say "  --- every short-lived process seen in the ~20s before the death ---"
    while IFS= read -r l; do say "    $l"; done < "$tmp"
  elif [ -z "${SAMPLER:-}" ]; then
    say "  --- ring was cold (sampler disarmed); it is armed now, so a repeat will be recorded ---"
  else
    say "  --- no short-lived process seen; not a local kill (OOM? compositor? clean exit?) ---"
  fi
  rm -f "$tmp"
}

# Every headless output Hyprland creates gets a fresh HEADLESS-N name, and the
# shell leaves a per-screen config dir behind for each one
# (~/.config/caelestia/monitors/HEADLESS-N). The counter is at 32 here, and
# sunshine-vdisplay only prunes them on an explicit `off`. Reap the ones whose
# output no longer exists, on the same schedule as the tmpfs instance dirs.
reap_stale_monitor_cfgs() {
  local base="$HOME/.config/caelestia/monitors" live d n=0
  [ -d "$base" ] || return 0
  live=$(hyprctl monitors all -j 2>/dev/null |
         python3 -c 'import sys,json; print(" ".join(m["name"] for m in json.load(sys.stdin)))' 2>/dev/null) || return 0
  [ -z "$live" ] && return 0   # compositor not answering: do not guess
  for d in "$base"/HEADLESS-*; do
    [ -d "$d" ] || continue
    case " $live " in *" $(basename "$d") "*) continue ;; esac
    rm -rf -- "$d" && n=$((n + 1))
  done
  [ "$n" -gt 0 ] && say "reaped $n stale per-monitor config dirs"
  return 0
}

reap_dead_instances() {
  # Quickshell registers every run under $XDG_RUNTIME_DIR/quickshell as a
  # by-pid/<pid> symlink into a by-id/<id> directory holding that instance's
  # socket and its logs. A clean exit removes them; a death by signal never
  # does -- and this shell dies by signal constantly, because every QML save
  # restarts it. 164 corpses had piled up here holding 35MB, and that path is
  # a tmpfs, so those megabytes are RAM. Worse, quickshell enumerates every
  # one of them at startup, so each corpse makes the next launch slower.
  #
  # Only the ones whose recorded pid is gone are touched. A live instance has
  # its pid in /proc, so it can never be caught by this.
  local base="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/quickshell"
  [ -d "$base/by-pid" ] || return 0
  local link pid target n=0
  for link in "$base"/by-pid/*; do
    [ -L "$link" ] || continue
    pid=$(basename "$link")
    case "$pid" in *[!0-9]*) continue ;; esac
    [ -d "/proc/$pid" ] && continue
    target=$(readlink -f "$link" 2>/dev/null)
    rm -f "$link"
    # Never delete outside by-id, however odd the link turns out to be.
    case "$target" in
      "$base"/by-id/?*) rm -rf -- "$target" ;;
    esac
    n=$((n + 1))
  done
  [ "$n" -gt 0 ] && say "reaped $n dead quickshell instance dir(s)"
  return 0
}

reap_orphaned_helpers() {
  # `nmcli monitor` is a long-lived helper the shell spawns and never gets to
  # tear down when it dies by signal: quickshell is killed before it can reap
  # its children, init adopts them, and nothing ever cleans them up. Every
  # restart then stacks another one. Seven had piled up here, the oldest 15h
  # old, holding 61MB and seven NetworkManager D-Bus connections between them.
  #
  # A candidate must be BOTH one of those helpers AND orphaned (ppid 1). A
  # monitor someone started in a terminal is a child of that terminal, and one
  # belonging to a live shell has that shell as its parent, so neither can be
  # caught by this. Orphaned is the whole signal -- nothing owns it any more.
  local p ppid args n=0
  for p in $(pgrep -x nmcli 2>/dev/null); do
    ppid=$(awk '/^PPid:/{print $2}' "/proc/$p/status" 2>/dev/null) || continue
    [ "$ppid" = 1 ] || continue
    args=" $(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null)"
    case "$args" in *" monitor "*) ;; *) continue ;; esac
    kill "$p" 2>/dev/null && n=$((n + 1))
  done
  [ "$n" -gt 0 ] && say "reaped $n orphaned nmcli monitor(s) left by dead shells"
  return 0
}

cmd_run() {
  # One supervisor only.
  # Single instance. NOTE: deleting $LOCK while a supervisor holds it defeats
  # this entirely -- the next start creates a fresh inode and takes its own
  # lock, and you end up with several supervisors and several samplers writing
  # over each other's ring. Never rm the lock file; stop the service instead.
  exec 9>"$LOCK"
  flock -n 9 || { echo "supervisor already running" >&2; exit 0; }
  say "=== supervisor started (pid $$) ==="
  trap '[ -n "${SAMPLER:-}" ] && kill "$SAMPLER" 2>/dev/null' EXIT
  # The old trap killed the sampler and then fell straight back into the loop,
  # so `systemctl stop` hung for its full 90s TimeoutStop and systemd SIGKILLed
  # us every time. Leave when asked.
  trap 'say "=== supervisor stopping on signal ==="; [ -n "${SAMPLER:-}" ] && kill "$SAMPLER" 2>/dev/null; exit 0' INT TERM
  reap_dead_instances
  reap_stale_monitor_cfgs
  local last_reap=$SECONDS last_health=0 last_lock=0

  local fails=0 up_since=0 gone=0
  # Liveness alone cannot see a kill that is followed by a restart inside
  # POLL+GRACE: the shell is present on both sides of the gap, so `running`
  # never goes false and nothing is recorded. That is precisely what "the shell
  # keeps going away" looks like from the outside -- the bar vanishes and comes
  # back, and the log is silent. Track the pid's IDENTITY, not just its
  # existence, so a replacement is on the record even when liveness never dips.
  local cur_pid= churn=0 churn_since=$SECONDS churn_warned=0
  while true; do
    local now_pid; now_pid=$(shell_pid) || now_pid=
    SHELL_PID_CACHE=$now_pid
    if [ -n "$now_pid" ]; then
      [ "$up_since" = 0 ] && up_since=$SECONDS
      if [ -z "$cur_pid" ]; then
        cur_pid=$now_pid
      elif [ "$now_pid" != "$cur_pid" ]; then
        say "shell was REPLACED externally: pid $cur_pid -> $now_pid (previous instance lived $((SECONDS - up_since))s, supervisor never saw it absent)"
        churn=$((churn + 1))
        # Name the culprit once per window. Dumping the ring on every
        # replacement would flood the log out of its own rotation limit when
        # something is restarting the shell every couple of minutes.
        [ "$churn" = 1 ] && { dump_ring; arm_sampler; }
        cur_pid=$now_pid
        up_since=$SECONDS
      fi
      # Churn window: repeated external replacement is not something a
      # restart-on-death supervisor can fix, so surface it instead of hiding it.
      if [ $((SECONDS - churn_since)) -ge 600 ]; then
        [ "$churn" -gt 0 ] && say "churn: $churn external replacement(s) in the last 10min"
        churn=0; churn_since=$SECONDS; churn_warned=0
      fi
      if [ "$churn" -ge 5 ] && [ "$churn_warned" = 0 ]; then
        churn_warned=1
        # Log only, deliberately. Editing the shell's QML restarts it on every
        # save, so churn is the NORMAL state while someone is working on it --
        # a desktop notification here would be pure noise. The record is still
        # in the log for when churn is not expected.
        say "  CHURN: $churn external restarts in <10min -- expected while the shell's QML is being edited, otherwise something is cycling it"
      fi
      if [ $((SECONDS - last_lock)) -ge 15 ]; then
        sample_lock_state
        last_lock=$SECONDS
      fi
      if [ $((SECONDS - up_since)) -ge $STABLE_AFTER ] && [ "$fails" -ne 0 ]; then
        say "shell stable for ${STABLE_AFTER}s -- resetting failure counter (was $fails)"
        fails=0
      fi
      gone=0
    else
      cur_pid=
      up_since=0
      gone=$((gone + POLL))
      if [ "$gone" -lt "$GRACE" ]; then sleep "$POLL"; continue; fi

      if [ -f "$STOP" ]; then sleep "$POLL"; continue; fi   # deliberate stop

      # No compositor, no shell -- and nothing we could do about it. This
      # matters at login, when systemd may start us before Hyprland exists:
      # without it we would burn through the failure budget and "fall back to
      # stock" before there was anything to fall back onto.
      hypr_env
      if ! hyprctl version >/dev/null 2>&1; then
        sleep "$POLL"; continue
      fi

      fails=$((fails + 1))
      say "shell is gone (failure #$fails)"
      forensics
      arm_sampler

      if [ "$fails" -ge "$MAX_FAST_FAILS" ]; then
        say "  $fails consecutive failures -- falling back to the packaged shell"
        notify "Forked shell keeps failing" \
          "Started the packaged shell instead. See $LOG" critical
        start_shell stock
        sleep 20
        gone=0
        continue
      fi

      reap_orphaned_helpers

      local backoff=$((2 ** (fails - 1))); [ "$backoff" -gt 60 ] && backoff=60
      say "  restarting in ${backoff}s"
      sleep "$backoff"
      start_shell local
      sleep 6
      if running; then
        say "  restarted OK (pid $(shell_pid))"
        was_locked_recently && restore_lock
      else
        say "  restart did NOT take"
      fi
      gone=0
    fi
    if [ -n "${SAMPLER:-}" ] && [ "$SECONDS" -gt "$SAMPLER_UNTIL" ]; then
      disarm_sampler
    fi
    if [ $((SECONDS - last_reap)) -ge 3600 ]; then
      reap_dead_instances
      reap_stale_monitor_cfgs
      reap_orphaned_helpers
      last_reap=$SECONDS
    fi
    # A slow fd or memory leak is the failure mode a restart-on-death
    # supervisor would otherwise hide: it just restarts every few hours and
    # nobody notices the trend. Sample it so the trend is on the record.
    if [ $((SECONDS - last_health)) -ge 300 ]; then
      local hp; hp=$(shell_pid)
      if [ -n "$hp" ] && [ -d "/proc/$hp" ]; then
        say "health up=$(ps -o etimes= -p "$hp" 2>/dev/null | tr -d ' ')s rss=$(awk '/VmRSS/{printf "%.0f",$2/1024}' /proc/$hp/status)MB fds=$(ls /proc/$hp/fd 2>/dev/null | wc -l) threads=$(awk '/Threads/{print $2}' /proc/$hp/status) cache=$(du -sm "$HOME/.cache/caelestia/imagecache" 2>/dev/null | cut -f1)MB"
      fi
      last_health=$SECONDS
    fi
    sleep "$POLL"
  done
}

case "${1:-status}" in
  run)    cmd_run ;;
  off)    touch "$STOP"; echo "supervisor paused (restarts disabled)" ;;
  on)     rm -f "$STOP"; echo "supervisor resumed" ;;
  status)
    if [ -e "$LOCK" ] && command -v flock >/dev/null && ! flock -n "$LOCK" true 2>/dev/null; then
      echo "supervising : yes"
    else
      echo "supervising : no"
    fi
    echo "restarts    : $([ -f "$STOP" ] && echo 'PAUSED (supervisor.off present)' || echo enabled)"
    echo "shell       : $(running && echo "up (pid $(shell_pid))" || echo down)"
    echo "log         : $LOG"
    if [ -f "$LOG" ]; then echo "--- last 15 lines ---"; tail -15 "$LOG"; else echo "(no events recorded)"; fi
    ;;
  *) echo "usage: $(basename "$0") {run|status|off|on}" >&2; exit 2 ;;
esac
