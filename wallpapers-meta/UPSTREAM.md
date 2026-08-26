# Upstream wallpaper source

300 of the wallpapers in `~/Pictures/Wallpapers` come from:

    https://github.com/SleepyCatHey/Ultimate-Win11-Setup  ->  Wallpapers/

They are **not stored in this repo**. Each was verified byte-identical to the
upstream copy (compared against the pre-optimization originals in the backup
mirror), so re-downloading gives exactly the same image. Keeping them out saves
354 MB.

`upstream-provided.txt` is that verified list. `capture.sh` skips those names;
`install.sh` clones the upstream repo and copies them in.

If upstream ever disappears, the files still exist in this repo's history prior
to the dedupe, and in the local backup mirror at /mnt/backup.
