# completions for yt

function __yt_no_sub
    set -l t (commandline -opc)
    test (count $t) -eq 1
end

function __yt_sub_is
    set -l t (commandline -opc)
    test (count $t) -ge 2; and contains -- $t[2] $argv
end

function __yt_categories
    command python3 -S "$HOME/.config/fish/yt/ytmain.py" cat names 2>/dev/null
end

function __yt_sub_names
    command sqlite3 "$HOME/.local/share/yt/yt.db" \
        "SELECT COALESCE(NULLIF('@'||handle,'@'), name) FROM subs ORDER BY name;" 2>/dev/null
end

complete -c yt -f

# top-level subcommands
complete -c yt -n __yt_no_sub -a home     -d 'Subscription / recommended feed'
complete -c yt -n __yt_no_sub -a search   -d 'Search YouTube (anonymous)'
complete -c yt -n __yt_no_sub -a lib      -d 'Browse your saved library'
complete -c yt -n __yt_no_sub -a continue -d 'Resume part-watched videos'
complete -c yt -n __yt_no_sub -a recent   -d 'Recently watched, newest first'
complete -c yt -n __yt_no_sub -a history  -d 'Local playback history'
complete -c yt -n __yt_no_sub -a offline  -d 'Downloaded videos'
complete -c yt -n __yt_no_sub -a ch       -d 'Browse a channel'
complete -c yt -n __yt_no_sub -a save     -d 'Save a video to the library'
complete -c yt -n __yt_no_sub -a unsave   -d 'Remove from the library'
complete -c yt -n __yt_no_sub -a note     -d 'Read or set a note'
complete -c yt -n __yt_no_sub -a recat    -d 'Move videos to a category'
complete -c yt -n __yt_no_sub -a cat      -d 'Manage categories'
complete -c yt -n __yt_no_sub -a subs     -d 'Manage subscriptions'
complete -c yt -n __yt_no_sub -a dl       -d 'Queue an offline download'
complete -c yt -n __yt_no_sub -a queue    -d 'Download queue status'
complete -c yt -n __yt_no_sub -a auth     -d 'Account cookie jars'
complete -c yt -n __yt_no_sub -a probe    -d 'Compare anonymous vs authenticated format access'
complete -c yt -n __yt_no_sub -a stats    -d 'Library statistics'
complete -c yt -n __yt_no_sub -a doctor   -d 'Health and rate-guard check'
complete -c yt -n __yt_no_sub -a ceiling  -d 'Measure how many bytes YouTube will serve right now'
complete -c yt -n __yt_no_sub -a refresh  -d 'Backfill missing metadata'
complete -c yt -n __yt_no_sub -a gc       -d 'Prune caches and vacuum'
complete -c yt -n __yt_no_sub -a export   -d 'Export the library as JSON'
complete -c yt -n __yt_no_sub -a settings -d 'Interactive settings editor'
complete -c yt -n __yt_no_sub -a config   -d 'Get or set configuration'
complete -c yt -n __yt_no_sub -a play     -d 'Play URLs directly'
complete -c yt -n __yt_no_sub -a help     -d 'Show help'

# home
complete -c yt -n '__yt_sub_is home' -l subs    -d 'Chronological subscriptions (RSS, no account)'
complete -c yt -n '__yt_sub_is home' -l rec     -d 'YouTube recommendations (uses main account)'
complete -c yt -n '__yt_sub_is home' -l auto    -d 'Recommended, falling back to RSS'
complete -c yt -n '__yt_sub_is home' -l refresh -d 'Bypass the cache'
complete -c yt -n '__yt_sub_is ceiling' -l limit -d 'Stop after N MiB (default 48)'
complete -c yt -n '__yt_sub_is search' -l refresh -d 'Bypass the cache'

# lib
complete -c yt -n '__yt_sub_is lib library l saved' -a '(__yt_categories)' -d category
complete -c yt -n '__yt_sub_is lib library l saved' -s c -l category -a '(__yt_categories)' -d 'Filter by category'
complete -c yt -n '__yt_sub_is lib library l saved' -s q -l query -d 'Full-text search titles, channels, notes'
complete -c yt -n '__yt_sub_is lib library l saved' -l sort -a 'added old title channel duration priority' -d 'Sort order'
complete -c yt -n '__yt_sub_is lib library l saved' -l archived -d 'Show archived entries'

# save
complete -c yt -n '__yt_sub_is save add' -s c -l category -a '(__yt_categories)' -d 'Category'
complete -c yt -n '__yt_sub_is save add' -s n -l note -d 'Why you are saving it'

# recat / cat
complete -c yt -n '__yt_sub_is recat mv' -a '(__yt_categories)' -d category
complete -c yt -n '__yt_sub_is cat categories' -a 'list add rm rename names'
complete -c yt -n '__yt_sub_is cat categories' -a '(__yt_categories)' -d category

# subs
complete -c yt -n '__yt_sub_is subs sub' -a 'list add rm mute unmute import'
complete -c yt -n '__yt_sub_is subs sub' -a '(__yt_sub_names)' -d channel

# auth / queue / dl
complete -c yt -n '__yt_sub_is auth' -a 'status sync clear'
complete -c yt -n '__yt_sub_is auth' -a 'main alt' -d 'Which cookie jar'
complete -c yt -n '__yt_sub_is queue q' -a clear -d 'Drop queued and failed entries'
complete -c yt -n '__yt_sub_is dl download' -l now -d 'Start the worker immediately'

# config keys
complete -c yt -n '__yt_sub_is config conf' -a 'quality video_dir thumb_quality thumb_format thumb_cache_mb db_max_mb dither dither_grain dither_intensity preview_pct play_auth play_client_fallback hide_terminal hide_terminal_audio search_count feed_count search_ttl rss_ttl rec_ttl home_mode shorts cookies_main cookies_alt disk_budget_gb sponsorblock prefetch_workers rss_workers audio_fmt avoid_av1 hwdec stream_client'

# value hints for the settings with a fixed set of choices
complete -c yt -n '__yt_sub_is config conf; and contains -- dither (commandline -opc)' -a 'none diffusion ordered noise'
complete -c yt -n '__yt_sub_is config conf; and contains -- dither_grain (commandline -opc)' -a '1x1 2x2 4x4 8x8'
complete -c yt -n '__yt_sub_is config conf; and contains -- thumb_quality (commandline -opc)' -a 'maxresdefault hq720 sddefault hqdefault mqdefault'
complete -c yt -n '__yt_sub_is config conf; and contains -- thumb_format (commandline -opc)' -a 'auto sixels kitty iterm symbols'
complete -c yt -n '__yt_sub_is config conf; and contains -- shorts (commandline -opc)' -a 'show hide only'
complete -c yt -n '__yt_sub_is config conf; and contains -- home_mode (commandline -opc)' -a 'auto subs rec'

complete -c yt -n '__yt_sub_is config conf; and contains -- stream_client (commandline -opc)' -a 'tv_simply android_vr tv ios android'
