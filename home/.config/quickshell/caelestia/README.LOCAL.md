# Local Caelestia shell overlay

This directory is a **fork of the packaged Caelestia shell**, taken from
`/etc/xdg/quickshell/caelestia` at the version recorded in `.upstream-version`.

Quickshell resolves `-c caelestia` against each XDG config directory in order,
and `$XDG_CONFIG_HOME` (`~/.config`) comes before `$XDG_CONFIG_DIRS`
(`/etc/xdg`). So as long as this directory exists, `caelestia shell -d` runs
**this** copy and the packaged one is inert. Nothing else had to change, and
`pacman -Syu` can no longer overwrite these edits.

To go back to stock, with no other cleanup needed:

    mv ~/.config/quickshell/caelestia ~/.config/quickshell/caelestia.disabled
    caelestia shell kill; caelestia shell -d

## What was changed and why

Every change is annotated in-place with a comment explaining the reasoning, so
`diff -ru /etc/xdg/quickshell/caelestia ~/.config/quickshell/caelestia` is the
authoritative list. Summary:

### Crash hardening

- **`modules/bar/popouts/ActiveWindow.qml`**, **`modules/windowinfo/Preview.qml`**
  Both held a *live* `ScreencopyView` whose `captureSource` was bound straight to
  the focused Hyprland toplevel. That re-points a running
  `ext-image-copy-capture` session at a different — or just-destroyed — window
  every time focus moves. Every Hyprland crash report in `~/.cache/hyprland`
  (nine of them, Jul 14 – Aug 1) is the same backtrace through that path:

      Screenshare::CScreenshareFrame::transform() const
      CImageCopyCaptureFrame::CImageCopyCaptureFrame(...)
      CExtForeignToplevelImageCaptureSourceManagerV1::setDestroy(...)

  The frame constructor reads `transform()` off the capture source, so a source
  that went away between the focus change and the next frame request is a null
  dereference *in the compositor* — the whole session dies, not just the shell.
  Both sites now rebuild the view per toplevel instead of rebinding, so a
  session is only ever pointed at the window it was created for and none exists
  while there is no active toplevel.

  The underlying null-deref is an upstream Hyprland bug; this only stops the
  shell from provoking it. To remove the bar-hover path entirely, set
  `bar.popouts.activeWindow: false` in `~/.config/caelestia/shell.json`.

### Startup and idle cost

- **`services/Time.qml`** — `SystemClock.Seconds` → `Minutes`. Nothing in the
  shell renders seconds: every consumer is `format()`, `hourStr`, `minuteStr` or
  `amPmStr`, all minute-granular. Seconds precision woke the process and
  re-evaluated the `timeStr → split → TextMetrics` chain 60x more often than any
  displayed value could change.

- **`modules/IdleMonitors.qml`** — `hasPlayer` and `isCharging` were eager
  bindings on `Players.list` and `UPower.onBattery`, so the shell stood up the
  full MPRIS player watcher and a UPower connection at startup and kept them
  alive all session — to feed an idle inhibitor that has no timeouts configured
  (`general.idle.timeouts` is `[]`). Both are now gated behind
  `anyTimeouts`, and `&&` short-circuits before touching either singleton.
  `IdleMonitors` was the *only* thing instantiating `Players` at startup; every
  other consumer is inside a lazily-loaded panel.

- **`modules/GSFLoader.qml`** — was a bare `FontLoader` for the bundled Google
  Sans Flex: a 3.9MB six-axis variable font that Qt read and registered on every
  start whether or not anything asked for it. Every family in
  `appearance.font` here points at an installed system font (Rubik / Material
  Symbols Rounded / CaskaydiaCove NF), so it was pure startup cost. Now only
  registered when a configured family actually names it.

- **`modules/BatteryMonitor.qml`** — every other UPower consumer in the shell
  guards on `isLaptopBattery`; this one did not, so on a desktop it kept two
  live D-Bus signal handlers for a device that reports `battery-missing`.

### Brightness correctness

- **`services/Brightness.qml`**
  - `ddcutil detect` walks every I²C bus, which on DisplayPort means DDC/CI
    traffic over the AUX channel. It ran once per monitor change, and monitors
    churn in bursts (creating or removing a headless output fires it for every
    screen). Now coalesced behind a 400ms debounce.
  - The `asdbctl get` probe ran unconditionally at startup even though it only
    applies to an Apple Studio Display, and `asdbctl` is not installed here.
    Now gated on one actually being attached.
  - **`brightnessctl` is now scoped with `-c backlight`.** Unscoped, it targets
    the first device of *any* class. This machine has no panel backlight
    (`/sys/class/backlight` is empty), so the first device was
    `enp5s0-3::lan` — the NIC's LAN activity LED. "Set screen brightness" was
    dimming the Ethernet port. Scoped, it is a clean no-op with no backlight,
    and DDC (which does work here — the G271C answers on `/dev/i2c-9`) is
    unaffected.
  - Guarded the non-DDC brightness parse against `NaN`, which used to propagate
    into every binding downstream when the read returned nothing.

## Keeping it alive

The shell is a single unsupervised process. Anything that kills it -- a stray
`pkill`, a compositor fault, an agent session restarting it to preview a change
-- used to leave the desktop with no bar until a human noticed.

`~/.config/hypr/scripts/caelestia-shell-supervisor.sh`, run as the systemd user
service `caelestia-shell-supervisor.service` (`Restart=always`), fixes that.
Measured, not assumed -- verified with three separate SIGKILL tests:

| | |
|---|---|
| detect death | ~5s (2s poll + grace) |
| restart | 7-9s |
| crash vs kill | checks for a coredump: present = crashed, absent = killed or clean exit |
| names the culprit | every process younger than 30s seen in the ~20s before the death |
| survives its own death | systemd brought it back in 5s when killed |
| repeated failures | 4 fast failures -> falls back to the packaged shell + critical notification |

    systemctl --user status caelestia-shell-supervisor
    ~/.config/hypr/scripts/caelestia-shell-supervisor.sh status
    ~/.local/state/caelestia-fork/supervisor.log

**Deliberate stops are respected.** The kill keybind writes
`~/.local/state/caelestia-fork/supervisor.off` and the restart keybind clears
it, so `CTRL+SUPER+SHIFT+R` still means "stay down" and `CTRL+SUPER+ALT+R` still
means "restart". `supervisor.sh off` / `on` do the same by hand.

**Never `rm` the lock file.** `supervisor.lock` is what `flock` uses for single
instance. Deleting it while a supervisor holds it lets the next start take a
lock on a fresh inode -- which is how four supervisors and four samplers ended
up running at once here. Stop the service instead.

### Why the ring captures by age, not by name

The first version grepped the process table for `qs|kill|pkill|caelestia`. It
missed a test killer completely, because the script was named something else and
used bash's `kill` **builtin**, which never appears as a process. Anything that
can kill the shell has to run, and anything that just ran is young -- so the
sampler now takes every process younger than 30s and the dump dedupes it.

## Cache

`~/.cache/caelestia/imagecache` is written by the native CachingImage plugin and
has **no eviction anywhere** -- not in the QML, not in caelestia-cli, not in
`libcaelestia-images.so`. It had reached **1.3 GB across 3059 files**: one
full-screen crop per wallpaper ever displayed, plus crops at streaming-client
geometries (`@9600x4320`, `@7680x4320`, `@2340x1080`) that match no live output.

`caelestia-cache-prune.sh` bounds it by shape rather than by a blunt size cap,
which would have evicted the cheap thumbnail grid along with everything else:
keep all thumbnails under 1 MB, keep the newest 40 native-geometry crops, drop
crops for geometries no monitor has. First run took it to **238 MB**. A daily
timer (`caelestia-cache-prune.timer`) keeps it there.

Quickshell also never removes its per-instance dirs under
`/run/user/*/quickshell/by-id` -- that is tmpfs, i.e. RAM, and it grows with
every restart (47 dead dirs / 31 MB had built up). The supervisor reaps them at
startup and hourly.

## Surviving upgrades

**`pacman -Syu` cannot touch any of this.** Verified, not assumed: `pacman -Qo`
reports no package owns anything under `~/.config`, and `caelestia-shell`'s
396-entry file list contains zero paths under `/home` — only
`/etc/xdg/quickshell/caelestia`, `/usr/lib/caelestia`, `/usr/lib/qt6/qml/{Caelestia,M3Shapes}`
and `/usr/share/licenses`. Further: `caelestia-shell`, `caelestia-cli` and
`quickshell-git` are all **AUR/foreign** packages (`pacman -Qm`), so plain
pacman does not upgrade them at all.

What *can* move under this fork:

- **`paru -Syu`, or `caelestia update`** (which runs `paru -Syu` first,
  unconditionally, at `update.py:32`). That upgrades `caelestia-shell` and swaps
  the native plugins in `/usr/lib/qt6/qml/Caelestia` that this QML imports
  unversioned — while this tree stays on its pinned version. The running shell
  survives (loaded QML, mapped inodes); it breaks at the *next* start.
- **`qt6-base` via plain `pacman -Syu`.** `quickshell` links 62 symbols on the
  `Qt_6_PRIVATE_API` node and subclasses `QtWaylandClient::QWaylandShellSurface`.
  Nothing rebuilds an AUR `quickshell-git` when Qt moves. This breaks stock and
  fork identically — it is not a fork problem. `quickshell-git` already ships
  `/usr/share/libalpm/hooks/quickshell-check.hook`, which prints a red
  COMPATIBILITY WARNING *during the transaction*. It is silent today (build and
  runtime Qt both 6.11.1), so its first appearance is a true positive.

### The routine

    paru -Syu --ignore caelestia-shell --ignore quickshell-git   # day to day
    # when you do want the shell upgrade:
    paru -S caelestia-shell
    ~/.config/quickshell/caelestia/sync-upstream.sh
    git -C ~/.config/quickshell/caelestia merge upstream/main
    qs -c caelestia kill; sleep .1; caelestia shell -d

`~/.local/bin/caelestia-upgrade-watch` (driven by the systemd user path unit
`caelestia-upgrade-watch.path`) notifies and toasts if those packages ever move
without you re-merging, and appends a before/after record to
`~/.local/state/caelestia-fork/history.log`.

If the fork ever fails to load after an upgrade, `hypr-user.lua` runs
`~/.config/hypr/scripts/caelestia-shell-guard.sh check` 25s after login: it
starts the packaged shell instead and notifies. You keep a desktop either way.
`caelestia-shell-guard.sh status` shows which is running and whether the pin is
stale.

### Do not use NoUpgrade / NoExtract

Needs root, protects nothing (nothing you customised is pacman-owned), and is
actively harmful here: `NoExtract` would leave stale bytes in `/etc/xdg` while
`pacman -Q` reports the new version, so `sync-upstream.sh`'s version guard would
pass, snapshot the stale tree, and then refuse to re-sync — silently destroying
the merge base.

### Your hand-edited Hyprland config is safe from `caelestia update`

`caelestia dots` deploys 25 files into `~/.config`, including every
`hypr/hyprland/*.lua` you have edited. It detects local modification by a
**byte-for-byte comparison against the git blob at `applied_rev`**
(`diff.py:122`) — not mtime, not a stored hash. If upstream changed a file you
edited, your file is left untouched and upstream's is written beside it as
`<name>.new` (`diff.py:125`, `update.py:149-151`). If upstream did not change it,
it never even reads your file (`diff.py:106`). The only in-place overwrite path
requires your file to be byte-identical to what dots deployed.

`caelestia install` is the dangerous one — it copies all of `~/.config` to
`~/.config.bak` and redeploys. `update` never does that.

## Re-merging after a caelestia-shell upgrade

    diff -ru /etc/xdg/quickshell/caelestia ~/.config/quickshell/caelestia

Everything that shows up is either one of the changes above (all commented) or
something new upstream that you want to pull in.
