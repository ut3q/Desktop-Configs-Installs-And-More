#!/usr/bin/env bash
# caelestia-cache-prune — bound the shell's image cache, without gutting it.
#
# ~/.cache/caelestia/imagecache is written by the native CachingImage plugin and
# has NO eviction anywhere: not in the QML, not in caelestia-cli, not in
# libcaelestia-images.so. Every wallpaper ever displayed is kept at every size it
# was ever drawn at, forever. On this machine it reached 1.3 GB / 3059 files.
#
# A blunt size cap is the wrong tool -- it would evict the cheap thumbnail grid
# along with everything else. The cache has a very clear shape:
#
#   ~950 MB   @1920x1080   one full-screen crop per wallpaper ever displayed.
#                          Only one is ever on screen; the rest are stale.
#   ~250 MB   odd sizes    @9600x4320, @7680x4320, @2340x1080 ... these match no
#                          live output. They are Sunshine streaming-client
#                          geometries, dead the moment the stream ends.
#   ~35 MB    @280x158     the picker's thumbnail grid, 797 files. Cheap. Keep.
#
# So: drop crops for geometries no monitor has, keep only the most recent N
# full-screen crops, and leave small thumbnails alone.
#
#   report / prune
#   --keep-full N     full-screen crops to retain (default 40, newest first)
#   --safety-min M    never touch anything written in the last M minutes (default 60).
#                     This is only a guard against deleting something mid-write --
#                     recency is already protected far more precisely by
#                     --keep-full, so a multi-day window is redundant and just
#                     blocks the cleanup on a day you browsed a lot of wallpapers.
#   --thumb-max-kb K  "small thumbnail" threshold (default 1024)
#
# /home is noatime, so mtime is the only usable age signal. mtime is when an
# entry was written, so a hot entry can look old -- fine for a cache, and
# --keep-full protects the recently-displayed set precisely.

set -uo pipefail

CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/caelestia/imagecache"
NOTIFS="$CACHE/notifs"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/caelestia"

KEEP_FULL=40
SAFETY_MIN=60
THUMB_MAX_KB=1024
MODE=report

while [ $# -gt 0 ]; do
  case "$1" in
    report|prune)     MODE=$1 ;;
    --keep-full)      KEEP_FULL=$2; shift ;;
    --safety-min)     SAFETY_MIN=$2; shift ;;
    --thumb-max-kb)   THUMB_MAX_KB=$2; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

[ -d "$CACHE" ] || { echo "no cache at $CACHE"; exit 0; }

# Geometries any current output actually uses (incl. common integer scales).
live_geoms() {
  hyprctl monitors all -j 2>/dev/null | python3 -c '
import sys, json
try: mons = json.load(sys.stdin)
except Exception: sys.exit(0)
out = set()
for m in mons:
    w, h = m["width"], m["height"]
    for s in (1, 2):
        out.add(f"{w*s}x{h*s}")
    out.add(f"{w}x{h}")
print("\n".join(sorted(out)))' 2>/dev/null
}

referenced_notifs() {
  python3 - "$STATE/notifs.json" <<'PY' 2>/dev/null
import json, sys, os
try: d = json.load(open(sys.argv[1]))
except Exception: sys.exit(0)
for n in d:
    if isinstance(n, dict) and n.get("image"):
        print(os.path.basename(n["image"]))
PY
}

total_mb() { du -sm "$CACHE" 2>/dev/null | cut -f1; }

GEOMS=$(live_geoms)
PROTECTED=$(referenced_notifs | sort -u)
THUMB_MAX=$((THUMB_MAX_KB * 1024))

# Emit "class<TAB>size<TAB>path" for every deletion candidate.
candidates() {
  # Small thumbnails are never candidates.
  find "$CACHE" -maxdepth 1 -type f -size +"${THUMB_MAX_KB}k" -mmin +"$SAFETY_MIN" \
       -printf '%s\t%T@\t%f\t%p\n' 2>/dev/null |
  while IFS=$'\t' read -r sz ts fname path; do
    geom=${fname#*@}; geom=${geom%%-*}
    if printf '%s\n' "$GEOMS" | grep -qxF "$geom"; then
      printf 'native\t%s\t%s\t%s\n' "$sz" "$ts" "$path"      # ranked below
    else
      printf 'stale-geom\t%s\t%s\t%s\n' "$sz" "$ts" "$path"  # no live output
    fi
  done
}

ALL=$(candidates)
STALE=$(printf '%s\n' "$ALL" | awk -F'\t' '$1=="stale-geom"')
# Keep the newest KEEP_FULL native-geometry crops; everything older goes.
NATIVE_OLD=$(printf '%s\n' "$ALL" | awk -F'\t' '$1=="native"' | sort -t$'\t' -k3 -rn | tail -n +$((KEEP_FULL + 1)))

DELETE=$(printf '%s\n%s\n' "$STALE" "$NATIVE_OLD" | awk -F'\t' 'NF>=4')

sum_mb() { awk -F'\t' '{t+=$2} END{printf "%.0f", t/1048576}'; }
count()  { grep -c . || true; }

echo "cache        : $CACHE"
echo "current      : $(total_mb) MB across $(find "$CACHE" -type f | wc -l) files"
echo "live geoms   : $(printf '%s' "$GEOMS" | tr '\n' ' ')"
echo "policy       : keep newest $KEEP_FULL native crops, drop non-live geometries,"
echo "               never touch <${THUMB_MAX_KB}KB thumbnails or anything written in the last ${SAFETY_MIN}min"
echo
echo "  stale geometry : $(printf '%s\n' "$STALE"      | count) files, $(printf '%s\n' "$STALE"      | sum_mb) MB"
echo "  surplus native : $(printf '%s\n' "$NATIVE_OLD" | count) files, $(printf '%s\n' "$NATIVE_OLD" | sum_mb) MB"
echo "  TOTAL to free  : $(printf '%s\n' "$DELETE"     | count) files, $(printf '%s\n' "$DELETE"     | sum_mb) MB"

if [ "$MODE" = prune ]; then
  n=$(printf '%s\n' "$DELETE" | count)
  [ "${n:-0}" -eq 0 ] && { echo; echo "nothing to do"; exit 0; }
  printf '%s\n' "$DELETE" | while IFS=$'\t' read -r cls sz ts path; do
    [ -n "${path:-}" ] || continue
    case "$path" in
      "$NOTIFS"/*) printf '%s\n' "$PROTECTED" | grep -qxF "$(basename "$path")" && continue ;;
    esac
    rm -f -- "$path"
  done
  echo
  echo "pruned       : now $(total_mb) MB across $(find "$CACHE" -type f | wc -l) files"
fi
