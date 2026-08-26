# Desktop-Configs-Installs-And-More

Everything needed to rebuild this Arch + Hyprland + Caelestia desktop from a blank install.
Games and proprietary app payloads are deliberately **not** here — they re-download.

## Restore onto a fresh machine

```bash
git clone https://github.com/ut3q/Desktop-Configs-Installs-And-More.git
cd Desktop-Configs-Installs-And-More
./install.sh --dry-run     # read what it intends to do
./install.sh               # do it
```

`install.sh` never destroys anything silently: every file it replaces is copied to
`~/.config-backup-<timestamp>/` first.

### Flags

| Flag | Effect |
|---|---|
| `--dry-run` | Print every action, change nothing |
| `--only <section>` | Run one section: `packages`, `configs`, `system`, `repos`, `wallpapers`, `units` |
| `--skip <section>` | Run everything except that section (repeatable) |
| `--yes` | No prompts |

## Saving new changes

```bash
./capture.sh          # live machine  ->  this repo
git add -A && git commit -m "what changed" && git push
```

`capture.sh` is the only thing that writes into `home/`, `system/`, `packages/`,
`repos/` and `wallpapers/`. Never hand-edit those — edit the real config on the
machine and re-capture, or the next capture will overwrite you.

## What is tracked

Edit [`manifest.conf`](manifest.conf) to add or drop things. One line per path:

```
home    .config/hypr                      # relative to $HOME
system  /etc/systemd/system/foo.service   # absolute, restored with sudo
repo    .config/hypr                      # also bundle its git history
```

| Directory | Contents |
|---|---|
| `home/` | 53 config trees — shell, Hyprland, Caelestia/quickshell, terminals, editors, GTK/Qt theming, OBS, OpenTabletDriver, mpv, yt tooling |
| `home/.local/bin` | 13 hand-written scripts (`sunshine-vdisplay`, `osu-server-picker`, `caelestia-upgrade-watch`, `flatpak-temp`, ...) |
| `home/.local/share/applications` | 20 custom `.desktop` launchers |
| `system/` | `/usr/local/bin` scripts (`backup-mirror`, `arch-cleaner`, `discord-updater`) and the `backup-mirror` systemd unit + excludes |
| `packages/` | 130 official + 22 AUR + 8 flatpak, plus the enabled-unit lists |
| `repos/` | `git bundle` archives — full commit history for repos that exist nowhere else |
| `wallpapers/` | 602 files (492M), losslessly optimized; `.current` records the active one |
| `wallpapers-meta/` | the 300 wallpapers fetched from upstream instead of stored — see below |
| `docs/` | `fstab.reference`, `system-snapshot.txt` — reference only, never auto-applied |

### Git bundles

`.config/hypr` and `.config/quickshell/caelestia` are real git repos whose history
existed on one drive and nowhere else. A bundle is a single file containing the
entire history — `install.sh` clones from it, so commits survive a disk loss.
Inspect one by hand with:

```bash
git clone repos/config-hypr.bundle /tmp/hypr && git -C /tmp/hypr log
```

## Wallpapers

Optimized losslessly with `oxipng`/`jpegoptim`: **1.40 GB → 0.83 GB** on disk.
Of 902 files, **300 were verified byte-identical** to
[SleepyCatHey/Ultimate-Win11-Setup](https://github.com/SleepyCatHey/Ultimate-Win11-Setup)
and are not stored here — `install.sh` clones that repo and copies them in,
saving 354 MB. Verification compared upstream file sizes against the
pre-optimization originals in the backup mirror, so it is an exact match, not
a guess by filename.

If upstream ever vanishes, those files are still in `/mnt/backup` and in this
repo's history prior to the dedupe commit.

To re-optimize after adding new wallpapers, re-run the optimizer, then
`./capture.sh`.

## Steam game saves

| Stored here | Not stored |
|---|---|
| `userdata/` — Steam Cloud payloads (Monster Hunter World's `SAVEDATA1000`) and client config, 23 MB | Steam's root `config.vdf`, `loginusers.vdf`, `ssfn*` — **account auth material**, excluded in the manifest, `capture.sh` and `.gitignore` |
| ARK `Config/`, `LocalProfiles/`, `SaveGames/` — 612 KB | ARK `SavedArksLocal/` etc — 308 MB of world saves that churn every session |

ARK's world saves would add ~300 MB to history *per capture*, so they go to the
local mirror instead: `backup-mirror` has a second rsync pass covering them,
since the main exclude list drops all of `~/.var/app/com.valvesoftware.Steam`
(448 GB of re-downloadable games). Install it with:

```bash
./install.sh --only system
```

## stability-check

`.local/bin/stability-check` counts MCEs, panics, lockups, segfaults, amdgpu
resets, EDAC errors and thermal throttling, then prints current temps. Run it
after a BIOS/tuning change and a session of real load.

```bash
stability-check       # this boot
stability-check -a    # last 14 days
```

## flatpak-temp

`.local/bin/flatpak-temp` runs a Flathub app without keeping it:

```bash
flatpak-temp org.gimp.GIMP           # install, run, remove on exit
flatpak-temp org.gimp.GIMP --purge   # also delete ~/.var/app data
flatpak-temp gimp                    # search when the id is not exact
flatpak-temp --dry-run org.gimp.GIMP
```

Runtimes you already have are reused, so a typical app is a few MB rather than
a gigabyte. Only genuinely orphaned runtimes are reclaimed, and an app you
installed permanently is detected and never removed.

Note: it installs to the **user** scope, which needs its own `flathub` remote.
The script adds it once, using the `.flatpakrepo` URL — a bare repo URL carries
no GPG key and every pull then fails with "public key not found".

## Deliberately excluded

- **Cookie jars and any `*cookies*` file** — live logged-in sessions. Never commit these.
- Build artifacts: `Vencord/dist`, `equicord.asar`, `quickshell/caelestia/build`
- `.bak` sprawl, `__pycache__`, caches, logs, sockets, pid files
- Games (~549G), browser profiles, Steam, `.local/share/osu`
- `/etc/fstab` — captured to `docs/` for reference only. **UUIDs are machine-specific;
  copying it blindly onto new hardware will produce an unbootable system.**

## Not restorable by any script

- Logging into Discord, Steam, browsers
- Re-pairing Bluetooth devices
- `/etc/fstab` UUIDs (see above)
