#!/usr/bin/env python3
"""ytlib - backing store and network layer for the `yt` fish function.

Stdlib only. One process per user action; never per item.

Design rules that matter:
  * Read-only against YouTube. We never POST, never mutate playlists,
    never touch YouTube's own Watch Later. Our "watch later" is the
    local `saved` table and nothing else.
  * Anonymous by default. Cookies are used only where the user asked
    for them (the recommended home feed) or as an explicit fallback.
  * Every authenticated request passes the rate guard first.
"""

import _thread
import os
import sys
import time

# _sqlite3 is the extension module; `sqlite3` is a package around it that adds
# DB-API trimmings - paramstyle, Date/Time/Timestamp helpers, and the
# deprecated datetime adapters - by way of importing datetime and
# collections.abc. None of that is used here, and skipping it is 1.1ms off
# every invocation. sqlite3.connect and sqlite3.Row are literally the same
# objects either way (`sqlite3.Row is _sqlite3.Row`), so this is a shorter
# path to the identical thing rather than a different implementation.
#
# It is a private module, so it is a preference and not a requirement: if a
# future Python moves or renames any of it, the package import behind this
# still works and nothing here notices.
try:
    import _sqlite3 as sqlite3
    (sqlite3.connect, sqlite3.Row, sqlite3.Error,
     sqlite3.OperationalError, sqlite3.IntegrityError)
except (ImportError, AttributeError):       # pragma: no cover - fallback
    import sqlite3


class _LazyModule:
    """A module that is not imported until something is asked of it.

    Every yt command pays this module's imports before it does any work, and
    the picker runs one per keystroke-bound action. Measured from a bare
    interpreter, against os/sys/time/sqlite3/unicodedata as the floor:

        subprocess    +9.6ms      re          +2.7ms
        urllib.parse  +8.7ms      json        +3.6ms  (json imports re)
        threading     +3.3ms      unicodedata +0.3ms

    and hardly any command needs any of them. A warm launch spawns nothing -
    the proxy and the minter are already up, so warm_proxy() only stats a pid
    file - and a list drawn from the database touches no network, no regex
    and no threads.

    On the first attribute access this replaces its own name in the module
    globals with the real module, so nothing after that pays for the
    indirection: `re.sub` becomes an ordinary global load again.
    """

    __slots__ = ("_name", "_root", "_bind")

    def __init__(self, name, bind=None):
        self._name = name                       # what to import
        self._root = name.split(".")[0]         # what the name here refers to
        self._bind = bind or self._root         # the global to overwrite

    def __getattr__(self, attr):
        # Importing "urllib.parse" has to bind "urllib", so that
        # urllib.parse.quote resolves the way the call sites are written.
        __import__(self._name)
        mod = sys.modules[self._root]
        globals()[self._bind] = mod
        return getattr(mod, attr)


subprocess = _LazyModule("subprocess")
urllib = _LazyModule("urllib.parse")
re = _LazyModule("re")
json = _LazyModule("json")
threading = _LazyModule("threading")
unicodedata = _LazyModule("unicodedata")


class _Rx:
    """A regular expression that is not compiled until it is first used.

    Seven patterns were compiled at import time for the benefit of whichever
    command happened to run - and searching, downloading, feed parsing and
    channel resolution each need a different one of them, so every command
    paid for all seven and for `re` itself. Like _LazyModule, the first use
    swaps the compiled pattern into the global, so a hot loop that reaches
    one of these is back to a plain global load immediately.
    """

    __slots__ = ("_name", "_pat", "_i")

    def __init__(self, name, pattern, ignorecase=False):
        self._name, self._pat, self._i = name, pattern, ignorecase

    def __getattr__(self, attr):
        import re as _re
        c = _re.compile(self._pat, _re.IGNORECASE if self._i else 0)
        globals()[self._name] = c
        return getattr(c, attr)

def exe(prog):
    """shutil.which for the six places we need it.

    Named `exe` rather than `which` on purpose: `which` is already a local
    loop variable in cmd_doctor and a keyword argument on run_ytdlp, and a
    global of the same name is silently shadowed by both.

    Importing shutil costs 3.6ms of every invocation - it drags in zlib, bz2,
    lzma and compression.zstd for archive support this program never uses -
    and `which` was the only thing on the hot path that wanted it.
    """
    if os.path.dirname(prog):
        return prog if os.access(prog, os.X_OK) else None
    for d in (os.environ.get("PATH") or os.defpath).split(os.pathsep):
        fp = os.path.join(d, prog)
        if os.path.isfile(fp) and os.access(fp, os.X_OK):
            return fp
    return None


HOME = os.path.expanduser("~")
CFG_DIR = os.environ.get("YT_CONFIG_DIR") or os.path.join(HOME, ".config", "yt")
DATA_DIR = os.environ.get("YT_DATA_DIR") or os.path.join(HOME, ".local", "share", "yt")
CACHE_DIR = os.environ.get("YT_CACHE_DIR") or os.path.join(HOME, ".cache", "yt")
THUMB_DIR = os.path.join(CACHE_DIR, "thumbs")
# Rendered sixels, one file per (video, pane width, graphics format).
# Written by the preview script, so nothing in Python creates it - which
# is exactly why it went unpruned until now.
SIXEL_DIR = os.path.join(CACHE_DIR, "sixel")
PROXY_LOG = os.path.join(CACHE_DIR, "proxy.log")
DB_PATH = os.path.join(DATA_DIR, "yt.db")
CFG_PATH = os.path.join(CFG_DIR, "config")


# These three directories hold, between them, the cookie jar, the proxy's
# auth token, the watch history and two logs that quote signed googlevideo
# URLs. os.makedirs() applies the process umask, which is 022 on a stock
# install, so every one of them was being created world-readable and only
# the individual files were private. Create them 0700 and correct an
# existing one on the way past.
def secure_dir(path):
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError:
        return path
    try:
        import stat as _stat
        if _stat.S_IMODE(os.stat(path).st_mode) & 0o077:
            os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def open_private(path, mode="w"):
    """open() for a file nobody else may read.

    open(path, 'w') creates at 0666 & ~umask and any chmod afterwards is a
    window, however short, in which a secret is on disk world-readable.
    O_CREAT carries the mode into the creating syscall instead; the chmod is
    only there because O_CREAT does not re-mode a file that already exists.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if "a" in mode:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass
    return os.fdopen(fd, "a" if "a" in mode else "w", encoding="utf-8")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

DEFAULTS = {
    "quality": "1080",
    "video_dir": os.path.join(HOME, "Videos", "yt"),
    # 1280x720 16:9. mqdefault (320x180) is a third of the preview pane's
    # pixel width, which is why it looked soft when scaled up.
    "thumb_quality": "maxresdefault",
    "thumb_cache_mb": "400",
    # Sixel renders are ~110 KB each and there is one per pane width per
    # video. Cheap to rebuild (~24ms of chafa), so this can be small.
    "sixel_cache_mb": "60",
    "db_max_mb": "500",             # hard cap on yt.db; prunes oldest first
    # chafa defaults to noise dithering for sixel. Every dithering mode lays a
    # visible pattern over flat areas at these sizes - diffusion a regular dot
    # grid, ordered a crosshatch - and 256 colours is enough for a thumbnail
    # that banding is not the worse trade. Verified by decoding the sixel back
    # to PNG and comparing at 3x on both photos and flat line art.
    "dither": "diffusion",
    "dither_grain": "1x1",
    # Full-strength diffusion lays a visible dot grid over flat areas; none at
    # all leaves blocky banding on smooth gradients. 0.5 breaks up the banding
    # while staying invisible on flat art. Verified by decoding the sixel back
    # to PNG and comparing both content types at 4x.
    "dither_intensity": "0.5",
    "thumb_format": "auto",         # auto | sixels | kitty | iterm | symbols
    "search_count": "40",
    # YouTube answers a search with twenty results and a continuation token,
    # so search_count above twenty was quietly capped there. Each further page
    # is a second request of the same size and about 600 ms - a doubling of
    # what a search costs - so this stays at one page unless it is asked for.
    "search_pages": "1",
    "feed_count": "60",
    "search_ttl": "600",
    # Use YouTube's own JSON search endpoint rather than scraping the results
    # page. Same results from the same renderers, 4.7x less off the wire.
    "search_api": "1",
    # How long a past-its-TTL search may still be shown instantly while a
    # fresh copy is fetched behind it. 0 turns that off and every stale
    # search waits for YouTube again.
    "search_stale_hours": "24",            # 10 min
    "rss_ttl": "900",               # matches YouTube's own cache-control
    "rec_ttl": "1800",              # 30 min; authenticated, so be gentle
    "home_mode": "auto",            # auto | subs | rec
    "shorts": "show",               # show | hide | only
    "cookies_main": "",
    "cookies_alt": "",
    "disk_budget_gb": "50",
    "sponsorblock": "1",
    "prefetch_workers": "16",
    "rss_workers": "12",
    # "bestaudio" alone fails whenever no audio-only stream is offered
    # (which happens when YouTube serves a restricted format list);
    # falling back to a muxed format keeps audio-only playback working.
    "audio_fmt": "bestaudio/best",
    # Most GPUs decode H.264/VP9 in hardware but not AV1. Preferring the
    # former keeps playback off the CPU. Set to 0 if your GPU does AV1.
    # Which InnerTube client media URLs are requested from. This matters far
    # more than it looks: YouTube has moved its web clients to SABR (a POST/UMP
    # protocol yt-dlp cannot speak), and the clients it can still use get
    # *truncated* delivery - yt-dlp's anonymous default, android_vr, serves a
    # few MiB of a file and then 403s everything past it, which looks exactly
    # like an IP ban but is not one. Measured on one untouched video:
    #   android_vr   ok @0M  ok @8M  403 @30M  403 @60M
    #   tv_simply    ok @0M  ok @8M   ok @30M   ok @60M
    #   web_embedded 403 everywhere
    # tv_simply reads a whole file straight through; the others do not.
    "stream_client": "tv_simply",
    # Picture tuning for playback started from this tool only - it goes in
    # mpv's argv, so ~/.config/mpv/mpv.conf is never touched.
    #
    # Debanding is the one setting that actually earns its place here. The
    # display is 1080p and YouTube is 1080p, so every upscaler (Anime4K,
    # FSRCNNX, ravu) and every scale/dscale tweak is inert at 1:1 - but
    # debanding runs at source resolution, before scaling. Measured on a real
    # YouTube 1080p file rendered 1:1: pixels inside a banded flat run fell
    # from 47.3% of the frame to 2.3%, with no pixel moved more than 2 levels
    # and the change concentrated in flat areas rather than textured ones.
    # Cost is well under 1% of a 170Hz frame on this GPU.
    #
    # Sub-options are left at mpv's defaults on purpose: iterations=4 barely
    # improved on 1 in testing, and deband-grain=0 - the obvious "clean it up"
    # tweak - throws away about two thirds of the benefit, because most of the
    # effect is grain dithering the contour away rather than rebuilding it.
    "mpv_deband": "1",
    # What to do about the glsl-shaders chain in ~/.config/mpv/mpv.conf while
    # yt is playing. Default is to leave it exactly as configured.
    #
    # "off" is worth knowing about: that chain is Anime4K Mode A, which
    # upscales 2x and then downscales back. On a 1080p stream shown on a
    # 1080p display it cannot add detail that is not there, and measured
    # through mpv's own vo-passes on this GPU it turns a 4-pass render into a
    # 26-pass one - 17.3x the shader time per frame. Ctrl+0..6 still enable it
    # per video from ~/.config/mpv/input.conf when a video actually wants it.
    "mpv_shaders": "inherit",       # inherit | off

    "avoid_av1": "1",
    "hwdec": "auto-safe",
    # YouTube's default stream URLs carry rqh=1 ("range header required").
    # ffmpeg's first probe request sends no Range header, so those URLs give
    # ffmpeg a 403 even though yt-dlp downloads them fine. The android client
    # hands out URLs that accept a plain GET, so mpv can stream them.
    # Anonymous extraction yields URLs ffmpeg cannot fetch (see play_auth),
    # and the clients that do work anonymously only expose 360p. Cookies on
    # the default client give full quality AND fetchable URLs. Playback never
    # sends a watch ping, so this does not touch your YouTube history.
    "play_auth": "main",            # main | alt | none
    "play_client_fallback": "android_vr",
    # Hide the terminal window while a video plays (Hyprland).
    "hide_terminal": "1",
    "hide_terminal_audio": "0",
    # Stream through the local range-chunking proxy. This is what makes
    # anonymous 1080p streaming work at all; without it ffmpeg gets 403s and
    # playback falls back to a 360p muxed rendition.
    "use_proxy": "1",
    # Keep bgutil's proof-of-origin minter warm in a background process.
    # Resolving a stream needs a PO token, and minting one runs BotGuard - a
    # browser-shaped JavaScript workload. yt-dlp's plugin will either ask a
    # long-lived server or spawn a fresh Deno + jsdom per video; measured on
    # this machine that is ~3ms against ~3953ms, which was 59% of the wait
    # before the first frame. See pot/potserver.ts.
    "pot_server": "1",
    "pot_idle_minutes": "30",       # the warm minter holds ~175 MB
    # That server's *first* token of a session is the expensive one - a
    # BotGuard challenge and an IntegrityToken - and yt-dlp asks for it in the
    # middle of resolving, so the wait lands on the first video. It depends
    # only on the visitor id, which the proxy holds as soon as the picker is
    # open, so the proxy hands it over then and the mint happens against an
    # idle machine. Costs one small visitor fetch in a session that ends up
    # playing nothing.
    "pot_prewarm": "1",
    # Keep one Deno process holding YouTube's player JS already parsed.
    # Answering the `n` challenge is the largest single piece of a stream
    # resolve - 664-830ms measured here, nearly all of it re-parsing the same
    # ~2 MB player for every video - and a resident copy answers in ~1ms.
    # Costs ~100 MB while it is up. See ytjsc.py.
    "jsc_resident": "1",
    # Skip the 1.4 MB watch page when resolving a stream, by handing yt-dlp a
    # visitor id fetched separately (2.8 KB, ~99ms) instead of letting it read
    # one out of the page. Measured against the same warm extractor:
    # 1151ms -> 316ms per video. The cost is that resolves inside one window
    # share a visitor id rather than each getting a fresh one - which is what
    # a browser does anyway, but it does link a session's playbacks together.
    "skip_webpage": "1",
    "visitor_ttl_hours": "6",
    # ...and it is only up for this long after the last video it resolved.
    # V8 will not hand a compiled player back, so letting the process go is
    # the only way to stop paying for it; waking it costs ~250ms, which lands
    # on a speculative resolve rather than on you.
    "jsc_idle_minutes": "5",
    # Resolve the stream for the row the cursor has settled on, so that
    # pressing enter starts playing instead of starting a 1-2s extraction.
    # dwell is how long the cursor must be still before it counts as interest,
    # and max bounds how many speculative extractions may happen in ten
    # minutes - every one of them is a real request to YouTube.
    "prefetch_focus": "1",
    "prefetch_dwell": "2",
    "prefetch_max": "6",
    # Listed in the settings screen but never had a default, so it showed
    # blank there until it had been set once. Same 40 the two call sites
    # already fall back to, so nothing else changes.
    "preview_pct": "40",
}

# ---- authenticated-request budget -------------------------------------
# Deliberately conservative. The point is that a normal day of use never
# looks like a scraper. Exceeding any of these flips home to RSS.
REC_MIN_INTERVAL = 900     # >=15 min between recommended-feed fetches
REC_PER_HOUR = 4
REC_PER_DAY = 30
AUTH_PER_HOUR = 20         # all authenticated yt-dlp calls combined
# Anonymous requests were previously untracked, on the theory that only the
# account needed protecting. That was wrong: YouTube rate-limits the IP too,
# and a burst of anonymous extraction gets downloads 403'd for everyone on it.
ANON_PER_HOUR = 60
ANON_BURST = 12            # per rolling 5 minutes

# What counts as "part-way through". A flat seconds threshold is wrong at
# both ends: 30s is nothing in a 3h talk but is half of a short.
RESUME_FLOOR = 10          # below this it was never really started
RESUME_MIN_SECS = 20
RESUME_MIN_FRAC = 0.08
WATCHED_FRAC = 0.92

# Strings in yt-dlp stderr that mean "back off now".
RISK_PATTERNS = _Rx(
    "RISK_PATTERNS",
    r"sign in to confirm|not a bot|HTTP Error 429|Too Many Requests|"
    r"/sorry/|unable to download API page|Precondition check failed|"
    r"HTTP Error 403|account has been terminated|blocked it in your country|"
    r"consent\.youtube|captcha",
    ignorecase=True,
)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS videos (
  id          TEXT PRIMARY KEY,
  title       TEXT NOT NULL DEFAULT '',
  channel     TEXT NOT NULL DEFAULT '',
  channel_id  TEXT NOT NULL DEFAULT '',
  handle      TEXT NOT NULL DEFAULT '',
  duration    INTEGER NOT NULL DEFAULT 0,
  views       INTEGER NOT NULL DEFAULT 0,
  published   INTEGER NOT NULL DEFAULT 0,
  is_short    INTEGER NOT NULL DEFAULT 0,
  description TEXT NOT NULL DEFAULT '',
  updated     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS saved (
  video_id  TEXT PRIMARY KEY,
  category  TEXT NOT NULL DEFAULT 'unsorted',
  note      TEXT NOT NULL DEFAULT '',
  added     INTEGER NOT NULL DEFAULT 0,
  priority  INTEGER NOT NULL DEFAULT 0,
  archived  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS saved_cat ON saved(category, archived, added DESC);

CREATE TABLE IF NOT EXISTS categories (
  name    TEXT PRIMARY KEY,
  created INTEGER NOT NULL DEFAULT 0,
  sort    INTEGER NOT NULL DEFAULT 100
);

CREATE TABLE IF NOT EXISTS subs (
  channel_id TEXT PRIMARY KEY,
  name       TEXT NOT NULL DEFAULT '',
  handle     TEXT NOT NULL DEFAULT '',
  added      INTEGER NOT NULL DEFAULT 0,
  muted      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS watch (
  video_id    TEXT PRIMARY KEY,
  position    REAL NOT NULL DEFAULT 0,
  duration    REAL NOT NULL DEFAULT 0,
  watched     INTEGER NOT NULL DEFAULT 0,
  last_played INTEGER NOT NULL DEFAULT 0,
  play_count  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS watch_recent ON watch(last_played DESC);

CREATE TABLE IF NOT EXISTS downloads (
  video_id TEXT PRIMARY KEY,
  path     TEXT NOT NULL DEFAULT '',
  size     INTEGER NOT NULL DEFAULT 0,
  status   TEXT NOT NULL DEFAULT 'queued',
  quality  TEXT NOT NULL DEFAULT '',
  added    INTEGER NOT NULL DEFAULT 0,
  done     INTEGER NOT NULL DEFAULT 0,
  err      TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_try INTEGER NOT NULL DEFAULT 0,
  dest     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS dl_status ON downloads(status, added);

CREATE TABLE IF NOT EXISTS cache (
  key     TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  ts      INTEGER NOT NULL,
  ttl     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS reqlog (
  ts   INTEGER NOT NULL,
  kind TEXT NOT NULL,
  ok   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS reqlog_ts ON reqlog(ts DESC);
"""

# One connection per thread rather than one shared connection: ytproxy calls
# in from its request-handler threads, and a single sqlite connection is not
# safe for concurrent use even with check_same_thread=False. WAL plus a busy
# timeout handles the cross-connection concurrency.
#
# The thread that imported this module - which is the whole of any CLI run,
# and the proxy's main thread - keeps its connection in a plain global. The
# threading.local is built only when a *second* thread first asks for a
# connection, because `import threading` costs 1.2ms on top of sqlite3 and a
# one-shot `yt lib` has no second thread to need it. _thread is a builtin the
# interpreter has already linked, and threading.Lock() is literally
# _thread.allocate_lock(), so nothing is given up here.
_HOME_THREAD = _thread.get_ident()
_home_conn = None
_tls = None
_schema_lock = _thread.allocate_lock()
_schema_ready = False


def _init_schema(conn):
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(videos)")}
    if "description" not in have:
        conn.execute("ALTER TABLE videos ADD COLUMN "
                     "description TEXT NOT NULL DEFAULT ''")
    have = {r[1] for r in conn.execute("PRAGMA table_info(downloads)")}
    for col, decl in (("attempts", "INTEGER NOT NULL DEFAULT 0"),
                      ("next_try", "INTEGER NOT NULL DEFAULT 0"),
                      ("dest", "TEXT NOT NULL DEFAULT ''")):
        if col not in have:
            conn.execute(f"ALTER TABLE downloads ADD COLUMN {col} {decl}")
    # FTS is optional; a sqlite build without FTS5 must not be fatal.
    try:
        conn.executescript("""
          CREATE VIRTUAL TABLE IF NOT EXISTS saved_fts USING fts5(
            video_id UNINDEXED, title, channel, note, category,
            tokenize='unicode61 remove_diacritics 2');
        """)
    except sqlite3.OperationalError:
        pass
    conn.commit()


def db():
    global _schema_ready, _home_conn, _tls
    home = _thread.get_ident() == _HOME_THREAD
    conn = _home_conn if home else (getattr(_tls, "conn", None) if _tls else None)
    if conn is None:
        secure_dir(DATA_DIR)
        conn = sqlite3.connect(DB_PATH, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        with _schema_lock:
            if not _schema_ready:
                _init_schema(conn)
                _schema_ready = True
            else:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            if not home and _tls is None:
                # Under the lock: two handler threads arriving together would
                # otherwise each build one, and the loser's connection would
                # be dropped on the floor and rebuilt on every later call.
                _tls = threading.local()
        if home:
            _home_conn = conn
        else:
            _tls.conn = conn
    return conn


def has_fts():
    try:
        db().execute("SELECT 1 FROM saved_fts LIMIT 1")
        return True
    except sqlite3.OperationalError:
        return False


# ---- config -----------------------------------------------------------

_cfg = None


def cfg(key=None):
    global _cfg
    c = _cfg
    if c is None:
        c = dict(DEFAULTS)
        try:
            with open(CFG_PATH, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    c[k.strip()] = v.strip().strip('"').strip("'")
        except OSError:
            pass
        for k in c:
            env = os.environ.get("YT_" + k.upper())
            if env:
                c[k] = env
        # Published only once it is complete: cfg_reload() runs in the proxy's
        # watcher thread while other threads are reading.
        _cfg = c
    return c if key is None else c.get(key, "")


def cfg_reload():
    """Drop the cached config so the next cfg() reads the file again.

    Ordinary invocations live for 15ms and never need this. The proxy does:
    it runs for half an hour at a time, and a setting changed in the picker
    would otherwise not reach it until it next restarted. Built into a local
    and assigned in one go, so a concurrent reader never sees a half-filled
    table.
    """
    global _cfg
    _cfg = None
    cfg()
    return _cfg


def cfg_int(key, fallback=0):
    try:
        return int(str(cfg(key)).strip())
    except (TypeError, ValueError):
        return fallback


def cfg_set(key, value):
    # The file is one key=value per line and is parsed back by splitting on
    # the first '='. A newline anywhere in either half therefore writes a
    # second setting that nobody asked for - `yt config x $'a\\nplay_auth=main'`
    # was enough to set a different key. Neither can legally contain one.
    key = str(key).replace("\n", " ").replace("\r", " ").strip()
    value = str(value).replace("\n", " ").replace("\r", " ")
    if not key or "=" in key or key.startswith("#"):
        raise ValueError(f"not a usable config key: {key!r}")
    c = dict(cfg())
    c[key] = value
    secure_dir(CFG_DIR)
    tmp = CFG_PATH + ".tmp"
    try:
        with open_private(tmp) as fh:
            fh.write("# yt config - edit freely, one key=value per line\n")
            for k in sorted(c):
                fh.write(f"{k}={c[k]}\n")
        os.replace(tmp, CFG_PATH)
    except OSError:
        # A half-written temp file left at 0600 next to the real config is
        # confusing at best; the config itself is untouched either way.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    global _cfg
    _cfg = None


# ---- kv state ---------------------------------------------------------

def kv_get(k, default=None):
    r = db().execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def kv_set(k, v):
    db().execute("INSERT INTO kv(k,v) VALUES(?,?) "
                 "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    db().commit()


def kv_int(k, default=0):
    try:
        return int(kv_get(k, default))
    except (TypeError, ValueError):
        return default


# ---- rate guard -------------------------------------------------------

def log_req(kind, ok=True):
    now = int(time.time())
    db().execute("INSERT INTO reqlog(ts,kind,ok) VALUES(?,?,?)",
                 (now, kind, 1 if ok else 0))
    # Keep the log bounded; nothing older than a week is interesting.
    db().execute("DELETE FROM reqlog WHERE ts < ?", (now - 604800,))
    db().commit()


# Playback resolution is user-initiated and exempt from the browsing budgets,
# so counting it against them would let an evening of watching lock the user
# out of search.
UNBUDGETED = ("anon:stream", "anon:dl")


def req_count(kind_like, window, budgeted_only=False):
    now = int(time.time())
    q = "SELECT COUNT(*) c FROM reqlog WHERE ts > ? AND kind LIKE ?"
    args = [now - window, kind_like]
    if budgeted_only:
        q += " AND kind NOT IN (%s)" % ",".join("?" * len(UNBUDGETED))
        args += list(UNBUDGETED)
    r = db().execute(q, args).fetchone()
    return r["c"] if r else 0


def risk_state():
    """Return (level, until_epoch). level 0 means healthy."""
    return kv_int("risk_level", 0), kv_int("risk_until", 0)


def at_risk():
    _, until = risk_state()
    return time.time() < until


def bump_risk(reason=""):
    lvl, _ = risk_state()
    lvl = min(lvl + 1, 6)
    # 15m, 30m, 1h, 2h, 4h, 8h, capped at 12h.
    backoff = min(900 * (2 ** (lvl - 1)), 43200)
    kv_set("risk_level", lvl)
    kv_set("risk_until", int(time.time() + backoff))
    kv_set("risk_reason", reason[:300])
    notify("yt: backing off YouTube",
           f"Auth requests paused {fmt_dur(backoff)} (level {lvl}). "
           f"Home feed switched to RSS.")
    return lvl, backoff


def ease_risk():
    lvl, until = risk_state()
    if lvl and time.time() >= until:
        kv_set("risk_level", max(0, lvl - 1))


def can_anon():
    """Throttle our own anonymous traffic. Returns (ok, reason)."""
    if at_risk():
        _, until = risk_state()
        return False, f"backing off for {fmt_dur(until - time.time())}"
    if req_count("anon:%", 300, budgeted_only=True) >= ANON_BURST:
        return False, "too many requests in the last 5 minutes"
    if req_count("anon:%", 3600, budgeted_only=True) >= ANON_PER_HOUR:
        return False, "hourly request budget reached"
    return True, ""


def can_auth(kind="auth"):
    """Gate for any authenticated request. Returns (ok, reason)."""
    if at_risk():
        _, until = risk_state()
        return False, f"backing off for {fmt_dur(until - time.time())}"
    if req_count("auth:%", 3600) >= AUTH_PER_HOUR:
        return False, "hourly authenticated-request budget reached"
    if kind == "auth:rec":
        last = kv_int("rec_last", 0)
        if time.time() - last < REC_MIN_INTERVAL:
            return False, f"recommended feed refreshed {fmt_dur(time.time()-last)} ago"
        if req_count("auth:rec", 3600) >= REC_PER_HOUR:
            return False, "hourly recommended-feed budget reached"
        if req_count("auth:rec", 86400) >= REC_PER_DAY:
            return False, "daily recommended-feed budget reached"
    return True, ""


# ---- small helpers ----------------------------------------------------

# notify-send children, waiting to be reaped. start_new_session detaches the
# terminal, not the parentage - they are still this process's children, and
# ytproxy is long-lived enough to accumulate one zombie per notification. It
# only notifies when YouTube starts refusing, so the leak arrived exactly when
# things were already going wrong.
#
# Reaping on the way in rather than spending a thread on each: the previous
# ones are long gone by the time there is another notification, so what is
# left is at most the one just started, instead of every one ever sent.
_notified = []


def notify(title, body=""):
    for p in _notified[:]:
        if p.poll() is not None:
            _notified.remove(p)          # reaped by poll() itself
    if exe("notify-send"):
        try:
            _notified.append(subprocess.Popen(
                ["notify-send", "-a", "yt", "-i", "video-x-generic", title, body],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True))
        except OSError:
            pass


def eprint(*a):
    print(*a, file=sys.stderr)


def fmt_dur(seconds):
    try:
        s = int(max(0, seconds))
    except (TypeError, ValueError):
        return "0s"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600}h"


def hms(seconds):
    try:
        s = int(seconds or 0)
    except (TypeError, ValueError):
        return "  --  "
    if s <= 0:
        return "  --  "
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def compact_num(n):
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "-"
    if n <= 0:
        return "-"
    for limit, suf in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= limit:
            v = n / limit
            return f"{v:.1f}{suf}" if v < 10 else f"{v:.0f}{suf}"
    return str(n)


def ago(epoch):
    if not epoch:
        return "-"
    # SQLite stores whatever it was given, so a publish date that arrived as
    # text once is text forever after. Every row on every screen goes through
    # here, and the old TypeError did not cost that row its date column - it
    # came out of render() and left the whole list empty.
    if not isinstance(epoch, (int, float)):
        try:
            epoch = float(epoch)
        except (TypeError, ValueError):
            return "-"
    # Finite, not merely a number: inf survives every comparison below and
    # then "inf // 31536000" is NaN, which int() refuses.
    if not -1e15 < epoch < 1e15:
        return "-"
    d = time.time() - epoch
    if d < 0:
        return "now"
    if d < 3600:
        return f"{int(d // 60)}m"
    if d < 86400:
        return f"{int(d // 3600)}h"
    if d < 2592000:
        return f"{int(d // 86400)}d"
    if d < 31536000:
        return f"{int(d // 2592000)}mo"
    return f"{int(d // 31536000)}y"


ZWJ = "\u200d"
VS16 = "\ufe0f"


# Per-character (category, is-wide), memoised. A rendered screen of rows asks
# unicodedata the same questions thousands of times about a repertoire of a
# couple of hundred distinct characters - the same channel name on twenty
# rows, the same emoji in every title from a series. A dict lookup is an order
# of magnitude cheaper than the two unicodedata calls it replaces, and the
# table is bounded by how many distinct characters actually appear.
_CHINFO = {}


def _chinfo(ch):
    v = _CHINFO.get(ch)
    if v is None:
        cat = unicodedata.category(ch)
        cp = ord(ch)
        wide = (unicodedata.east_asian_width(ch) in ("W", "F")
                or 0x1F000 <= cp <= 0x1FAFF)
        v = _CHINFO[ch] = (cat, wide)
    return v


def wsegments(s):
    """Yield (text, display_width) grapheme-ish chunks.

    stdlib has no wcwidth, and terminals disagree about ambiguous-width
    characters, so this handles the cases that actually show up in video
    titles: CJK, emoji with a variation selector (which render double-wide
    even though the base codepoint is 'ambiguous'), and ZWJ sequences such
    as family emoji that occupy one cell per sequence rather than per base.
    """
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        cat, wide = _chinfo(ch)
        if cat in ("Mn", "Me", "Cf") and ch not in (ZWJ, VS16):
            yield ch, 0
            i += 1
            continue
        if ch == VS16:
            yield ch, 0
            i += 1
            continue
        if ch == ZWJ:
            # Swallow the joiner and whatever it joins: one glyph total.
            j = i + 1
            if j < n:
                j += 1
                while j < n and (s[j] == VS16 or _chinfo(s[j])[0] == "Mn"):
                    j += 1
            yield s[i:j], 0
            i = j
            continue
        j = i + 1
        # A trailing VS16 forces emoji presentation, i.e. two cells.
        if j < n and s[j] == VS16:
            wide = True
            j += 1
        while j < n and _chinfo(s[j])[0] == "Mn":
            j += 1
        yield s[i:j], 2 if wide else 1
        i = j


# Measured widths of non-ASCII strings. Rendering a screen wraps sixty
# descriptions a word at a time, and the same words - and the same channel
# names, and the same emoji-laden series titles - come round again and again.
_DWIDTH = {}
_DWIDTH_MAX = 8192


def dwidth(s):
    # Fast path: the overwhelming majority of titles are plain ASCII, where
    # width is just length. Only pay for Unicode segmentation when needed.
    if s.isascii():
        return len(s)
    w = _DWIDTH.get(s)
    if w is None:
        w = sum(cw for _t, cw in wsegments(s))
        if len(_DWIDTH) < _DWIDTH_MAX:
            _DWIDTH[s] = w
    return w


def pad(s, width):
    if s.isascii():
        return s[:width - 1] + "…" if len(s) > width else s + " " * (width - len(s))
    s = clip(s, width)
    return s + " " * max(0, width - dwidth(s))


def clip(s, width):
    if s.isascii():
        return s if len(s) <= width else s[:width - 1] + "…"
    if dwidth(s) <= width:
        return s
    out, w = [], 0
    for text, cw in wsegments(s):
        if w + cw > width - 1:
            out.append("…")
            break
        out.append(text)
        w += cw
    return "".join(out)


def split_width(s, width):
    """Cut `s` into pieces of at most `width` display columns, losing nothing.

    `clip` is the wrong tool for this: it appends an ellipsis, so the caller
    cannot tell how much of the string it actually consumed. wrap_text used to
    assume len(clip(...)) characters had been shown, which is one too many -
    the ellipsis - so every wrap of an over-long token silently swallowed a
    character. A description full of long URLs lost one character per line.

    It also walks the string once. The old loop re-measured the whole
    remainder on every pass, which is quadratic, and CJK defeats dwidth's
    ASCII fast path: a 1400-character Japanese description cost 27ms to wrap
    against 0.05ms for the same length of English, on every preview render.
    """
    if width < 1:
        return [s] if s else []
    if s.isascii():
        return [s[i:i + width] for i in range(0, len(s), width)] or [""]
    out, cur, w = [], [], 0
    for text, cw in wsegments(s):
        if w + cw > width and cur:
            out.append("".join(cur))
            cur, w = [], 0
        cur.append(text)
        w += cw
    if cur:
        out.append("".join(cur))
    return out or [""]


# str.translate rather than a regex: identical on every codepoint (checked
# exhaustively over U+0000-U+01FF plus the separators and BOM), 27% faster,
# and it means the busiest function on the render path does not need `re`
# imported at all - which is 2.7ms off every command that draws a list.
_CTRL_MAP = dict.fromkeys(range(0x20), " ")
_CTRL_MAP[0x7f] = " "
# The preview pane's variant: the same control stripping, plus the backslash
# doubling that keeps user text from inventing escape sequences once printf
# %b interprets the field. Doing both in one translate table is one pass over
# the string instead of a translate and then a replace, on every title,
# channel name and description of every row drawn.
_ESC_MAP = dict(_CTRL_MAP)
_ESC_MAP[0x5c] = "\\\\"


def clean(s):
    """Strip control chars so a title can never break TSV framing."""
    return str(s or "").translate(_CTRL_MAP).strip()


def clean_multiline(s, limit=1400):
    """Like clean(), but keeps paragraph breaks as \n for the preview pane."""
    s = str(s or "").replace("\r\n", "\n").replace("\r", "\n")
    s = "\n".join(ln.translate(_CTRL_MAP).rstrip() for ln in s.split("\n"))
    # Collapsing runs of blank lines by replacement rather than by r"\n{3,}":
    # each pass halves the longest run, so it converges in log2(run) steps,
    # and it keeps `re` off the path that renders every description.
    while "\n\n\n" in s:
        s = s.replace("\n\n\n", "\n\n")
    return s.strip()[:limit]


# URL forms, plus the "[id]" suffix yt-dlp puts in downloaded filenames -
# mpv keys its resume files by local path, so without the bracket form we
# would lose the position of every offline video.
VID_RE = _Rx(
    "VID_RE",
    r"(?:v=|/shorts/|youtu\.be/|/embed/|/live/|/v/)([A-Za-z0-9_-]{11})"
    r"|\[([A-Za-z0-9_-]{11})\](?=\.[A-Za-z0-9]{2,5}$|\.[A-Za-z0-9]{2,5}\b)")


def parse_video_id(s):
    s = (s or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    m = VID_RE.search(s)
    return (m.group(1) or m.group(2)) if m else None


# maxresdefault is absent for a fair number of videos, so fall back through
# progressively smaller 16:9 sources rather than showing an empty pane.
THUMB_CHAIN = {
    "maxresdefault": ["maxresdefault", "hq720", "mqdefault"],
    "hq720": ["hq720", "mqdefault"],
    "sddefault": ["sddefault", "hqdefault", "mqdefault"],
    "hqdefault": ["hqdefault", "mqdefault"],
    "mqdefault": ["mqdefault"],
}


def thumb_qualities():
    q = cfg("thumb_quality") or "maxresdefault"
    return THUMB_CHAIN.get(q, [q, "mqdefault"])


def thumb_url(vid, quality=None):
    # Both halves are quoted: the id is free text as far as this function
    # knows, and the quality is a config value, so either could otherwise put
    # a slash or a query into a path this program fetches.
    q = urllib.parse.quote(str(quality or cfg("thumb_quality") or ""), safe="")
    return f"https://i.ytimg.com/vi/{urllib.parse.quote(str(vid), safe='')}/{q}.jpg"


def thumb_path(vid):
    return os.path.join(THUMB_DIR, f"{vid}.jpg")


# ---- cache ------------------------------------------------------------

def cache_get(key):
    r = db().execute("SELECT payload, ts, ttl FROM cache WHERE key=?", (key,)).fetchone()
    if not r:
        return None
    if time.time() - r["ts"] > r["ttl"]:
        return None
    try:
        return json.loads(r["payload"])
    except ValueError:
        return None


# ---- cached row lists -------------------------------------------------
#
# Feeds, searches and channel listings are all the same shape: a list of flat
# rows whose values are strings and integers. They were stored as JSON, which
# cost twice over on a screen that shows sixty of them:
#
#   * `import json` is 3.6ms, and it drags `re` in with it, on a path that
#     otherwise needs neither;
#   * json.loads is all-or-nothing, so drawing the top sixty rows of the
#     subscriptions feed meant building 1,961 dictionaries and discarding
#     1,901 of them - 2.2ms of parsing for 3% of the result.
#
# So: one line per row, fields in a fixed order. Decoding sixty rows means
# splitting sixty records off the front of the string and nothing else.
#
# The separators are safe by construction rather than by escaping. Every text
# field on its way in has been through clean() or clean_multiline(), which
# replace every C0 control character with a space - clean_multiline keeps
# \n and nothing else - so \x1e and \x1f cannot occur in the data. The
# encoder re-checks anyway, because "cannot occur" is a claim about code
# somewhere else.
_ROW_FIELDS = ("id", "title", "channel", "channel_id", "handle",
               "duration", "views", "published", "is_short")
_ROW_INTS = frozenset(("duration", "views", "published", "is_short"))
_RS = "\x1e"                 # between rows
_US = "\x1f"                 # between fields
_SEP_MAP = {0x1e: " ", 0x1f: " "}


def rows_encode(rows):
    out = []
    for r in rows:
        vals = []
        for k in _ROW_FIELDS:
            v = r.get(k)
            if k in _ROW_INTS:
                vals.append(str(_num(v)))
            else:
                v = str(v or "")
                vals.append(v.translate(_SEP_MAP) if (_RS in v or _US in v) else v)
        out.append(_US.join(vals))
    return _RS.join(out)


def rows_decode(blob, limit=None):
    """Rows off the front of an encoded blob. `limit` bounds the work, not
    just the result: str.split stops once it has that many separators."""
    if not blob:
        return []
    if limit:
        # Not blob.split(_RS, limit): that hands back the entire unparsed
        # remainder as a final element, which is a copy of ~220 KB to throw
        # away. Find where the wanted records end and slice only those.
        cut = -1
        for _ in range(limit):
            nxt = blob.find(_RS, cut + 1)
            if nxt < 0:
                cut = len(blob)
                break
            cut = nxt
        parts = blob[:cut].split(_RS)
    else:
        parts = blob.split(_RS)
    out = []
    for part in parts:
        f = part.split(_US)
        if len(f) != len(_ROW_FIELDS):
            continue                      # a truncated or foreign record
        r = {"description": ""}
        for k, v in zip(_ROW_FIELDS, f):
            r[k] = _num(v) if k in _ROW_INTS else v
        out.append(r)
    return out


def rows_cache_get(key, limit=None):
    r = db().execute("SELECT payload, ts, ttl FROM cache WHERE key=?",
                     (key,)).fetchone()
    if not r or time.time() - r["ts"] > r["ttl"]:
        return None
    payload = r["payload"] or ""
    if payload[:1] in ("[", "{"):
        # Written by the JSON version. Read it once, so upgrading does not
        # throw away a warm feed and send a burst of requests at YouTube;
        # the next write is in the new format and this never runs again.
        try:
            rows = json.loads(payload)
        except ValueError:
            return None
        return rows[:limit] if limit else rows
    return rows_decode(payload, limit)


def rows_cache_put(key, rows, ttl):
    db().execute(
        "INSERT INTO cache(key,payload,ts,ttl) VALUES(?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, "
        "ts=excluded.ts, ttl=excluded.ttl",
        (key, rows_encode(rows), int(time.time()), int(ttl)))
    db().commit()


def cache_del(key):
    """Forget one cache entry. Used to lift a self-imposed pause early."""
    db().execute("DELETE FROM cache WHERE key=?", (key,))
    db().commit()


def cache_put(key, obj, ttl):
    db().execute(
        "INSERT INTO cache(key,payload,ts,ttl) VALUES(?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, "
        "ts=excluded.ts, ttl=excluded.ttl",
        (key, json.dumps(obj, separators=(",", ":")), int(time.time()), int(ttl)))
    db().commit()


def cache_age(key):
    r = db().execute("SELECT ts FROM cache WHERE key=?", (key,)).fetchone()
    return time.time() - r["ts"] if r else None


# ---- cookies / auth ---------------------------------------------------

# The only two jars there are. `which` reaches here from cfg("play_auth"),
# which the CLI validates but a hand-edited config file or YT_PLAY_AUTH does
# not - and it was being pasted straight into a filename.
COOKIE_JARS = ("main", "alt")


def cookie_file(which):
    """Path to a snapshotted cookies.txt, or '' when not configured."""
    if which not in COOKIE_JARS:
        return ""
    p = cfg(f"cookies_{which}")
    if p:
        p = os.path.expanduser(p)
        return p if os.path.exists(p) else ""
    p = os.path.join(CFG_DIR, f"cookies-{which}.txt")
    return p if os.path.exists(p) else ""


def cookie_age(which):
    p = cookie_file(which)
    if not p:
        return None
    try:
        return time.time() - os.path.getmtime(p)
    except OSError:
        return None


def detect_browser_profiles():
    """Firefox-family profiles whose cookies yt-dlp can read unencrypted."""
    found = []
    for root, label in ((os.path.join(HOME, ".zen"), "zen"),
                        (os.path.join(HOME, ".mozilla", "firefox"), "firefox"),
                        (os.path.join(HOME, ".librewolf"), "librewolf")):
        if not os.path.isdir(root):
            continue
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for name in entries:
            prof = os.path.join(root, name)
            if os.path.exists(os.path.join(prof, "cookies.sqlite")):
                found.append((label, name, prof))
    return found


# ---- yt-dlp -----------------------------------------------------------

def ytdlp_base():
    return [
        "yt-dlp",
        "--no-warnings",
        "--ignore-config",          # user's global yt-dlp.conf must not surprise us
        "--no-progress",
        "--socket-timeout", "20",
        "--retries", "2",
        "--extractor-retries", "1",
    ]


def client_args():
    """Extractor args pinning the client that media URLs come from.

    Only used where a real media URL is needed - streaming, downloading. Plain
    metadata extraction is left on yt-dlp's defaults, which are fine for it.
    """
    c = (cfg("stream_client") or "tv_simply").strip()
    return ["--extractor-args", f"youtube:player_client={c}"] if c else []


def run_ytdlp(args, which=None, kind="anon", timeout=120, force=False):
    """Run yt-dlp. `which` selects a cookie jar ('main'/'alt'/None).

    Returns (returncode, stdout, stderr). Risk signals in stderr trip the
    guard, and while the guard is tripped further requests are refused
    outright - continuing to hammer a host that is already rate-limiting us
    is what turns a short cooldown into a long one.
    """
    if not force and at_risk():
        _, until = risk_state()
        return 1, "", (f"yt is backing off for {fmt_dur(until - time.time())} "
                       f"after: {kv_get('risk_reason', 'a rate-limit response')}")
    # can_anon() existed and was reported by `yt doctor` but was called from
    # nowhere, so the anonymous burst and hourly budgets were never actually
    # enforced. Playback is exempt: those budgets are there to keep browsing
    # from hammering YouTube, and refusing to resolve a stream the user just
    # pressed enter on would make the tool look broken.
    if not force and not which and kind not in ("stream", "dl"):
        ok, why = can_anon()
        if not ok:
            return 1, "", f"yt is throttling its own requests: {why}"
    cmd = ytdlp_base() + list(args)
    if which:
        cf = cookie_file(which)
        if cf:
            cmd += ["--cookies", cf]
        else:
            return 1, "", f"no cookies configured for '{which}'"
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired:
        if which:
            log_req(f"auth:{kind}", ok=False)
        return 124, "", "yt-dlp timed out"
    except FileNotFoundError:
        return 127, "", "yt-dlp is not installed"

    risky = bool(RISK_PATTERNS.search(p.stderr or ""))
    log_req(f"auth:{kind}" if which else f"anon:{kind}", ok=not risky)
    if which:
        if kind == "rec" and not risky and p.returncode == 0:
            kv_set("rec_last", int(time.time()))
    if risky:
        _lines = [l for l in (p.stderr or "").splitlines() if l.strip()]
        bump_risk(_lines[-1].strip() if _lines else "unknown")
    elif p.returncode == 0:
        ease_risk()
    return p.returncode, p.stdout, p.stderr


# Cap on the lines either pipe may hold. Everything that reads them wants
# either the last line or a substring search, so an unbounded transcript of a
# two-hour download is memory spent on nothing.
STREAM_KEEP_LINES = 400


def run_ytdlp_stream(args, on_line, which=None, kind="dl", timeout=7200):
    """run_ytdlp, but every stdout line is handed to `on_line` as it arrives.

    A download runs for minutes and yt-dlp reports its progress on the way
    past. capture_output() holds all of that until the process exits, which
    is precisely when the progress has stopped being interesting - so this
    reads the pipe as it fills instead.

    stderr is drained by a second thread rather than read after the fact: a
    verbose failure can fill the 64 KiB pipe buffer and deadlock a process
    that is only being read from on stdout.

    `on_line` returning True means "I have dealt with this line, do not keep
    it". A download emits a progress line twice a second for as long as it
    runs - about 14,000 of them over the 2-hour timeout - and holding every
    one costs a couple of megabytes to end up scanning past all of them for
    the single line that carries the output path.
    """
    if at_risk():
        _, until = risk_state()
        return 1, "", (f"yt is backing off for {fmt_dur(until - time.time())} "
                       f"after: {kv_get('risk_reason', 'a rate-limit response')}")
    cmd = ytdlp_base() + list(args)
    if which:
        cf = cookie_file(which)
        if cf:
            cmd += ["--cookies", cf]
        else:
            return 1, "", f"no cookies configured for '{which}'"
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             stdin=subprocess.DEVNULL, text=True,
                             errors="replace", bufsize=1)
    except FileNotFoundError:
        return 127, "", "yt-dlp is not installed"
    except OSError as e:
        return 1, "", str(e)

    err_lines, out_lines = [], []

    def drain_err():
        # Bounded for the same reason: a --verbose failure can run to tens of
        # thousands of lines, and everything downstream only ever looks at
        # the last one and at whether a risk pattern appears anywhere.
        try:
            for line in p.stderr:
                err_lines.append(line.rstrip("\n"))
                if len(err_lines) > STREAM_KEEP_LINES:
                    del err_lines[:len(err_lines) - STREAM_KEEP_LINES]
        except (OSError, ValueError):
            pass

    th = threading.Thread(target=drain_err, daemon=True)
    th.start()
    # A hung yt-dlp produces no lines at all, so the deadline cannot live in
    # the read loop - it has to be able to fire while we are blocked on it.
    timed_out = []

    def expire():
        timed_out.append(True)
        try:
            p.kill()
        except OSError:
            pass

    killer = threading.Timer(timeout, expire)
    killer.daemon = True
    killer.start()
    try:
        for line in p.stdout:
            line = line.rstrip("\n")
            handled = False
            try:
                handled = bool(on_line(line))
            except Exception:
                pass                    # a broken display never stops a download
            if not handled:
                out_lines.append(line)
                if len(out_lines) > STREAM_KEEP_LINES:
                    del out_lines[:len(out_lines) - STREAM_KEEP_LINES]
    except (OSError, ValueError):
        pass
    finally:
        killer.cancel()
        try:
            p.stdout.close()
        except (OSError, ValueError):
            pass
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        th.join(timeout=5)

    stderr = "\n".join(err_lines)
    if timed_out:
        return 124, "\n".join(out_lines), "yt-dlp timed out"
    rc = p.returncode or 0
    risky = bool(RISK_PATTERNS.search(stderr))
    log_req(f"auth:{kind}" if which else f"anon:{kind}", ok=not risky)
    if risky:
        bad = [l for l in stderr.splitlines() if l.strip()]
        bump_risk(bad[-1].strip() if bad else "unknown")
    elif rc == 0:
        ease_risk()
    return rc, "\n".join(out_lines), stderr


def _num(v, default=0):
    """int() over a field some extractor filled in. yt-dlp normalises most of
    them, but not all: a duration can arrive as "1234.0" and a view count as
    "NA", and int() raises on both. ytdlp_entries had no guard, so one odd
    field did not cost one row - it threw out of the whole listing and the
    feed came back empty."""
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        pass
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError, OverflowError):
        return default


def _entry_timestamp(d):
    """yt-dlp populates different date fields depending on the extractor path,
    so take whichever is present rather than only `timestamp`."""
    for key in ("timestamp", "release_timestamp"):
        v = d.get(key)
        if v:
            got = _num(v)
            if got:
                return got
    ud = d.get("upload_date") or d.get("release_date")
    if ud and len(str(ud)) == 8:
        # Also UTC: yt-dlp's upload_date is the date YouTube reports, not a
        # date in whatever zone this machine happens to sit in. mktime read it
        # as local midnight, which is a fixed offset out for every video.
        return _utc_epoch(str(ud)[:4] + "-" + str(ud)[4:6] + "-" + str(ud)[6:8]
                          + "T00:00:00")
    return 0


def ytdlp_entries(args, which=None, kind="anon", timeout=120):
    """Flat listing -> list of normalised dicts."""
    rc, out, err = run_ytdlp(
        list(args) + ["--flat-playlist", "--dump-json"],
        which=which, kind=kind, timeout=timeout)
    rows = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        vid = d.get("id") or ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
            continue
        dur = _num(d.get("duration"))
        rows.append({
            "id": vid,
            "title": clean(d.get("title") or "(untitled)"),
            "channel": clean(d.get("channel") or d.get("uploader") or ""),
            "channel_id": str(d.get("channel_id") or ""),
            "handle": str(d.get("uploader_id") or "").lstrip("@"),
            "duration": dur,
            "views": _num(d.get("view_count")),
            "published": _entry_timestamp(d),
            "is_short": 1 if (0 < dur <= 60) else 0,
            "description": clean_multiline(d.get("description") or ""),
        })
    if rc != 0 and not rows:
        return None, (err or "yt-dlp failed").strip()
    return rows, ""


# ---- plain HTTP (RSS + thumbnails only) -------------------------------

_BROTLI = None


def _brotlidec():
    """The system libbrotlidec, with argtypes set, or False.

    CPython has no brotli in the standard library and the PyPI binding is not
    installed, but libbrotlidec.so is - brotli is a hard dependency of half the
    desktop - and the streaming decoder is three calls away through ctypes.
    """
    global _BROTLI
    import ctypes
    if _BROTLI is None:
        for name in ("libbrotlidec.so.1", "libbrotlidec.so"):
            try:
                lib = ctypes.CDLL(name)
            except OSError:
                continue
            lib.BrotliDecoderCreateInstance.restype = ctypes.c_void_p
            lib.BrotliDecoderCreateInstance.argtypes = [ctypes.c_void_p] * 3
            lib.BrotliDecoderDestroyInstance.argtypes = [ctypes.c_void_p]
            lib.BrotliDecoderDecompressStream.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_void_p),
                ctypes.c_void_p,
            ]
            lib.BrotliDecoderDecompressStream.restype = ctypes.c_int
            _BROTLI = lib
            break
        else:
            _BROTLI = False
    return _BROTLI


class BrotliError(Exception):
    pass


class BrotliDecompressor:
    """Incremental brotli decode, the shape urllib3 and yt-dlp expect.

    Kept streaming rather than one-shot because that is what urllib3 calls:
    it feeds socket reads in as they arrive and would otherwise have to buffer
    the whole body first.
    """

    # BROTLI_DECODER_RESULT_*
    _ERROR, _SUCCESS, _NEED_INPUT, _NEED_OUTPUT = 0, 1, 2, 3
    _BUF = 1 << 18

    def __init__(self):
        import ctypes
        lib = _brotlidec()
        if not lib:
            raise BrotliError("libbrotlidec is not available")
        self._lib = lib
        self._ctypes = ctypes
        self._st = lib.BrotliDecoderCreateInstance(None, None, None)
        if not self._st:
            raise BrotliError("BrotliDecoderCreateInstance failed")
        self._out = ctypes.create_string_buffer(self._BUF)
        self._done = False

    def decompress(self, data, max_bytes=0):
        """max_bytes=0 means no ceiling - that is what urllib3 and yt-dlp get,
        because they feed this one socket read at a time and cap the body
        themselves. brotli_decompress() passes a real one."""
        ctypes = self._ctypes
        if self._done or not self._st:
            return b""
        src = ctypes.create_string_buffer(data, len(data))
        avail_in = ctypes.c_size_t(len(data))
        next_in = ctypes.c_void_p(ctypes.addressof(src))
        chunks = []
        produced = 0
        while True:
            avail_out = ctypes.c_size_t(self._BUF)
            next_out = ctypes.c_void_p(ctypes.addressof(self._out))
            r = self._lib.BrotliDecoderDecompressStream(
                ctypes.c_void_p(self._st),
                ctypes.byref(avail_in), ctypes.byref(next_in),
                ctypes.byref(avail_out), ctypes.byref(next_out), None)
            n = self._BUF - avail_out.value
            if n:
                chunks.append(self._out.raw[:n])
                produced += n
                if max_bytes and produced > max_bytes:
                    raise BrotliError(
                        f"brotli stream expands past {max_bytes} bytes")
            if r == self._SUCCESS:
                self._done = True
                break
            if r == self._NEED_OUTPUT:
                continue
            if r == self._NEED_INPUT:
                break                    # hand us the next socket read
            raise BrotliError(f"brotli decode failed ({r})")
        return b"".join(chunks)

    def flush(self):
        return b""

    def close(self):
        if getattr(self, "_st", None):
            self._lib.BrotliDecoderDestroyInstance(
                self._ctypes.c_void_p(self._st))
            self._st = None

    __del__ = close


def brotli_decompress(data, max_bytes=0):
    """One-shot decode. Raises BrotliError on a truncated or corrupt stream,
    or on one that expands past max_bytes."""
    d = BrotliDecompressor()
    try:
        out = d.decompress(data, max_bytes)
        if not d._done:
            raise BrotliError("truncated brotli stream")
        return out
    finally:
        d.close()


def install_brotli_shim():
    """Publish `brotli` in sys.modules so urllib3 and yt-dlp will use it.

    yt-dlp asks YouTube for `identity` on media but for compressed HTML on the
    pages it extracts from, and a watch page is ~1.4 MB gzipped against ~130 KB
    brotli. Both libraries decide brotli support at import time, so this has to
    run before yt_dlp is imported. No-op if the library is missing, and the
    caller keeps its subprocess fallback either way.
    """
    if "brotli" in sys.modules or "brotlicffi" in sys.modules:
        return bool(sys.modules.get("brotli"))
    if not have_brotli():
        return False
    import types
    mod = types.ModuleType("brotli")
    mod.error = BrotliError
    mod.Decompressor = BrotliDecompressor
    mod.decompress = brotli_decompress
    mod.__version__ = "ctypes/libbrotlidec"
    sys.modules["brotli"] = mod
    return True


_HAVE_BR = None


def have_brotli():
    """Only advertise br once a real round trip through the library has worked.

    Asking for an encoding we then cannot decode would turn every request into
    an exception, so the probe is a decode of a two-byte brotli stream rather
    than a check that the .so exists.
    """
    global _HAVE_BR
    if _HAVE_BR is None:
        try:
            _HAVE_BR = brotli_decompress(b"\x8f\x00\x80yt\x03") == b"yt"
        except Exception:
            _HAVE_BR = False
    return _HAVE_BR


# The largest thing this is ever asked to fetch is a watch page, about 1.4 MB
# of HTML. The cap is well clear of that and exists because both ends of this
# are unbounded otherwise: a response has no declared limit, and a compressed
# one expands by a ratio the sender picks. Twelve of these run at once on a
# feed refresh, so "however big it turns out to be" is not a size to hand to
# a decompressor.
HTTP_MAX_BYTES = 32 * 1024 * 1024


def http_get(url, timeout=15, headers=None, max_bytes=HTTP_MAX_BYTES, data=None):
    # Imported here, not at module scope: urllib.request drags in http.client,
    # ssl and email for ~17ms, and most invocations of this program never make
    # a plain HTTP request at all.
    import gzip
    import urllib.request
    enc = "br, gzip" if have_brotli() else "gzip"
    h = {"User-Agent": UA, "Accept-Encoding": enc}
    if headers:
        h.update(headers)
    # `data` makes it a POST. Only used for YouTube's own JSON search endpoint,
    # which is still a read - it takes a query and returns results.
    # urlopen speaks file:// and ftp:// as readily as https, and every URL
    # that reaches here is built from a template plus an id that came out of
    # YouTube's own JSON or the subscriptions table. Neither is a place to
    # trust with a scheme, so only the two this program actually uses are
    # allowed - and urllib's redirect handler already refuses to leave them.
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise ValueError(f"refusing to fetch a non-http url: {url[:60]}")
    req = urllib.request.Request(url, headers=h, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as f:
        # read(n+1) rather than read(): a truncated body is a failure to
        # report, not a prefix to go on and parse as if it were the whole
        # thing.
        raw = f.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError(f"response larger than {max_bytes} bytes")
        ce = (f.headers.get("Content-Encoding") or "").lower()
        if ce == "gzip":
            raw = _bounded_gunzip(raw, max_bytes)
        elif ce == "br":
            raw = brotli_decompress(raw, max_bytes)
        return raw


def _bounded_gunzip(raw, max_bytes):
    """gzip.decompress with a ceiling. The stdlib one has none: a few KB on
    the wire can decide to become gigabytes in memory."""
    import zlib
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(raw, max_bytes + 1)
    if len(out) > max_bytes or d.unconsumed_tail:
        raise ValueError(f"gzip stream expands past {max_bytes} bytes")
    return out


RSS_NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "m": "http://search.yahoo.com/mrss/",
}


def _utc_epoch(stamp):
    """An ISO-8601 UTC stamp -> epoch seconds, or 0.

    Deliberately not mktime(): mktime reads a struct_time as *local* time and
    guesses DST for it, so "mktime(utc) - time.timezone" is exact in winter
    and an hour early all summer. Every feed row goes through here, so that
    was every upload time on the home screen reading an hour older than it
    was, for half the year. calendar.timegm does the conversion the struct
    actually asks for, with no zone involved at all.
    """
    # datetime rather than calendar.timegm: timegm is the textbook answer but
    # importing calendar costs 5.5ms, and this module is re-imported by every
    # single yt invocation. datetime costs 0.25ms, and only on the network
    # path that actually parses a feed - a render out of the database never
    # reaches this at all.
    import datetime
    try:
        t = time.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S")
        return int(datetime.datetime(*t[:6],
                                     tzinfo=datetime.timezone.utc).timestamp())
    except (ValueError, OverflowError, TypeError):
        return 0


class FeedError(Exception):
    """A channel feed we could not read, as opposed to one with no videos.

    Without this the two cases are the same empty list, and a fan-out that
    lost a quarter of its channels to a blip got cached as a complete feed
    for the next 15 minutes with nothing to show anything was missing.
    """


def fetch_channel_rss(channel_id):
    """One channel's latest 15 uploads. Unauthenticated, CDN-cached, cheap."""
    # Quoted rather than interpolated raw: these come out of the subs table,
    # and one containing an & would have appended parameters of its own to a
    # URL this program then fetches.
    url = ("https://www.youtube.com/feeds/videos.xml?channel_id="
           + urllib.parse.quote(str(channel_id or ""), safe=""))
    try:
        raw = http_get(url, timeout=12)
    except Exception as e:
        raise FeedError(str(e)[:120]) from None
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise FeedError("malformed feed") from None
    cname = root.findtext("a:title", default="", namespaces=RSS_NS)
    out = []
    for e in root.findall("a:entry", RSS_NS):
        vid = e.findtext("yt:videoId", default="", namespaces=RSS_NS)
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid or ""):
            continue
        pub = e.findtext("a:published", default="", namespaces=RSS_NS)
        ts = _utc_epoch(pub) if pub else 0
        grp = e.find("m:group", RSS_NS)
        views = 0
        desc = ""
        if grp is not None:
            st = grp.find("m:community/m:statistics", RSS_NS)
            if st is not None:
                try:
                    views = int(st.get("views") or 0)
                except ValueError:
                    views = 0
            # RSS carries the full description - the richest source we get
            # without a per-video extraction.
            desc = clean_multiline(grp.findtext("m:description", default="",
                                                namespaces=RSS_NS))
        out.append({
            "id": vid,
            "title": clean(e.findtext("a:title", default="", namespaces=RSS_NS)),
            "channel": clean(cname),
            "channel_id": channel_id,
            "handle": "",
            "duration": 0,          # RSS omits duration
            "views": views,
            "published": ts,
            "is_short": 0,
            "description": desc,
        })
    return out


def fetch_subs_feed(channel_ids, workers=None):
    """Returns (rows, failed_count). A caller that is about to cache the
    result needs to know whether it is complete."""
    workers = workers or cfg_int("rss_workers", 12)
    rows = []
    failed = 0
    if not channel_ids:
        return rows, failed
    from concurrent.futures import ThreadPoolExecutor

    def one(cid):
        try:
            return fetch_channel_rss(cid)
        except FeedError:
            return None

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(channel_ids)))) as ex:
        for res in ex.map(one, channel_ids):
            if res is None:
                failed += 1
            else:
                rows.extend(res)
    rows.sort(key=lambda r: r["published"], reverse=True)
    return rows, failed


def _fetch_thumb(vid):
    p = thumb_path(vid)
    if os.path.exists(p) and os.path.getsize(p) > 512:
        return False
    raw = None
    for q in thumb_qualities():
        try:
            raw = http_get(thumb_url(vid, q), timeout=12)
        except Exception:
            continue
        if raw and len(raw) >= 2048:
            break
        raw = None
    if not raw:
        return False
    tmp = f"{p}.{os.getpid()}.tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, p)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


QUALITY_MARKER = os.path.join(THUMB_DIR, ".quality")


def check_thumb_quality():
    """Drop the cache when thumb_quality changes.

    _fetch_thumb skips any file that already exists, so without this a
    resolution change would never reach anything already cached.
    """
    want = cfg("thumb_quality") or "maxresdefault"
    try:
        with open(QUALITY_MARKER, encoding="utf-8") as fh:
            have = fh.read().strip()
    except OSError:
        have = ""
    if have == want:
        return 0
    n = 0
    if os.path.isdir(THUMB_DIR):
        for f in os.listdir(THUMB_DIR):
            if not f.endswith(".jpg"):
                continue
            try:
                os.unlink(os.path.join(THUMB_DIR, f))
                n += 1
            except OSError:
                pass
    try:
        with open(QUALITY_MARKER, "w", encoding="utf-8") as fh:
            fh.write(want)
    except OSError:
        pass
    return n


def prefetch_thumbs(vids):
    secure_dir(CACHE_DIR)
    os.makedirs(THUMB_DIR, exist_ok=True)
    check_thumb_quality()
    todo = [v for v in dict.fromkeys(vids)
            if not (os.path.exists(thumb_path(v))
                    and os.path.getsize(thumb_path(v)) > 512)]
    if not todo:
        return 0
    workers = max(1, min(cfg_int("prefetch_workers", 16), len(todo)))
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return sum(1 for ok in ex.map(_fetch_thumb, todo) if ok)


def _launcher():
    """The import-based entry point. Re-launching ourselves through ytlib.py
    directly would recompile 130 KB of source in the child for nothing."""
    d = os.path.dirname(os.path.abspath(__file__))
    lp = os.path.join(d, "ytmain.py")
    return lp if os.path.exists(lp) else os.path.abspath(__file__)


def spawn_prefetch(vids):
    """Detach thumbnail warming so fzf opens immediately.

    Every emit() used to spawn a whole Python interpreter for this, including
    the overwhelmingly common case where the list is the same one as last time
    and every thumbnail is already on disk. Checking first is one stat per row
    - about 60 microseconds for a full screen - against ~20ms of CPU and a
    process, and it also keeps the `subprocess` import (~3ms of start-up) off
    the warm path entirely.
    """
    vids = [v for v in dict.fromkeys(vids) if v]
    if not vids:
        return
    missing = []
    for v in vids:
        try:
            if os.path.getsize(thumb_path(v)) > 512:
                continue
        except OSError:
            pass
        missing.append(v)
    if not missing:
        return
    vids = missing
    try:
        p = subprocess.Popen(
            [sys.executable, _launcher(), "prefetch"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, text=True)
        p.stdin.write("\n".join(vids))
        p.stdin.close()
    except (OSError, BrokenPipeError, ValueError):
        pass


# ---- video store ------------------------------------------------------

def upsert_videos(rows):
    """Merge listing rows into `videos`, never clobbering good data with blanks."""
    now = int(time.time())
    db().executemany("""
      INSERT INTO videos(id,title,channel,channel_id,handle,duration,views,
                         published,is_short,description,updated)
      VALUES(:id,:title,:channel,:channel_id,:handle,:duration,:views,
             :published,:is_short,:description,:now)
      ON CONFLICT(id) DO UPDATE SET
        title      = CASE WHEN excluded.title      != '' THEN excluded.title      ELSE videos.title END,
        channel    = CASE WHEN excluded.channel    != '' THEN excluded.channel    ELSE videos.channel END,
        channel_id = CASE WHEN excluded.channel_id != '' THEN excluded.channel_id ELSE videos.channel_id END,
        handle     = CASE WHEN excluded.handle     != '' THEN excluded.handle     ELSE videos.handle END,
        duration   = CASE WHEN excluded.duration   >  0  THEN excluded.duration   ELSE videos.duration END,
        views      = CASE WHEN excluded.views      >  0  THEN excluded.views      ELSE videos.views END,
        published  = CASE WHEN excluded.published  >  0  THEN excluded.published  ELSE videos.published END,
        is_short   = CASE WHEN excluded.duration   >  0  THEN excluded.is_short   ELSE videos.is_short END,
        description= CASE WHEN length(excluded.description) > length(videos.description)
                          THEN excluded.description ELSE videos.description END,
        updated    = excluded.updated
    """, [dict(r, now=now) for r in rows])
    db().commit()


def get_video(vid):
    return db().execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()


def watch_page_meta(vid):
    """One video's metadata from its watch page, or None.

    yt-dlp is the reliable way to do this, but it costs a whole extraction per
    video - process start, a PO token, a JS challenge - and none of that is
    needed to read a title. The page already carries ytInitialPlayerResponse,
    which has everything the library stores, including the full description.
    Measured: ~0.7s for one page against ~2.4s for one yt-dlp run, and pages
    fetch in parallel where yt-dlp URLs go one after another.
    """
    try:
        raw = http_get(f"https://www.youtube.com/watch?v={vid}", timeout=15,
                       headers={"Accept-Language": "en-US,en;q=0.9"})
        html = raw.decode("utf-8", "replace")
    except Exception:
        return None
    i = html.find("ytInitialPlayerResponse")
    if i == -1:
        return None
    j = html.find("{", i)
    if j == -1:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html, j)
    except ValueError:
        return None
    vd = (data or {}).get("videoDetails") or {}
    if vd.get("videoId") != vid or not vd.get("title"):
        return None            # age-gated, private, or an error page
    mf = ((data.get("microformat") or {})
          .get("playerMicroformatRenderer") or {})
    dur = _num(vd.get("lengthSeconds"))
    views = _num(vd.get("viewCount"))
    date = mf.get("publishDate") or mf.get("uploadDate") or ""
    published = _utc_epoch(date) if date else 0
    return {
        "id": vid,
        "title": clean(vd.get("title") or "(untitled)"),
        "channel": clean(vd.get("author") or ""),
        "channel_id": vd.get("channelId") or "",
        "handle": (mf.get("ownerProfileUrl") or "").rsplit("/@", 1)[-1]
                  if "/@" in (mf.get("ownerProfileUrl") or "") else "",
        "duration": dur,
        "views": views,
        "published": published,
        "is_short": 1 if (0 < dur <= 60) else 0,
        "description": clean_multiline(vd.get("shortDescription") or ""),
    }


def fetch_video_meta(vids, which=None):
    """Fill in metadata for ids we don't know yet.

    Watch pages first, in parallel; yt-dlp for whatever they could not answer
    (age-gated or region-blocked videos mostly). Pasting five URLs used to mean
    five sequential extractions - about twelve seconds - and now costs one
    round of parallel GETs.
    """
    unknown = [v for v in dict.fromkeys(vids)
               if not (get_video(v) and get_video(v)["title"])]
    if not unknown:
        return 0
    rows = []
    if not which:
        from concurrent.futures import ThreadPoolExecutor
        workers = max(1, min(cfg_int("rss_workers", 12), len(unknown)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            rows = [r for r in ex.map(watch_page_meta, unknown) if r]
        if rows:
            upsert_videos(rows)
        unknown = [v for v in unknown
                   if not (get_video(v) and get_video(v)["title"])]
        if not unknown:
            return len(rows)
    urls = [f"https://www.youtube.com/watch?v={v}" for v in unknown]
    more, err = ytdlp_entries(urls, which=which, timeout=180)
    if more:
        upsert_videos(more)
        return len(rows) + len(more)
    return len(rows)


# ---- row rendering ----------------------------------------------------

SEP = "\t"


def term_cols():
    try:
        c = int(os.environ.get("YT_COLUMNS") or os.environ.get("COLUMNS") or 0)
    except ValueError:
        c = 0
    if c <= 0:
        # stdout is a pipe under command substitution; stderr usually is not.
        for fd in (1, 2, 0):
            try:
                c = os.get_terminal_size(fd).columns
                break
            except OSError:
                continue
    if c <= 0:
        c = 120
    return max(32, min(c, 400))


def preview_cols():
    """Width of fzf's preview pane."""
    pct = cfg_int("preview_pct", 40)
    if not 20 <= pct <= 70:
        pct = 40
    return max(20, int(term_cols() * pct / 100) - 4)


def wrap_text(text, width):
    """Word-wrap on display width, so CJK and emoji do not overflow."""
    out = []
    for para in text.split("\n"):
        if not para.strip():
            out.append("")
            continue
        if para.isascii():
            # The overwhelming majority of paragraphs are plain ASCII, where
            # display width is just len() and none of the machinery below can
            # change the answer. Drawing a screen of sixty descriptions is
            # ~13,000 words, and taking dwidth's function call out of that
            # loop is most of what the general path costs.
            cur = ""
            for word in para.split(" "):
                cand = cur + " " + word if cur else word
                if len(cand) > width and cur:
                    out.append(cur)
                    cur = word
                else:
                    cur = cand
                while len(cur) > width:         # a single over-long token
                    out.append(cur[:width])
                    cur = cur[width:]
            if cur:
                out.append(cur)
            continue
        cur, curw = "", 0
        for word in para.split(" "):
            # Carrying the width makes this linear. Re-measuring the whole
            # accumulated line once per word was quadratic, and dwidth's ASCII
            # fast path is defeated by a single emoji anywhere in the string,
            # so one long description cost hundreds of thousands of
            # unicodedata lookups.
            ww = dwidth(word)
            if cur:
                cand, candw = cur + " " + word, curw + 1 + ww
                # Widths add across the joining space except when the space is
                # absorbed: a trailing ZWJ swallows it, a leading VS16 turns it
                # into emoji presentation. Rare enough to just re-measure.
                if cur[-1] == ZWJ or word[:1] == VS16:
                    candw = dwidth(cand)
            else:
                cand, candw = word, ww
            if candw > width and cur:
                out.append(cur)
                cur, curw = word, ww
            else:
                cur, curw = cand, candw
            if curw > width:                    # a single over-long token
                parts = split_width(cur, width)
                out.extend(parts[:-1])
                cur = parts[-1]
                curw = dwidth(cur)
        if cur:
            out.append(cur)
    return out


def layout_cols():
    """Width the rows should be laid out for.

    When feeding fzf, that is the LIST pane - not the whole terminal. Padding
    to the terminal width makes the title column swallow the pane and pushes
    channel/duration/views off the right-hand edge where fzf clips them.
    """
    if os.environ.get("YT_PANE") != "1":
        return term_cols()
    pct = cfg_int("preview_pct", 40)
    if not 20 <= pct <= 70:
        pct = 40
    return max(32, int(term_cols() * (100 - pct) / 100) - 4)


def resumable(pos, dur):
    """Meaningfully started and not effectively finished."""
    pos = pos or 0
    dur = dur or 0
    if pos < RESUME_FLOOR:
        return False
    if pos < RESUME_MIN_SECS and not (dur and pos >= dur * RESUME_MIN_FRAC):
        return False
    return not (dur and pos >= dur * WATCHED_FRAC)


def marks(vid, st, dur=0):
    """Four-slot status gutter: saved / progress / download / has-note.

    The third slot has one character to say everything about a download, so a
    running one fills it in proportion: the block glyph rises from ▁ to █ as
    the bytes come in. The exact figure is in the preview pane and in
    `yt queue`; this only has to make it obvious at a glance which row is
    moving and roughly how far along it is.
    """
    s = st.get(vid, {})
    a = "●" if s.get("saved") else " "          # ●
    if s.get("watched"):
        b = "✓"                                  # ✓
    elif resumable(s.get("pos", 0), dur):
        b = "▸"                                  # ▸
    else:
        b = " "
    prog = s.get("prog")
    state = s.get("dlstate")
    if prog:
        c = DL_BARS[min(8, int(prog["pct"] / 12.5))]
    elif s.get("dl"):
        c = "⬇"                                  # ⬇ already on disk
    elif state == "queued":
        c = "·"                                  # waiting its turn
    elif state == "running":
        c = DL_BARS[0]                           # started, no bytes yet
    elif state == "error":
        c = "!"
    else:
        c = " "
    d = "✎" if s.get("note") else " "           # ✎ note attached
    return a + b + c + d


def status_map(vids):
    """One query per table instead of one per row."""
    if not vids:
        return {}
    out = {v: {} for v in vids}
    # Chunked so a large library cannot trip SQLITE_MAX_VARIABLE_NUMBER.
    for chunk in (vids[i:i + 400] for i in range(0, len(vids), 400)):
        q = ",".join("?" * len(chunk))
        for r in db().execute(
                f"SELECT video_id, category, note FROM saved "
                f"WHERE video_id IN ({q}) AND archived=0", chunk):
            out[r["video_id"]].update(saved=True, category=r["category"],
                                      note=r["note"])
        for r in db().execute(
                f"SELECT video_id, position, watched FROM watch "
                f"WHERE video_id IN ({q})", chunk):
            out[r["video_id"]].update(pos=r["position"], watched=r["watched"])
        for r in db().execute(
                f"SELECT video_id, path, status FROM downloads "
                f"WHERE video_id IN ({q})", chunk):
            if r["status"] == "done":
                if r["path"] and os.path.exists(r["path"]):
                    out[r["video_id"]].update(dl=True, path=r["path"])
            else:
                out[r["video_id"]]["dlstate"] = r["status"]
    # One listdir for the whole screen, and only when something is actually
    # queued or running - an idle library never touches the directory.
    if any(v.get("dlstate") in ("queued", "running") for v in out.values()):
        for vid, prog in dl_progress_all().items():
            if vid in out:
                out[vid]["prog"] = prog
    return out


def render(rows, mode="feed"):
    """rows -> list of 'id\tkind\tdisplay\tdetail' strings.

    Columns are dropped progressively as the pane narrows so the title keeps
    as much room as possible and nothing ever overflows into fzf's clipping.
    """
    cols = layout_cols()
    pw = preview_cols()
    ids = [r["id"] for r in rows]
    st = status_map(ids)

    w_mark, w_dur = 4, 8
    # Only show a column if at least one row has something to put in it.
    # The recommended feed carries no upload dates, and a full column of "-"
    # reads as breakage rather than as absent data.
    has_age = any(r.get("published") for r in rows)
    has_views = any(r.get("views") for r in rows)
    has_dur = any(r.get("duration") for r in rows)
    show_age = cols >= 64 and has_age
    show_views = cols >= 84 and has_views
    w_cat = 12 if (mode == "lib" and cols >= 96) else 0
    # The note is the reason a video is in the library at all, so in that view
    # it outranks the channel - show the note and drop the channel rather than
    # starving the title of width.
    w_note = 0
    if mode == "lib" and cols >= 100 and any(st.get(v, {}).get("note") for v in ids):
        w_note = min(30, max(14, (cols - 76)))
    if w_note:
        w_ch = 0
    elif cols >= 104:
        w_ch = 18
    elif cols >= 88:
        w_ch = 14
    elif cols >= 68:
        w_ch = 10
    else:
        w_ch = 0

    fixed = w_mark + 1 + (w_dur if has_dur else 0)
    if w_cat:
        fixed += w_cat + 1
    if w_note:
        fixed += w_note + 1
    if w_ch:
        fixed += w_ch + 1
    if show_views:
        fixed += 6 + 1
    if show_age:
        fixed += 4 + 1
    w_title = max(16, cols - fixed - 1)

    out = []
    for r in rows:
        vid = r["id"]
        s = st.get(vid, {})
        # Sanitise here too, not just at ingest: a stray tab in a title
        # would shift every field and bind the wrong id to the row.
        title = clean(r.get("title")) or "(untitled)"
        if r.get("is_short"):
            title = "↯ " + title            # ↯ shorts marker
        parts = [marks(vid, st, r.get("duration")), pad(title, w_title)]
        if w_cat:
            parts.append(pad(clean(s.get("category", "")), w_cat))
        if w_note:
            parts.append(pad(clean(s.get("note", "")), w_note))
        if w_ch:
            parts.append(pad(clean(r.get("channel")), w_ch))
        # Blank, not a dash: an unknown value should read as absent rather
        # than as a placeholder glyph repeated down the column.
        if has_dur:
            parts.append(pad(hms(r["duration"]) if r.get("duration") else "", w_dur))
        if show_views:
            parts.append(pad(compact_num(r["views"]) if r.get("views") else "", 6))
        if show_age:
            parts.append(pad(ago(r["published"]) if r.get("published") else "", 4))
        out.append(SEP.join([vid, mode, " ".join(parts).rstrip(),
                             detail(r, s, pw)]))
    return out


def detail(r, s, width=0):
    """Preview body, pre-rendered so the preview pane spawns no extra work.

    `width` is passed in by render(), which already knows it: working it out
    costs a config lookup and an ioctl, and it is the same answer for every
    row on the screen.


    Wrapped here rather than by fold(1) because the ANSI codes below would
    otherwise be counted as visible characters and wrap the text short.
    Newlines are a literal backslash-n, expanded by printf %b in the preview.
    """
    w = width or preview_cols()
    B, D, R = "\\033[1m", "\\033[2m", "\\033[0m"

    def esc(t):
        return str(t or "").translate(_ESC_MAP).strip()

    lines = []
    for i, ln in enumerate(wrap_text(esc(r.get("title")) or "(untitled)", w)):
        lines.append(B + ln + R)

    facts = []
    if r.get("channel"):
        facts.append(esc(r["channel"]))
    if r.get("duration"):
        facts.append(hms(r["duration"]))
    if r.get("views"):
        facts.append(f"{compact_num(r['views'])} views")
    if r.get("published"):
        facts.append(f"{ago(r['published'])} ago")
    if facts:
        for ln in wrap_text("  ·  ".join(facts), w):
            lines.append(D + ln + R)

    if s.get("note"):
        for ln in wrap_text("note: " + esc(s["note"]), w):
            lines.append(B + ln + R)

    status = []
    if s.get("category"):
        status.append(f"[{esc(s['category'])}]")
    pos = s.get("pos", 0)
    if s.get("watched"):
        status.append("watched")
    elif resumable(pos, r.get("duration")):
        left = (r.get("duration") or 0) - pos
        status.append("resume " + hms(pos)
                      + (f" ({hms(left)} left)" if left > 0 else ""))
    if s.get("dl"):
        status.append("offline")
    if status:
        for ln in wrap_text("  ·  ".join(status), w):
            lines.append(D + ln + R)

    prog = s.get("prog")
    if prog:
        bits = [f"{prog['phase']} {prog['pct']:.0f}%"]
        if prog["total"]:
            bits.append(f"{prog['done'] / 1048576:.0f}"
                        f"/{prog['total'] / 1048576:.0f} MiB")
        if prog["speed"]:
            bits.append(f"{prog['speed'] / 1048576:.1f} MiB/s")
        if prog["eta"]:
            bits.append("eta " + hms(prog["eta"]))
        lines.append("")
        for ln in wrap_text(dl_bar(prog["pct"], min(24, max(8, w - 4))), w):
            lines.append(B + ln + R)
        for ln in wrap_text("  ·  ".join(bits), w):
            lines.append(D + ln + R)
    elif s.get("dlstate") == "queued":
        lines.append("")
        lines.append(D + "queued for download" + R)
    elif s.get("dlstate") == "error":
        lines.append("")
        lines.append(D + "download failed - ^d to try again" + R)

    desc = clean_multiline(r.get("description") or "")
    if desc:
        lines.append("")
        for ln in wrap_text(desc.replace("\\", "\\\\"), w):
            lines.append(D + ln + R if ln else "")

    return "\\n".join(lines)


def enrich(rows):
    """RSS carries no duration/views. Fill gaps from what we already know,
    in one query, so feed rows are as informative as search rows."""
    gaps = [r["id"] for r in rows
            if not r.get("duration") or not r.get("views")
            or not r.get("description")]
    if not gaps:
        return rows
    known = {}
    for chunk in (gaps[i:i + 400] for i in range(0, len(gaps), 400)):
        q = ",".join("?" * len(chunk))
        for v in db().execute(
                f"SELECT id,duration,views,is_short,channel,title,description "
                f"FROM videos WHERE id IN ({q})", chunk):
            known[v["id"]] = v
    for r in rows:
        v = known.get(r["id"])
        if not v:
            continue
        if not r.get("duration") and v["duration"]:
            r["duration"] = v["duration"]
            r["is_short"] = v["is_short"]
        if not r.get("views") and v["views"]:
            r["views"] = v["views"]
        if not r.get("channel") and v["channel"]:
            r["channel"] = v["channel"]
        if not r.get("description") and v["description"]:
            r["description"] = v["description"]
    return rows


def emit(rows, mode="feed", limit=None):
    # Enrich first: RSS rows carry no duration, so short-ness is unknown
    # until local metadata is merged in. Filtering before that is a no-op.
    # But when no shorts filter is active nothing after this point can drop a
    # row, so enriching the whole 1900-row feed to print 60 is dead work.
    if limit and cfg("shorts") not in ("hide", "only"):
        rows = rows[:limit]
    rows = enrich(rows)
    rows = filter_shorts(rows)
    if limit:
        rows = rows[:limit]
    spawn_prefetch([r["id"] for r in rows])
    for line in render(rows, mode):
        sys.stdout.write(line + "\n")
    sys.stdout.flush()


def filter_shorts(rows):
    m = cfg("shorts")
    if m == "hide":
        return [r for r in rows if not r.get("is_short")]
    if m == "only":
        return [r for r in rows if r.get("is_short")]
    return rows


# ---- search -----------------------------------------------------------

def _yt_text(node):
    """YouTube renders text as either {"simpleText":...} or {"runs":[...]}.

    Written to survive shapes it has never seen. This parses a remote JSON
    blob that changes without notice, and a run that is a bare string, or a
    "text" that is a number, used to raise straight out of the search loop -
    past the fallback that is supposed to catch exactly that.
    """
    if not isinstance(node, dict):
        return ""
    simple = node.get("simpleText")
    if isinstance(simple, str):
        return simple
    runs = node.get("runs")
    if not isinstance(runs, list):
        return ""
    out = []
    for r in runs:
        if isinstance(r, dict):
            t = r.get("text")
            if isinstance(t, str):
                out.append(t)
            elif t is not None:
                out.append(str(t))
    return "".join(out)


def _walk_renderers(node, key, out):
    if isinstance(node, dict):
        if key in node and isinstance(node[key], dict):
            out.append(node[key])
        for v in node.values():
            _walk_renderers(v, key, out)
    elif isinstance(node, list):
        for v in node:
            _walk_renderers(v, key, out)


_DUR_RE = _Rx("_DUR_RE", r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")
_AGO_RE = _Rx("_AGO_RE",
              r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago",
              ignorecase=True)
_AGO_SECS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400,
             "week": 604800, "month": 2592000, "year": 31536000}


def _yt_initial_data(html):
    """The ytInitialData blob out of a YouTube HTML page, or None.

    raw_decode reads the object in place from the index where it starts, so
    this never builds the 690 KiB substring a capturing regex would, and it
    stops at the object's real end rather than at the first `}` followed by
    `</script>` - which is what a non-greedy pattern is really matching.
    """
    i = html.find("ytInitialData")
    while i != -1:
        j = html.find("{", i)
        if j == -1:
            return None
        try:
            data, _ = json.JSONDecoder().raw_decode(html, j)
        except ValueError:
            i = html.find("ytInitialData", i + 13)
            continue
        return data if isinstance(data, dict) else None
    return None


# The videos-only filter, the same one the website puts in the URL.
SEARCH_PARAMS = "EgIQAQ%3D%3D"
CV_KEY = "search:client_version"
API_BENCH_KEY = "search:api_bench"
# How long the JSON endpoint stays benched after a failure. It used to be a
# flat hour on the first failure, which is the wrong shape twice over: a
# transient 5xx cost sixty minutes of the slower route, and a real breakage
# still spent a wasted request every hour forever. Backing off instead means a
# blip costs five minutes and something genuinely broken settles at an hour,
# and the step is remembered in the cached value rather than in a global, so it
# survives the process that noticed.
API_BENCH_STEPS = (300, 900, 3600)


SEARCH_ENDPOINT = "https://www.youtube.com/youtubei/v1/search?prettyPrint=false"


def _innertube_post(payload, cv):
    """One call to the search endpoint, decoded."""
    body = json.dumps(payload).encode()
    raw = http_get(SEARCH_ENDPOINT, timeout=15, data=body,
                   max_bytes=16 * 1024 * 1024,
                   headers={"Content-Type": "application/json",
                            "Accept-Language": "en-US,en;q=0.9",
                            "Origin": "https://www.youtube.com",
                            "X-Youtube-Client-Name": "1",
                            "X-Youtube-Client-Version": cv})
    return json.loads(raw)


def _continuation_token(data):
    """The token for the next page of results, if there is one."""
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            cc = node.get("continuationCommand")
            if isinstance(cc, dict) and isinstance(cc.get("token"), str):
                return cc["token"]
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _innertube_search(query, n, cv, pages=1):
    """The JSON endpoint the results page itself calls.

    Same request, same answer, a great deal less of it: 61 KiB on the wire
    against 286 KiB, 529 KiB to parse against 1.34 MB of HTML, and 558 ms
    against 632 ms. The renderers inside are the identical shape, so this
    hands the very same blob to the very same parser - there is no second
    format to keep working.

    YouTube answers a search with twenty results and a continuation token, and
    that is all either route has ever returned - so `search_count=40` quietly
    got twenty. Each further page is a second request of the same size and
    about 600 ms, which is why `search_pages` exists and why it is 1 by
    default: paging is the user's call, not something to spend on their behalf.
    """
    data = _innertube_post({
        "context": {"client": {"clientName": "WEB", "clientVersion": cv,
                               "hl": "en", "gl": "US"}},
        "query": query,
        "params": SEARCH_PARAMS,
    }, cv)
    rows = _search_rows(data, n)
    # None means the page parsed but did not look like search results, and the
    # caller's contract is to fall back to yt-dlp on exactly that. Paging a
    # page we did not understand would be worse than not paging.
    if not rows:
        return rows
    seen = {r["id"] for r in rows}
    for _ in range(max(0, pages - 1)):
        if len(rows) >= n:
            break
        token = _continuation_token(data)
        if not token:
            break
        try:
            data = _innertube_post({
                "context": {"client": {"clientName": "WEB",
                                       "clientVersion": cv,
                                       "hl": "en", "gl": "US"}},
                "continuation": token,
            }, cv)
            page = _search_rows(data, n)
        except Exception:
            # A page that fails is a shorter list, not a failed search: the
            # rows already in hand are good ones, and they are what the first
            # request paid for.
            break
        if not page:
            break
        more = [r for r in page if r["id"] not in seen]
        if not more:
            break                       # same page again, or the end of them
        seen.update(r["id"] for r in more)
        rows.extend(more)
    return rows[:n]


_CV_RE = _Rx("_CV_RE", r'"INNERTUBE_CLIENT_VERSION":"([0-9][0-9.]{6,24})"')


def _api_bench_step():
    """How many times in a row the JSON endpoint has just failed.

    Read past the TTL on purpose - the whole point is to remember the last
    failure after its bench has expired - but not past a day, so a machine
    that has been off overnight starts again from the short wait. Not
    cache_get_stale(): that one decodes row lists, and this is one integer.
    """
    r = db().execute("SELECT payload, ts FROM cache WHERE key=?",
                     (API_BENCH_KEY,)).fetchone()
    if not r or time.time() - r["ts"] > 86400:
        return 0
    try:
        step = int(json.loads(r["payload"]))
    except (ValueError, TypeError):
        return 0
    return step if 0 < step <= len(API_BENCH_STEPS) else 0


def search_fast(query, n):
    """Search without a yt-dlp process: ~0.6s against ~3.1s for `ytsearch40:`.

    Two ways in, both read-only and both anonymous. The JSON endpoint is
    tried first because it is smaller and faster, but it needs a client
    version to be accepted - and the results page is where that comes from,
    so the page is both the fallback and how the fast path learns to work.
    A fresh install therefore uses the page once and the endpoint after.

    Either way the blob goes to the same parser, and any failure at all
    returns None so the caller can fall back to yt-dlp.
    """
    cv = cache_get(CV_KEY)
    if (isinstance(cv, str) and cv and cfg("search_api") != "0"
            and cache_get(API_BENCH_KEY) is None):
        try:
            rows = _innertube_search(query, n, cv,
                                     pages=cfg_int("search_pages", 1))
            if rows:
                # A search that works clears the backoff, so the next failure
                # is treated as the blip it probably is rather than inheriting
                # a penalty from something that happened hours ago.
                if _api_bench_step():
                    cache_del(API_BENCH_KEY)
                return rows
        except Exception:
            pass
        # One wasted request is a fair price for a 12% saving on every other
        # search; a run of them is not. A stale client version is the likely
        # cause and the page below refreshes it, so the first wait is short.
        step = min(_api_bench_step() + 1, len(API_BENCH_STEPS))
        cache_put(API_BENCH_KEY, step, API_BENCH_STEPS[step - 1])

    url = ("https://www.youtube.com/results?search_query="
           + urllib.parse.quote(query) + "&sp=" + SEARCH_PARAMS)   # videos only
    try:
        raw = http_get(url, timeout=15,
                       headers={"Accept-Language": "en-US,en;q=0.9"})
        html = raw.decode("utf-8", "replace")
    except Exception:
        return None
    m = _CV_RE.search(html)
    if m and m.group(1) != cv:
        # Learned from the page we were fetching anyway, so the endpoint above
        # keeps working across YouTube's own version bumps.
        cache_put(CV_KEY, m.group(1), 7 * 86400)
        cache_del(API_BENCH_KEY)
    data = _yt_initial_data(html)
    if data is None:
        return None
    try:
        return _search_rows(data, n)
    except Exception:
        # The docstring above promises that any failure falls back to yt-dlp.
        # It was only true for a page that failed to parse as JSON: a shape
        # change inside the renderers - a run that is a bare string, a field
        # that is suddenly a number - raised straight past the fallback and
        # out of `yt search` as a traceback.
        return None


def _search_rows(data, n):
    found = []
    _walk_renderers(data, "videoRenderer", found)
    now = int(time.time())
    rows, seen = [], set()
    for v in found:
        if not isinstance(v, dict):
            continue
        vid = v.get("videoId") or ""
        if not isinstance(vid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
            continue
        if vid in seen:
            continue
        try:
            rows.append(_search_row(v, vid, now))
        except Exception:
            # One malformed renderer should cost one result, not the search.
            continue
        seen.add(vid)
        if len(rows) >= n:
            break
    # A page that parsed but yielded almost nothing means the shape changed.
    if len(rows) < max(3, min(5, n)):
        return None
    # So does a page full of rows with no title: the ids are still where they
    # were but the text has moved, and handing back a screen of "(untitled)"
    # is worse than spending 3s on yt-dlp.
    named = sum(1 for r in rows if r["title"] != "(untitled)")
    return rows if named * 2 >= len(rows) else None


def _search_row(v, vid, now):
    """One videoRenderer -> one row. Raises on a shape it does not know."""
    dur = 0
    dm = _DUR_RE.match(_yt_text(v.get("lengthText")).strip())
    if dm:
        h, mi, sec = dm.groups()
        dur = int(h or 0) * 3600 + int(mi) * 60 + int(sec)
    views = 0
    vt = _yt_text(v.get("viewCountText")).replace(",", "")
    vm = re.search(r"(\d+)", vt)
    if vm:
        views = int(vm.group(1))
    published = 0
    am = _AGO_RE.search(_yt_text(v.get("publishedTimeText")))
    if am:
        published = now - int(am.group(1)) * _AGO_SECS[am.group(2).lower()]
    owner = v.get("ownerText") or v.get("longBylineText") or {}
    cid = ""
    try:
        cid = (owner["runs"][0]["navigationEndpoint"]["browseEndpoint"]
               ["browseId"]) or ""
    except (KeyError, IndexError, TypeError):
        pass
    return {
        "id": vid,
        "title": clean(_yt_text(v.get("title")) or "(untitled)"),
        "channel": clean(_yt_text(owner)),
        "channel_id": cid,
        "handle": "",
        "duration": dur,
        "views": views,
        "published": published,
        "is_short": 1 if (0 < dur <= 60) else 0,
        "description": clean_multiline(_snippet(v)),
    }


def _search_stale(key, n):
    """Rows past their TTL, served now with a refresh started behind them.

    Returns None - so the caller fetches synchronously as it always did -
    when there is nothing usable, when the window is switched off, or when
    the rate-limit guard is unhappy. The refresh is one request, the same one
    that would otherwise have been made in the foreground, so this spends
    nothing extra; the marker only stops a burst of identical searches
    spawning a refresh each.
    """
    hours = cfg_int("search_stale_hours", 24)
    if hours <= 0:
        return None
    rows = cache_get_stale(key, hours * 3600, n)
    if not rows:
        return None
    if at_risk():
        return rows           # usable, and this is no time to go asking
    if cache_get(f"ref:{key}") is None:
        cache_put(f"ref:{key}", 1, max(60, cfg_int("search_ttl", 600)))
        _spawn_refresh(key)
    return rows


def _spawn_refresh(key):
    """Re-run this search in the background, detached from the picker.

    A whole 15 ms interpreter to save 600 ms of waiting, and it is gone
    before fzf has drawn the list.
    """
    query = key.split(":", 2)[-1]
    if not query:
        return
    try:
        subprocess.Popen(
            [sys.executable, "-S",
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "ytmain.py"),
             "search", "--refresh", query],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def _snippet(v):
    snips = v.get("detailedMetadataSnippets")
    if not isinstance(snips, list) or not snips or not isinstance(snips[0], dict):
        return ""
    return _yt_text(snips[0].get("snippetText"))


def cmd_search(argv):
    refresh = "--refresh" in argv
    argv = [a for a in argv if a != "--refresh"]
    query = " ".join(argv).strip()
    if not query:
        eprint("yt: empty search")
        return 2
    n = cfg_int("search_count", 40)
    key = f"search:{n}:{query.lower()}"

    rows = None if refresh else rows_cache_get(key)
    if rows is None and not refresh:
        # Nothing here is slow except YouTube. Measured end to end: 626 ms
        # cold against 25 ms warm, and of the 626 about 500 is YouTube
        # generating the page - it streams the result out as it builds it, so
        # neither a smaller request (the InnerTube API is 4.4x less on the
        # wire and 13% faster) nor better compression (brotli is 2.4x smaller
        # and *slower*) buys anything. Parsing the 1.4 MB page is 7 ms.
        #
        # So the only thing left is not waiting for it. Past the ten-minute
        # TTL, hand back what we already had and go and get the new copy
        # behind you: search results for a phrase you have typed before do
        # not change in a way worth 600 ms of staring at a blank terminal.
        # `^r` still forces a live one.
        rows = _search_stale(key, n)

    if rows is None:
        # Anonymous first - this is the user's explicit preference and it
        # keeps the account entirely out of ordinary searching.
        # Fast path first; yt-dlp remains the fallback for when YouTube
        # changes the page shape.
        rows = search_fast(query, n)
        err = ""
        if rows:
            log_req("anon:search", ok=True)
        else:
            rows, err = ytdlp_entries([f"ytsearch{n}:{query}"], which=None, timeout=90)
        if not rows and cookie_file("alt"):
            eprint("yt: anonymous search failed, retrying with alt account")
            rows, err = ytdlp_entries([f"ytsearch{n}:{query}"],
                                      which="alt", kind="search", timeout=90)
        if not rows:
            eprint(f"yt: search failed - {err or 'no results'}")
            return 1
        rows_cache_put(key, rows, cfg_int("search_ttl", 600))
        upsert_videos(rows)

    emit(rows, "search")
    return 0


# ---- home feed --------------------------------------------------------

def sub_ids():
    return [r["channel_id"] for r in
            db().execute("SELECT channel_id FROM subs WHERE muted=0")]


def home_subs(refresh=False, limit=None):
    ids = sub_ids()
    if not ids:
        return None, "no subscriptions yet - run `yt subs import`"
    key = "home:subs"
    rows = None if refresh else rows_cache_get(key, limit)
    if rows is None:
        rows, failed = fetch_subs_feed(ids)
        if not rows:
            return None, "no RSS results (network down?)"
        # Descriptions reach `videos` through upsert_videos and come back via
        # enrich(), so a second copy in the cache blob is ~900 KB of JSON that
        # is parsed on every warm render and never read.
        # A feed missing channels must not be cached as if it were complete:
        # a blip would otherwise hide part of the subscriptions for a full
        # rss_ttl with nothing to indicate it. Cache it briefly so a retry is
        # cheap, and say what happened.
        ttl = cfg_int("rss_ttl", 900) if not failed else 60
        # rows_encode drops description by not having a column for it; it
        # comes back through enrich() from the videos table either way.
        rows_cache_put(key, rows, ttl)
        upsert_videos(rows)
        if failed:
            return rows, f"{failed} of {len(ids)} channel feeds failed to load"
    return rows, ""


def home_rec(refresh=False, limit=None):
    """YouTube's own recommendations. Authenticated, so heavily gated."""
    if not cookie_file("main"):
        return None, "no main-account cookies (run `yt auth sync main`)"
    key = "home:rec"
    rows = None if refresh else rows_cache_get(key, limit)
    if rows is not None:
        return rows, ""
    ok, why = can_auth("auth:rec")
    if not ok:
        stale = cache_get_stale(key, limit=limit)
        if stale:
            return stale, f"stale cache ({why})"
        return None, why
    if not refresh:
        # The expensive branch: an authenticated :ytrec extraction, measured at
        # 5.5s. With rec_ttl at 30 minutes that lands on the first `yt` of most
        # sessions, and it is the whole reason opening the feed feels slow.
        # Yesterday's recommendations are a fine thing to look at for the two
        # seconds it takes to fetch today's, so serve the stale copy and go get
        # the fresh one in a detached child.
        #
        # Only for the recommended feed: it has no user-driven invalidation
        # (nothing like `yt subs rm`), so a refresh landing after the fact
        # cannot resurrect something the user just removed.
        stale = cache_get_stale(key, limit=limit)
        if stale and spawn_rec_refresh():
            return stale, "refreshing in the background"
    n = cfg_int("feed_count", 60)
    rows, err = ytdlp_entries([":ytrec", "-I", f"1:{n}"],
                              which="main", kind="rec", timeout=120)
    if not rows:
        stale = cache_get_stale(key, limit=limit)
        if stale:
            return stale, f"stale cache ({err})"
        return None, err or "recommended feed empty"
    rows_cache_put(key, rows, cfg_int("rec_ttl", 1800))
    upsert_videos(rows)
    return rows, ""


REC_REFRESH_LOCK = "rec_refreshing"


def spawn_rec_refresh():
    """Detach a recommended-feed refresh. Returns False if one is already
    running, so a burst of `yt` in one minute cannot start a pile of them."""
    now = int(time.time())
    last = kv_int(REC_REFRESH_LOCK, 0)
    if now - last < 180:            # a refresh takes ~6s; 3 min is generous
        return True                 # already in flight - still serve stale
    kv_set(REC_REFRESH_LOCK, now)
    try:
        subprocess.Popen(
            [sys.executable, _launcher(), "rec-refresh"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        kv_set(REC_REFRESH_LOCK, 0)
        return False


def cmd_rec_refresh(argv):
    """Background half of the stale-while-revalidate above."""
    try:
        home_rec(refresh=True)
    finally:
        kv_set(REC_REFRESH_LOCK, 0)
    return 0


def cache_get_stale(key, max_age=86400, limit=None):
    """Expired-but-usable rows, for when the guard says don't fetch."""
    r = db().execute("SELECT payload, ts FROM cache WHERE key=?", (key,)).fetchone()
    if not r or time.time() - r["ts"] > max_age:
        return None
    payload = r["payload"] or ""
    if payload[:1] in ("[", "{"):
        try:
            rows = json.loads(payload)
        except ValueError:
            return None
        return rows[:limit] if limit else rows
    return rows_decode(payload, limit) or None


def maybe_trim_db():
    """Cheap guard so the database cannot creep past its cap between gc runs.

    Called from the feed render, so it runs again on every reload the picker
    does. It used to checkpoint the WAL to get an exact size - a write, an
    exclusive lock and an fsync, taken to ask a question whose answer is "no"
    every time until the database is ten times its present size. Worse, it
    undid WAL mode on the spot: truncating the log on every read means every
    write after it grows the log again from nothing, and the download worker
    committing in the background had a foreground render to wait for.

    So: measure by stat first, and only pay for an accurate figure once the
    cheap over-estimate says it might matter.
    """
    try:
        cap = cfg_int("db_max_mb", 500)
        if db_size_mb(checkpoint=False) > cap and db_size_mb() > cap:
            enforce_db_budget()
    except sqlite3.Error:
        pass


def cmd_home(argv):
    refresh = "--refresh" in argv
    want = cfg("home_mode")
    for a in argv:
        if a in ("--subs", "--rss"):
            want = "subs"
        elif a == "--rec":
            want = "rec"
        elif a == "--auto":
            want = "auto"

    count = cfg_int("feed_count", 60)
    # How many cached rows are actually needed to draw `count` of them. With a
    # shorts filter on, any row can be dropped, so there is no answer short of
    # all of them; otherwise the feed is already in display order and the rest
    # of the blob is never looked at.
    hint = None if cfg("shorts") in ("hide", "only") else count

    rows, note, used = None, "", ""
    if want in ("rec", "auto"):
        if at_risk() and want == "auto":
            note = "risk backoff active"
        else:
            rows, note = home_rec(refresh, hint)
            used = "rec"
    if rows is None:
        # Even an explicit --rec falls back rather than leaving you with
        # nothing: protecting the account outranks honouring the flag.
        rows, n2 = home_subs(refresh, hint)
        used = "subs"
        if note and rows is not None:
            eprint(f"yt: recommended unavailable ({note}); using subscriptions")
        note = note or n2
    if rows is None:
        eprint(f"yt: home unavailable - {note}")
        return 1

    kv_set("home_last_mode", used)
    emit(rows, "home", limit=count)
    maybe_trim_db()
    maybe_resume_downloads()
    return 0


# ---- library ----------------------------------------------------------

def row_from_db(r):
    keys = r.keys()
    return {"id": r["id"], "title": r["title"], "channel": r["channel"],
            "channel_id": r["channel_id"], "duration": r["duration"],
            "views": r["views"], "published": r["published"],
            "is_short": r["is_short"],
            "description": r["description"] if "description" in keys else ""}


def cmd_lib(argv):
    """Browse the local library. This is our watch-later; YouTube's is untouched."""
    cat, query, order = None, None, "added"
    show_archived = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-c", "--category") and i + 1 < len(argv):
            cat = argv[i + 1]; i += 1
        elif a in ("-q", "--query") and i + 1 < len(argv):
            query = argv[i + 1]; i += 1
        elif a == "--archived":
            show_archived = True
        elif a in ("--sort",) and i + 1 < len(argv):
            order = argv[i + 1]; i += 1
        elif not a.startswith("-") and cat is None:
            cat = a
        i += 1

    order_sql = {"added": "s.added DESC", "old": "s.added ASC",
                 "title": "COALESCE(v.title,'') COLLATE NOCASE",
                 "channel": "COALESCE(v.channel,'') COLLATE NOCASE, s.added DESC",
                 "duration": "COALESCE(v.duration,0) DESC",
                 "priority": "s.priority DESC, s.added DESC"}.get(order, "s.added DESC")

    params, where = [], ["s.archived = ?"]
    params.append(1 if show_archived else 0)
    if cat and cat not in ("all", "*"):
        where.append("s.category = ?")
        params.append(cat)

    ids = None
    if query:
        # An all-punctuation query tokenises to nothing, and MATCH '' is a
        # syntax error in FTS5 - fall straight through to LIKE instead.
        if has_fts() and fts_query(query):
            try:
                ids = [r["video_id"] for r in db().execute(
                    "SELECT video_id FROM saved_fts WHERE saved_fts MATCH ?",
                    (fts_query(query),))]
            except sqlite3.OperationalError:
                ids = None
        if ids is None:
            like = f"%{query}%"
            ids = [r["video_id"] for r in db().execute(
                "SELECT s.video_id FROM saved s JOIN videos v ON v.id=s.video_id "
                "WHERE v.title LIKE ? OR v.channel LIKE ? OR s.note LIKE ?",
                (like, like, like))]
        if not ids:
            eprint(f"yt: nothing in the library matches '{query}'")
            return 1
        where.append(f"s.video_id IN ({','.join('?' * len(ids))})")
        params.extend(ids)

    rows = [row_from_db(r) for r in db().execute(
        f"SELECT s.video_id AS id, COALESCE(v.title,'') title, "
        f"COALESCE(v.channel,'') channel, COALESCE(v.channel_id,'') channel_id, "
        f"COALESCE(v.duration,0) duration, COALESCE(v.views,0) views, "
        f"COALESCE(v.published,0) published, COALESCE(v.is_short,0) is_short, "
        f"COALESCE(v.description,'') description "
        f"FROM saved s LEFT JOIN videos v ON v.id = s.video_id "
        f"WHERE {' AND '.join(where)} ORDER BY {order_sql}", params)]
    if not rows:
        eprint("yt: library is empty" + (f" for category '{cat}'" if cat else ""))
        return 1
    emit(rows, "lib")
    return 0


def fts_query(q):
    """Make user text safe for FTS5 MATCH: quote each token, prefix-match."""
    toks = re.findall(r"\w+", q, re.UNICODE)
    return " ".join(f'"{t}"*' for t in toks)


def reindex_fts(vid=None):
    if not has_fts():
        return
    if vid:
        db().execute("DELETE FROM saved_fts WHERE video_id=?", (vid,))
        rows = db().execute(
            "SELECT s.video_id, v.title, v.channel, s.note, s.category "
            "FROM saved s JOIN videos v ON v.id=s.video_id WHERE s.video_id=?", (vid,))
    else:
        db().execute("DELETE FROM saved_fts")
        rows = db().execute(
            "SELECT s.video_id, v.title, v.channel, s.note, s.category "
            "FROM saved s JOIN videos v ON v.id=s.video_id")
    db().executemany(
        "INSERT INTO saved_fts(video_id,title,channel,note,category) "
        "VALUES(?,?,?,?,?)", [tuple(r) for r in rows])
    db().commit()


# ---- categories -------------------------------------------------------

def ensure_category(name):
    name = (name or "unsorted").strip() or "unsorted"
    db().execute("INSERT OR IGNORE INTO categories(name,created) VALUES(?,?)",
                 (name, int(time.time())))
    db().commit()
    return name


def suggest_category(vid):
    """Guess from your own filing history. Deterministic - no model involved.

    Strongest signal first: what you filed from this exact channel before,
    then this channel's most recent filing, then your overall favourite.
    """
    v = get_video(vid)
    if v and v["channel_id"]:
        r = db().execute(
            "SELECT s.category, COUNT(*) n, MAX(s.added) recent FROM saved s "
            "JOIN videos vv ON vv.id = s.video_id "
            "WHERE vv.channel_id = ? GROUP BY s.category "
            "ORDER BY n DESC, recent DESC LIMIT 1", (v["channel_id"],)).fetchone()
        if r:
            return r["category"]
    r = db().execute(
        "SELECT category, COUNT(*) n FROM saved GROUP BY category "
        "ORDER BY n DESC LIMIT 1").fetchone()
    return r["category"] if r else "unsorted"


def cmd_cat(argv):
    sub = argv[0] if argv else "list"
    rest = argv[1:]
    if sub in ("list", "ls"):
        rows = db().execute(
            "SELECT c.name, COUNT(s.video_id) n FROM categories c "
            "LEFT JOIN saved s ON s.category=c.name AND s.archived=0 "
            "GROUP BY c.name ORDER BY c.sort, c.name").fetchall()
        for r in rows:
            print(f"{r['name']}\tcat\t{pad(r['name'], 24)} {r['n']:>4} saved")
        return 0
    if sub == "names":
        for r in db().execute("SELECT name FROM categories ORDER BY sort, name"):
            print(r["name"])
        return 0
    if sub == "add" and rest:
        ensure_category(rest[0])
        print(f"yt: category '{rest[0]}' ready")
        return 0
    if sub in ("rm", "del") and rest:
        name = rest[0]
        n = db().execute("SELECT COUNT(*) c FROM saved WHERE category=?",
                         (name,)).fetchone()["c"]
        db().execute("UPDATE saved SET category='unsorted' WHERE category=?", (name,))
        db().execute("DELETE FROM categories WHERE name=?", (name,))
        db().commit()
        reindex_fts()
        print(f"yt: removed '{name}' ({n} moved to unsorted)")
        return 0
    if sub == "rename" and len(rest) >= 2:
        old, new = rest[0], ensure_category(rest[1])
        db().execute("UPDATE saved SET category=? WHERE category=?", (new, old))
        db().execute("DELETE FROM categories WHERE name=?", (old,))
        db().commit()
        reindex_fts()
        print(f"yt: '{old}' -> '{new}'")
        return 0
    eprint("usage: yt cat [list|names|add NAME|rm NAME|rename OLD NEW]")
    return 2


# ---- saving -----------------------------------------------------------

def save_video(vid, category=None, note="", priority=0):
    category = ensure_category(category or suggest_category(vid))
    now = int(time.time())
    existing = db().execute("SELECT 1 FROM saved WHERE video_id=?", (vid,)).fetchone()
    db().execute("""
      INSERT INTO saved(video_id,category,note,added,priority,archived)
      VALUES(?,?,?,?,?,0)
      ON CONFLICT(video_id) DO UPDATE SET
        category=excluded.category,
        note=CASE WHEN excluded.note != '' THEN excluded.note ELSE saved.note END,
        archived=0
    """, (vid, category, note, now, priority))
    db().commit()
    reindex_fts(vid)
    return bool(existing), category


def cmd_save(argv):
    category, note, ids = None, "", []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-c", "--category") and i + 1 < len(argv):
            category = argv[i + 1]; i += 1
        elif a in ("-n", "--note") and i + 1 < len(argv):
            note = argv[i + 1]; i += 1
        else:
            v = parse_video_id(a)
            if v:
                ids.append(v)
            else:
                eprint(f"yt: not a video URL or id: {a}")
        i += 1
    if not ids:
        eprint("yt: nothing to save")
        return 2
    fetch_video_meta(ids)
    dupes, unknown = [], []
    for vid in ids:
        was, cat = save_video(vid, category, note)
        v = get_video(vid)
        if not (v and v["title"]):
            unknown.append(vid)
        title = clip((v["title"] if v else "") or f"({vid} - metadata unavailable)", 60)
        if was:
            dupes.append(title)
            print(f"yt: updated  [{cat}] {title}")
        else:
            print(f"yt: saved    [{cat}] {title}")
    if dupes:
        eprint(f"yt: {len(dupes)} already in the library (updated in place)")
    if unknown:
        eprint(f"yt: {len(unknown)} saved without metadata (deleted, private, or "
               f"network down): {', '.join(unknown)}")
        eprint("yt: run `yt refresh` to retry, or `yt unsave ID` to drop them")
    return 0


def cmd_unsave(argv):
    n = 0
    for a in argv:
        vid = parse_video_id(a)
        if not vid:
            continue
        db().execute("DELETE FROM saved WHERE video_id=?", (vid,))
        if has_fts():
            db().execute("DELETE FROM saved_fts WHERE video_id=?", (vid,))
        n += 1
    db().commit()
    print(f"yt: removed {n} from library")
    return 0


def cmd_note(argv):
    """Read or set a note. Fish drives $EDITOR; we just persist."""
    if not argv:
        return 2
    vid = parse_video_id(argv[0])
    if not vid:
        eprint("yt: bad video id")
        return 2
    if len(argv) == 1:
        r = db().execute("SELECT note FROM saved WHERE video_id=?", (vid,)).fetchone()
        print(r["note"] if r else "")
        return 0
    note = " ".join(argv[1:])
    db().execute("UPDATE saved SET note=? WHERE video_id=?", (note, vid))
    db().commit()
    reindex_fts(vid)
    return 0


def cmd_recat(argv):
    if len(argv) < 2:
        return 2
    cat = ensure_category(argv[-1])
    ids = [parse_video_id(a) for a in argv[:-1]]
    ids = [v for v in ids if v]
    db().executemany("UPDATE saved SET category=? WHERE video_id=?",
                     [(cat, v) for v in ids])
    db().commit()
    for v in ids:
        reindex_fts(v)
    print(f"yt: moved {len(ids)} to '{cat}'")
    return 0


# ---- subscriptions ----------------------------------------------------

CID_RE = _Rx("CID_RE", r"^UC[A-Za-z0-9_-]{22}$")


def add_sub(channel_id, name="", handle=""):
    db().execute(
        "INSERT INTO subs(channel_id,name,handle,added) VALUES(?,?,?,?) "
        "ON CONFLICT(channel_id) DO UPDATE SET "
        "name=CASE WHEN excluded.name!='' THEN excluded.name ELSE subs.name END,"
        "handle=CASE WHEN excluded.handle!='' THEN excluded.handle ELSE subs.handle END",
        (channel_id, clean(name), handle.lstrip("@"), int(time.time())))
    db().commit()


def resolve_channel(spec):
    """@handle / URL / UC... -> (channel_id, name). One yt-dlp call at most."""
    spec = spec.strip()
    if CID_RE.match(spec):
        return spec, ""
    m = re.search(r"(UC[A-Za-z0-9_-]{22})", spec)
    if m:
        return m.group(1), ""
    # Local first: if we have ever seen this channel, no network at all.
    handle = spec.lstrip("@").strip()
    m = re.search(r"/@([\w.-]+)", spec)
    if m:
        handle = m.group(1)
    if handle:
        r = db().execute(
            "SELECT channel_id, name FROM subs WHERE handle=? COLLATE NOCASE "
            "OR name=? COLLATE NOCASE LIMIT 1", (handle, handle)).fetchone()
        if r:
            return r["channel_id"], r["name"]
        r = db().execute(
            "SELECT channel_id, channel FROM videos WHERE channel_id != '' AND "
            "(handle=? COLLATE NOCASE OR channel=? COLLATE NOCASE) LIMIT 1",
            (handle, handle)).fetchone()
        if r:
            return r["channel_id"], r["channel"]

    if spec.startswith("@"):
        url = f"https://www.youtube.com/{spec}"
    elif spec.startswith("http"):
        url = spec.rstrip("/")
        for suffix in ("/videos", "/streams", "/shorts", "/featured"):
            if url.endswith(suffix):
                url = url[:-len(suffix)]
                break
    else:
        url = f"https://www.youtube.com/@{spec}"
    # Playlist-level metadata carries channel_id; the flat entries do not.
    rc, out, err = run_ytdlp(
        [url + "/videos", "--flat-playlist", "--dump-single-json", "-I", "1:1"],
        timeout=90)
    try:
        d = json.loads((out or "").strip() or "{}")
    except ValueError:
        d = {}
    cid = d.get("channel_id") or d.get("id") or ""
    if CID_RE.match(cid):
        return cid, clean(d.get("channel") or d.get("uploader") or "")
    tail = (err or "could not resolve").strip().splitlines()
    return None, (tail[-1] if tail else "could not resolve")


def cmd_subs(argv):
    sub = argv[0] if argv else "list"
    rest = argv[1:]

    if sub in ("list", "ls"):
        rows = db().execute(
            "SELECT * FROM subs ORDER BY muted, name COLLATE NOCASE").fetchall()
        for r in rows:
            flag = "muted" if r["muted"] else ""
            print(f"{r['channel_id']}\tsub\t{pad(r['name'] or r['channel_id'], 40)} "
                  f"{pad('@' + r['handle'] if r['handle'] else '', 24)} {flag}")
        if not rows:
            eprint("yt: no subscriptions - `yt subs import` or `yt subs add @name`")
        return 0

    if sub == "add" and rest:
        n = 0
        for spec in rest:
            cid, name = resolve_channel(spec)
            if not cid:
                eprint(f"yt: could not resolve '{spec}' - {name}")
                continue
            add_sub(cid, name, spec if spec.startswith("@") else "")
            print(f"yt: subscribed to {name or cid}")
            n += 1
        if n:
            db().execute("DELETE FROM cache WHERE key='home:subs'")
            db().commit()
        return 0 if n else 1

    if sub in ("rm", "del") and rest:
        for spec in rest:
            cid = spec if CID_RE.match(spec) else (resolve_channel(spec)[0] or "")
            db().execute("DELETE FROM subs WHERE channel_id=?", (cid,))
        db().commit()
        db().execute("DELETE FROM cache WHERE key='home:subs'")
        db().commit()
        print("yt: unsubscribed")
        return 0

    if sub in ("mute", "unmute") and rest:
        val = 1 if sub == "mute" else 0
        for spec in rest:
            cid = spec if CID_RE.match(spec) else (resolve_channel(spec)[0] or "")
            db().execute("UPDATE subs SET muted=? WHERE channel_id=?", (val, cid))
        db().commit()
        db().execute("DELETE FROM cache WHERE key='home:subs'")
        db().commit()
        return 0

    if sub == "import":
        src = rest[0] if rest else ""
        if src and os.path.exists(src):
            return import_subs_csv(src)
        return import_subs_account()

    eprint("usage: yt subs [list|add SPEC|rm SPEC|mute SPEC|unmute SPEC|import [CSV]]")
    return 2


def import_subs_csv(path):
    """Google Takeout subscriptions.csv - fully offline, zero account risk."""
    import csv
    n = 0
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                cid = ""
                name = ""
                for k, v in row.items():
                    kl = (k or "").strip().lower()
                    if "channel id" in kl:
                        cid = (v or "").strip()
                    elif "title" in kl:
                        name = (v or "").strip()
                if CID_RE.match(cid):
                    add_sub(cid, name)
                    n += 1
    except OSError as e:
        eprint(f"yt: cannot read {path} - {e}")
        return 1
    db().execute("DELETE FROM cache WHERE key='home:subs'")
    db().commit()
    print(f"yt: imported {n} subscriptions from {path}")
    return 0 if n else 1


def import_subs_account():
    if not cookie_file("main"):
        eprint("yt: no main-account cookies. Either `yt auth sync main`, or "
               "import a Google Takeout subscriptions.csv with `yt subs import FILE`.")
        return 1
    ok, why = can_auth("auth:subs")
    if not ok:
        eprint(f"yt: holding off - {why}")
        return 1
    rc, out, err = run_ytdlp(
        ["https://www.youtube.com/feed/channels", "--flat-playlist", "--dump-json"],
        which="main", kind="subs", timeout=180)
    n = 0
    for line in (out or "").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        cid = d.get("channel_id") or d.get("id") or ""
        if CID_RE.match(cid):
            add_sub(cid, clean(d.get("title") or d.get("channel") or ""),
                    (d.get("uploader_id") or "").lstrip("@"))
            n += 1
    if not n:
        eprint(f"yt: import found nothing - {(err or '').strip()[:200]}")
        return 1
    db().execute("DELETE FROM cache WHERE key='home:subs'")
    db().commit()
    print(f"yt: imported {n} subscriptions")
    return 0


# ---- watch tracking ---------------------------------------------------

WL_DIR = os.path.join(DATA_DIR, "watchlater")


def cmd_playstart(argv):
    now = int(time.time())
    for a in argv:
        vid = parse_video_id(a)
        if not vid:
            continue
        db().execute("""
          INSERT INTO watch(video_id,position,duration,watched,last_played,play_count)
          VALUES(?,0,0,0,?,1)
          ON CONFLICT(video_id) DO UPDATE SET
            last_played=excluded.last_played, play_count=watch.play_count+1
        """, (vid, now))
    db().commit()
    if argv:
        kv_set("last_played_id", parse_video_id(argv[0]) or "")
    return 0


def read_watchlater():
    """mpv's own resume files. No polling, no IPC - it already wrote this."""
    out = {}
    try:
        names = os.listdir(WL_DIR)
    except OSError:
        return out
    for name in names:
        p = os.path.join(WL_DIR, name)
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                txt = fh.read(4096)
        except OSError:
            continue
        if txt.startswith("# redirect entry"):
            continue          # mpv writes one of these per parent directory
        vid = None
        start = 0.0
        for line in txt.splitlines():
            if line.startswith("#"):
                v = parse_video_id(line[1:].strip())
                if v:
                    vid = v
            elif line.startswith("start="):
                try:
                    v = float(line.partition("=")[2])
                except ValueError:
                    continue
                # float() happily parses "inf" and "nan". A resume position of
                # inf compares >= any duration, so one corrupt resume file
                # would mark that video watched and put a non-finite number in
                # the database for every later read to format.
                if v == v and -1e12 < v < 1e12:
                    start = max(0.0, v)
        if vid:
            out[vid] = start
    return out


# How much of what was left to watch the player has to have been up for,
# before "no resume file" is allowed to mean "watched it to the end". Well
# under 1.0 because a paused player, a seek forward or --speed all make the
# wall clock disagree with the timeline; the job here is only to tell a
# finished video from a window that opened and closed again.
PLAYED_ENOUGH_FRAC = 0.5
# ...and when the duration is not known, this instead.
PLAYED_ENOUGH_SECS = 15.0


def cmd_playend(argv):
    """Reconcile after mpv exits.

    mpv writes a resume file when you quit part-way and deletes it when a
    file plays to the end - so absence is the completion signal. Absence is
    also what a player that never started leaves behind, though, and those
    are not rare: a stream that failed, a window closed in the first second,
    a wrong keypress. Every one of them was being recorded as fully watched.

    So the caller passes how long the player was actually up, as `id=seconds`,
    and a run too short to have reached the end is left alone instead - only
    `last_played` moves. A bare id still means "no idea", which is what every
    older caller sends and reads the way it always did.
    """
    positions = read_watchlater()
    now = int(time.time())
    for a in argv:
        ran = None
        if "=" in a:
            head, _, tail = a.rpartition("=")
            if head and tail.isdigit():
                a, ran = head, float(tail)
        vid = parse_video_id(a)
        if not vid:
            continue
        v = get_video(vid)
        dur = float(v["duration"]) if v and v["duration"] else 0.0
        if vid in positions:
            pos = positions[vid]
            done = 1 if (dur and pos >= dur * WATCHED_FRAC) else 0
            db().execute(
                "UPDATE watch SET position=?, duration=?, watched=?, last_played=? "
                "WHERE video_id=?", (pos, dur, done, now, vid))
            continue
        if ran is not None and not _played_enough(vid, dur, ran):
            # Nothing is known about where it got to, so nothing about where
            # it got to is written. This row keeps whatever the last real
            # playback left in it.
            db().execute("UPDATE watch SET last_played=? WHERE video_id=?",
                         (now, vid))
            continue
        db().execute(
            "UPDATE watch SET position=0, duration=?, watched=1, last_played=? "
            "WHERE video_id=?", (dur, now, vid))
    db().commit()
    return 0


def _played_enough(vid, dur, ran):
    """Was the player up long enough to have finished what was left?"""
    if dur <= 0:
        return ran >= PLAYED_ENOUGH_SECS
    row = db().execute("SELECT position FROM watch WHERE video_id=?",
                       (vid,)).fetchone()
    # Resuming three seconds from the end and letting it finish is a real
    # completion with a three second run, so what counts is what was left.
    left = dur - float(row["position"] or 0.0) if row else dur
    return ran >= max(0.0, left) * PLAYED_ENOUGH_FRAC


def cmd_continue(argv):
    """Partly-watched things, most recent first."""
    rows = [row_from_db(r) for r in db().execute("""
      SELECT v.* FROM watch w JOIN videos v ON v.id = w.video_id
      WHERE w.watched = 0
        AND w.position >= ?
        AND (w.position >= ?
             OR (v.duration > 0 AND w.position >= v.duration * ?))
        AND (v.duration = 0 OR w.position < v.duration * ?)
      ORDER BY w.last_played DESC LIMIT 100""",
      (RESUME_FLOOR, RESUME_MIN_SECS, RESUME_MIN_FRAC, WATCHED_FRAC))]
    if not rows:
        eprint("yt: nothing part-watched")
        return 1
    emit(rows, "continue")
    return 0


def cmd_history(argv):
    """Everything you have played, most recent first."""
    limit = 500
    for i, a in enumerate(argv):
        if a in ("-n", "--limit") and i + 1 < len(argv):
            try:
                limit = max(1, int(argv[i + 1]))
            except ValueError:
                pass
    rows = [row_from_db(r) for r in db().execute("""
      SELECT v.* FROM watch w JOIN videos v ON v.id = w.video_id
      WHERE w.last_played > 0 ORDER BY w.last_played DESC LIMIT ?""", (limit,))]
    if not rows:
        eprint("yt: no local playback history yet")
        return 1
    emit(rows, "history")
    return 0


def cmd_resumepos(argv):
    """Seconds to resume at, for fish to hand mpv as --start."""
    if not argv:
        return 2
    vid = parse_video_id(argv[0])
    r = db().execute(
        "SELECT w.position, w.watched, COALESCE(v.duration,0) dur FROM watch w "
        "LEFT JOIN videos v ON v.id = w.video_id WHERE w.video_id=?",
        (vid,)).fetchone()
    ok = r and not r["watched"] and resumable(r["position"], r["dur"])
    print(int(r["position"]) if ok else 0)
    return 0


def cmd_togglewatched(argv):
    """Flip watched state. Anything already watched becomes unwatched again,
    so the same key both marks and un-marks."""
    now = int(time.time())
    flipped = []
    for a in argv:
        vid = parse_video_id(a)
        if not vid:
            continue
        r = db().execute("SELECT watched FROM watch WHERE video_id=?",
                         (vid,)).fetchone()
        new = 0 if (r and r["watched"]) else 1
        db().execute("""
          INSERT INTO watch(video_id,position,duration,watched,last_played,play_count)
          VALUES(?,0,0,?,?,0)
          ON CONFLICT(video_id) DO UPDATE SET
            watched=excluded.watched,
            position=CASE WHEN excluded.watched=1 THEN 0 ELSE watch.position END,
            last_played=CASE WHEN watch.last_played>0
                             THEN watch.last_played ELSE excluded.last_played END
        """, (vid, new, now))
        flipped.append((vid, new))
    db().commit()
    on = sum(1 for _v, n in flipped if n)
    print(f"yt: {on} marked watched, {len(flipped) - on} un-marked")
    return 0


def cmd_markwatched(argv):
    now = int(time.time())
    for a in argv:
        vid = parse_video_id(a)
        if not vid:
            continue
        db().execute("""
          INSERT INTO watch(video_id,position,duration,watched,last_played,play_count)
          VALUES(?,0,0,1,?,0)
          ON CONFLICT(video_id) DO UPDATE SET watched=1, position=0,
            last_played=excluded.last_played""", (vid, now))
    db().commit()
    print(f"yt: marked {len(argv)} watched")
    return 0


# ---- downloads --------------------------------------------------------

def safe_dirname(cat):
    """A category name turned into one safe path component.

    Categories are free text, and downloads are filed under them. Replacing
    everything outside [word . - space] means no separator survives, but dots
    are legal in a directory name and a category called ".." would have filed
    downloads into the PARENT of video_dir.
    """
    safe = re.sub(r"[^\w.\- ]+", "_", cat or "").strip()
    if not safe or safe.strip(".") == "":
        return "unsorted"
    return safe


def managed_root():
    """The folder yt files downloads into and is allowed to reap from."""
    return os.path.realpath(os.path.expanduser(cfg("video_dir")))


def is_managed(path):
    root = managed_root()
    try:
        rp = os.path.realpath(path)
    except OSError:
        return False
    return rp == root or rp.startswith(root + os.sep)


def video_dir_for(vid):
    """Where this video's file goes.

    A queued download may carry a one-off `dest` chosen at queue time. It is
    deliberately stored on the download row and not in the config: pressing
    ^alt-d picks a folder for that download and nothing else, and the next
    ordinary ^d on the same video clears it again.
    """
    r = db().execute("SELECT dest FROM downloads WHERE video_id=?", (vid,)).fetchone()
    dest = (r["dest"] if r else "") or ""
    if dest:
        d = os.path.expanduser(dest)
        try:
            os.makedirs(d, exist_ok=True)
            if os.access(d, os.W_OK):
                return d
            eprint(f"yt: {d} is not writable; using the default folder")
        except OSError as e:
            eprint(f"yt: cannot use {d} ({e}); using the default folder")
    r = db().execute("SELECT category FROM saved WHERE video_id=?", (vid,)).fetchone()
    cat = (r["category"] if r else "unsorted") or "unsorted"
    d = os.path.join(os.path.expanduser(cfg("video_dir")), safe_dirname(cat))
    os.makedirs(d, exist_ok=True)
    return d


# What a byte-ceiling cut-off looks like coming back from yt-dlp. These are
# retryable; a private video or a bad format selector is not.
CUTOFF_RE = _Rx(
    "CUTOFF_RE",
    r"unable to download video data|HTTP Error 403|Stream ends prematurely|"
    r"fragment.*not found|Did not get any data blocks", ignorecase=True)
DL_MAX_ATTEMPTS = 5
DL_BACKOFF_BASE = 1800          # 30 minutes, doubling
DL_BACKOFF_MAX = 14400          # capped at 4 hours

# Live progress lives in one small file per video rather than in the database.
# The worker updates it twice a second for as long as a download runs, and a
# WAL commit at that rate would sit on the write lock that every foreground
# `yt` process needs. A file is a write and a rename; reading one back is a
# stat and a read, cheap enough to do for every row of a rendered list.
DL_DIR = os.path.join(CACHE_DIR, "dl")
DL_STALE = 45            # seconds; past this the worker is gone, not slow
# Eight levels of fill in the one character the status gutter has to spare.
DL_BARS = "▁▁▂▃▄▅▆▇█"


def dl_progress_put(vid, pct, done=0, total=0, speed=0.0, eta=0, phase="video"):
    """Publish one progress tick. Never raises: a full disk or a swept cache
    must not take a download down with it."""
    try:
        os.makedirs(DL_DIR, exist_ok=True)
        tmp = os.path.join(DL_DIR, f".{vid}.{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(f"{pct:.2f}\t{int(done)}\t{int(total)}\t{speed:.0f}\t"
                     f"{int(eta)}\t{phase}\n")
        os.replace(tmp, os.path.join(DL_DIR, vid))
    except (OSError, ValueError, OverflowError):
        pass


def dl_progress_clear(vid):
    try:
        os.unlink(os.path.join(DL_DIR, vid))
    except OSError:
        pass


def dl_progress_all():
    """{video_id: {pct, done, total, speed, eta, phase}} for live downloads.

    A worker killed mid-download leaves its last tick behind, so anything
    older than DL_STALE is ignored rather than shown as a bar frozen at 38%.
    Removing it is left to `yt gc`: a render is not the place to be deleting
    files another process may be about to rewrite.
    """
    out = {}
    try:
        names = os.listdir(DL_DIR)
    except OSError:
        return out
    now = time.time()
    for name in names:
        if name.startswith("."):
            continue
        path = os.path.join(DL_DIR, name)
        try:
            if now - os.stat(path).st_mtime > DL_STALE:
                continue
            # errors="replace": this reads a file another process is rewriting
            # under it, so a half-written multi-byte character is expected,
            # and a UnicodeDecodeError here would take the whole render down.
            with open(path, encoding="utf-8", errors="replace") as fh:
                f = fh.readline().rstrip("\n").split("\t")
        except (OSError, ValueError):
            continue
        if len(f) < 6:
            continue
        try:
            out[name] = {"pct": max(0.0, min(100.0, float(f[0]))),
                         "done": max(0, int(f[1])), "total": max(0, int(f[2])),
                         "speed": max(0.0, float(f[3])), "eta": max(0, int(f[4])),
                         "phase": f[5][:12]}
        except ValueError:
            continue
    return out


def dl_bar(pct, width=20):
    filled = int(round(max(0.0, min(100.0, pct)) * width / 100.0))
    return "█" * filled + "░" * (width - filled)


def due_downloads():
    """Queued downloads whose retry time has arrived."""
    return db().execute(
        "SELECT COUNT(*) c FROM downloads WHERE status='queued' AND next_try <= ?",
        (int(time.time()),)).fetchone()["c"]


def maybe_resume_downloads():
    """Nudge the worker if a paused download is due. Called from the feed
    render, so a retry lands the next time you open yt rather than needing a
    daemon sitting around."""
    try:
        if due_downloads():
            start_worker()
    except sqlite3.Error:
        pass


def dest_suggestions():
    """Folders worth offering when asked where to put a download."""
    out = []
    seen = set()
    for d in (kv_get("dl_last_dest", ""),
              os.path.expanduser(cfg("video_dir")),
              os.path.join(HOME, "Videos"),
              os.path.join(HOME, "Downloads"),
              HOME):
        if not d:
            continue
        d = os.path.abspath(os.path.expanduser(d))
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def validate_dest(raw):
    """(path, error). Creates the folder if it does not exist yet.

    Every answer is a message, never an exception: this is reached from a
    prompt and from `yt dl -o`, and a traceback in place of "that is not a
    folder" is the wrong end of the deal. Strings that the filesystem cannot
    even be asked about - an embedded NUL, an unpaired surrogate off a paste
    - are just another kind of no.
    """
    if not isinstance(raw, str):
        raw = "" if raw is None else str(raw)
    raw = raw.strip()
    # Test this before abspath(): abspath("") is the current directory, so
    # `yt dl -o ""` would otherwise file the download into whatever folder
    # the shell happened to be sitting in.
    if not raw:
        return "", "no folder given"
    if "\x00" in raw:
        return "", "that is not a folder"
    if len(raw) > 4096:
        return "", "that path is too long"     # the message is for a terminal
    try:
        d = os.path.abspath(os.path.expanduser(raw))
        if d == os.sep:
            return "", "that is not a folder"
        if os.path.exists(d) and not os.path.isdir(d):
            return "", f"{d} is a file, not a folder"
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            return "", f"cannot create {d} ({e.strerror or e})"
        if not os.access(d, os.W_OK | os.X_OK):
            return "", f"{d} is not writable"
    except (ValueError, UnicodeError, OSError) as e:
        return "", f"that is not a usable folder ({e})"
    return d, ""


def ask_dest(ids):
    """Prompt for a one-off folder. "" means the caller should give up.

    Runs under fzf's execute(), which hands the terminal over for the length
    of the command, so an ordinary prompt is all this needs. Anything that is
    not a listed number is taken as a path, because typing one is usually
    faster than hunting for it in a list.
    """
    if not sys.stdin.isatty():
        eprint("yt: --ask needs a terminal")
        return ""
    sugg = dest_suggestions()
    # Tag whichever entry is the ordinary destination, wherever it landed in
    # the list - "the second one" stopped being true the first time there was
    # no previous choice to put in front of it.
    normal = os.path.abspath(os.path.expanduser(cfg("video_dir")))
    print()
    print(f"  save {len(ids)} download{'' if len(ids) == 1 else 's'} to:")
    for i, d in enumerate(sugg, 1):
        tag = "  (the usual place)" if d == normal else ""
        print(f"    {i}) {d}{tag}")
    print("    or type a path.  empty cancels.")
    try:
        raw = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    if not raw:
        return ""
    if raw.isdigit() and 1 <= int(raw) <= len(sugg):
        raw = sugg[int(raw) - 1]
    d, err = validate_dest(raw)
    if err:
        eprint(f"yt: {err}")
        return ""
    kv_set("dl_last_dest", d)
    return d


def cmd_dl(argv):
    """Queue downloads. Playback is never blocked on this.

    `dest` is per download and is rewritten on every queue, including with
    the empty string. That is what keeps a folder chosen once from becoming
    a folder chosen forever: the next plain ^d on the same video files it
    under video_dir again.
    """
    now_flag = "--now" in argv
    ask = "--ask" in argv
    argv = [a for a in argv if a not in ("--now", "--ask")]
    dest = ""
    if "-o" in argv:
        i = argv.index("-o")
        if i + 1 >= len(argv):
            eprint("yt: -o needs a folder")
            return 2
        dest, err = validate_dest(argv[i + 1])
        if err:
            eprint(f"yt: {err}")
            return 2
        del argv[i:i + 2]
    ids = [v for v in (parse_video_id(a) for a in argv) if v]
    if not ids:
        eprint("yt: nothing to download")
        return 2
    if ask:
        dest = ask_dest(ids)
        if not dest:
            return 1
    fetch_video_meta(ids)
    n = 0
    for vid in ids:
        r = db().execute("SELECT status, path FROM downloads WHERE video_id=?",
                         (vid,)).fetchone()
        if r and r["status"] == "done" and r["path"] and os.path.exists(r["path"]):
            print(f"yt: already downloaded - {clip(r['path'], 70)}")
            continue
        db().execute("""
          INSERT INTO downloads(video_id,status,quality,added,dest)
          VALUES(?,'queued',?,?,?)
          ON CONFLICT(video_id) DO UPDATE SET status='queued', err='',
                                              dest=excluded.dest
        """, (vid, cfg("quality"), int(time.time()), dest))
        n += 1
    db().commit()
    if n:
        where = f" to {clip(dest, 60)}" if dest else ""
        print(f"yt: queued {n}{where}")
    if now_flag or n:
        start_worker()
    return 0


def worker_alive(pid):
    """Is `pid` really our download worker, and not just some live pid?

    os.kill(pid, 0) only answers "a process with this number exists and I may
    signal it". A worker that was killed leaves its pid in the database, and
    the moment the kernel hands that number to anything else the queue looks
    permanently busy and stops draining - with nothing on screen to say why.
    Reading the command line settles it.
    """
    if pid <= 0:
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            argv = fh.read().split(b"\0")
    except OSError:
        return False
    return any(b"dl-run" == a for a in argv) and any(b"yt" in a for a in argv)


def start_worker():
    """One detached, low-priority drain process. Never two at once."""
    if worker_alive(kv_int("worker_pid", 0)):
        return              # already draining
    cmd = []
    if exe("nice"):
        cmd += ["nice", "-n", "15"]
    if exe("ionice"):
        cmd += ["ionice", "-c", "3"]
    cmd += [sys.executable, _launcher(), "dl-run"]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             start_new_session=True)
        kv_set("worker_pid", p.pid)
    except OSError as e:
        eprint(f"yt: could not start download worker - {e}")


def dl_format():
    """Format selector and the resolution cap it enforces."""
    q = re.sub(r"\D", "", cfg("quality")) or "1080"
    if cfg("avoid_av1") == "1":
        # Try hardware-friendly codecs first, but never fail outright when a
        # video is only published as AV1 - just fall through to it.
        # Order matters: a pre-merged stream caps out around 360p, so it must
        # come AFTER allowing AV1. Otherwise a video whose only 1080p rendition
        # is AV1 silently drops to 360p h264.
        fmt = (f"bv*[height<=?{q}][vcodec!^=av01][protocol!*=m3u8]+ba[protocol!*=m3u8]/"
               f"bv*[height<=?{q}][protocol!*=m3u8]+ba[protocol!*=m3u8]/"
               f"b[height<=?{q}][protocol!*=m3u8]/"
               f"bv*[height<=?{q}]+ba/b[height<=?{q}]/bv*+ba/b")
    else:
        fmt = (f"bv*[height<=?{q}][protocol!*=m3u8]+ba[protocol!*=m3u8]/"
               f"b[height<=?{q}][protocol!*=m3u8]/"
               f"bv*[height<=?{q}]+ba/b[height<=?{q}]/bv*+ba/b")
    return fmt, q


def cmd_dlrun(argv):
    kv_set("worker_pid", os.getpid())
    try:
        while True:
            r = db().execute(
                "SELECT video_id FROM downloads WHERE status='queued' "
                "AND next_try <= ? ORDER BY added LIMIT 1",
                (int(time.time()),)).fetchone()
            if not r:
                break
            if at_risk():
                _, until = risk_state()
                notify("yt: downloads paused",
                       f"rate-limited, resuming in {fmt_dur(until - time.time())}")
                break
            vid = r["video_id"]
            # Claim it conditionally. start_worker() checks for a running
            # worker and then spawns one, and two ^d presses a moment apart
            # can both pass that check - at which point the loser must find
            # this row already taken rather than start a second yt-dlp on the
            # same video and double the requests going to YouTube.
            cur = db().execute(
                "UPDATE downloads SET status='running' "
                "WHERE video_id=? AND status='queued'", (vid,))
            db().commit()
            if not cur.rowcount:
                continue
            # Publish a tick before yt-dlp has said anything, so a row that
            # has just started shows as started rather than as still queued
            # for the several seconds extraction takes.
            dl_progress_put(vid, 0.0, phase="starting")
            ok, path, err = do_download(vid)
            if ok:
                size = os.path.getsize(path) if os.path.exists(path) else 0
                db().execute(
                    "UPDATE downloads SET status='done', path=?, size=?, done=?, err='' "
                    "WHERE video_id=?", (path, size, int(time.time()), vid))
                v = get_video(vid)
                notify("yt: download finished", clip(v["title"] if v else vid, 80))
            elif CUTOFF_RE.search(err or ""):
                # YouTube stopped serving bytes for this video from this
                # address. That ceiling lifts again after a couple of hours,
                # and yt-dlp resumes from the .part file, so this is a wait
                # rather than a failure. Backoff doubles: 30m, 1h, 2h, 4h.
                row = db().execute("SELECT attempts FROM downloads WHERE video_id=?",
                                   (vid,)).fetchone()
                n = (row["attempts"] if row else 0) + 1
                if n > DL_MAX_ATTEMPTS:
                    db().execute("UPDATE downloads SET status='error', err=?, attempts=? "
                                 "WHERE video_id=?",
                                 (f"gave up after {n - 1} attempts: {err[:300]}", n, vid))
                    notify("yt: download failed", clip(err, 120))
                else:
                    wait = min(DL_BACKOFF_BASE * (2 ** (n - 1)), DL_BACKOFF_MAX)
                    db().execute(
                        "UPDATE downloads SET status='queued', attempts=?, next_try=?, err=? "
                        "WHERE video_id=?",
                        (n, int(time.time() + wait),
                         f"cut off by YouTube; retry {n} in {fmt_dur(wait)}", vid))
                    v = get_video(vid)
                    notify("yt: download paused",
                           f"{clip(v['title'] if v else vid, 60)} - YouTube cut the "
                           f"stream off; resuming in {fmt_dur(wait)}")
            else:
                db().execute("UPDATE downloads SET status='error', err=? "
                             "WHERE video_id=?", (err[:400], vid))
                notify("yt: download failed", clip(err, 120))
            db().commit()
        enforce_disk_budget()
    finally:
        kv_set("worker_pid", 0)
        # Nothing is draining the queue any more, so nothing should look like
        # it is still moving.
        try:
            for f in os.listdir(DL_DIR):
                dl_progress_clear(f)
        except OSError:
            pass
    return 0


# Progress lines are tagged so they can be told apart from the one line we
# actually want out of stdout - the path yt-dlp moved the finished file to.
_DL_TAG = "@ytprog"
_PP_TAG = "@ytpost"
# Tab-separated because a title never reaches these fields, only numbers and
# a codec name. "NA" is what yt-dlp prints for a value it does not have yet.
_DL_TEMPLATE = (_DL_TAG + "\t%(progress.status)s\t%(progress.downloaded_bytes)s"
                "\t%(progress.total_bytes)s\t%(progress.total_bytes_estimate)s"
                "\t%(progress.speed)s\t%(progress.eta)s\t%(info.vcodec)s")


def _dl_num(x):
    """A finite float, or zero. yt-dlp prints "NA" for a figure it does not
    have yet, and float() will happily hand back inf or nan for text that
    says so - which then raises out of int() somewhere far away from here."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return v if -1e15 < v < 1e15 else 0.0        # nan fails both comparisons


def _dl_progress_line(vid, line):
    """Turn one tagged yt-dlp progress line into a published tick.

    Percentages are per file, not for the job as a whole: a 1080p download is
    a video stream and an audio stream merged afterwards, and yt-dlp counts
    each separately. Rather than invent a combined figure out of a total that
    is not known until the second file starts, the phase says which stream is
    moving - and the audio one is seconds long next to the video.
    """
    if line.startswith(_PP_TAG):
        dl_progress_put(vid, 100.0, phase="merging")
        return
    if not line.startswith(_DL_TAG + "\t"):
        return
    f = line.split("\t")
    if len(f) < 8:
        return
    done = _dl_num(f[2])
    total = _dl_num(f[3]) or _dl_num(f[4])
    pct = (100.0 * done / total) if total else 0.0
    phase = "audio" if f[7] in ("none", "NA", "") else "video"
    if f[1] == "finished":
        pct = 100.0
        # The audio stream is the last one down, and what follows it is
        # ffmpeg merging the two. FFmpegMergerPP reports no progress of its
        # own, so this is the only notice the merge gets.
        if phase == "audio":
            phase = "merging"
    dl_progress_put(vid, pct, done, total, _dl_num(f[5]), _dl_num(f[6]), phase)


def do_download(vid):
    fmt, q = dl_format()
    outdir = video_dir_for(vid)
    tmpl = os.path.join(outdir, "%(title).150B [%(id)s].%(ext)s")
    args = [
        f"https://www.youtube.com/watch?v={vid}",
        "-f", fmt,
        "-S", f"res:{q},vcodec:h264,ext:mp4:m4a",
        "--merge-output-format", "mp4",
        "-o", tmpl,
        "--no-playlist",
        "--embed-metadata",
        "--embed-thumbnail",
        "--no-simulate",
        "--print", "after_move:filepath",
        # ytdlp_base() passes --no-progress; these override it. --progress-delta
        # keeps the tick rate near 2/s rather than one line per 1 KiB chunk.
        "--progress", "--newline", "--progress-delta", "0.5",
        "--progress-template", _DL_TEMPLATE,
        "--progress-template", "postprocess:" + _PP_TAG,
    ] + client_args()
    if cfg("sponsorblock") == "1":
        args += ["--sponsorblock-remove", "sponsor,selfpromo,interaction"]

    def watch(line):
        _dl_progress_line(vid, line)
        # Claimed: these are the two tags below, thousands of them, and the
        # only line this function wants back is the output path.
        return line.startswith(_DL_TAG) or line.startswith(_PP_TAG)

    try:
        rc, out, err = run_ytdlp_stream(args, watch, kind="dl", timeout=7200)
        path = ""
        for line in (out or "").splitlines():
            line = line.strip()
            if line.startswith(_DL_TAG) or line.startswith(_PP_TAG):
                continue
            if line and os.path.exists(line):
                path = line
        if rc != 0 or not path:
            # Cookies can rescue an age-gated or member-only video.
            if cookie_file("alt") and re.search(r"age|sign in|private|members",
                                                err or "", re.I):
                rc, out, err = run_ytdlp_stream(args, watch, which="alt",
                                                kind="dl", timeout=7200)
                for line in (out or "").splitlines():
                    line = line.strip()
                    if line.startswith(_DL_TAG) or line.startswith(_PP_TAG):
                        continue
                    if line and os.path.exists(line):
                        path = line
    finally:
        dl_progress_clear(vid)
    if rc == 0 and path:
        return True, path, ""
    lines = [l for l in (err or "").splitlines() if l.strip()]
    return False, "", lines[-1].strip() if lines else "download failed"


def cmd_playfmt(argv):
    """The mpv --ytdl-format string, so fish never hand-rolls its own."""
    print(dl_format()[0])
    return 0


def _dl_rows():
    return db().execute(
        "SELECT d.*, v.title FROM downloads d LEFT JOIN videos v ON v.id=d.video_id "
        "ORDER BY CASE d.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 "
        "WHEN 'error' THEN 2 ELSE 3 END, d.added DESC LIMIT 200").fetchall()


def _dl_lines(rows, prog, width):
    icon = {"queued": "…", "running": "▶", "done": "✓", "error": "✗"}
    w_title = max(20, min(60, width - 46))
    out = []
    for r in rows:
        vid = r["video_id"]
        p = prog.get(vid)
        if p:
            bits = [f"{p['phase']} {p['pct']:3.0f}%"]
            if p["speed"]:
                bits.append(f"{p['speed'] / 1048576:.1f} MiB/s")
            if p["eta"]:
                bits.append("eta " + hms(p["eta"]))
            tail = f"{dl_bar(p['pct'], 12)} " + "  ".join(bits)
        elif r["status"] == "done":
            sz = f"{r['size'] / 1048576:.0f} MiB" if r["size"] else ""
            tail = f"{pad(sz, 10)} {clip(r['path'] or '', 40)}"
        else:
            tail = f"{pad(r['status'], 9)} {clip(r['err'] or '', 44)}"
        line = (f"{icon.get(r['status'], '?')} "
                f"{pad(clean(r['title'] or vid), w_title)} {tail}")
        if r["dest"] and r["status"] != "done":
            line += f"  → {clip(r['dest'], 30)}"
        out.append(line.rstrip())
    return out


def cmd_dlstatus(argv):
    """The queue. `--live` redraws it until nothing is left to watch.

    Pane mode still emits the id/kind/display/detail shape the picker parses,
    so this stays usable as a list source; the live view is only for a
    terminal someone is looking at.
    """
    live = bool({"--live", "-l", "live", "watch"} & set(argv))
    rows = _dl_rows()
    if not rows:
        eprint("yt: download queue is empty")
        return 1
    if not live or not sys.stdout.isatty():
        prog = dl_progress_all()
        icon = {"queued": "…", "running": "▶", "done": "✓", "error": "✗"}
        for r in rows:
            p = prog.get(r["video_id"])
            sz = f"{r['size'] / 1048576:.0f}M" if r["size"] else ""
            state = (f"{p['pct']:.0f}%" if p else r["status"])
            print(f"{r['video_id']}\tdl\t{icon.get(r['status'], '?')} "
                  f"{pad(r['title'] or r['video_id'], 60)} {pad(state, 8)} "
                  f"{pad(sz, 6)} {clip(r['err'], 40)}")
        return 0

    width = term_cols()
    printed = 0
    try:
        while True:
            rows = _dl_rows()
            prog = dl_progress_all()
            body = _dl_lines(rows, prog, width)[:20]
            # Redraw in place rather than scrolling: move up over what was
            # written last time and clear each line as it is rewritten.
            if printed:
                sys.stdout.write(f"\033[{printed}A")
            for line in body:
                sys.stdout.write("\033[2K" + clip(line, width) + "\n")
            for _ in range(max(0, printed - len(body))):
                sys.stdout.write("\033[2K\n")
            printed = max(printed, len(body))
            sys.stdout.flush()
            if not any(r["status"] in ("queued", "running") for r in rows):
                return 0
            time.sleep(1.0)
    except KeyboardInterrupt:
        print()
        return 130


def cmd_dlclear(argv):
    db().execute("DELETE FROM downloads WHERE status IN ('error','queued')")
    db().commit()
    print("yt: cleared queued and failed entries")
    return 0


def enforce_disk_budget():
    """Evict watched downloads, oldest first, until under budget."""
    budget = cfg_int("disk_budget_gb", 50) * 1024 ** 3
    if budget <= 0:
        return
    rows = db().execute(
        "SELECT d.video_id, d.path, d.size, d.done, "
        "COALESCE(w.watched,0) watched FROM downloads d "
        "LEFT JOIN watch w ON w.video_id = d.video_id "
        "WHERE d.status='done' ORDER BY d.done ASC").fetchall()
    # A file the user sent somewhere specific with ^alt-d is not ours to
    # delete, and it should not count against a budget for a folder it is
    # not in either.
    rows = [r for r in rows if r["path"] and is_managed(r["path"])]
    total = sum(r["size"] for r in rows)
    if total <= budget:
        return
    freed = 0
    for r in rows:
        if total - freed <= budget:
            break
        if not r["watched"]:
            continue          # only ever evict things already watched
        if r["path"] and os.path.exists(r["path"]):
            try:
                os.unlink(r["path"])
                freed += r["size"]
            except OSError:
                continue
        db().execute("DELETE FROM downloads WHERE video_id=?", (r["video_id"],))
    db().commit()
    if freed:
        notify("yt: freed disk", f"{freed / 1073741824:.1f} GB of watched downloads")


def cmd_offline(argv):
    rows = [row_from_db(r) for r in db().execute(
        "SELECT v.* FROM downloads d JOIN videos v ON v.id=d.video_id "
        "WHERE d.status='done' ORDER BY d.done DESC")]
    if not rows:
        eprint("yt: nothing downloaded yet")
        return 1
    emit(rows, "offline")
    return 0


def cmd_path(argv):
    """Local file for a video, if we have one. Fish uses this to play offline."""
    if not argv:
        return 2
    vid = parse_video_id(argv[0])
    r = db().execute("SELECT path FROM downloads WHERE video_id=? AND status='done'",
                     (vid,)).fetchone()
    if r and r["path"] and os.path.exists(r["path"]):
        print(r["path"])
        return 0
    return 1


# ---- auth -------------------------------------------------------------

# Deliberately narrow. A jar on disk should expose YouTube and nothing
# else - not Gmail, Docs, Calendar or the account chooser. yt-dlp derives
# its SAPISIDHASH auth from the youtube.com cookies alone.
COOKIE_DOMAINS = ("youtube.com", "googlevideo.com")


def snapshot_firefox_cookies(profile, out_path):
    """Copy the profile DB aside and write a Netscape jar of YouTube cookies only.

    Reading a snapshot rather than the live browser session means we never
    race the browser and never cause YouTube to rotate the session out from
    under it. Only the domains we actually need are written out.
    """
    import tempfile
    src = os.path.join(profile, "cookies.sqlite")
    if not os.path.exists(src):
        return 0, f"no cookies.sqlite in {profile}"
    with tempfile.TemporaryDirectory() as td:
        tmp = os.path.join(td, "cookies.sqlite")
        try:
            import shutil
            shutil.copy2(src, tmp)
            for ext in ("-wal", "-shm"):
                if os.path.exists(src + ext):
                    shutil.copy2(src + ext, tmp + ext)
        except OSError as e:
            return 0, f"copy failed: {e}"
        try:
            c = sqlite3.connect(f"file:{tmp}?immutable=0", uri=True, timeout=5)
            rows = c.execute(
                "SELECT host, path, isSecure, expiry, name, value, isHttpOnly "
                "FROM moz_cookies").fetchall()
            c.close()
        except sqlite3.Error as e:
            return 0, f"cannot read cookie db: {e}"

    keep = []
    for host, path, secure, expiry, name, value, _http in rows:
        h = (host or "").lstrip(".")
        if not any(h == d or h.endswith("." + d) for d in COOKIE_DOMAINS):
            continue
        # A Netscape jar is tab-separated, one cookie per line, and has no
        # escaping at all: a tab or a newline inside a name or a value ends
        # the field or the record early and the rest of it is read back as
        # another cookie, for another domain if it says so. Browsers are
        # supposed to reject those characters; the jar is not the place to
        # find out that one got through.
        fields = [host, path or "/", str(name or ""), str(value or "")]
        if any(ch in f for f in fields for ch in ("\t", "\n", "\r")):
            continue
        try:
            exp = int(expiry or 0)
        except (TypeError, ValueError):
            exp = 0
        keep.append((host, path or "/", bool(secure), exp, name, value))
    if not keep:
        return 0, "no YouTube cookies in that profile (is it logged in?)"

    secure_dir(os.path.dirname(out_path))
    tmp_out = out_path + ".tmp"
    try:
        # Live session cookies: they must never exist on disk at the default
        # 0644, not even for the microsecond between write and chmod.
        with open_private(tmp_out) as fh:
            fh.write("# Netscape HTTP Cookie File\n# written by yt\n")
            for host, path, secure, expiry, name, value in keep:
                fh.write("\t".join([
                    host, "TRUE" if host.startswith(".") else "FALSE", path,
                    "TRUE" if secure else "FALSE", str(expiry), name, value]) + "\n")
        os.replace(tmp_out, out_path)
    except OSError as e:
        try:
            os.unlink(tmp_out)
        except OSError:
            pass
        return 0, f"cannot write {out_path}: {e}"
    try:
        os.chmod(out_path, 0o600)   # O_CREAT does not re-mode an existing file
    except OSError:
        pass
    return len(keep), ""


def cmd_auth(argv):
    sub = argv[0] if argv else "status"
    rest = argv[1:]

    if sub in ("status", "list"):
        profs = detect_browser_profiles()
        print("browser profiles yt can read (unencrypted cookie stores):")
        if profs:
            for i, (label, name, path) in enumerate(profs, 1):
                print(f"  [{i}] {label:10s} {name}")
        else:
            print("  none found")
        print("\ncookie jars:")
        for which in ("main", "alt"):
            p = cookie_file(which)
            if p:
                age = cookie_age(which)
                print(f"  {which:5s} {p}  (synced {fmt_dur(age)} ago)")
            else:
                print(f"  {which:5s} not configured")
        print("\n  main = used for the recommended home feed")
        print("  alt  = fallback for search only if anonymous search fails")
        print("\nNote: yt is strictly read-only. It never modifies your")
        print("YouTube playlists and never touches YouTube's Watch Later.")
        return 0

    if sub == "sync":
        which = rest[0] if rest else "main"
        if which not in ("main", "alt"):
            eprint("yt: which jar? use 'main' or 'alt'")
            return 2
        profs = detect_browser_profiles()
        if not profs:
            eprint("yt: no readable browser profile found.\n"
                   "    Firefox-family profiles (Zen, Firefox, LibreWolf) work "
                   "directly.\n    Chromium-family (Helium, Brave) encrypt cookies "
                   "with the system keyring;\n    export a cookies.txt by hand and "
                   "point yt at it:\n      yt config cookies_main /path/to/cookies.txt")
            return 1
        sel = rest[1] if len(rest) > 1 else None
        if sel is None:
            if len(profs) == 1:
                chosen = profs[0]
            else:
                eprint("yt: several profiles - pick one:")
                for i, (label, name, _) in enumerate(profs, 1):
                    eprint(f"  yt auth sync {which} {i}   # {label} / {name}")
                return 2
        else:
            try:
                chosen = profs[int(sel) - 1]
            except (ValueError, IndexError):
                eprint(f"yt: no profile #{sel}")
                return 2
        out = os.path.join(CFG_DIR, f"cookies-{which}.txt")
        n, err = snapshot_firefox_cookies(chosen[2], out)
        if not n:
            eprint(f"yt: {err}")
            return 1
        print(f"yt: wrote {n} cookies -> {out} (from {chosen[0]}/{chosen[1]})")
        print("yt: re-run this when the session expires")
        return 0

    if sub in ("clear", "rm"):
        which = rest[0] if rest else "main"
        p = os.path.join(CFG_DIR, f"cookies-{which}.txt")
        try:
            os.unlink(p)
            print(f"yt: removed {p}")
        except OSError:
            eprint(f"yt: no {which} jar to remove")
        return 0

    eprint("usage: yt auth [status|sync main|sync alt|clear main]")
    return 2


# ---- channel browse ---------------------------------------------------

def cmd_channel(argv):
    if not argv:
        eprint("usage: yt ch @handle|url")
        return 2
    spec = argv[0]
    n = cfg_int("search_count", 40)
    cid, name = resolve_channel(spec)
    if not cid:
        eprint(f"yt: cannot resolve '{spec}' - {name}")
        return 1
    key = f"channel:{cid}:{n}"
    rows = rows_cache_get(key)
    if rows is None:
        rows, err = ytdlp_entries(
            [f"https://www.youtube.com/channel/{cid}/videos", "-I", f"1:{n}"],
            timeout=120)
        if not rows:
            eprint(f"yt: channel fetch failed - {err}")
            return 1
        rows_cache_put(key, rows, cfg_int("rss_ttl", 900))
        upsert_videos(rows)
    emit(rows, "channel")
    return 0


# ---- stats / doctor / maintenance -------------------------------------

def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def cmd_stats(argv):
    c = db()
    saved_n = c.execute("SELECT COUNT(*) n FROM saved WHERE archived=0").fetchone()["n"]
    vids_n = c.execute("SELECT COUNT(*) n FROM videos").fetchone()["n"]
    subs_n = c.execute("SELECT COUNT(*) n FROM subs").fetchone()["n"]
    dl = c.execute("SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM downloads "
                   "WHERE status='done'").fetchone()
    watched = c.execute("SELECT COUNT(*) n FROM watch WHERE watched=1").fetchone()["n"]
    part = c.execute("SELECT COUNT(*) n FROM watch WHERE watched=0 AND position>30"
                     ).fetchone()["n"]
    backlog = c.execute("""
      SELECT COALESCE(SUM(v.duration),0) d FROM saved s JOIN videos v ON v.id=s.video_id
      LEFT JOIN watch w ON w.video_id=s.video_id
      WHERE s.archived=0 AND COALESCE(w.watched,0)=0""").fetchone()["d"]

    print(f"  saved            {saved_n}")
    print(f"  backlog          {fmt_dur(backlog)} unwatched")
    print(f"  watched          {watched}   ({part} part-way)")
    print(f"  subscriptions    {subs_n}")
    print(f"  known videos     {vids_n}")
    print(f"  downloaded       {dl['n']}   ({dl['s'] / 1073741824:.2f} GB "
          f"of {cfg_int('disk_budget_gb', 50)} GB budget)")
    thumbs = dir_size(THUMB_DIR)
    print(f"  thumbnail cache  {thumbs / 1048576:.1f} MB")
    try:
        print(f"  database         {os.path.getsize(DB_PATH) / 1048576:.1f} MB")
    except OSError:
        pass
    print("\n  by category")
    for r in c.execute("SELECT category, COUNT(*) n FROM saved WHERE archived=0 "
                       "GROUP BY category ORDER BY n DESC"):
        print(f"    {pad(r['category'], 20)} {r['n']:>4}")
    top = c.execute("""
      SELECT v.channel, COUNT(*) n FROM saved s JOIN videos v ON v.id=s.video_id
      WHERE s.archived=0 AND v.channel != '' GROUP BY v.channel
      ORDER BY n DESC LIMIT 8""").fetchall()
    if top:
        print("\n  most saved channels")
        for r in top:
            print(f"    {pad(r['channel'], 28)} {r['n']:>4}")
    return 0


PROBE_VIDEO = "SpwzRDUQ1GI"   # a long-lived public video, used only as a probe


def probe_formats(which=None):
    """How many adaptive (video-only) formats YouTube offers us.

    A healthy response has ~18. A response with 0 means we are being served
    the restricted list, which is what throttling looks like in practice.
    """
    args = [f"https://www.youtube.com/watch?v={PROBE_VIDEO}", "-F"]
    rc, out, err = run_ytdlp(args, which=which, kind="probe", timeout=90)
    if rc != 0 and not out:
        return None
    return sum(1 for l in (out or "").splitlines() if "video only" in l)


def cmd_probe(argv):
    anon = probe_formats(None)
    print(f"anonymous       {anon if anon is not None else 'failed'} adaptive formats")
    for which in ("main", "alt"):
        if cookie_file(which):
            n = probe_formats(which)
            print(f"{which:15} {n if n is not None else 'failed'} adaptive formats"
                  + ("   <-- restricted" if n == 0 else ""))
    return 0


def cmd_cooldown(argv):
    """Show or clear the backoff. Clearing is deliberate, not automatic."""
    if argv and argv[0] in ("clear", "reset"):
        kv_set("risk_level", 0)
        kv_set("risk_until", 0)
        print("yt: cooldown cleared")
        return 0
    lvl, until = risk_state()
    if time.time() < until:
        print(f"backing off for {fmt_dur(until - time.time())} (level {lvl})")
        print(f"reason: {kv_get('risk_reason', '?')}")
        print("cached feeds still work; run `yt cooldown clear` to override")
        return 1
    print("no cooldown active")
    return 0


def cmd_doctor(argv):
    ok = True
    print("dependencies")
    for tool, needed in (("yt-dlp", True), ("mpv", True), ("fzf", True),
                         ("chafa", False), ("ffmpeg", False), ("notify-send", False),
                         ("wl-copy", False), ("ionice", False),
                         ("AtomicParsley", False)):
        p = exe(tool)
        mark = "ok " if p else ("MISSING" if needed else "absent ")
        if needed and not p:
            ok = False
        hint = ""
        if not p and tool == "AtomicParsley":
            hint = "  (optional: embeds thumbnails into downloaded mp4s)"
        elif not p and tool == "chafa":
            hint = "  (optional: image thumbnails in the picker)"
        print(f"  {mark:8s} {tool}{hint}")

    print("\nauth")
    for which in ("main", "alt"):
        p = cookie_file(which)
        if p:
            age = cookie_age(which)
            stale = " STALE - re-run `yt auth sync`" if age and age > 2592000 else ""
            print(f"  ok       {which} jar, synced {fmt_dur(age)} ago{stale}")
        else:
            print(f"  absent   {which} jar")

    lvl, until = risk_state()
    print("\nrate guard")
    if time.time() < until:
        print(f"  BACKOFF  level {lvl}, {fmt_dur(until - time.time())} remaining")
        print(f"           reason: {kv_get('risk_reason', '?')}")
    else:
        print(f"  ok       healthy (level {lvl})")
    print(f"  auth requests   {req_count('auth:%', 3600)}/{AUTH_PER_HOUR} this hour")
    print(f"  anon requests   {req_count('anon:%', 3600, True)}/{ANON_PER_HOUR} this hour, "
          f"{req_count('anon:%', 300, True)}/{ANON_BURST} in 5 min "
          f"(+{req_count('anon:stream', 3600)} playback, unbudgeted)")
    print(f"  recommended     {req_count('auth:rec', 3600)}/{REC_PER_HOUR} this hour, "
          f"{req_count('auth:rec', 86400)}/{REC_PER_DAY} today")
    last = kv_int("rec_last", 0)
    if last:
        print(f"  last rec fetch  {fmt_dur(time.time() - last)} ago "
              f"(min interval {fmt_dur(REC_MIN_INTERVAL)})")

    print("\nstreaming proxy")
    st = proxy_state()
    print(f"  {'ok      ' if st else 'stopped '} "
          + (f"running on 127.0.0.1:{st[0]}" if st else "not running (starts on demand)"))
    stalls = []
    try:
        with open(PROXY_LOG, encoding="utf-8") as fh:
            stalls = [l.strip() for l in fh if "stalled" in l]
    except OSError:
        pass
    if stalls:
        # YouTube meters how many bytes it will serve for a given video from a
        # given address; past that ceiling every URL for it 403s, freshly
        # extracted ones included. Nothing local fixes it, so say so plainly
        # rather than letting mpv look broken.
        print(f"  cut off  YouTube stopped serving {len(stalls)} stream(s) mid-playback")
        for l in stalls[-3:]:
            print(f"           {l.split('ytproxy: ')[-1]}")
        print(f"           truncated delivery, not an IP block. Currently using"
              f"\n           player_client={cfg('stream_client')}; `yt ceiling` measures it")

    print("\nPO-token minter")
    if cfg("pot_server") != "1":
        print("  off      pot_server=0 (yt-dlp will spawn a script per video, ~4s each)")
    elif not pot_home():
        print("  missing  bgutil provider not found via pot/src -> <repo>/server/src")
        print("           without it every stream resolution pays ~4s to mint a token")
    elif not exe("deno"):
        print("  missing  deno is not installed; the minter cannot run")
    else:
        vsn = pot_ping()
        if vsn:
            print(f"  ok       bgutil {vsn} warm on 127.0.0.1:{POT_PORT}")
        elif pot_hint():
            print("  starting bgutil server launched, not answering yet (~9s from cold)")
        else:
            print("  stopped  not running (starts when the picker opens)")

    print("\ncaches")
    for key, label in (("home:subs", "subscriptions feed"), ("home:rec", "recommended feed")):
        a = cache_age(key)
        print(f"  {'ok      ' if a is not None else 'empty   '} {label}"
              + (f", {fmt_dur(a)} old" if a is not None else ""))
    nthumbs = len([f for f in os.listdir(THUMB_DIR)]) if os.path.isdir(THUMB_DIR) else 0
    print(f"  ok       {nthumbs} thumbnails cached")

    print("\nplayback")
    print(f"  auth mode       {cfg('play_auth')}  (cookies used for streaming)")
    print(f"  fallback client {cfg('play_client_fallback')}")
    print("  run `yt probe` to compare anonymous vs authenticated format access")

    print("\nwindow hiding")
    if cfg("hide_terminal") != "1":
        print("  off      hide_terminal is disabled")
    else:
        hit, info = find_own_window()
        if hit:
            # Verify the dispatcher actually works rather than only that the
            # window was found: Hyprland 0.56 changed the dispatch API, and the
            # old syntax failed silently.
            try:
                probe = subprocess.run(
                    ["hyprctl", "dispatch",
                     'hl.dsp.window.move({window="address:0x0", workspace="1"})'],
                    capture_output=True, text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                probe = ""
            if probe.startswith("ok"):
                print(f"  ok       will hide {info or 'this window'} "
                      f"({hit[0]} on workspace {hit[1]})")
            else:
                print(f"  BROKEN   hyprctl rejected the dispatcher: {probe[:70]}")
        else:
            print(f"  UNAVAIL  {info}")
        if not exe("jq"):
            print("  note     jq is not installed (not required any more)")

    print("\nstorage")
    vd = os.path.expanduser(cfg("video_dir"))
    print(f"  video dir       {vd}")
    try:
        st = os.statvfs(vd)
        print(f"  free space      {st.f_bavail * st.f_frsize / 1073741824:.0f} GB")
    except OSError:
        pass
    print(f"  fts5 search     {'available' if has_fts() else 'unavailable (LIKE fallback)'}")
    wp = kv_int("worker_pid", 0)
    if wp:
        alive = True
        try:
            os.kill(wp, 0)
        except OSError:
            alive = False
        print(f"  download worker {'running pid ' + str(wp) if alive else 'stale entry'}")
    return 0 if ok else 1


def db_size_mb(checkpoint=True):
    """On-disk size. With checkpoint=True the WAL is folded in first: a VACUUM
    leaves a large write-ahead log behind, and measuring that made the pruner
    believe it had freed nothing and run every step needlessly.

    checkpoint=False is for callers that only want to know whether the size is
    anywhere near a limit. Skipping it can only ever over-report - unfolded WAL
    pages are counted twice - so "under the cap without checkpointing" is a
    safe answer, and it is a read where the other is a write.
    """
    if checkpoint:
        try:
            db().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(DB_PATH + suffix)
        except OSError:
            pass
    return total / 1048576.0


def enforce_db_budget(verbose=False):
    """Keep yt.db under db_max_mb, pruning the least valuable rows first.

    Saved videos, their metadata and their notes are never touched - only
    caches, request logs and metadata for videos you have no relationship to.
    """
    budget = cfg_int("db_max_mb", 500)
    if budget <= 0:
        return 0
    steps = [
        ("expired cache entries",
         "DELETE FROM cache WHERE ts + ttl < strftime('%s','now')"),
        ("request log older than a day",
         "DELETE FROM reqlog WHERE ts < strftime('%s','now') - 86400"),
        ("all cached feeds",
         "DELETE FROM cache"),
        ("descriptions of unsaved videos",
         "UPDATE videos SET description='' WHERE id NOT IN "
         "(SELECT video_id FROM saved) AND id NOT IN (SELECT video_id FROM watch)"),
        ("metadata for videos you have no link to",
         "DELETE FROM videos WHERE id NOT IN (SELECT video_id FROM saved) "
         "AND id NOT IN (SELECT video_id FROM watch) "
         "AND id NOT IN (SELECT video_id FROM downloads)"),
        ("watch history beyond the newest 500",
         "DELETE FROM watch WHERE video_id NOT IN "
         "(SELECT video_id FROM watch ORDER BY last_played DESC LIMIT 500)"),
    ]
    pruned = []
    for label, sql in steps:
        if db_size_mb() <= budget:
            break
        db().execute(sql)
        db().commit()
        db().execute("VACUUM")
        db().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        pruned.append(label)
    if verbose and pruned:
        for label in pruned:
            print(f"  pruned {label}")
    return len(pruned)


def cmd_gc(argv):
    c = db()
    c.execute("DELETE FROM cache WHERE ts + ttl < ?", (int(time.time()) - 86400,))
    c.commit()
    keep = {r["id"] for r in c.execute(
        "SELECT v.id FROM videos v WHERE v.id IN (SELECT video_id FROM saved) "
        "OR v.id IN (SELECT video_id FROM watch) "
        "OR v.updated > ?", (int(time.time()) - 604800,))}
    removed = 0
    if os.path.isdir(THUMB_DIR):
        for f in os.listdir(THUMB_DIR):
            vid = f[:-4] if f.endswith(".jpg") else None
            if vid and vid not in keep:
                try:
                    os.unlink(os.path.join(THUMB_DIR, f))
                    removed += 1
                except OSError:
                    pass
    trim_thumb_cache()
    sixels = trim_sixel_cache(keep)
    players = trim_jsc_cache()
    # Progress ticks from a worker that was killed rather than finishing.
    # dl_progress_all() ignores them once they go stale; this is what
    # actually removes them.
    if os.path.isdir(DL_DIR):
        live = {r["video_id"] for r in c.execute(
            "SELECT video_id FROM downloads WHERE status IN ('queued','running')")}
        cutoff = time.time() - DL_STALE
        for f in os.listdir(DL_DIR):
            path = os.path.join(DL_DIR, f)
            try:
                if f.lstrip(".") in live and os.stat(path).st_mtime > cutoff:
                    continue
                os.unlink(path)
            except OSError:
                pass
    c.execute("DELETE FROM videos WHERE id NOT IN (SELECT video_id FROM saved) "
              "AND id NOT IN (SELECT video_id FROM watch) "
              "AND id NOT IN (SELECT video_id FROM downloads) AND updated < ?",
              (int(time.time()) - 2592000,))
    c.commit()
    enforce_disk_budget()
    c.execute("VACUUM")
    before = db_size_mb()
    enforce_db_budget(verbose=True)
    after = db_size_mb()
    print(f"yt: pruned {removed} thumbnails, {sixels} rendered previews and "
          f"{players} parsed players, database {before:.1f} -> {after:.1f} MB "
          f"(cap {cfg_int('db_max_mb', 500)} MB)")
    return 0


JSC_DIR = os.path.join(CACHE_DIR, "jsc")


def trim_jsc_cache(keep=2):
    """Parsed copies of YouTube's player JS, ~4 MB each.

    ytjsc writes one per player and rotates them itself, so this only has
    anything to do when a proxy died between writing and rotating - or when
    the user wants the space back now rather than at the next resolve.
    """
    n = 0
    try:
        files = sorted((os.path.join(JSC_DIR, f) for f in os.listdir(JSC_DIR)
                        if f.endswith(".js")),
                       key=lambda f: os.stat(f).st_mtime, reverse=True)
    except OSError:
        return 0
    for f in files[keep:]:
        try:
            os.unlink(f)
            n += 1
        except OSError:
            pass
        try:
            os.unlink(f + ".url")
        except OSError:
            pass
    return n


def trim_sixel_cache(keep):
    """Drop rendered previews for videos we no longer keep, then cap the rest.

    These are written by the generated preview script, never by Python, which
    is how they escaped every previous sweep: one file per video per pane
    width per graphics format, growing forever. They are pure derivatives of
    a thumbnail, so losing one costs ~24ms of chafa the next time it is shown.
    """
    if not os.path.isdir(SIXEL_DIR):
        return 0
    budget = cfg_int("sixel_cache_mb", 60) * 1024 * 1024
    removed = 0
    files, total = [], 0
    for name in os.listdir(SIXEL_DIR):
        fp = os.path.join(SIXEL_DIR, name)
        # ".warmed-<geometry>" markers: one per pane size per session, and
        # they must go or the background warmer never runs again.
        if name.startswith(".warmed-"):
            try:
                os.unlink(fp)
                removed += 1
            except OSError:
                pass
            continue
        vid = name.split("-", 1)[0]
        try:
            if len(vid) == 11 and vid not in keep:
                os.unlink(fp)
                removed += 1
                continue
            st = os.stat(fp)
        except OSError:
            continue
        files.append((st.st_atime, st.st_size, fp))
        total += st.st_size
    if budget > 0 and total > budget:
        files.sort()
        for _atime, size, fp in files:
            if total <= budget:
                break
            try:
                os.unlink(fp)
                total -= size
                removed += 1
            except OSError:
                pass
    return removed


def trim_thumb_cache():
    """High-resolution thumbnails are ~220KB each, so cap the cache and drop
    the least recently used. They are trivially refetched."""
    budget = cfg_int("thumb_cache_mb", 400) * 1024 * 1024
    if budget <= 0 or not os.path.isdir(THUMB_DIR):
        return 0
    files = []
    total = 0
    for name in os.listdir(THUMB_DIR):
        fp = os.path.join(THUMB_DIR, name)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        files.append((st.st_atime, st.st_size, fp))
        total += st.st_size
    if total <= budget:
        return 0
    files.sort()
    freed = 0
    for _atime, size, fp in files:
        if total - freed <= budget:
            break
        try:
            os.unlink(fp)
            freed += size
        except OSError:
            pass
    return freed


def cmd_export(argv):
    out = {
        "exported": int(time.time()),
        "categories": [dict(r) for r in db().execute("SELECT * FROM categories")],
        "subs": [dict(r) for r in db().execute("SELECT * FROM subs")],
        "saved": [dict(r) for r in db().execute(
            "SELECT s.*, v.title, v.channel, v.channel_id, v.duration "
            "FROM saved s LEFT JOIN videos v ON v.id=s.video_id")],
        "watch": [dict(r) for r in db().execute("SELECT * FROM watch")],
    }
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if argv:
        with open(os.path.expanduser(argv[0]), "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"yt: exported {len(out['saved'])} saved to {argv[0]}")
    else:
        print(text)
    return 0


BOOL_KEYS = {"mpv_deband", "sponsorblock", "avoid_av1", "hide_terminal", "hide_terminal_audio",
             "use_proxy", "jsc_resident", "prefetch_focus", "skip_webpage",
             "search_api"}


def humanise(key, val):
    """Booleans are stored as 1/0 but shown as words - nobody should have to
    remember which way round the digit goes."""
    if key in BOOL_KEYS:
        return "enabled" if str(val).strip() in ("1", "true", "yes", "on",
                                                 "enabled") else "disabled"
    return str(val)


def dehumanise(key, val):
    if key in BOOL_KEYS:
        return "1" if str(val).strip().lower() in ("1", "true", "yes", "on",
                                                   "enabled") else "0"
    return val


SETTINGS = [
    ("quality",             "Max download/stream height",        ["2160","1440","1080","720","480"]),
    ("video_dir",           "Where downloads are filed",          None),
    ("disk_budget_gb",      "Disk budget for downloads (GB, 0=off)", None),
    ("sponsorblock",        "Strip sponsor segments on download",  ["1","0"]),
    ("avoid_av1",           "Prefer hardware-decodable codecs",    ["1","0"]),
    ("hwdec",               "mpv hardware decoding",               ["auto-safe","auto","no","vulkan","vaapi"]),
    ("use_proxy",           "Stream via the local 1080p proxy",    ["1","0"]),
    ("skip_webpage",        "Resolve without the 1.4MB watch page", ["1","0"]),
    ("visitor_ttl_hours",   "Hours before a new visitor id",        ["1","6","12","24"]),
    ("jsc_resident",        "Keep the JS challenge solver warm",   ["1","0"]),
    ("jsc_idle_minutes",    "...and drop it after this long idle",  ["2","5","10","30"]),
    ("pot_prewarm",         "Mint the first PO token up front",    ["1","0"]),
    ("prefetch_focus",      "Resolve the row you settle on",       ["1","0"]),
    ("prefetch_dwell",      "Seconds still before that counts",     ["1","2","3","5"]),
    ("prefetch_max",        "Speculative resolves per 10 minutes",  ["0","3","6","12"]),
    ("play_auth",           "Cookies used for playback (proxy off)", ["main","alt","none"]),
    ("play_client_fallback","Player client retried on failure",    ["android","web_safari","mweb","tv_embedded"]),
    ("hide_terminal",       "Hide terminal while a video plays",   ["1","0"]),
    ("hide_terminal_audio", "Also hide it for audio-only",         ["1","0"]),
    ("home_mode",           "Default home feed",                   ["auto","subs","rec"]),
    ("shorts",              "Shorts filter",                       ["show","hide","only"]),
    ("feed_count",          "Rows in the home feed",               None),
    ("search_count",        "Rows returned by search",             None),
    ("search_pages",        "Result pages fetched (+1 request each)", ["1","2","3"]),
    ("search_api",          "Search via YouTube's JSON endpoint",   ["1","0"]),
    ("search_stale_hours",  "Show an old search while refreshing",  ["0","1","6","24"]),
    ("preview_pct",         "Preview pane width (% of terminal)",  None),
    ("thumb_quality",       "Thumbnail resolution",                ["maxresdefault","hq720","sddefault","hqdefault","mqdefault"]),
    ("thumb_format",        "Terminal graphics protocol",          ["auto","sixels","kitty","iterm","symbols"]),
    ("dither",              "Thumbnail dithering",                 ["diffusion","none","ordered","noise"]),
    ("mpv_shaders",         "mpv.conf shaders during yt playback", ["inherit","off"]),
    ("dither_grain",        "Dither cell size",                    ["1x1","2x1","2x2","4x4","8x8"]),
    ("dither_intensity",    "Dither strength (0=off, 1=full)",     ["0.3","0.5","0.7","1.0"]),
    ("thumb_cache_mb",      "Thumbnail cache budget (MB)",         None),
    ("db_max_mb",           "Database size cap (MB, 0=unlimited)", None),
    ("rss_ttl",             "Subscription feed cache (seconds)",   None),
    ("rec_ttl",             "Recommended feed cache (seconds)",    None),
    ("search_ttl",          "Search cache (seconds)",              None),
    ("mpv_deband",          "Debanding while playing (^7 toggles)", ["1","0"]),
    ("stream_client",       "Client media URLs come from",         ["tv_simply","android_vr","tv","ios","android"]),
    ("audio_fmt",           "Format used for audio-only playback", None),
]


def proc_ancestry(pid=None, limit=12):
    """Our pid and its ancestors, via /proc - no ps subprocesses."""
    out = []
    pid = pid or os.getpid()
    for _ in range(limit):
        if not pid or pid <= 1:
            break
        out.append(pid)
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                data = fh.read()
            # comm can contain spaces/parens, so parse after the final ')'
            pid = int(data[data.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return out


def find_own_window():
    """(address, workspace) of the window that owns this process.

    Matching on ancestry rather than the focused window: the focused window
    is not necessarily the terminal that launched us.
    """
    if not exe("hyprctl"):
        return None, "hyprctl not found (window hiding needs Hyprland)"
    try:
        out = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True,
                             text=True, timeout=5).stdout
        clients = json.loads(out or "[]")
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return None, f"cannot query hyprctl ({e})"
    by_pid = {}
    for c in clients:
        by_pid.setdefault(c.get("pid"), c)
    for pid in proc_ancestry():
        c = by_pid.get(pid)
        if c and c.get("address"):
            ws = (c.get("workspace") or {}).get("id")
            if ws is not None:
                return (c["address"], ws), c.get("class", "")
    return None, "no Hyprland window owns this process"


def cmd_winself(argv):
    hit, info = find_own_window()
    if not hit:
        eprint(f"yt: {info}")
        return 1
    print(f"{hit[0]} {hit[1]}")
    return 0


def cmd_settings(argv):
    """Emit settings as picker rows: key \t setting \t display \t detail."""
    cols = layout_cols()
    # Adaptive, same as the video rows: the description is the first thing to
    # go when the pane is narrow, then the value column shrinks.
    kw = max(12, min(26, cols // 3))
    rest = cols - kw - 1
    vw = max(6, min(24, rest // 2))
    dw = rest - vw - 1
    for key, desc, choices in SETTINGS:
        val = humanise(key, cfg(key))
        if key in BOOL_KEYS:
            choices = ["enabled", "disabled"]
        if dw >= 10:
            line = f"{pad(key, kw)} {pad(clip(str(val), vw), vw)} {clip(desc, dw)}"
        else:
            line = f"{pad(key, kw)} {clip(str(val), rest)}"
        det = [f"\\033[1m{key}\\033[0m", desc, "", f"current: {val}"]
        if choices:
            det += ["", "choices: " + ", ".join(choices)]
        else:
            det += ["", "free-form value"]
        print(SEP.join([key, "setting", line.rstrip(), "\\n".join(det)]))
    return 0


def settings_choices(key):
    """The fixed value set for a setting, or None if it is free-form."""
    for k, _desc, ch in SETTINGS:
        if k == key:
            if k in BOOL_KEYS:
                return ["0", "1"]
            return list(ch) if ch else None
    return None


def cmd_choices(argv):
    """Allowed values for a setting, one per line (empty if free-form)."""
    if not argv:
        return 2
    for key, _desc, ch in SETTINGS:
        if key == argv[0]:
            if key in BOOL_KEYS:
                print("enabled")
                print("disabled")
                return 0
            for c in (ch or []):
                print(c)
            return 0
    return 0


KEYS = [
    ("enter", "play"), ("^f", "search youtube"), ("^a", "audio"),
    ("^s", "save"), ("^d", "download"), ("^alt-d", "download to…"),
    ("^y", "url"), ("^o", "browser"),
    ("^x", "remove"), ("^t", "shorts"), ("^r", "refresh"),
    ("alt-w", "toggle watched"), ("alt-n", "note"), ("tab", "multi"),
    ("^p", "settings"), ("alt-c", "choose list"), ("alt-h", "watched list"),
    ("alt-l", "library"), ("alt-f", "feed"),
]


def wrap_header(width):
    """Greedy-wrap the key legend so every binding is always on screen."""
    sep = " \u00b7 "
    lines, cur = [], ""
    for k, v in KEYS:
        item = f"{k} {v}"
        cand = item if not cur else cur + sep + item
        if dwidth(cand) > width and cur:
            lines.append(cur)
            cur = item
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines


def cmd_header(argv):
    """Just the wrapped key legend, for fzf's transform-header on resize."""
    for line in wrap_header(layout_cols()):
        print(line)
    return 0


def checked(key, fallback):
    """A setting's value, or the default if it is not one of the allowed ones.

    cmd_config refuses a bad value, but the config file is a plain text file
    anyone can edit, and several of these are pasted straight into the chafa
    command line in the generated preview script. Checking on the way out
    means a hand-edited or imported config cannot put anything there that
    was never on the list.
    """
    allowed = settings_choices(key)
    val = cfg(key) or fallback
    return val if not allowed or val in allowed else fallback


def cmd_ui(argv):
    """Everything the fish front-end needs to build the fzf command, in one
    process: settings it cannot read itself, plus a legend pre-wrapped to the
    pane width so no binding is ever truncated away."""
    print(f"pct={cfg_int('preview_pct', 40)}")
    print(f"gfx={checked('thumb_format', 'auto')}")
    print(f"tq={checked('thumb_quality', 'maxresdefault')}")
    print(f"dither={checked('dither', 'diffusion')}")
    print(f"grain={checked('dither_grain', '1x1')}")
    print(f"intensity={checked('dither_intensity', '0.5')}")
    print(f"playauth={cfg('play_auth') or 'main'}")
    print(f"playfallback={cfg('play_client_fallback') or 'android'}")
    print(f"hideterm={cfg('hide_terminal') or '0'}")
    print(f"hidetermaudio={cfg('hide_terminal_audio') or '0'}")
    print(f"useproxy={cfg('use_proxy') or '0'}")
    print(f"cookies={cookie_file(cfg('play_auth') or 'main')}")
    # Playback settings too: __yt_play used to spend a Python start-up each
    # on hwdec, playfmt and audio_fmt before mpv could even be invoked.
    print(f"hwdec={cfg('hwdec') or 'auto-safe'}")
    print(f"vfmt={dl_format()[0]}")
    print(f"afmt={cfg('audio_fmt') or 'bestaudio/best'}")
    print(f"streamclient={cfg('stream_client') or ''}")
    print(f"deband={cfg('mpv_deband') or '0'}")
    print(f"shaders={checked('mpv_shaders', 'inherit')}")
    cols = layout_cols()
    print(f"cols={cols}")
    for line in wrap_header(cols):
        print(f"hdr={line}")
    return 0


BOOT_SEP = "\x1e---rows---"


def cmd_boot(argv):
    """Everything needed to put the picker on screen, in one process.

    Opening it used to be two Python start-ups back to back - `ui` for the
    settings and legend, then the mode for the rows - at ~28ms each. They are
    independent of one another, so paying process start-up twice was pure
    overhead: this emits the ui block, a separator, then the rows.
    """
    if not argv:
        return 2
    mode = argv[0]
    fn = DISPATCH.get(mode)
    if fn is None:
        eprint(f"yt: unknown mode '{mode}'")
        return 2
    cmd_ui([])
    print(BOOT_SEP)
    sys.stdout.flush()
    # Opening the picker is a strong signal that something is about to be
    # played, and starting the proxy takes ~110ms we would otherwise pay on
    # the first video. Fire-and-forget: nothing here waits on it.
    warm_proxy()
    # The PO-token minter needs ~9s before it can answer, so it has to be
    # started as early as anything possibly can be - by the time a video is
    # picked it is usually ready, and until it is, yt-dlp just falls back to
    # spawning the per-video script.
    warm_pot()
    return fn(argv[1:])


def cmd_config_get(argv):
    """Print several config values, one per line, in one process.

    The fish side needs a handful of settings before it can even build the
    fzf command; fetching them individually cost a Python start-up each.
    """
    for k in argv:
        print(cfg(k))
    return 0


def cmd_config(argv):
    if not argv:
        for k in sorted(cfg()):
            print(f"{k}={cfg(k)}")
        return 0
    if len(argv) == 1:
        print(cfg(argv[0]))
        return 0
    key = argv[0]
    raw = " ".join(argv[1:])
    val = dehumanise(key, raw)
    # Settings with a fixed set of values are checked here rather than only in
    # the picker. Several of them are interpolated into the generated preview
    # script - `dither`, `dither_grain`, `dither_intensity` all end up inside a
    # chafa command line in ~/.cache/yt/preview.sh - so a value nobody
    # validated is a value that runs.
    allowed = settings_choices(key)
    if allowed and val not in allowed:
        eprint(f"yt: {key} must be one of: {', '.join(allowed)}")
        return 2
    cfg_set(key, val)
    print(f"yt: {key} = {humanise(key, cfg(key))}")
    return 0


def cmd_refresh(argv):
    """Backfill metadata for saved videos we never managed to identify."""
    ids = [r["video_id"] for r in db().execute(
        "SELECT s.video_id FROM saved s LEFT JOIN videos v ON v.id = s.video_id "
        "WHERE v.id IS NULL OR v.title = ''")]
    if not ids:
        print("yt: every saved video already has metadata")
        return 0
    print(f"yt: refreshing {len(ids)} video(s)...")
    got = fetch_video_meta(ids)
    still = [v for v in ids if not (get_video(v) and get_video(v)["title"])]
    for v in ids:
        reindex_fts(v)
    print(f"yt: recovered {got}")
    if still:
        eprint(f"yt: still unavailable (likely deleted or private): {', '.join(still)}")
        eprint("yt: drop them with: yt unsave " + " ".join(still))
    return 0


PROXY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ytproxy.py")
PROXY_STATE = os.path.join(DATA_DIR, "proxy.json")

# ---- bgutil PO-token server -------------------------------------------
# yt-dlp's bgutil plugin looks for an HTTP provider on this port before it
# falls back to spawning a Deno script per video, so the port is not ours to
# choose. See pot/potserver.ts for why we run our own entry point rather than
# the provider's (that one binds every interface).
POT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pot")
POT_SCRIPT = os.path.join(POT_DIR, "potserver.ts")
POT_STATE = os.path.join(DATA_DIR, "pot.json")
POT_LOG = os.path.join(CACHE_DIR, "pot.log")
POT_PORT = 4416
POT_CACHE = os.path.join(HOME, ".cache", "bgutil-ytdlp-pot-provider")


def pot_home():
    """The provider checkout, reached through pot/src -> <repo>/server/src.

    Kept as a symlink rather than a config value so there is exactly one place
    the path lives, and so Deno resolves the provider's npm dependencies from
    the sibling node_modules link.
    """
    try:
        src = os.path.realpath(os.path.join(POT_DIR, "src"))
    except OSError:
        return None
    # .../<repo>/server/src -> .../<repo>
    server = os.path.dirname(src)
    repo = os.path.dirname(server)
    return repo if os.path.isdir(src) and os.path.isdir(
        os.path.join(server, "node_modules")) else None


def pot_hint():
    """True if a POT server we started still has a live pid. No round trip."""
    try:
        pid = _state_pid(POT_STATE)
        if pid > 0:
            os.kill(pid, 0)
            return True
    except OSError:
        pass
    return False


def pot_ping(timeout=1.0):
    """The provider's own health check: version string, or "" if nothing."""
    try:
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", POT_PORT, timeout=timeout)
        c.request("GET", "/ping")
        r = c.getresponse()
        body = json.loads(r.read() or b"{}") if r.status == 200 else {}
        c.close()
        return body.get("version") or ""
    except Exception:
        return ""


def warm_pot():
    """Start the PO-token server if it is wanted and not already up.

    Fire-and-forget, like warm_proxy(): it takes ~9s to have a minter ready,
    which is far too long to block a picker launch on, and yt-dlp degrades to
    the per-video script in the meantime rather than failing.
    """
    if cfg("pot_server") != "1" or not os.path.exists(POT_SCRIPT):
        return
    if pot_hint():
        return
    repo = pot_home()
    if not repo or not exe("deno"):
        return
    # Something else may already be serving this port (the provider's own
    # server, or a container). Leave it alone: two minters on one port is a
    # startup race, and the plugin only ever talks to one of them.
    if pot_ping(timeout=0.4):
        return
    secure_dir(CACHE_DIR)
    secure_dir(POT_CACHE)
    try:
        # Every minted PO token and IntegrityToken is echoed in here.
        log = open_private(POT_LOG)
    except OSError:
        log = subprocess.DEVNULL
    cmd = [
        "deno", "run", "--allow-env", "--allow-sys", "--allow-net",
        "--allow-ffi=" + os.path.join(repo, "server", "node_modules"),
        "--allow-read=" + ",".join([repo, POT_CACHE, POT_DIR]),
        "--allow-write=" + POT_CACHE,
        POT_SCRIPT,
        "--port", str(POT_PORT),
        "--idle-minutes", str(cfg_int("pot_idle_minutes", 30)),
    ]
    try:
        p = subprocess.Popen(cmd, cwd=POT_DIR, stdout=log,
                             stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return
    finally:
        if log is not subprocess.DEVNULL:
            log.close()
    try:
        fd = os.open(POT_STATE, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"pid": p.pid, "port": POT_PORT}, fh)
        os.chmod(POT_STATE, 0o600)      # O_CREAT does not re-mode an existing file
    except OSError:
        pass


def proxy_state():
    """(port, token) if the proxy is up, else None. Deliberately duplicated
    from ytproxy rather than imported, to keep the two modules acyclic."""
    try:
        with open(PROXY_STATE, encoding="utf-8") as fh:
            st = json.load(fh)
        port, token, pid = int(st["port"]), st["token"], int(st["pid"])
    except (OSError, ValueError, KeyError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    try:
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        c.request("GET", "/health")
        r = c.getresponse()
        ok = r.status == 200 and r.getheader("Server", "").startswith("ytproxy")
        r.read(); c.close()
        return (port, token) if ok else None
    except Exception:
        return None


def _state_pid(path):
    """The "pid" field out of one of our own small state files.

    These are JSON, and `import json` is 3.6ms - which is most of what the
    picker's launch path spends deciding that the proxy it wants is already
    running. The file is a flat object this program wrote, so finding one
    integer in it is a find() and a digit scan; anything unexpected falls
    through to the real parser rather than guessing.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read(4096)
    except OSError:
        return 0
    i = text.find('"pid"')
    if i != -1:
        i = text.find(":", i + 5)
        if i != -1:
            i += 1
            while i < len(text) and text[i] in " \t\r\n":
                i += 1
            j = i
            while j < len(text) and text[j].isdigit():
                j += 1
            if j > i:
                return int(text[i:j])
    try:
        pid = int(json.loads(text)["pid"])
        return pid if pid > 0 else 0
    except (ValueError, KeyError, TypeError):
        return 0


def proxy_hint():
    """(port, token) from the state file if its pid is alive - no health check.

    For callers that are about to talk to the proxy anyway and can recover if
    it turns out to be stale.
    """
    try:
        with open(PROXY_STATE, encoding="utf-8") as fh:
            st = json.load(fh)
        os.kill(int(st["pid"]), 0)
        return int(st["port"]), st["token"]
    except (OSError, ValueError, KeyError):
        return None


def warm_proxy():
    """Start the streaming proxy without waiting for it.

    ensure_proxy() blocks up to 6s for the port to answer, which is fine on
    the play path but not here. The picker calls this at launch so the proxy
    is already listening by the time anything is played - it was costing
    ~110ms on the first video of a session.
    """
    # Deliberately NOT proxy_state(): that does an HTTP health check, which
    # pulls in http.client and a TCP round trip for ~13ms on every picker
    # open - more than this saves once the proxy is already up. A live pid is
    # enough of a hint here; the play path still does the full check.
    if cfg("use_proxy") != "1" or not os.path.exists(PROXY_PATH):
        return
    try:
        pid = _state_pid(PROXY_STATE)
        if pid > 0:
            os.kill(pid, 0)
            return                  # something is already running
    except OSError:
        pass
    try:
        # Quotes signed googlevideo URLs and the proxy's own token.
        log = open_private(PROXY_LOG)
    except OSError:
        log = subprocess.DEVNULL
    try:
        subprocess.Popen([sys.executable, PROXY_PATH],
                         stdout=log, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass
    finally:
        if log is not subprocess.DEVNULL:
            log.close()


def ensure_proxy():
    st = proxy_state()
    if st:
        return st
    if not os.path.exists(PROXY_PATH):
        return None
    # The proxy is the component most likely to fail and the only one the user
    # never sees, so its output goes to a log rather than to /dev/null. Opened
    # truncating: this runs only when no proxy is up, so nothing is lost.
    try:
        # Quotes signed googlevideo URLs and the proxy's own token.
        log = open_private(PROXY_LOG)
    except OSError:
        log = subprocess.DEVNULL
    try:
        subprocess.Popen([sys.executable, PROXY_PATH],
                         stdout=log, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return None
    finally:
        if log is not subprocess.DEVNULL:
            log.close()
    # Six seconds of patience, but spent unevenly: the proxy binds its port in
    # about 110 ms, and a flat 100 ms poll meant the common case waited for the
    # next tick rather than for the proxy. Tight at first, then backing off for
    # the machine that really is slow to start one.
    waited = 0.0
    step = 0.01
    while waited < 6.0:
        time.sleep(step)
        waited += step
        st = proxy_state()
        if st:
            return st
        step = min(step * 1.5, 0.2)
    return None


def stream_cache_get(vid):
    row = db().execute("SELECT payload, ts, ttl FROM cache WHERE key=?",
                       (f"stream:{vid}",)).fetchone()
    if not row or time.time() - row["ts"] > row["ttl"]:
        return None
    try:
        got = json.loads(row["payload"])
    except ValueError:
        return None
    # Which client a URL came from decides whether YouTube will serve the whole
    # file from it, so a cached URL minted by a different client is worse than
    # no cache at all: changing stream_client would otherwise leave hours of
    # truncated URLs in place, and the setting would look like it did nothing.
    if got.get("client") != (cfg("stream_client") or ""):
        return None
    return got


def stream_cache_put(vid, urls):
    cache_put(f"stream:{vid}", dict(urls, client=cfg("stream_client") or ""), 3 * 3600)


def cmd_stream(argv):
    """Print the local proxy URLs for a video: video stream, then audio.

    Going through the proxy is what makes full-quality streaming possible at
    all - see the note at the top of ytproxy.py.
    """
    if not argv:
        return 2
    vid = parse_video_id(argv[0])
    if not vid:
        eprint("yt: bad video id")
        return 2
    st = ensure_proxy()
    if not st:
        eprint("yt: could not start the streaming proxy")
        return 1
    port, token = st
    tok = urllib.parse.quote(token, safe="")
    info = _proxy_info(port, tok, vid)
    if not info.get("video"):
        eprint("yt: could not resolve a stream for this video")
        return 1
    print(f"http://127.0.0.1:{port}/v/{vid}/video?t={tok}")
    if info.get("audio"):
        print(f"http://127.0.0.1:{port}/v/{vid}/audio?t={tok}")
    return 0


def cmd_playinfo(argv):
    """Everything fish needs to start mpv, in one process.

    Playing used to cost five Python start-ups (path, title, playfmt, config
    hwdec, stream) plus a sixth for playstart - ~70ms each before mpv was even
    exec'd. This does the lot in one, and records the play while it is here.

    One TSV line per id:  id \t local-path \t video-url \t audio-url \t title
    Empty local-path means stream; empty video-url means "let mpv extract".
    """
    want_win = "--win" in argv
    argv = [a for a in argv if a != "--win"]
    ids = [v for v in (parse_video_id(a) for a in argv) if v]
    if not ids:
        return 2
    if want_win:
        # Which terminal window to hide. This used to be a separate `win-self`
        # invocation costing a whole Python start-up on the way to mpv.
        hit, _ = find_own_window()
        print("win\t" + (f"{hit[0]}\t{hit[1]}" if hit else "\t"))
    # Playing without ever opening the picker (`yt <url>`) skips cmd_boot, so
    # this is the other place the minter gets a chance to be warm.
    warm_pot()
    want_proxy = cfg("use_proxy") == "1"
    port = tok = None
    if want_proxy:
        # Optimistic: if a proxy pid is alive, use its port straight away. The
        # /health round trip ensure_proxy() does is redundant here because the
        # very next thing we do is ask that same proxy to resolve a video - so
        # a dead proxy is discovered there instead, at no extra cost, and only
        # then do we pay for the full start-and-verify.
        st = proxy_hint() or ensure_proxy()
        if st:
            port, token = st
            tok = urllib.parse.quote(token, safe="")
        else:
            eprint("yt: could not start the streaming proxy")

    # The database work first, in this thread: there is one connection per
    # thread and opening more of them to read two rows would cost more than it
    # saved.
    plan = []
    for vid in ids:
        r = db().execute(
            "SELECT path FROM downloads WHERE video_id=? AND status='done'",
            (vid,)).fetchone()
        local = r["path"] if r and r["path"] and os.path.exists(r["path"]) else ""
        v = get_video(vid)
        plan.append((vid, local, (v["title"] if v else "") or vid))

    need = [i for i, (_, local, _) in enumerate(plan) if not local and port]
    info_for = {}
    if need:
        # The first one alone, because this is where a stale port is
        # discovered - every request to a proxy that is no longer there would
        # otherwise fail at once, and the recovery would happen four times.
        first = need[0]
        vid, _, title = plan[first]
        sys.stderr.write(f"yt: resolving {clip(title, 60)}... ")
        sys.stderr.flush()
        info = _proxy_info(port, tok, vid)
        if not info:
            # The optimistic port was stale. Start one properly and retry.
            st = ensure_proxy()
            if st:
                port, token = st
                tok = urllib.parse.quote(token, safe="")
                info = _proxy_info(port, tok, vid)
        sys.stderr.write("ok\n" if info.get("video") else "failed\n")
        info_for[first] = info

    rest = need[1:]
    if rest:
        # fish runs one mpv per video, so the second URL is not needed until
        # the first video ends - but it was being waited for before the first
        # one started, at 0.3-2s each. They are independent extractions
        # against different videos and the proxy locks per video, so they
        # overlap cleanly. Bounded low: this is concurrency to hide latency,
        # not a way to ask YouTube for more at once.
        sys.stderr.write(f"yt: resolving {len(rest)} more... ")
        sys.stderr.flush()
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(3, len(rest))) as ex:
            for i, info in zip(rest, ex.map(
                    lambda i: _proxy_info(port, tok, plan[i][0]), rest)):
                info_for[i] = info
        bad = sum(1 for i in rest if not info_for[i].get("video"))
        sys.stderr.write("ok\n" if not bad else f"{bad} failed\n")

    rows = []
    for i, (vid, local, title) in enumerate(plan):
        vurl = aurl = ""
        info = info_for.get(i) or {}
        if info.get("video"):
            vurl = f"http://127.0.0.1:{port}/v/{vid}/video?t={tok}"
            if info.get("audio"):
                aurl = f"http://127.0.0.1:{port}/v/{vid}/audio?t={tok}"
        rows.append((vid, local, vurl, aurl, title))

    now = int(time.time())
    for vid, *_ in rows:
        db().execute("""
          INSERT INTO watch(video_id,position,duration,watched,last_played,play_count)
          VALUES(?,0,0,0,?,1)
          ON CONFLICT(video_id) DO UPDATE SET
            last_played=excluded.last_played, play_count=watch.play_count+1
        """, (vid, now))
    db().commit()
    kv_set("last_played_id", rows[0][0])

    for r in rows:
        print("\t".join(x.replace("\t", " ") for x in r))
    return 0


def _proxy_info(port, tok, vid):
    """Ask the proxy to resolve a video. Returns {} on any failure."""
    try:
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
        c.request("GET", f"/v/{vid}/info?t={tok}")
        info = json.loads(c.getresponse().read() or b"{}")
        c.close()
        return info
    except Exception as e:
        eprint(f"yt: proxy did not respond ({e})")
        return {}


def cmd_ceiling(argv):
    """Measure how many bytes YouTube will actually serve right now.

    YouTube meters media delivery per video per IP address: a stream runs
    normally and then every request past some offset returns 403, freshly
    signed URLs included. Neither signing in nor a PO token lifts it; it does
    decay after a couple of hours. This measures where the wall currently is,
    which is the only way to tell a throttled address apart from a broken one.

    It is a real read, so it spends some of that budget - hence the cap and
    the fact that it is never run automatically.
    """
    limit_mib = 48
    vid = None
    for a in argv:
        if a.startswith("--limit="):
            limit_mib = max(1, min(512, int(re.sub(r"\D", "", a) or 48)))
        else:
            vid = parse_video_id(a) or vid
    if not vid:
        r = db().execute(
            "SELECT s.video_id FROM saved s LEFT JOIN watch w ON w.video_id=s.video_id "
            "ORDER BY COALESCE(w.last_played, 0) ASC LIMIT 1").fetchone()
        vid = r["video_id"] if r else None
    if not vid:
        eprint("yt: give a video id (nothing saved to pick from)")
        return 2

    v = get_video(vid)
    print(f"measuring: {clip((v['title'] if v else vid), 60)}")
    print(f"  client     {cfg('stream_client') or 'yt-dlp default'}")
    rc, out, err = run_ytdlp(
        [f"https://www.youtube.com/watch?v={vid}", "-f", dl_format()[0], "-g"] + client_args(),
        which=None, kind="stream", timeout=120)
    urls = [u.strip() for u in (out or "").splitlines() if u.strip().startswith("http")]
    if not urls:
        eprint(f"yt: could not resolve a stream - {(err or '').strip()[:160]}")
        return 1
    url = urls[0]
    parts = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(parts.query))
    total = int(q.get("clen") or 0)
    dur = float(q.get("dur") or (v["duration"] if v else 0) or 0)

    import http.client
    path = parts.path + "?" + parts.query
    c = http.client.HTTPSConnection(parts.netloc, timeout=30)
    pos, size, status = 0, 1 << 18, 206
    cap = limit_mib << 20
    t0 = time.time()
    try:
        while pos < min(cap, total or cap):
            c.request("GET", path, headers={"User-Agent": UA, "Accept": "*/*",
                                            "Connection": "keep-alive",
                                            "Range": f"bytes={pos}-{pos + size - 1}"})
            r = c.getresponse()
            body = r.read()
            status = r.status
            if status not in (200, 206) or not body:
                break
            pos += len(body)
    except Exception as e:
        eprint(f"yt: read failed - {e}")
    finally:
        try:
            c.close()
        except Exception:
            pass
    dt = max(time.time() - t0, 0.001)

    print(f"  served     {pos / 1048576:.1f} MiB"
          + (f" of {total / 1048576:.1f} MiB" if total else ""))
    print(f"  throughput {pos / dt / 1048576:.2f} MiB/s")
    if pos >= min(cap, total or cap):
        if total and pos >= total:
            print("  verdict    whole file served - no ceiling on this video")
        else:
            print(f"  verdict    no ceiling within the {limit_mib} MiB test limit")
        return 0
    print(f"  verdict    cut off at {pos / 1048576:.1f} MiB (HTTP {status})")
    if total and dur:
        mins = dur / 60.0 * (pos / total)
        print(f"             about {mins:.0f} min of this video at the current quality")
    print(f"             truncated delivery on player_client={cfg('stream_client')}."
          "\n             Try another: yt config stream_client tv_simply|android_vr|ios")
    return 1


def cmd_proxy(argv):
    sub = argv[0] if argv else "status"
    if sub in ("stop", "kill"):
        try:
            with open(PROXY_STATE, encoding="utf-8") as fh:
                pid = int(json.load(fh)["pid"])
            os.kill(pid, 15)
            print("yt: proxy stopped")
        except (OSError, ValueError, KeyError):
            print("yt: no proxy running")
        return 0
    st = proxy_state()
    print(f"proxy running on 127.0.0.1:{st[0]}" if st else "proxy not running")
    return 0 if st else 1


def cmd_previewtest(argv):
    """Render a preview straight to the terminal, outside fzf.

    If this looks right but the picker's preview does not, the problem is fzf
    or the pane geometry; if this looks wrong too, it is chafa or the terminal.
    """
    vid = parse_video_id(argv[0]) if argv else None
    if not vid:
        r = db().execute("SELECT video_id FROM saved LIMIT 1").fetchone()
        vid = r["video_id"] if r else None
    if not vid:
        eprint("yt: give a video id")
        return 2
    script = os.path.join(CACHE_DIR, "preview.sh")
    if not os.path.exists(script):
        eprint(f"yt: {script} not generated yet - open the picker once first")
        return 1
    rows = [r for r in db().execute(
        "SELECT v.* FROM videos v WHERE v.id=?", (vid,))]
    if not rows:
        eprint("yt: unknown video id")
        return 1
    st = status_map([vid])
    det = detail(row_from_db(rows[0]), st.get(vid, {}))
    sz = os.get_terminal_size() if sys.stdout.isatty() else os.terminal_size((100, 30))
    cols, lines = sz.columns, sz.lines
    env = dict(os.environ,
               FZF_PREVIEW_COLUMNS=str(max(20, cols - 2)),
               FZF_PREVIEW_LINES=str(max(10, lines - 2)))
    print(f"yt: rendering {vid} at {env['FZF_PREVIEW_COLUMNS']}x"
          f"{env['FZF_PREVIEW_LINES']} via {script}\n", flush=True)
    try:
        subprocess.run([script, vid, det], env=env, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        eprint(f"yt: preview script failed - {e}")
        return 1
    return 0


def cmd_toggle_shorts(argv):
    """Cycle the shorts filter. One call, so fzf can bind it directly."""
    nxt = {"show": "hide", "hide": "only", "only": "show"}
    cfg_set("shorts", nxt.get(cfg("shorts"), "show"))
    print(cfg("shorts"))
    return 0


def cmd_urls(argv):
    """Watch URLs for whatever was passed, or bare ids with --ids.

    --ids exists so the fish side can validate a pasted block in one process
    instead of round-tripping every line."""
    bare = "--ids" in argv
    for a in argv:
        if a == "--ids":
            continue
        v = parse_video_id(a)
        if v:
            print(v if bare else f"https://www.youtube.com/watch?v={v}")
    return 0


def cmd_prefetch(argv):
    vids = [l.strip() for l in sys.stdin.read().splitlines() if l.strip()] if not argv else argv
    prefetch_thumbs([v for v in vids if re.fullmatch(r"[A-Za-z0-9_-]{11}", v)])
    return 0


def cmd_suggest(argv):
    print(suggest_category(parse_video_id(argv[0])) if argv else "unsorted")
    return 0


def cmd_title(argv):
    if not argv:
        return 2
    v = get_video(parse_video_id(argv[0]))
    print(v["title"] if v else "")
    return 0


def cmd_meta(argv):
    """Ensure we know these ids, fetching if needed. Used before saving.

    With --print, also emit `id \t title` per id afterwards - otherwise the
    caller pays a whole Python start-up per video just to show a list.
    """
    show = "--print" in argv
    ids = [v for v in (parse_video_id(a) for a in argv if a != "--print") if v]
    if ids:
        fetch_video_meta(ids)
    if show:
        for vid in ids:
            v = get_video(vid)
            title = (v["title"] if v else "") or ""
            print(f"{vid}\t{title or vid + '  (metadata unavailable)'}")
    return 0


DISPATCH = {
    "search": cmd_search, "home": cmd_home, "lib": cmd_lib,
    "save": cmd_save, "unsave": cmd_unsave, "note": cmd_note, "recat": cmd_recat,
    "cat": cmd_cat, "subs": cmd_subs, "auth": cmd_auth, "ch": cmd_channel,
    "playstart": cmd_playstart, "playend": cmd_playend, "resumepos": cmd_resumepos,
    "togglewatched": cmd_togglewatched,
    "continue": cmd_continue, "history": cmd_history, "markwatched": cmd_markwatched,
    "dl": cmd_dl, "dl-run": cmd_dlrun, "dl-status": cmd_dlstatus,
    "dl-clear": cmd_dlclear, "offline": cmd_offline, "path": cmd_path,
    "stats": cmd_stats, "doctor": cmd_doctor, "gc": cmd_gc, "export": cmd_export,
    "config": cmd_config, "config-get": cmd_config_get, "ui": cmd_ui,
    "settings": cmd_settings, "choices": cmd_choices, "win-self": cmd_winself,
    "header": cmd_header, "boot": cmd_boot,
    "probe": cmd_probe, "cooldown": cmd_cooldown,
    "toggle-shorts": cmd_toggle_shorts, "urls": cmd_urls,
    "preview-test": cmd_previewtest,
    "stream": cmd_stream, "proxy": cmd_proxy, "ceiling": cmd_ceiling, "playinfo": cmd_playinfo,
    "prefetch": cmd_prefetch, "suggest": cmd_suggest,
    "title": cmd_title, "meta": cmd_meta, "playfmt": cmd_playfmt,
    "refresh": cmd_refresh, "rec-refresh": cmd_rec_refresh,
}


def main(argv):
    if not argv:
        eprint("ytlib: need a subcommand")
        return 2
    fn = DISPATCH.get(argv[0])
    if not fn:
        eprint(f"ytlib: unknown subcommand '{argv[0]}'")
        return 2
    try:
        return fn(argv[1:]) or 0
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    except KeyboardInterrupt:
        return 130
    except sqlite3.Error as e:
        eprint(f"yt: database error - {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
