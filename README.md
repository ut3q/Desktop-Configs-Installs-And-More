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
| `wallpapers/` | 902 files (1.3G); `.current` records which one is active |
| `docs/` | `fstab.reference`, `system-snapshot.txt` — reference only, never auto-applied |

### Git bundles

`.config/hypr` and `.config/quickshell/caelestia` are real git repos whose history
existed on one drive and nowhere else. A bundle is a single file containing the
entire history — `install.sh` clones from it, so commits survive a disk loss.
Inspect one by hand with:

```bash
git clone repos/config-hypr.bundle /tmp/hypr && git -C /tmp/hypr log
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
- `.local/bin/jan` — a 9MB downloaded binary, not a script
- `/etc/fstab` — captured to `docs/` for reference only. **UUIDs are machine-specific;
  copying it blindly onto new hardware will produce an unbootable system.**

## Not restorable by any script

- Logging into Discord, Steam, browsers
- Re-pairing Bluetooth devices
- `/etc/fstab` UUIDs (see above)
