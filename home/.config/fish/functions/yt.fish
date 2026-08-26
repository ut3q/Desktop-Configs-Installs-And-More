# yt - YouTube from the terminal: search, subscriptions, a local library
#      with categories and notes, offline downloads, resume tracking.
#
# Backing store and network layer live in ~/.config/fish/yt/ytlib.py
# (stdlib Python, one process per action).
#
# This tool is strictly READ-ONLY against YouTube. It never modifies your
# playlists and never reads or writes YouTube's own Watch Later. The
# "saved" list here is a local SQLite table and nothing else.

set -g __yt_lib "$HOME/.config/fish/yt/ytmain.py"
# The launcher imports this one; it is what actually has to exist.
set -g __yt_core "$HOME/.config/fish/yt/ytlib.py"
# ytlib takes these from YT_CACHE_DIR / YT_DATA_DIR and falls back to the
# XDG defaults; fish only ever had the fallbacks. So a test run with those set
# still wrote its play log, its mpv log and its watch-later files into the real
# ones - the Python half was sandboxed and this half was not.
set -g __yt_cache "$HOME/.cache/yt"
test -n "$YT_CACHE_DIR"; and set -g __yt_cache "$YT_CACHE_DIR"
set -g __yt_data "$HOME/.local/share/yt"
test -n "$YT_DATA_DIR"; and set -g __yt_data "$YT_DATA_DIR"
set -g __yt_thumbs "$__yt_cache/thumbs"
set -g __yt_wl "$__yt_data/watchlater"

# One python start-up supplies every setting fish needs plus a key legend
# already wrapped to the pane width, so no binding can be truncated off.
function __yt_cfg_init
    set -q __yt_cfg_pct; and return
    __yt_cfg_parse (__yt_py_list ui 2>/dev/null)
end

# Parses the key=value block that `ui` and `boot` both emit. Split out so the
# picker can get its settings from the same process that produced its rows.
function __yt_cfg_parse
    set -g __yt_cfg_hdr
    for line in $argv
        set -l kv (string split -m1 '=' -- $line)
        test (count $kv) -eq 2; or continue
        switch $kv[1]
            case pct
                set -g __yt_cfg_pct $kv[2]
            case gfx
                set -g __yt_cfg_gfx $kv[2]
            case tq
                set -g __yt_cfg_tq $kv[2]
            case dither
                set -g __yt_cfg_dither $kv[2]
            case grain
                set -g __yt_cfg_grain $kv[2]
            case intensity
                set -g __yt_cfg_intensity $kv[2]
            case playfallback
                set -g __yt_cfg_playfallback $kv[2]
            case hideterm
                set -g __yt_cfg_hideterm $kv[2]
            case hidetermaudio
                set -g __yt_cfg_hidetermaudio $kv[2]
            case useproxy
                set -g __yt_cfg_useproxy $kv[2]
            case cookies
                set -g __yt_cfg_cookies $kv[2]
            case hwdec
                set -g __yt_cfg_hwdec $kv[2]
            case vfmt
                set -g __yt_cfg_vfmt $kv[2]
            case afmt
                set -g __yt_cfg_afmt $kv[2]
            case streamclient
                set -g __yt_cfg_client $kv[2]
            case deband
                set -g __yt_cfg_deband $kv[2]
            case shaders
                set -g __yt_cfg_shaders $kv[2]
            case hdr
                set -a __yt_cfg_hdr $kv[2]
        end
    end
    string match -qr '^\d+$' -- "$__yt_cfg_pct"
    and test "$__yt_cfg_pct" -ge 20 -a "$__yt_cfg_pct" -le 70
    or set -g __yt_cfg_pct 40
    test -n "$__yt_cfg_tq"; or set -g __yt_cfg_tq maxresdefault
    test -n "$__yt_cfg_dither"; or set -g __yt_cfg_dither diffusion
    test -n "$__yt_cfg_grain"; or set -g __yt_cfg_grain 1x1
    test -n "$__yt_cfg_intensity"; or set -g __yt_cfg_intensity 0.5
    test -n "$__yt_cfg_playfallback"; or set -g __yt_cfg_playfallback android
    test -n "$__yt_cfg_hideterm"; or set -g __yt_cfg_hideterm 0
    test -n "$__yt_cfg_hidetermaudio"; or set -g __yt_cfg_hidetermaudio 0
    test -n "$__yt_cfg_useproxy"; or set -g __yt_cfg_useproxy 0
    test -n "$__yt_cfg_hwdec"; or set -g __yt_cfg_hwdec auto-safe
    test -n "$__yt_cfg_vfmt"; or set -g __yt_cfg_vfmt 'bv*+ba/b'
    test -n "$__yt_cfg_afmt"; or set -g __yt_cfg_afmt bestaudio/best
    test -n "$__yt_cfg_client"; or set -g __yt_cfg_client tv_simply
    test -n "$__yt_cfg_deband"; or set -g __yt_cfg_deband 1
    test (count $__yt_cfg_hdr) -gt 0; or set -g __yt_cfg_hdr 'enter play · ^s save · ^d download'
end

function __yt_preview_pct
    __yt_cfg_init
    echo $__yt_cfg_pct
end

function __yt_py
    command python3 -S $__yt_lib $argv
end

# Same, but tells the renderer to lay rows out for fzf's list pane rather
# than the full terminal width. Python reads the real width from the tty.
function __yt_py_list
    set -lx YT_PANE 1
    command python3 -S $__yt_lib $argv
end

# Which graphics protocol the terminal can actually do. Resolved once per
# call; chafa cannot autodetect from inside an fzf preview pane because
# its stdout there is a pipe, not a tty.
function __yt_cols
    set -l c 0
    set -q COLUMNS; and string match -qr '^\d+$' -- "$COLUMNS"; and set c $COLUMNS
    test "$c" -gt 0 2>/dev/null; or set c 100
    echo $c
end

function __yt_rows
    set -l r 0
    set -q LINES; and string match -qr '^\d+$' -- "$LINES"; and set r $LINES
    test "$r" -gt 0 2>/dev/null; or set r 30
    echo $r
end

function __yt_gfx
    __yt_cfg_init
    set -l override $__yt_cfg_gfx
    if test -n "$override" -a "$override" != auto
        echo $override
        return
    end
    if test -n "$KITTY_WINDOW_ID"; or test "$TERM" = xterm-kitty
        echo kitty
    else
        switch "$TERM"
            case 'foot*' 'wezterm*' 'contour*' 'mlterm*' 'xterm-256color'
                echo sixels
            case '*'
                if test "$TERM_PROGRAM" = WezTerm -o "$TERM_PROGRAM" = iTerm.app
                    echo sixels
                else
                    echo symbols
                end
        end
    end
end

function __yt_shq
    # Wrap for /bin/sh: single-quote, escaping embedded single quotes.
    for a in $argv
        printf "'%s' " (string replace -a -- "'" "'\\''" $a)
    end
end

function __yt_preview_cmd
    # The preview runs as a standalone sh script rather than an inline string:
    # quoting it through fish -> fzf -> sh was a repeated source of bugs.
    __yt_cfg_init
    set -l fmt (__yt_gfx)
    set -l tq $__yt_cfg_tq
    set -l dopt "--dither=$__yt_cfg_dither --dither-grain=$__yt_cfg_grain --dither-intensity=$__yt_cfg_intensity"
    test "$__yt_cfg_dither" = none; and set dopt "--dither=none"
    if not command -q chafa
        set fmt ""
    end

    set -l script "$__yt_cache/preview.sh"
    command mkdir -p "$__yt_cache" "$__yt_cache/sixel"
    printf '%s\n' \
        '#!/bin/sh' \
        '# generated by yt; edits are overwritten' \
        'id=$1; detail=$2' \
        "yc=\"$__yt_cache\"" \
        '# The row under the cursor, for the proxy to resolve ahead of time if' \
        '# it settles here. A builtin writing 12 bytes: no process, no socket,' \
        '# and nothing at all reads it unless the proxy is up.' \
        'echo "$id" > "$yc/focus" 2>/dev/null' \
        'C=${FZF_PREVIEW_COLUMNS:-60}; L=${FZF_PREVIEW_LINES:-20}' \
        'cap=$(( L * 6 / 10 )); ir=0' \
        "thumb=\"\$yc/thumbs/\$id.jpg\"" \
        "fmt='$fmt'" \
        'sd="$yc/sixel"' \
        "sx=\"\$sd/\$id-\${C}x\${cap}-$fmt.six\"" \
        'if [ -n "$fmt" ]; then' \
        "  [ -s \"\$thumb\" ] || curl -sfL --max-time 5 -o \"\$thumb\" \"https://i.ytimg.com/vi/\$id/$tq.jpg\" 2>/dev/null" \
        '  if [ -s "$sx" ]; then' \
        '    cat "$sx"; ir=$(( C * 9 / 32 + 1 ))' \
        '  elif [ -s "$thumb" ]; then' \
        '    # $$-unique temp: two pickers at the same width, or the warmer' \
        '    # below racing the foreground, would otherwise both write one' \
        '    # shared .tmp and the loser mv would blank the preview.' \
        "    chafa -f \"\$fmt\" --animate=off -c 256 $dopt -s \"\${C}x\${cap}\" \"\$thumb\" > \"\$sx.\$\$\" 2>/dev/null \\" \
        '      && mv -f "$sx.$$" "$sx" && cat "$sx"' \
        '    rm -f "$sx.$$"' \
        '    ir=$(( C * 9 / 32 + 1 ))' \
        '  fi' \
        '  [ $ir -gt $cap ] && ir=$cap' \
        '  # Warm the rest of the visible list once, at the exact geometry we' \
        '  # just used, so scrolling does not pay ~24ms of chafa per row. The' \
        '  # id list is written by the picker at launch. Detached and niced;' \
        '  # the marker keeps it to one run per size per session.' \
        '  wl="$yc/warm-ids"' \
        "  wm=\"\$sd/.warmed-\${C}x\${cap}-$fmt\"" \
        '  if [ -s "$wl" ] && [ ! -e "$wm" ]; then' \
        '    : > "$wm"' \
        '    (' \
        '      while read -r w; do' \
        "        o=\"\$sd/\$w-\${C}x\${cap}-$fmt.six\"" \
        '        [ -s "$o" ] && continue' \
        '        t="$yc/thumbs/$w.jpg"' \
        '        [ -s "$t" ] || continue' \
        "        nice -n 19 chafa -f \"\$fmt\" --animate=off -c 256 $dopt -s \"\${C}x\${cap}\" \"\$t\" > \"\$o.\$\$\" 2>/dev/null \\" \
        '          && mv -f "$o.$$" "$o" || rm -f "$o.$$"' \
        '      done < "$wl"' \
        '    ) </dev/null >/dev/null 2>&1 &' \
        '  fi' \
        'fi' \
        'n=$(( L - cap - 2 )); [ $n -lt 3 ] && n=3' \
        "printf '\\n'" \
        'printf "%b\\n" "$detail" | head -n $n' \
        > $script
    command chmod +x $script
    echo "$script {1} {4}"
end

function __yt_url --argument-names id
    echo "https://www.youtube.com/watch?v=$id"
end

# ---- window hiding ----------------------------------------------------

# Park the terminal on a special workspace for the duration of playback, then
# put it back exactly where it was. Silent moves so focus does not jump.
# Window identification lives in ytlib (find_own_window): it walks our
# process ancestry and matches against Hyprland's client list, so we hide the
# terminal that launched us rather than whatever happens to have focus.
#
# Hyprland 0.56 replaced the old string dispatchers with a Lua API, so
# `hyprctl dispatch movetoworkspacesilent ...` is now a parse error. Note also
# that `hyprctl eval "return hl.dsp.window.move(...)"` only CONSTRUCTS the
# dispatcher - `hyprctl dispatch` is what actually executes it.
function __yt_hypr
    hyprctl dispatch "$argv[1]" 2>&1
end

function __yt_win_hide
    # Address and workspace come from the caller (playinfo already looked them
    # up); falls back to its own lookup so the function still stands alone.
    command -q hyprctl; or return 1
    set -l parts $argv
    if test (count $parts) -ne 2
        set parts (string split ' ' -- (__yt_py win-self 2>/dev/null))
        test (count $parts) -eq 2; or return 1
    end

    # Moving a window always drags the view with it - there is no silent
    # variant in the 0.56 API, and `silent=true` is accepted but ignored. A
    # *special* workspace is worse still: it stays visible as an overlay AND
    # captures newly-spawned windows, so mpv ended up hidden alongside the
    # terminal. So: park the terminal on a plain named workspace, then switch
    # the view straight back, which leaves mpv opening where you are looking.
    # `hyprctl activeworkspace` without -j already prints "workspace ID n (name):",
    # so parsing it here saves forking jq purely to read one field.
    set -l cur (string replace -rf '^workspace ID [0-9-]+ \(([^)]*)\).*' '$1' \
        -- (hyprctl activeworkspace 2>/dev/null | head -1))
    set -l out (__yt_hypr "hl.dsp.window.move({window=\"address:$parts[1]\", workspace=\"name:ythidden\"})")
    if not string match -qr '^ok' -- "$out"
        echo "yt: could not hide the terminal: $out" >&2
        return 1
    end
    if test -n "$cur"; and test "$cur" != ythidden
        __yt_hypr "hl.dsp.focus({workspace=\"$cur\"})" >/dev/null
    end
    echo "$parts[1] $parts[2]"
end

function __yt_win_restore
    set -l addr $argv[1]
    set -l ws $argv[2]
    test -n "$addr"; and test -n "$ws"; or return
    set -l out (__yt_hypr "hl.dsp.window.move({window=\"address:$addr\", workspace=\"$ws\"})")
    string match -qr '^ok' -- "$out"; or echo "yt: could not restore the terminal: $out" >&2
    __yt_hypr "hl.dsp.focus({window=\"address:$addr\"})" >/dev/null
end

# ---- playback ---------------------------------------------------------

# mpv dying and mpv quitting look the same from here unless somebody looks at
# the status. fish reports 128+signal for a process killed by one, and those
# statuses were falling through the `contains 1 2 3` check untouched - so a
# player that was killed mid-video ended with the terminal coming back and no
# word about why, and the watch state recorded as if it had been watched.
#
# One line per abnormal exit, kept in a file that is trimmed rather than
# allowed to grow, because "it crashed again" is only answerable with a
# history of when and how.
# mpv's log quotes the whole stream URL - the proxy's auth token and
# YouTube's signature both travel in the query string - and mpv creates it at
# whatever the umask allows, then keeps the mode it finds on a re-run. So the
# file has to exist, privately, before mpv opens it. `install -m` carries the
# mode into the creating syscall; touch-then-chmod would not.
function __yt_private --argument-names path
    command chmod 700 (dirname $path) 2>/dev/null
    if test -e $path
        command chmod 600 $path 2>/dev/null
    else
        command install -m 600 /dev/null $path 2>/dev/null
    end
end

# Append one line to the playback record, trimmed rather than left to grow.
function __yt_play_note --argument-names text
    set -l log "$__yt_cache/play.log"
    __yt_private $log
    echo (date '+%Y-%m-%d %H:%M:%S')" $text" >> $log
    # 200 lines is months of this; rewriting it costs one tail and one move.
    if test (count (cat $log 2>/dev/null)) -gt 200
        tail -n 200 $log > $log.tmp 2>/dev/null; and command mv -f $log.tmp $log
    end
end

# A video that opens and closes again is not always a failure mpv reports:
# it can quit with status 0 having played half a second, because something
# closed its window. Status alone cannot tell those apart, so the record
# keeps how long it actually ran.
set -g __yt_play_short 5

function __yt_play_ran --argument-names rc vid secs
    test "$rc" -eq 0 2>/dev/null; or return 1
    test "$secs" -lt $__yt_play_short 2>/dev/null; or return 1
    __yt_play_note "$vid mpv exited 0 after only $secs seconds"
    return 0
end

# mpv has five builtin bindings that run a bare `quit`: q, Ctrl+w, CLOSE_WIN
# (the compositor asking the window to close), POWER and STOP. All five log
# the identical line and all five exit 0, so "the video just closed by
# itself" was unanswerable - a stray media key and a close request from the
# window manager were the same event from out here. __yt_play rebinds them to
# distinct exit codes; this names them again. `quit N` is still quit, so
# --save-position-on-quit and the watch-later file are unaffected.
function __yt_quit_cause --argument-names rc
    switch $rc
        case 90
            echo "the q key"
        case 91
            echo Ctrl+w
        case 92
            echo "a close request from the compositor (CLOSE_WIN)"
        case 93
            echo "the POWER key"
        case 94
            echo "the STOP media key"
    end
end

function __yt_play_signal --argument-names rc vid
    test "$rc" -ge 129 2>/dev/null; or return 1
    set -l sig (math $rc - 128)
    set -l name
    switch $sig
        case 1
            set name HUP
        case 2
            set name INT
        case 6
            set name ABRT
        case 9
            set name KILL
        case 11
            set name SEGV
        case 15
            set name TERM
        case '*'
            set name $sig
    end
    __yt_play_note "$vid mpv killed by SIG$name (status $rc)"
    echo "yt: mpv was killed by SIG$name - see ~/.cache/yt/mpv.log and ~/.cache/yt/play.log" >&2
    return 0
end


function __yt_play
    set -l audio 0
    if test "$argv[1]" = --audio
        set audio 1
        set -e argv[1]
    end
    set -l ids $argv
    test (count $ids) -gt 0; or return 1

    __yt_cfg_init
    # Resume files name every video and how far into it you got. mkdir and
    # mpv both write at the umask, so this is the only place that can make
    # the viewing history private.
    mkdir -p -m 700 $__yt_wl; and command chmod 700 $__yt_wl 2>/dev/null

    # One Python start-up: local paths, titles, proxy URLs, and the play is
    # recorded while we are in there. This used to be five separate calls at
    # ~70ms each before mpv was even exec'd, and it resolved a proxy stream
    # even for videos we already had on disk.
    #
    # Four parallel lists rather than packed strings: a proxy URL contains
    # both ? and =, so any single-character packing is a quoting bug waiting
    # to happen.
    set -l p_kind
    set -l p_id
    set -l p_url
    set -l p_aud
    set -l p_title
    set -l winline
    # Ask for the window address in the same call when we are going to hide,
    # rather than spending a second Python start-up on `win-self`.
    set -l wantwin
    if test "$__yt_cfg_hideterm" = 1; and command -q hyprctl
        if test $audio -eq 0; or test "$__yt_cfg_hidetermaudio" = 1
            set wantwin --win
        end
    end
    for line in (__yt_py playinfo $wantwin $ids)
        set -l f (string split \t -- $line)
        if test "$f[1]" = win
            test (count $f) -ge 3; and test -n "$f[2]"; and set winline $f[2] $f[3]
            continue
        end
        test (count $f) -ge 4; or continue
        set -a p_id $f[1]
        if test -n "$f[2]"
            # Local copy: no network, no transcode, instant seeking.
            set -a p_kind local
            set -a p_url $f[2]
        else if test -n "$f[3]"
            set -a p_kind proxy
            set -a p_url $f[3]
        else
            set -a p_kind direct
            set -a p_url (__yt_url $f[1])
        end
        set -a p_aud "$f[4]"
        # mpv names a stream after its URL, so every video was titled
        # "http://127.0.0.1:8791/v/<id>/video?t=..." in the window title and
        # the OSD. playinfo already knows the real one.
        if test (count $f) -ge 5; and test -n "$f[5]"
            set -a p_title $f[5]
        else
            set -a p_title $f[1]
        end
    end
    test (count $p_kind) -gt 0; or return 1

    # mpv's own account of why it stopped, kept for exactly one playback.
    # A video that opens and closes again leaves nothing behind otherwise -
    # the terminal is hidden at that moment, so its stdout is gone too - and
    # this is the difference between guessing and reading the reason.
    __yt_private "$__yt_cache/mpv.log"
    set -l opts \
        --log-file="$__yt_cache/mpv.log" \
        --watch-later-dir=$__yt_wl \
        --save-position-on-quit \
        --write-filename-in-watch-later-config \
        --resume-playback=yes \
        --force-window=immediate \
        --keep-open=no \
        --hwdec=$__yt_cfg_hwdec
    # Every builtin binding that runs a bare `quit` gets its own exit code, so
    # a window that vanished can say what closed it. Additive: `keybind` on a
    # live player leaves ~/.config/mpv/input.conf alone, unlike --input-conf.
    set -l icmds 'keybind q "quit 90"' 'keybind Ctrl+w "quit 91"' \
        'keybind CLOSE_WIN "quit 92"' 'keybind POWER "quit 93"' \
        'keybind STOP "quit 94"'
    # Picture tuning, argv-only so the user's mpv.conf is untouched. ^7 binds
    # additively via --input-commands: --input-conf would REPLACE
    # ~/.config/mpv/input.conf and silently destroy the Ctrl+0-6 Anime4K
    # bindings that live there. Verified: 7/7 of those survive this.
    # The inner command must be quoted - `keybind K cycle deband` parses as
    # cmd="cycle", comment="deband" and the binding then does nothing.
    if test $audio -eq 0
        test "$__yt_cfg_deband" = 1; and set -a opts --deband=yes
        # mpv.conf's glsl-shaders chain is Anime4K Mode A: a 2x upscale and a
        # downscale back, which cannot add detail to a 1080p stream on a 1080p
        # display, and measured 17.3x the per-frame shader cost through mpv's
        # own vo-passes. Cleared only for videos started from here, and only
        # when asked for - ^0..^6 still switch it on per video.
        test "$__yt_cfg_shaders" = off; and set -a opts --glsl-shaders=''
        set -a icmds 'keybind Ctrl+7 "cycle deband ; show-text \"yt: debanding ${deband}\" 1200"'
    end
    # One --input-commands only: a second occurrence replaces the first rather
    # than appending, so everything additive has to travel in this one list.
    set -a opts "--input-commands="(string join ' ; ' $icmds)

    if test $audio -eq 1
        set -a opts --no-video --force-window=no --vid=no "--ytdl-format=$__yt_cfg_afmt"
    else
        # Same selector the downloader uses, so streaming and offline copies
        # never disagree about codec or resolution.
        set -a opts "--ytdl-format=$__yt_cfg_vfmt"
    end

    # Hide the terminal only when there is actually a video window to look at.
    set -l hidden
    if test (count $winline) -eq 2
        set hidden (string split ' ' -- (__yt_win_hide $winline))
    end

    # mpv's own diagnostics are the most useful thing a user can see when a
    # video will not play; never redirect or capture them.
    set -l rc 0
    set -l ran 0
    # How long each player was up, alongside the id it played. Absence of a
    # resume file is how playend tells "watched to the end" from "quit part
    # way", and a window that closed in the first second leaves the same
    # absence as a video that finished - so it needs the clock as well.
    set -l p_ran
    for i in (seq (count $p_kind))
        set -l ptitle "--force-media-title=$p_title[$i]"
        set -l t0 (date +%s)
        switch $p_kind[$i]
            case local
                # Nothing here needs yt-dlp: it is a file on disk. Loading
                # ytdl_hook anyway cost ~8ms of start-up.
                command mpv $opts $ptitle --no-ytdl $p_url[$i]
                set rc $status
            case proxy
                # Stream through the local range-chunking proxy: ffmpeg cannot
                # make the bounded 1MiB requests YouTube demands, so without
                # this we are forced onto a 360p muxed rendition.
                set -l margs $ptitle $p_url[$i]
                test -n "$p_aud[$i]"; and set -a margs "--audio-file=$p_aud[$i]"
                # Already-resolved proxy URLs; ytdl has nothing to do here
                # either. The direct fallback below still gets it.
                set -a margs --no-ytdl
                command mpv $opts $margs
                set rc $status
                if contains -- $rc 1 2 3
                    # Proxy could not serve it; let mpv extract it itself.
                    __yt_play_direct $opts $ptitle -- (__yt_url $p_id[$i])
                    set rc $status
                end
            case direct
                __yt_play_direct $opts $ptitle -- $p_url[$i]
                set rc $status
        end
        # Back to the status mpv would have returned, so nothing downstream
        # sees a quit as a failure; the cause goes to the record instead.
        set -l cause (__yt_quit_cause $rc)
        if test -n "$cause"
            __yt_play_note "$p_id[$i] mpv quit via $cause"
            set rc 0
        end
        set ran (math (date +%s) - $t0)
        set -a p_ran $ran
        __yt_play_ran $rc $p_id[$i] $ran
    end

    test (count $hidden) -eq 2; and __yt_win_restore $hidden[1] $hidden[2]

    # mpv reports 1/2/3 when it never managed to play anything. Reconciling
    # watch state then would record an unplayed video as fully watched.
    if contains -- $rc 1 2 3
        echo "yt: mpv could not play that (exit $rc)" >&2
        return $rc
    end
    # Killed by a signal is the same story: whatever was on screen, it did not
    # finish, so the watch state must not be reconciled from it either.
    if __yt_play_signal $rc $ids[-1]
        return $rc
    end
    # id=seconds, in the order they were played. A bare id still means "no
    # idea how long", so anything that calls playend by hand behaves as before.
    set -l ended
    for i in (seq (count $p_id))
        if test $i -le (count $p_ran)
            set -a ended "$p_id[$i]=$p_ran[$i]"
        else
            set -a ended $p_id[$i]
        end
    end
    __yt_py playend $ended
end

# mpv doing its own extraction, with cookies if configured and one retry on a
# different player client. Status is returned, not echoed: capturing it would
# swallow mpv's terminal output along with it.
function __yt_play_direct
    set -l opts
    while test (count $argv) -gt 0 -a "$argv[1]" != --
        set -a opts $argv[1]
        set -e argv[1]
    end
    set -e argv[1]
    set -l targets $argv

    # mpv drives its own yt-dlp here, so it needs the same client pin the
    # proxy uses - yt-dlp's default hands back truncated media URLs.
    set -l ca "--ytdl-raw-options=extractor-args=youtube:player_client=$__yt_cfg_client"
    if test -n "$__yt_cfg_cookies"
        command mpv $opts $ca --ytdl-raw-options=cookies=$__yt_cfg_cookies $targets
    else
        command mpv $opts $ca $targets
    end
    set -l rc $status
    if contains -- $rc 1 2 3
        echo "yt: retrying with player_client=$__yt_cfg_playfallback" >&2
        command mpv $opts \
            --ytdl-raw-options=extractor-args=youtube:player_client=$__yt_cfg_playfallback \
            $targets
        set rc $status
    end
    return $rc
end

# ---- saving -----------------------------------------------------------

function __yt_pick_category --argument-names suggested
    set -l cats (__yt_py cat names 2>/dev/null)
    set -l out (printf '%s\n' $cats | env SHELL=/bin/sh fzf --print-query --height=45% --reverse \
        --prompt='category> ' --query="$suggested" \
        --header='enter to pick · type a new name to create it')
    set -l rc $status
    # 130 is ESC / ctrl-c. fzf still prints the query in that case, so
    # without this the cancel would quietly create a category.
    if test $rc -eq 130
        return 1
    end
    set -l n (count $out)
    if test $n -ge 2
        echo $out[2]
    else if test $n -eq 1
        echo $out[1]
    else
        echo $suggested
    end
end

function __yt_add_flow
    # Bulk add: paste a block of links, pick one category, done. The per-video
    # "why are you saving this?" prompt in __yt_save_flow is the right question
    # for one video off the picker and the wrong one for twenty pasted links.
    __yt_cfg_init
    set -l raw
    set -l cat

    # `yt add -c watchlater` skips the category picker entirely.
    set -l i 1
    while test $i -le (count $argv)
        if contains -- $argv[$i] -c --category; and test (math $i + 1) -le (count $argv)
            set cat $argv[(math $i + 1)]
            set i (math $i + 2)
        else
            set i (math $i + 1)
        end
    end

    if not isatty stdin
        # `pbpaste | yt add`, `cat links.txt | yt add`
        while read -l line
            set -a raw (string split -n ' ' -- $line)
        end
    else
        echo ''
        set_color --bold
        echo '  paste the links, one per line'
        set_color normal
        set_color 555
        echo '  then press enter twice (or ctrl-d) to finish'
        set_color normal
        echo ''
        # A bracketed paste of several lines arrives as ONE multi-line value:
        # fish's reader inserts the newlines into the buffer rather than
        # submitting each line, so this has to split them back out. Terminals
        # without bracketed paste send a line per read, which the loop covers.
        while read -l -P '  > ' line
            test -n "$line"; or break
            for part in (string split \n -- $line)
                set -a raw (string split -n ' ' -- $part)
            end
        end
        echo ''
    end

    set -l ids (__yt_py urls --ids $raw 2>/dev/null)
    if test (count $ids) -eq 0
        echo 'yt: no video links found' >&2
        return 1
    end

    # Titles first: picking a category against a list of bare ids is guesswork.
    # One call fetches them and prints them - a `yt title` per video would be a
    # Python start-up each, and the lookup itself is now one round of parallel
    # page fetches rather than one yt-dlp run per video.
    echo "  looking up "(count $ids)" video(s)..."
    for line in (__yt_py meta --print $ids)
        echo "    "(string split -f2 \t -- $line)
    end
    echo ''

    if test -z "$cat"
        set -l suggested (__yt_py suggest $ids[1] 2>/dev/null)
        test -n "$suggested"; or set suggested unsorted
        if isatty stdin
            set cat (__yt_pick_category $suggested)
            or return 1
        else
            # Piped in, so there is no keyboard for fzf to read: take the
            # suggestion rather than printing an ioctl error and saving there
            # anyway. `-c` is the way to be explicit.
            set cat $suggested
            echo "  category: $cat  (piped input; pass -c to choose)"
        end
        test -n "$cat"; or return 1
    end

    __yt_py save -c $cat $ids
end

function __yt_save_flow
    set -l ids $argv
    test (count $ids) -gt 0; or return 1

    # Make sure titles exist before we start showing them in prompts.
    __yt_py meta $ids >/dev/null 2>&1

    set -l suggested (__yt_py suggest $ids[1] 2>/dev/null)
    test -n "$suggested"; or set suggested unsorted
    set -l cat (__yt_pick_category $suggested)
    or return 1
    test -n "$cat"; or return 1

    for id in $ids
        set -l title (__yt_py title $id 2>/dev/null)
        test -n "$title"; or set title $id
        echo ''
        set_color --bold; echo "  $title"; set_color normal
        read -P '  why are you saving this? (enter to skip) > ' note
        __yt_py save -c $cat -n "$note" $id
    end
end

# ---- settings ---------------------------------------------------------

function __yt_settings
    __yt_cfg_init
    while true
        set -l rows (__yt_py_list settings)
        test (count $rows) -gt 0; or return 1
        set -l out (printf '%s\n' $rows | env SHELL=/bin/sh fzf \
            --delimiter=\t --with-nth=3 --layout=reverse \
            --prompt='settings> ' \
            --header='enter to change · esc to close' \
            --preview="printf '%b\n' {4}" \
            --preview-window="right:"(__yt_preview_pct)"%" \
            --no-hscroll --cycle)
        test (count $out) -ge 1; or return 0
        set -l key (string split -f1 \t -- $out[1])
        test -n "$key"; or return 0

        set -l choices (__yt_py choices $key)
        set -l new
        if test (count $choices) -gt 0
            set new (printf '%s\n' $choices | env SHELL=/bin/sh fzf \
                --layout=reverse --height=40% --prompt="$key = " \
                --header="current: "(__yt_py config $key))
        else
            echo ''
            echo "  $key = "(__yt_py config $key)
            read -P "  new value (enter to keep) > " new
        end
        if test -n "$new"
            __yt_py config $key $new
            # settings that change how the picker itself is built
            set -e __yt_cfg_pct
            __yt_cfg_init
        end
    end
end

# ---- the picker -------------------------------------------------------

function __yt_picker
    set -l mode $argv[1]
    set -e argv[1]
    set -l libargs $argv

    set -l keys ctrl-s,ctrl-a,ctrl-r,ctrl-x,ctrl-p,ctrl-f,alt-n,alt-c,alt-h,alt-l,alt-f

    # First pass gets settings and rows from one Python start-up instead of
    # two; ~28ms of a ~93ms cold open. Later passes round the loop already
    # have the settings cached, so they just fetch rows.
    set -l booted
    if not set -q __yt_cfg_pct
        set -l out (__yt_py_list boot $mode $libargs)
        set -l i (contains -i -- \x1e---rows--- $out)
        if test -n "$i"
            __yt_cfg_parse $out[1..(math $i - 1)]
            test (math $i + 1) -le (count $out); and set booted $out[(math $i + 1)..-1]
        end
    end
    __yt_cfg_init
    # string collect keeps the embedded newlines: command substitution would
    # otherwise split them into separate list elements, and "$hdr" would then
    # rejoin them with spaces into one over-long line.
    set -l hdr (string join \n $__yt_cfg_hdr | string collect)

    set -l refresh
    while true
        set -l rows
        if test (count $booted) -gt 0
            set rows $booted
            set -e booted[1..-1]
        else
            set rows (__yt_py_list $mode $libargs $refresh)
        end
        set -e refresh[1..-1]
        if test (count $rows) -eq 0
            return 1
        end

        # Ids the preview warmer should pre-render, in display order. Written
        # before fzf starts so the very first preview can kick it off; capped
        # because warming more than a couple of screens is wasted work.
        begin
            for r in $rows[1..(math "min(40, "(count $rows)")")]
                string split -f1 \t -- $r
            end
        end >$__yt_cache/warm-ids 2>/dev/null
        # fish makes an unmatched glob a hard error, and `yt gc` now clears
        # these markers itself - so once the cache had been swept, opening the
        # picker aborted here. Expand it into a list first: an empty list is
        # nothing to delete rather than a failed command.
        set -l warmed $__yt_cache/sixel/.warmed-*
        test (count $warmed) -gt 0; and command rm -f $warmed

        # Command fzf re-runs to refresh the list after an in-place action.
        set -l py "python3 -S "(__yt_shq $__yt_lib | string trim)
        set -l reload "env YT_PANE=1 YT_COLUMNS=\$FZF_COLUMNS $py "(__yt_shq $mode $libargs | string trim)
        set -l hdrcmd "env YT_PANE=1 YT_COLUMNS=\$FZF_COLUMNS $py header"

        set -l out (printf '%s\n' $rows | env SHELL=/bin/sh fzf \
            --multi --ansi --delimiter=\t --with-nth=3 \
            --expect=$keys \
            --layout=reverse \
            --prompt="$mode> " --header="$hdr" \
            --preview=(__yt_preview_cmd) \
            --preview-window="right:"(__yt_preview_pct)"%" \
            --bind="resize:reload($reload)+transform-header($hdrcmd)" \
            --bind="alt-w:execute-silent($py togglewatched {+1})+reload($reload)" \
            --bind="ctrl-d:execute-silent($py dl {+1})+reload($reload)" \
            --bind="ctrl-alt-d:execute($py dl --ask {+1})+reload($reload)" \
            --bind="ctrl-t:execute-silent($py toggle-shorts)+reload($reload)" \
            --bind="ctrl-y:execute-silent($py urls {+1} | wl-copy)" \
            --bind="ctrl-o:execute-silent($py urls {+1} | xargs -r -n1 xdg-open)" \
            --no-hscroll --cycle)

        # fzf has gone: whatever row was last under the cursor is no longer
        # something the user is looking at, so stop the proxy speculating on it.
        command truncate -s 0 $__yt_cache/focus 2>/dev/null

        test (count $out) -ge 1; or return 0
        set -l key $out[1]
        set -l picks $out[2..-1]
        test (count $picks) -ge 1; or return 0

        set -l ids
        for p in $picks
            set -a ids (string split -f1 \t -- $p)
        end

        switch $key
            case ctrl-p
                # settings, reachable from inside the picker
                __yt_settings
                set -e __yt_cfg_pct
                __yt_cfg_init
                set hdr (string join \n $__yt_cfg_hdr | string collect)
            case alt-h
                set mode history
                set -e libargs[1..-1]
            case alt-c
                # Browse a specific saved list rather than everything.
                set -l cats (__yt_py cat names 2>/dev/null)
                set -l pick (printf '%s\n' all $cats \
                    | env SHELL=/bin/sh fzf --height=45% --reverse \
                        --prompt='list> ' --header='which saved list?')
                if test -n "$pick"
                    set mode lib
                    set -e libargs[1..-1]
                    test "$pick" != all; and set libargs $pick
                end
            case alt-l
                set mode lib
                set -e libargs[1..-1]
            case alt-f
                set mode home
                set -e libargs[1..-1]
            case ctrl-f
                # fzf's own typing filters the current list; this searches
                # YouTube itself.
                echo ''
                read -P '  search youtube > ' q
                if test -n "$q"
                    set mode search
                    set -e libargs[1..-1]
                    set libargs (string split ' ' -- $q)
                end
            case ctrl-r
                set refresh --refresh
            case ctrl-s
                __yt_save_flow $ids
            case ctrl-a
                __yt_play --audio $ids
            case ctrl-x
                if test "$mode" = lib
                    __yt_py unsave $ids
                else
                    echo 'yt: ^x only removes things from the library'
                end
            case alt-n
                for id in $ids
                    set -l title (__yt_py title $id)
                    set -l old (__yt_py note $id)
                    echo ''
                    set_color --bold; echo "  $title"; set_color normal
                    test -n "$old"; and echo "  current: $old"
                    read -P '  note > ' note
                    test -n "$note"; and __yt_py note $id "$note"
                end
            case '*'
                __yt_play $ids
        end
    end
end

# ---- entry point ------------------------------------------------------

function __yt_help
    echo 'yt - YouTube from the terminal (read-only; never touches your playlists)

  yt                        subscription / recommended home feed
  yt QUERY...               search (anonymous)
  yt home [--subs|--rec] [--refresh]
  yt lib [CATEGORY] [-q TEXT] [--sort added|title|channel|duration]
  yt continue               things you left part-way through
  yt recent | yt history    everything you have watched, newest first
  yt offline                downloaded videos
  yt ch @HANDLE             browse a channel

  yt add [-c CAT]           paste a block of links, pick one category
  yt save URL... [-c CAT] [-n NOTE]
  yt note ID [TEXT]         read or set the note for a saved video
  yt recat ID... CATEGORY   move videos to a category
  yt cat [list|add|rm|rename]
  yt subs [list|add|rm|mute|import [CSV]]

  yt dl URL...              queue an offline download
  yt queue                  download queue status
  yt queue clear            drop queued and failed entries

  yt settings               interactive settings editor
  yt auth [status|sync main|sync alt|clear]
  yt ceiling [ID]           how many bytes YouTube will serve you right now
  yt preview-test · yt cooldown · yt probe · yt stats · yt doctor · yt gc · yt export [FILE] · yt config [K [V]]

In the picker: enter play · ^s save · ^d download · ^a audio-only
               ^y copy url · ^o browser · ^x remove · ^t shorts filter
               ^r refresh · alt-w mark watched · alt-n note · tab multi-select'
end

function yt --description 'YouTube: search, feed, local library, offline'
    if not command -q python3
        echo 'yt: python3 is required' >&2
        return 1
    end
    for f in $__yt_core $__yt_lib
        if not test -f $f
            echo "yt: missing helper at $f" >&2
            return 1
        end
    end

    # Drop the per-call config cache so `yt config ...` takes effect on the
    # very next command rather than only in a fresh shell.
    set -e __yt_cfg_pct

    set -l cmd $argv[1]
    set -l rest $argv[2..-1]

    switch "$cmd"
        case '' home h
            __yt_picker home $rest
        case s search
            if test (count $rest) -eq 0
                read -P 'search> ' q
                test -n "$q"; or return 1
                set rest (string split ' ' -- $q)
            end
            __yt_picker search $rest
        case lib library l saved
            __yt_picker lib $rest
        case continue cont resume
            __yt_picker continue
        case history hist recent watched
            __yt_picker history $rest
        case offline down downloaded
            __yt_picker offline
        case ch channel
            __yt_picker ch $rest
        case save add
            # `yt save URL...` saves what it was given; `yt add` (with only
            # flags, or nothing) opens the paste prompt instead.
            set -l target 0
            set -l i 1
            while test $i -le (count $rest)
                if contains -- $rest[$i] -c --category -n --note
                    set i (math $i + 2)
                else
                    set target 1
                    break
                end
            end
            if test $target -eq 1
                __yt_py save $rest
            else
                __yt_add_flow $rest
            end
        case unsave
            __yt_py unsave $rest
        case note
            if test (count $rest) -eq 1
                set -l cur (__yt_py note $rest[1])
                test -n "$cur"; and echo "current: $cur"
                read -P 'note > ' n
                test -n "$n"; and __yt_py note $rest[1] "$n"
            else
                __yt_py note $rest
            end
        case recat mv
            __yt_py recat $rest
        case cat categories
            __yt_py cat $rest
        case subs sub
            __yt_py subs $rest
        case dl download
            __yt_py dl $rest
        case queue q
            # `yt queue` prints the queue once; `yt queue live` stays and
            # redraws it until nothing is downloading any more.
            switch "$rest[1]"
                case clear
                    __yt_py dl-clear
                case live watch -l --live
                    __yt_py dl-status --live
                case '*'
                    __yt_py dl-status
            end
        case auth
            __yt_py auth $rest
        case preview-test
            # Render a preview straight to this terminal, outside fzf. If this
            # looks right but the picker's preview does not, the problem is fzf
            # or the pane geometry rather than chafa or the terminal.
            __yt_cfg_init
            set -l pc (__yt_preview_cmd)          # regenerates preview.sh here
            set -l script (string split ' ' -- $pc)[1]
            set -l vid $rest[1]
            test -n "$vid"; or set vid (__yt_py_list lib 2>/dev/null | head -1 | string split -f1 \t)
            if test -z "$vid"
                echo 'yt: give a video id' >&2
            else
                set -l det (__yt_py_list lib 2>/dev/null | string match -r "^$vid\t.*" | string split -f4 \t)
                test -n "$det"; or set det "$vid"
                echo "yt: graphics=$(__yt_gfx)  script=$script"
                echo "yt: if no image appears here, try: yt config thumb_format symbols"
                echo ''
                env FZF_PREVIEW_COLUMNS=(math (__yt_cols) - 4) \
                    FZF_PREVIEW_LINES=(math (__yt_rows) - 6) \
                    $script $vid "$det"
            end
        case probe
            __yt_py probe
        case cooldown
            __yt_py cooldown $rest
        case ceiling
            __yt_py ceiling $rest
        case stats
            __yt_py stats
        case doctor check
            __yt_py doctor
        case gc clean
            __yt_py gc
        case export
            __yt_py export $rest
        case settings prefs
            __yt_settings
        case unhide
            # Recover any window left parked on the hidden workspace, e.g.
            # after a crash between hiding and restoring.
            if not command -q hyprctl
                echo 'yt: unhide needs hyprctl (Hyprland)' >&2
            else
                set -l ws (hyprctl activeworkspace -j 2>/dev/null | jq -r '.id')
                set -l addrs (hyprctl clients -j 2>/dev/null \
                    | jq -r '.[] | select(.workspace.name=="ythidden" or .workspace.name=="special:ythidden") | .address')
                if test (count $addrs) -eq 0
                    echo 'yt: nothing is hidden'
                else
                    for a in $addrs
                        __yt_hypr "hl.dsp.window.move({window=\"address:$a\", workspace=\"$ws\"})" >/dev/null
                    end
                    echo "yt: brought back "(count $addrs)" window(s) to workspace $ws"
                end
            end
        case config conf
            __yt_py config $rest
        case play
            set -l ids
            for a in $rest
                set -l m (string match -rg '(?:v=|/shorts/|youtu\.be/|/embed/|/live/)([A-Za-z0-9_-]{11})|^([A-Za-z0-9_-]{11})$' -- $a)
                set -l id (string join '' $m)
                test -n "$id"; and set -a ids $id
            end
            test (count $ids) -gt 0; or begin
                echo 'yt: no video ids found' >&2
                return 1
            end
            __yt_play $ids
        case help --help -h
            __yt_help
        case '*'
            # Anything unrecognised is a search query.
            __yt_picker search $argv
    end
end
