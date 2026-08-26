#!/usr/bin/env python3
"""Local range-chunking proxy for YouTube streams.

Why this exists
---------------
YouTube's media URLs carry `rqh=1`: they answer only *bounded* range requests,
of at most about 1 MiB. ffmpeg opens a stream with a single open-ended GET
(`Range: bytes=0-`, or none at all), which YouTube answers with 403. That is
why yt-dlp can download a video fine while mpv cannot stream the same URL, and
why streaming otherwise falls back to a 360p muxed rendition.

No ffmpeg option fixes it - `-end_offset`, `-headers`, `-seekable` and
`--stream-lavf-o` were all tried. So this sits in between: mpv makes ordinary
requests to it, and it talks to YouTube in the small bounded chunks YouTube
insists on. Result: anonymous 1080p streaming, no cookies, seeking intact.

URL shape
---------
    http://127.0.0.1:<port>/v/<videoid>/video?t=<token>
    http://127.0.0.1:<port>/v/<videoid>/audio?t=<token>

Deliberately addressed by *video id*, not by the signed googlevideo URL. Those
URLs are re-signed on every extraction, so embedding one would give mpv a
different filename each time and break watch-later resume - mpv keys resume
state on the path. Port and token are stable across runs, so this URL is
stable, and `/v/<id>/` also lets the id be recovered from mpv's resume file.

Security
--------
Narrow on purpose: binds 127.0.0.1 only; every request needs the session token;
upstream is restricted to *.googlevideo.com so this is not a general web proxy;
it exits on its own once idle.
"""

import base64
import http.client
import http.server
import json
import os
import queue
import re
import secrets
import socket
import socketserver
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _ytlib():
    """Imported lazily: ytlib launches this module, so a module-level import
    here would be circular."""
    import ytlib
    return ytlib

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# Measured against live googlevideo on player_client=tv_simply, sequential
# reads on one connection, 8 chunks each:
#   256 KiB  8/8 ok  1.65 MiB/s        1 MiB  8/8 ok  4.83 MiB/s
#   512 KiB  8/8 ok  3.28 MiB/s        2 MiB  8/8 ok  2.16 MiB/s
# 1 MiB is the sweet spot. (On yt-dlp's default android_vr client the same
# test gave 1/5 at 1 MiB and forced a much smaller value - that turned out to
# be truncated delivery on that client rather than a real transfer limit,
# which is why stream_client exists.)
# The first chunk stays small regardless: time-to-first-byte is 380ms at 1 MiB
# against ~30ms at 128 KiB, and mpv only needs the container header to start.
FIRST_CHUNK = 1 << 17      # 128 KiB - just enough for mpv to start decoding
CHUNK = 1 << 20            # 1 MiB steady state
READAHEAD = 1              # chunks fetched ahead of the write
# A response that hits the end of a signed URL's budget mid-video has to
# re-extract or stop, and stopping is the visible failure: mpv reads short and
# ends the file. Both numbers were sized when a resolve cost 2.5s, which left
# room for barely one attempt inside the window; on the fast path it is nearer
# 320ms, and the two streams now share one extraction between them, so the
# budget buys three real tries instead of one and a timeout.
MAX_RESOLVES = 3           # re-extractions allowed within one response
RESOLVE_BUDGET = 15.0      # seconds one response may spend re-extracting
CAP_MEMORY = 600           # seconds to remember a confirmed cut-off offset
RETRIES = 4                # YouTube 403s intermittently under sequential load
# The longest a single chunk may take before the response gives up on it:
# every retry, every re-extraction, and room to spare. Only ever reached if
# the reader thread is gone, which is the case this exists to survive.
CHUNK_DEADLINE = 180.0
IDLE_EXIT = 1800           # seconds without traffic before shutting down
URL_TTL = 3 * 3600         # re-resolve well before googlevideo URLs expire
DEFAULT_PORT = 8791
ALLOWED_HOST = re.compile(r"^[A-Za-z0-9.-]+\.googlevideo\.com$")
VID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

STATE_DIR = (os.environ.get("YT_DATA_DIR")
             or os.path.join(os.path.expanduser("~"), ".local", "share", "yt"))
STATE_FILE = os.path.join(STATE_DIR, "proxy.json")
# The token is part of the URL mpv sees, and mpv keys watch-later resume on
# that URL. Re-minting it on every cold start would orphan every resume file
# and quietly break "continue watching", so it is persisted separately from
# the per-run state.
TOKEN_FILE = os.path.join(STATE_DIR, "proxy.token")

_last = time.time()
_inflight = 0
_lock = threading.Lock()
# (vid, kind) -> (offset, learned_at). YouTube serves a bounded number of bytes
# per video per address and then 403s everything past that point, fresh URLs
# included. Re-extracting to find that out costs 3-5s of yt-dlp, so once it is
# known it is remembered: a seek past the ceiling then fails instantly instead
# of freezing playback while three extractions discover the same thing again.
_caps = {}
_caps_lock = threading.Lock()


# A cap is only enforced once the same ceiling has been hit twice. One
# failure is not proof of a ceiling - a dropped connection, a 403 that a
# retry would have cleared, a moment of 429 all look identical from here -
# and enforcing it on the strength of one meant a transient failure truncated
# every later play of that video at the same byte for ten minutes, which is a
# video that plays and then stops, again and again, at the same spot.
def cap_of(vid, kind):
    with _caps_lock:
        hit = _caps.get((vid, kind))
        if not hit:
            return None
        off, at, seen = hit
        if time.time() - at > CAP_MEMORY:
            _caps.pop((vid, kind), None)
            return None
        return off if seen >= 2 else None


def note_cap(vid, kind, off):
    with _caps_lock:
        now = time.time()
        for k in [k for k, v in _caps.items() if now - v[1] > CAP_MEMORY]:
            _caps.pop(k, None)          # cap_of() only expires what it reads
        hit = _caps.get((vid, kind))
        if not hit:
            _caps[(vid, kind)] = (off, now, 1)
        else:
            # Confirmation, not a new observation, unless it moved lower.
            _caps[(vid, kind)] = (min(off, hit[0]), now, hit[2] + 1)


def clear_cap(vid, kind):
    with _caps_lock:
        _caps.pop((vid, kind), None)
_resolved = {}             # videoid -> (expiry, {"video": url, "audio": url})
_resolve_lock = threading.Lock()
# Idle upstream connections, shared across threads.
#
# These used to be keyed by (thread, host), which made exclusivity trivial but
# meant every connection mpv opened started with its own TLS handshake:
# measured at 248 ms for the first range request against 41-48 ms once the
# connection was warm, on every playback, for video and for audio. Keyed by
# host and checked in and out, a handshake paid during a speculative resolve
# is still there when mpv arrives.
#
# Exclusivity now comes from ownership: a connection is out of the pool for as
# long as somebody is using it, and either goes back or gets closed.
_pool = {}                 # host -> [(HTTPSConnection, idle since), ...]
_pool_lock = threading.Lock()
POOL_PER_HOST = 8          # a burst of seeks must not leave dozens open
POOL_MAX_IDLE = 90.0       # google closes these on its own; do not race it


# ---- upstream connections ---------------------------------------------

def _take(host):
    """(connection, was_it_already_open). The caller owns it either way."""
    now = time.time()
    dead = []
    with _pool_lock:
        idle = _pool.get(host)
        got = None
        while idle:
            c, since = idle.pop()
            if now - since <= POOL_MAX_IDLE:
                got = c
                break
            dead.append(c)
        if idle is not None and not idle:
            # Every video can land on a different rrN---snXXX host, so the
            # empty lists have to go or the map grows for the life of the run.
            _pool.pop(host, None)
    for c in dead:
        _shut(c)
    if got is not None:
        return got, True
    return http.client.HTTPSConnection(host, timeout=30), False


def _give(host, c):
    over = None
    with _pool_lock:
        idle = _pool.setdefault(host, [])
        if len(idle) >= POOL_PER_HOST:
            over = c
        else:
            idle.append((c, time.time()))
    _shut(over)


def _pool_sweep():
    """Close connections nobody came back for.

    Without this a host visited once keeps a socket until the proxy exits,
    and googlevideo hosts change from video to video.
    """
    cut = time.time() - POOL_MAX_IDLE
    dead = []
    with _pool_lock:
        for host in list(_pool):
            keep = [(c, since) for c, since in _pool[host] if since > cut]
            dead.extend(c for c, since in _pool[host] if since <= cut)
            if keep:
                _pool[host] = keep
            else:
                _pool.pop(host, None)
    for c in dead:
        _shut(c)


def _shut(c):
    if c is None:
        return
    try:
        c.close()
    except Exception:
        pass


def _pool_size():
    with _pool_lock:
        return sum(len(v) for v in _pool.values())


def fetch_chunk(url, start, end):
    """One bounded range request, retried. Returns (bytes|None, last_status).

    403 is handled differently from 429/5xx on purpose. A signed URL serves a
    limited number of bytes and then refuses everything past that point
    permanently - waiting does not help, and the old uniform backoff turned
    each such refusal into an 8-second stall inside the response body. Only a
    fresh extraction gets past it, so 403 is reported quickly and the caller
    decides whether to spend a re-resolve on it.
    """
    parts = urllib.parse.urlsplit(url)
    if not ALLOWED_HOST.match(parts.hostname or ""):
        return None, 0
    path = parts.path + ("?" + parts.query if parts.query else "")
    headers = {"User-Agent": UA, "Range": f"bytes={start}-{end}",
               "Accept": "*/*", "Connection": "keep-alive"}
    delay = 0.25
    status = 0
    attempt = 0
    stale = 0
    while attempt < RETRIES:
        c, reused = _take(parts.netloc)
        try:
            c.request("GET", path, headers=headers)
            r = c.getresponse()
            body = r.read()
            status = r.status
        except Exception:
            _shut(c)
            if reused and stale < POOL_PER_HOST:
                # A pooled connection the far end had already closed. That is
                # not a failure to back off from - it is the ordinary cost of
                # keeping connections, and the retry gets a fresh one. It must
                # not spend an attempt either: a pool full of connections
                # googlevideo hung up on would otherwise use up every retry
                # without one real request ever leaving the machine, and the
                # caller would read that as "YouTube refused us".
                stale += 1
                continue
            attempt += 1
            time.sleep(delay)
            delay = min(delay * 2, 4.0)
            continue
        if status in (200, 206):
            _give(parts.netloc, c)
            return body, status
        _shut(c)
        attempt += 1
        if status == 403:
            if attempt >= 2:
                return None, status
            time.sleep(0.15)
            continue
        if status in (429, 500, 502, 503, 504):
            time.sleep(delay)
            delay = min(delay * 2, 4.0)
            continue
        return None, status
    return None, status


# ---- resolving a video id to stream urls -------------------------------

def invalidate(vid):
    """Forget a cached resolution so the next resolve() re-extracts."""
    with _resolve_lock:
        _resolved.pop(vid, None)
    try:
        _ytlib().db().execute("DELETE FROM cache WHERE key=?", (f"stream:{vid}",))
        _ytlib().db().commit()
    except Exception:
        pass


# ---- extraction, in this process --------------------------------------
#
# Shelling out to yt-dlp costs the same fixed start-up on every single video:
# ~130ms of interpreter, ~150ms to load 1744 extractors, and ~380ms for the
# PO-token plugin to work out which providers exist. This daemon is already
# long-lived, so it can hold one YoutubeDL and pay all of that once. Measured
# per resolve on this machine: 2.35s via subprocess, 1.75s in process.
#
# The subprocess path stays as the fallback for anything this cannot do -
# yt_dlp missing, an API change, an unexpected exception - because a slow
# resolve is a much better failure than no playback.
_ydl = None
_ydl_key = None
_ydl_dead = False
_ydl_misses = 0           # consecutive in-process failures
_YDL_GIVE_UP = 3          # ...after which every video would pay for both paths
_ydl_lock = threading.Lock()


class _Collector:
    """yt-dlp logger that keeps what a subprocess would have put on stderr,
    so the rate-limit guard sees exactly the same text it always has."""

    def __init__(self):
        self.lines = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.lines.append(str(msg))

    def error(self, msg):
        self.lines.append(str(msg))


_jsc = None                # the ytjsc module, once it has registered itself
_jsc_tried = False


def _install_resident_jsc(yl):
    """Register the resident JS-challenge solver, if it is wanted and works.

    Answering YouTube's `n` challenge is the single most expensive part of a
    resolve - 664-830 ms measured here, almost all of it re-parsing the same
    player JS for every video. ytjsc keeps one Deno process with that player
    already compiled, which takes the per-video cost to about 1 ms.

    It is registered as a *provider*, so if it is missing, misbehaving, or
    switched off, yt-dlp's own deno provider handles the challenge exactly as
    it did before. Costs ~130 MB resident while up; `jsc_resident=0` opts out.
    """
    global _jsc, _jsc_tried
    if _jsc_tried:
        return _jsc
    _jsc_tried = True
    try:
        if yl.cfg("jsc_resident") == "0":
            return None
        import ytjsc
    except Exception as e:
        sys.stderr.write(f"ytproxy: no resident JS solver ({e})\n")
        sys.stderr.flush()
        return None
    _jsc = ytjsc
    # Compiling the cached player takes ~550 ms and needs no network, so it
    # happens off the play path: by the time the first video is resolved the
    # process is usually already holding it.
    threading.Thread(target=_warm_jsc, daemon=True).start()
    return _jsc


def _warm_jsc():
    try:
        if _jsc.warm():
            sys.stderr.write("ytproxy: resident JS solver warm\n")
            sys.stderr.flush()
    except Exception:
        pass


def _warm_all(yl):
    """Everything that can be made ready before a video is picked."""
    try:
        _install_resident_jsc(yl)
    except Exception:
        pass
    threading.Thread(target=_warm_ydl, args=(yl,), daemon=True).start()
    threading.Thread(target=_warm_pot, args=(yl,), daemon=True).start()


# The PO-token minter's first token of a session costs a BotGuard challenge
# and an IntegrityToken; every one after it is served from its own cache. That
# first mint happens *inside* the first resolve, because yt-dlp asks for a
# token while extracting and waits for the answer - which is a large part of
# why the first video of a session is slower than the rest.
#
# It does not depend on which video: it depends on the visitor id, which this
# process already holds by the time the picker is open. So hand it over and let
# the mint happen while the user is still reading titles.
_pot_warmed = set()


def _warm_pot(yl):
    """Tell the local minter which visitor id to have a token ready for."""
    vd = None
    try:
        if yl.cfg("pot_server") != "1" or yl.cfg("pot_prewarm") == "0":
            return
        if yl.at_risk():
            return
        # Fetching a visitor id costs one small request, but it is the same
        # request the first resolve would make a moment later - so this moves
        # it rather than adding it, and it is cached for six hours either way.
        # A session that ends without playing anything has spent one GET of a
        # static asset and left the next session warmer.
        vd = _visitor_data(yl)
        if not vd or vd in _pot_warmed:
            return
        _pot_warmed.add(vd)
        body = json.dumps({"content_binding": vd}).encode()
        # ytlib starts the minter at the same moment the picker opens, and it
        # needs a few seconds before it is listening - from genuinely cold, on
        # a machine that has not run Deno since boot, about nine. Giving up on
        # the first refused connection would mean the pre-warm only ever works
        # when it was not needed, so this waits for it. All of it happens in a
        # daemon thread while the user reads titles.
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                c = http.client.HTTPConnection("127.0.0.1", yl.POT_PORT,
                                               timeout=5)
                c.request("POST", "/warm", body,
                          {"Content-Type": "application/json"})
                r = c.getresponse()
                r.read()
                c.close()
            except OSError:
                time.sleep(0.5)         # not listening yet
                continue
            if r.status == 202:
                sys.stderr.write("ytproxy: PO-token minter pre-warming\n")
                sys.stderr.flush()
                return
            # A minter that answers but refuses is an older one without this
            # endpoint, or something else on the port. Retrying will not help
            # and the first resolve mints as it always did.
            break
    except Exception:
        pass
    # Reached only when the warm did not take. Leaving the id in the set would
    # mean never trying it again for the life of this daemon.
    if vd:
        _pot_warmed.discard(vd)


def _warm_ydl(yl):
    """Build the YoutubeDL before the first video needs it.

    Constructing one loads 1744 extractors and asks the PO-token plugin which
    providers exist: ~500 ms of pure CPU, no network, and it was being paid on
    the first resolve of every session. Nothing here extracts anything, so it
    cannot spend a request.
    """
    try:
        with _ydl_lock:
            if _ydl_for(yl, yl.dl_format()[0], (yl.cfg("stream_client") or "").strip()):
                sys.stderr.write("ytproxy: extractor warm\n")
                sys.stderr.flush()
    except Exception:
        pass


def _ydl_for(yl, fmt, client):
    """One YoutubeDL per (format, client). Rebuilt when either changes, so a
    settings edit takes effect without restarting the daemon."""
    global _ydl, _ydl_key, _ydl_dead
    key = (fmt, client)
    if _ydl is not None and _ydl_key == key:
        return _ydl
    if _ydl_dead:
        return None
    # Deliberately NOT installing ytlib's brotli shim into yt_dlp/urllib3 here.
    # It does halve the watch page (302 KiB -> 114 KiB), but paired against
    # gzip over six extractions it was faster in only three of them, median
    # +34ms - the page is bound by how fast YouTube generates it, not by the
    # wire. Monkey-patching a third-party HTTP stack on the playback path for
    # a result that does not survive measurement is not a trade worth making.
    try:
        import yt_dlp
    except Exception as e:
        _ydl_dead = True
        sys.stderr.write(f"ytproxy: no in-process yt-dlp ({e}); "
                         "falling back to a subprocess per video\n")
        sys.stderr.flush()
        return None
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "socket_timeout": 20, "retries": 2, "extractor_retries": 1,
        "format": fmt, "skip_download": True, "logger": _Collector(),
    }
    if client:
        opts["extractor_args"] = {"youtube": {"player_client": [client]}}
    try:
        if _ydl is not None:
            _ydl.close()
    except Exception:
        pass
    _install_resident_jsc(yl)
    try:
        _ydl = yt_dlp.YoutubeDL(opts)
    except Exception:
        _ydl, _ydl_dead = None, True
        return None
    if _ydl_key is None:
        # Said once per daemon, because "why is playback slow again" is
        # otherwise unanswerable: this line is the difference between 1.7s and
        # 2.4s per video, and nothing else reports which path was taken.
        try:
            vsn = yt_dlp.version.__version__
        except Exception:
            vsn = "?"
        sys.stderr.write(f"ytproxy: extracting in process (yt-dlp {vsn})\n")
        sys.stderr.flush()
    _ydl_key = key
    return _ydl


# ---- skipping the watch page ------------------------------------------

# The watch page is 1.4 MB of HTML that YouTube streams out over ~700 ms, and
# it is 68% of what a resolve costs once the JS challenge is no longer the
# bottleneck. Measured raw against a socket it is just as slow, so it is not
# yt-dlp overhead: it is how fast YouTube builds the page.
#
# The only thing that extraction actually needs out of it is a visitor id.
# `sw.js_data` hands one over in ~99 ms and 2.8 KB, and yt-dlp takes it as an
# extractor argument - so with `player_skip=webpage,initial_data` the whole
# page, and the `next` API call that would otherwise replace it, both go away.
# Measured against the same warm extractor, alternating: 1151 ms -> 316 ms.
#
# The trade is that resolves inside one window share a visitor id instead of
# each getting its own from a fresh page. That is what a browser does - it
# keeps one for the life of the profile - so it is if anything less unusual
# looking, but it does link a session's playbacks together, which is why the
# window is a setting and `skip_webpage=0` turns the whole thing off.
VISITOR_KEY = "proxy:visitor"
_vd_memo = None                # (value, when to re-read the shared copy)
_vd_lock = threading.Lock()
_vd_strikes = 0                # consecutive fast-path failures
_vd_benched = 0.0              # ...and until when we stop trying
_VD_STRIKES = 2
_VD_BENCH = 1800.0


def _fetch_visitor(yl):
    """A fresh anonymous visitor id. One small GET, no account, no cookies."""
    try:
        raw = yl.http_get("https://www.youtube.com/sw.js_data", timeout=10,
                          max_bytes=1 << 20)
        txt = raw.decode("utf-8", "replace")
        i = txt.find("[")
        data = json.loads(txt[i:]) if i >= 0 else None
    except Exception:
        return None
    # The id is buried at an unstable depth in a nested array, so this looks
    # for its shape rather than its position.
    found = []

    def walk(node, depth=0):
        if found or depth > 8:
            return
        if isinstance(node, str):
            if len(node) > 30 and node.startswith("Cg"):
                found.append(node)
        elif isinstance(node, list):
            for x in node:
                walk(x, depth + 1)

    walk(data)
    return found[0] if found else None


def _visitor_data(yl, force=False):
    global _vd_memo
    now = time.time()
    if not force:
        m = _vd_memo
        if m and m[1] > now:
            return m[0]
    with _vd_lock:
        m = _vd_memo
        if m and m[1] > now and not force:
            return m[0]
        ttl = max(600, int(float(yl.cfg("visitor_ttl_hours") or 6) * 3600))
        got = None
        if not force:
            try:
                got = yl.cache_get(VISITOR_KEY)
            except Exception:
                got = None
        if not isinstance(got, str) or not got:
            got = _fetch_visitor(yl)
            if not got:
                return None
            try:
                yl.cache_put(VISITOR_KEY, got, ttl)
            except Exception:
                pass
        # Memo expires long before the shared copy, so a rotation started by
        # another thread or a later run is picked up without a DB read per
        # video.
        _vd_memo = (got, now + 300)
        return got


def _skip_args(ydl, vd):
    """Point a live YoutubeDL at the fast path, or back at the slow one.

    Mutated in place rather than rebuilt: extractor args are read from
    params on every call, and constructing a YoutubeDL costs ~500 ms and
    throws away the cached player JS with it.
    """
    ya = ydl.params.setdefault("extractor_args", {}).setdefault("youtube", {})
    if vd:
        ya["player_skip"] = ["webpage", "initial_data"]
        ya["visitor_data"] = [vd]
    else:
        ya.pop("player_skip", None)
        ya.pop("visitor_data", None)


# ---- where a resolve actually goes ------------------------------------

# "The first video takes longer than the rest" is the one performance report
# that no amount of reading the code settles, because every stage of a resolve
# has a cold cost and a warm cost and they are paid in a different order
# depending on what the picker managed to warm first. So each resolve says
# where its time went, once, in one line. It costs a handful of
# perf_counter() calls against an operation measured in hundreds of
# milliseconds.
class _Phases:
    __slots__ = ("t0", "last", "marks", "notes")

    def __init__(self):
        self.t0 = self.last = time.perf_counter()
        self.marks = []
        self.notes = []

    def mark(self, name):
        now = time.perf_counter()
        self.marks.append((name, (now - self.last) * 1000))
        self.last = now

    def note(self, text):
        self.notes.append(text)

    def report(self, what):
        total = (time.perf_counter() - self.t0) * 1000
        # Sub-millisecond stages are noise here and there are several of them;
        # printing them buries the one that mattered.
        parts = " ".join(f"{n} {ms:.0f}" for n, ms in self.marks if ms >= 1)
        tail = (" [" + ", ".join(self.notes) + "]") if self.notes else ""
        sys.stderr.write(f"ytproxy: {what} in {total:.0f}ms"
                         + (f" ({parts})" if parts else "") + tail + "\n")
        sys.stderr.flush()


def _extract_inproc(yl, vid, fmt, ph=None):
    """[video_url, audio_url] or None to mean "use the subprocess instead".

    Everything run_ytdlp() does around the call - the back-off check, the
    request log, the risk patterns - has to happen here too, or the budgets
    silently stop counting playback.
    """
    global _vd_strikes, _vd_benched
    if yl.at_risk():
        if ph:
            ph.note("rate-limit guard unhappy")
        return None
    client = (yl.cfg("stream_client") or "").strip()
    want_skip = (yl.cfg("skip_webpage") != "0"
                 and time.time() > _vd_benched)
    with _ydl_lock:
        # Only ever waited on when two videos resolve at once, but that is
        # exactly what the first video of a session does against a speculative
        # prefetch, so it has to be visible.
        if ph:
            ph.mark("lock")
        ydl = _ydl_for(yl, fmt, client)
        if ph:
            ph.mark("ydl")
        if ydl is None:
            return None
        log = ydl.params["logger"]
        vd = _visitor_data(yl) if want_skip else None
        if ph:
            ph.mark("visitor")
        info = None
        for attempt in (0, 1):
            _skip_args(ydl, vd)
            del log.lines[:]
            try:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}",
                                        download=False)
            except Exception as e:
                log.lines.append(str(e))
                info = None
            if info or not vd:
                break
            # The fast path came back empty. An expired visitor id is the
            # likely reason and it is cheap to rule out, so try once with a
            # fresh one, then once the ordinary way - which is the path that
            # has always worked.
            if attempt == 0:
                vd = _visitor_data(yl, force=True)
                continue
        if vd and info:
            _vd_strikes = 0
        elif want_skip and not info:
            _vd_strikes += 1
            if _vd_strikes >= _VD_STRIKES:
                # Paying for both paths on every video is worse than never
                # having tried, so stop for a while and say so once.
                _vd_benched = time.time() + _VD_BENCH
                _vd_strikes = 0
                sys.stderr.write("ytproxy: skipping the watch page is not "
                                 "working; using the full path for the next "
                                 f"{int(_VD_BENCH / 60)} minutes\n")
                sys.stderr.flush()
        if want_skip and not info:
            # Last chance before the subprocess: the slow path, in process.
            _skip_args(ydl, None)
            del log.lines[:]
            try:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}",
                                        download=False)
            except Exception as e:
                log.lines.append(str(e))
                info = None
        if ph:
            ph.mark("extract")
            ph.note("fast path" if vd else "watch page")
        stderr = "\n".join(log.lines)

    risky = bool(yl.RISK_PATTERNS.search(stderr))
    yl.log_req("anon:stream", ok=not risky)
    if risky:
        yl.bump_risk(log.lines[-1].strip() if log.lines else "unknown")
        return None
    global _ydl_misses, _ydl_dead
    if not info:
        _ydl_misses += 1
        if _ydl_misses >= _YDL_GIVE_UP:
            # Falling back per video means paying for both paths every time,
            # which is worse than never having tried. Say so and stop.
            _ydl_dead = True
            sys.stderr.write(f"ytproxy: in-process extraction failed "
                             f"{_ydl_misses}x, using subprocesses from now on"
                             + (f" (last: {stderr.splitlines()[-1][:120]})"
                                if stderr.strip() else "") + "\n")
            sys.stderr.flush()
        return None
    _ydl_misses = 0
    yl.ease_risk()
    fmts = info.get("requested_formats") or ([info] if info.get("url") else [])
    urls = [f["url"] for f in fmts if f.get("url")]
    return urls or None


# mpv opens the video stream and the audio stream in the same instant, and a
# speculative resolve may still be in flight when it does. Nothing stopped the
# three of them extracting the same video independently: the request log shows
# pairs a second apart, so every cold playback cost two full round trips to
# YouTube instead of one, counted twice against the request budget, and the
# audio stream waited for its own extraction rather than reusing the answer
# that was already on its way.
#
# Per video rather than one global lock: two different videos have no reason to
# wait for each other, and the picker resolving one must not hold up playback
# of another. The entry is dropped as soon as nobody is waiting on it, so this
# does not grow with the number of videos seen.
_resolve_busy = {}             # videoid -> [lock, waiters]
_resolve_busy_lock = threading.Lock()


def _busy_lock(vid):
    with _resolve_busy_lock:
        ent = _resolve_busy.get(vid)
        if ent is None:
            ent = _resolve_busy[vid] = [threading.Lock(), 0]
        ent[1] += 1
        return ent[0]


def _busy_done(vid):
    with _resolve_busy_lock:
        ent = _resolve_busy.get(vid)
        if ent is None:
            return
        ent[1] -= 1
        if ent[1] <= 0:
            _resolve_busy.pop(vid, None)


def resolve(vid):
    """videoid -> {"video": url, "audio": url|None}, cached until near expiry.

    Anonymous on purpose: an unauthenticated extraction is the one that still
    offers the full adaptive format list, and it keeps the account out of
    ordinary playback entirely.
    """
    now = time.time()
    with _resolve_lock:
        hit = _resolved.get(vid)
        if hit and hit[0] > now:
            return hit[1]
        # Expired entries were never dropped, only overwritten if the same
        # video came round again - so a long session accumulated a pair of
        # multi-kilobyte signed URLs for every video ever played. Sweeping
        # here costs one pass over a dict that is normally a handful of
        # entries, and only when something actually needs re-resolving.
        for k in [k for k, v in _resolved.items() if v[0] <= now]:
            _resolved.pop(k, None)
    lk = _busy_lock(vid)
    try:
        with lk:
            return _resolve_locked(vid)
    finally:
        _busy_done(vid)


def refresh(vid, kind, spent):
    """A fresh URL for one stream, extracted once however many streams ask.

    A signed URL serves a bounded number of bytes, and mpv's video and audio
    reach the end of theirs within a second of each other. Each one used to
    invalidate the other's brand new resolution on its way to making its own,
    so a mid-video stall cost two extractions and could leave a stream holding
    the very URL the other had just discarded - which is a video that plays
    for a while and then stops, with a re-extraction that fixed nothing.
    """
    lk = _busy_lock(vid)
    try:
        with lk:
            with _resolve_lock:
                hit = _resolved.get(vid)
            if hit and hit[0] > time.time():
                fresh = (hit[1] or {}).get(kind)
                if fresh and fresh != spent:
                    return hit[1]          # the other stream already did it
            invalidate(vid)
            return _resolve_locked(vid)
    finally:
        _busy_done(vid)


def _resolve_locked(vid):
    """resolve() with this video's extraction lock held."""
    # Whoever we queued behind has published their answer by now.
    now = time.time()
    with _resolve_lock:
        hit = _resolved.get(vid)
        if hit and hit[0] > now:
            return hit[1]
    ph = _Phases()
    try:
        yl = _ytlib()
    except Exception:
        return None
    # Persisted across proxy restarts, so replaying something you watched
    # earlier skips the 2-5s extraction entirely.
    try:
        cached = yl.stream_cache_get(vid)
    except Exception:
        cached = None
    if cached and cached.get("video"):
        with _resolve_lock:
            _resolved[vid] = (now + URL_TTL, cached)
        ph.report(f"{vid} from the stream cache")
        return cached
    ph.mark("cache")
    fmt = yl.dl_format()[0]
    urls = _extract_inproc(yl, vid, fmt, ph)
    if urls is None:
        # The subprocess path is ~700ms of interpreter and extractor loading
        # before it has even asked YouTube anything, so a session that ends up
        # here is a session where every video is slow. Worth naming.
        ph.note("subprocess fallback")
        rc, out, err = yl.run_ytdlp(
            [f"https://www.youtube.com/watch?v={vid}", "-f", fmt, "-g"] + yl.client_args(),
            which=None, kind="stream", timeout=120)
        urls = [u.strip() for u in (out or "").splitlines()
                if u.strip().startswith("http")]
        ph.mark("subprocess")
    if not urls:
        ph.report(f"{vid} FAILED to resolve")
        return None
    got = {"video": urls[0], "audio": urls[1] if len(urls) > 1 else None}
    with _resolve_lock:
        _resolved[vid] = (now + URL_TTL, got)
    try:
        yl.stream_cache_put(vid, got)
    except Exception:
        pass
    ph.report(f"resolved {vid}")
    return got


def total_size(url):
    m = re.search(r"[?&]clen=(\d+)", url)
    if m:
        return int(m.group(1))
    parts = urllib.parse.urlsplit(url)
    if not ALLOWED_HOST.match(parts.hostname or ""):
        return None
    # Two goes, because the first connection may be one googlevideo closed
    # while it sat in the pool. There is no retry above this: a None here is
    # a 502 to mpv, which shows a window and closes it again.
    for _ in range(2):
        c, _reused = _take(parts.netloc)
        try:
            c.request("GET", parts.path + "?" + parts.query,
                      headers={"User-Agent": UA, "Range": "bytes=0-0"})
            r = c.getresponse()
            cr = r.getheader("Content-Range", "")
            r.read()
            _give(parts.netloc, c)
            m = re.search(r"/(\d+)$", cr)
            if m:
                return int(m.group(1))
            return None
        except Exception:
            _shut(c)
    return None


# ---- http ---------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ytproxy"

    def log_message(self, *a):
        pass

    @staticmethod
    def _why(msg):
        sys.stderr.write(f"ytproxy: {msg}\n")
        sys.stderr.flush()

    def _fail(self, code):
        try:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
        except Exception:
            pass
        self.close_connection = True

    def _parse(self):
        u = urllib.parse.urlparse(self.path)
        if u.netloc or u.scheme:
            # Absolute-form request URIs are for talking to a proxy, and this
            # is an origin server. mpv never sends one; anything that does is
            # asking this daemon to answer for a host that is not it, so the
            # authority is not quietly ignored the way urlparse would have it.
            return None, None
        q = urllib.parse.parse_qs(u.query)
        tok = q.get("t", [""])[0]
        try:
            if not secrets.compare_digest(tok, self.server.token):
                return None, None
        except TypeError:
            return None, None            # non-ASCII token: not ours
        m = re.match(r"^/v/([A-Za-z0-9_-]{11})/(video|audio|info)$", u.path)
        if not m:
            return None, None
        return m.group(1), m.group(2)

    def _guard(self, head_only):
        """Count in-flight requests so the idle watchdog cannot shut down
        mid-stream, and turn an unexpected error into a real response rather
        than a silently dropped socket."""
        global _last, _inflight
        with _lock:
            _inflight += 1
        try:
            self._serve(head_only)
        except Exception:
            self._fail(500)
        finally:
            with _lock:
                _inflight -= 1
                _last = time.time()

    def do_HEAD(self):
        self._guard(True)

    def do_GET(self):
        self._guard(False)

    def _serve(self, head_only):
        global _last
        with _lock:
            _last = time.time()

        if urllib.parse.urlparse(self.path).path == "/health":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head_only:
                self.wfile.write(body)
            return

        vid, kind = self._parse()
        if not vid:
            return self._fail(403)
        try:
            got = resolve(vid)
        except Exception:
            got = None
        if kind == "info":
            # This request is the last thing that happens before mpv is
            # exec'd, and mpv's first read is ~250 ms of TLS handshake against
            # a cold googlevideo connection - measured against 41-48 ms on a
            # warm one. The picker's speculative resolve already warms the
            # pool for the row it guessed; this covers every play it did not
            # guess, and it runs while mpv is still starting up rather than
            # holding this response.
            if got:
                threading.Thread(target=_warm_media, args=(got,),
                                 daemon=True).start()
            body = json.dumps({
                "video": bool(got and got.get("video")),
                "audio": bool(got and got.get("audio")),
            }).encode()
            self.send_response(200 if got else 502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head_only:
                self.wfile.write(body)
            return
        if not got or not got.get(kind):
            # Silent until now, which made "mpv opened and closed again" an
            # unanswerable question: this is the one line that says whether
            # the stream never existed or the transfer died partway.
            self._why(f"{vid} {kind}: nothing to serve "
                      f"({'no formats' if not got else 'no ' + kind + ' url'})")
            return self._fail(502)
        url = got[kind]
        total = total_size(url)
        if not total:
            self._why(f"{vid} {kind}: no content length")
            return self._fail(502)

        start, end, partial = 0, total - 1, False
        rng = (self.headers.get("Range") or "").strip()
        if rng:
            # Bounded digits on purpose. int() refuses to parse a string of
            # more than 4300 digits, and a header may carry 64 KiB of them,
            # so an unbounded \d* turned a malformed range into a 500 out of
            # the exception handler. Anything this long is out of range by
            # definition; 20 digits is past any file size there will ever be.
            m = re.match(r"^bytes=(\d{0,20})-(\d{0,20})$", rng)
            if not m and re.match(r"^bytes=\d*-\d*$", rng):
                # digits past what any file could hold: unsatisfiable, and
                # answering 200-with-the-whole-file would be a lie about what
                # was asked for.
                try:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except Exception:
                    pass
                self.close_connection = True
                return
            if m and (m.group(1) or m.group(2)):
                partial = True
                if not m.group(1):
                    # suffix form "bytes=-N": the final N bytes
                    start = max(0, total - int(m.group(2)))
                else:
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), total - 1)
                # `end < start` is a legal thing to write and an
                # unsatisfiable thing to ask for. Falling through with it
                # produced "Content-Length: -1" and no body, which leaves a
                # keep-alive connection framed wrong for every request after
                # it rather than failing the one that was malformed.
                if start >= total or end < start:
                    try:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{total}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                    except Exception:
                        pass
                    self.close_connection = True
                    return

        length = end - start + 1
        try:
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.end_headers()
        except Exception:
            return
        if head_only:
            return

        state = {"url": url, "resolves": 0}

        def pull(a, b):
            """One chunk, re-extracting the URL if the signed one is spent."""
            known = cap_of(vid, kind)
            if known is not None and a >= known:
                # Already established that YouTube will not serve this far.
                # Answering immediately keeps a seek past the ceiling from
                # freezing mpv for the length of three yt-dlp runs.
                return None
            body, status = fetch_chunk(state["url"], a, b)
            if body:
                if known is not None:
                    clear_cap(vid, kind)      # budget came back
                return body
            # A signed URL serves a bounded number of bytes and then 403s
            # everything beyond it. Only a fresh extraction gets past that,
            # and each one costs a yt-dlp run, so they are budgeted twice
            # over - by count and by wall clock - because a blocked stream
            # would otherwise turn into an extraction per chunk.
            started = time.time()
            while (not body and state["resolves"] < MAX_RESOLVES
                   and time.time() - started < RESOLVE_BUDGET):
                state["resolves"] += 1
                fresh = refresh(vid, kind, state["url"])
                if not (fresh and fresh.get(kind)) or fresh[kind] == state["url"]:
                    break
                state["url"] = fresh[kind]
                body, status = fetch_chunk(state["url"], a, b)
            if not body:
                # A refusal at byte zero is not a ceiling. YouTube serves the
                # start of any URL it signed, so this is a transport failure or
                # a URL that was never good - and remembering it as a cap made
                # every later play of the same video answer an empty body
                # instantly for the next ten minutes, which looks exactly like
                # mpv opening and closing again for no reason.
                if a > 0:
                    note_cap(vid, kind, a)
                sys.stderr.write(
                    f"ytproxy: {vid} {kind} "
                    + (f"would not start (status {status})"
                       if a == 0 else
                       f"cut off at {a} of {total} bytes "
                       f"({100.0 * a / total:.0f}%)")
                    + f" after {state['resolves']} re-extraction(s), "
                      f"last status {status}\n")
                sys.stderr.flush()
            return body

        # One chunk in flight while the previous one is written. A single
        # long-lived reader thread rather than a pool: upstream connections
        # are keyed per thread, so a fresh thread per chunk would mean a TLS
        # handshake per chunk and undo the gain.
        req_q = queue.Queue()
        res_q = queue.Queue()

        def reader():
            # Nothing above this catches. An exception out of pull() used to
            # end this thread with a chunk still owed, and the writer below
            # waits on that queue with no timeout - so the response hung for
            # ever, holding a thread and an in-flight count that stopped the
            # idle watchdog from ever shutting the daemon down. A failure has
            # to come back as "no body", which is a path that already exists.
            try:
                while True:
                    item = req_q.get()
                    if item is None:
                        return
                    try:
                        res_q.put(pull(*item))
                    except Exception as e:
                        self._why(f"{vid} {kind}: reader failed at "
                                  f"{item[0]}: {type(e).__name__}: {e}")
                        res_q.put(None)
            finally:
                pass        # connections go back to the shared pool, not here

        th = threading.Thread(target=reader, daemon=True)
        th.start()
        pos = start
        try:
            size = FIRST_CHUNK
            req_q.put((pos, min(pos + size - 1, end)))
            while pos <= end:
                # Bounded even so: the reader answers every request, but a
                # thread that died between the two is not a reason to wait
                # for ever. The ceiling is generous - one chunk may spend
                # RESOLVE_BUDGET re-extracting on top of its own retries.
                try:
                    body = res_q.get(timeout=CHUNK_DEADLINE)
                except queue.Empty:
                    self._why(f"{vid} {kind}: no chunk within "
                              f"{CHUNK_DEADLINE:.0f}s at byte {pos}")
                    body = None
                if not body:
                    # Truncating makes the client see a short read and retry,
                    # which is better than leaving it waiting on bytes that
                    # are never coming.
                    self.close_connection = True
                    return
                nxt = pos + len(body)
                size = CHUNK          # ramp up once playback has started
                if nxt <= end:
                    req_q.put((nxt, min(nxt + size - 1, end)))
                # Refresh before the blocking write, not only after: mpv
                # pausing stalls this write indefinitely, and the watchdog
                # must not read that as an idle proxy.
                with _lock:
                    _last = time.time()
                self.wfile.write(body)
                pos = nxt
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError) as e:
            self.close_connection = True     # mpv seeked or exited: normal
            if pos == start:
                # Except when not a byte got through. A client that dropped
                # before the first chunk arrived is not seeking, it is giving
                # up, and that is the failure worth a line in the log.
                self._why(f"{vid} {kind}: client left before byte {start} "
                          f"({type(e).__name__})")
        except Exception as e:
            self.close_connection = True
            self._why(f"{vid} {kind}: {type(e).__name__}: {e}")
        finally:
            req_q.put(None)
            # Nothing to sweep: the pool is keyed by host, bounded per host,
            # and every connection is either handed back or closed by the
            # code that took it.


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    token = ""

    def handle_error(self, request, client_address):
        pass                                  # keep tracebacks off the terminal


# ---- lifecycle ----------------------------------------------------------

# secrets.token_urlsafe(24) is 32 characters. Anything materially shorter
# came from a truncated write or a hand-edited file, and a short token is a
# guessable one: mint a new one rather than serve behind it.
TOKEN_MIN = 24


def stable_token():
    """Read the persisted token, minting one only the first time."""
    try:
        with open(TOKEN_FILE, encoding="utf-8") as fh:
            tok = fh.read().strip()
        if len(tok) >= TOKEN_MIN:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    try:
        fd = os.open(TOKEN_FILE, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        # O_CREAT's mode applies only when it creates the file. Re-minting
        # into one that already existed - which is exactly what the check
        # above does to a truncated token - would otherwise keep whatever
        # mode it had.
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w") as fh:
            fh.write(tok)
    except OSError:
        pass
    return tok


def read_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            st = json.load(fh)
        port, token, pid = st["port"], st["token"], st["pid"]
    except (OSError, ValueError, KeyError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    # A recycled pid could belong to something else, so confirm the port is
    # actually ours by asking it.
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        c.request("GET", "/health")
        r = c.getresponse()
        ok = r.getheader("Server", "").startswith("ytproxy")
        r.read()
        c.close()
        if not ok:
            return None
    except Exception:
        return None
    return st


# ---- speculative resolve ----------------------------------------------

# The picker's preview script writes the id of the row under the cursor here
# on every focus change. That is a shell builtin writing eleven bytes to a
# file already in page cache, so it costs the picker nothing measurable - no
# process, no socket, no work at all if this daemon is not running.
FOCUS_FILE = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"),
    "yt", "focus")

_PREFETCH_TICK = 0.25      # how often the file is looked at
_PREFETCH_STALE = 45.0     # ignore a focus older than this: the picker is gone
_REWARM = 60.0             # how often to check that the warm things still are


def _warm_media(got):
    """Open the TLS connection mpv is about to need, and leave it in the pool.

    A handshake and nothing else - no range request, so not a byte comes off
    the ceiling YouTube meters per video per address. Measured through the
    proxy: 248 ms to first byte on a cold connection against 41-48 ms on a
    warm one, for video and for audio.
    """
    if not got:
        return
    # Held, then all given back at the end: video and audio are normally on
    # the same googlevideo host, and giving each one back before opening the
    # next would just hand the same single connection round and leave mpv's
    # second stream to do its own handshake.
    held = []
    try:
        for url in (got.get("video"), got.get("audio")):
            if not url:
                continue
            parts = urllib.parse.urlsplit(url)
            if not ALLOWED_HOST.match(parts.hostname or ""):
                continue
            try:
                c, reused = _take(parts.netloc)
                if not reused:
                    c.connect()
                held.append((parts.netloc, c))
            except Exception:
                pass
    finally:
        for netloc, c in held:
            _give(netloc, c)


def prefetch_watcher():
    """Resolve the row the cursor has settled on, before Enter is pressed.

    Resolving a stream costs 1-2.3s and is the whole of the wait before the
    first frame. Almost all of it can be spent while the user is still reading
    the title and looking at the thumbnail, because the row they play is
    nearly always the row they were just looking at.

    Deliberately cautious, because every prefetch is a real extraction against
    YouTube and a wasted one is a request spent for nothing:

      - it waits for the cursor to *settle* (`prefetch_dwell`), so scrolling
        through a screen of rows asks for nothing at all;
      - it does at most `prefetch_max` in any ten minutes;
      - it does nothing while the rate-limit guard is unhappy, nothing for a
        video already resolved or already on disk, and nothing at all when
        `prefetch_focus` is off.
    """
    yl = None
    seen = None                 # the id this thread has already acted on
    recent = []                 # timestamps of prefetches, for the budget
    reread = 0.0                # config is cached per process; this is 30 min
    swept = 0.0
    warmed = 0.0                # when the warm-ups were last looked at
    while True:
        time.sleep(_PREFETCH_TICK)
        try:
            if time.time() - swept > 30:
                swept = time.time()
                _pool_sweep()
            if _jsc is not None:
                # The solver exits on its own idle timer, well before this proxy
                # does. Somebody has to waitpid it or it lingers as a zombie for
                # the rest of the proxy's half hour.
                try:
                    _jsc.reap()
                except Exception:
                    pass
            if yl is not None and time.time() - reread > 30:
                reread = time.time()
                try:
                    yl.cfg_reload()
                except Exception:
                    pass
            try:
                st = os.stat(FOCUS_FILE)
            except OSError:
                continue
            age = time.time() - st.st_mtime
            if age > _PREFETCH_STALE:
                continue
            try:
                if yl is None:
                    yl = _ytlib()
                dwell = float(yl.cfg("prefetch_dwell") or 2)
            except Exception:
                continue
            try:
                with open(FOCUS_FILE, encoding="utf-8") as fh:
                    vid = fh.read(64).strip()
            except OSError:
                continue
            if not vid or not VID_RE.match(vid):
                # The picker empties this on the way out, and an empty file has a
                # fresh mtime - so it is the one case that must not read as
                # interest, or leaving the picker would warm a proxy that is
                # about to be idle.
                continue
            if time.time() - warmed > _REWARM:
                # A row under the cursor means the picker is open and something
                # is likely to be played, which is the moment to pay the
                # warm-up costs - and the only moment worth paying them.
                # Importing yt-dlp is 34 MB of extractor classes and the solver
                # is ~100 MB, and a proxy that is only streaming a video mpv
                # resumed needs neither. Both are pure CPU and no request is
                # spent, so this happens before the dwell gate rather than after.
                #
                # On a timer rather than once, because the solver drops itself
                # after a few minutes idle - which is most of the length of a
                # video - and nothing else noticed. Coming back to the picker
                # after watching something was paying the ~250 ms respawn and
                # the compile all over again, on the play. Each of these is a
                # cheap no-op when there is nothing to do.
                warmed = time.time()
                _warm_all(yl)
            if age < dwell:
                continue            # still moving
            if vid == seen:
                continue
            seen = vid
            try:
                if yl.cfg("prefetch_focus") == "0":
                    continue
                now = time.time()
                recent[:] = [t for t in recent if now - t < 600]
                if len(recent) >= int(yl.cfg("prefetch_max") or 6):
                    continue
                if yl.at_risk():
                    continue
                with _resolve_lock:
                    if vid in _resolved and _resolved[vid][0] > now:
                        continue
                if yl.stream_cache_get(vid):
                    continue
                # A downloaded copy is played from disk; nothing to resolve.
                row = yl.db().execute(
                    "SELECT 1 FROM downloads WHERE video_id=? AND status='done'",
                    (vid,)).fetchone()
                if row:
                    continue
                recent.append(now)
                _warm_media(resolve(vid))
            except Exception:
                continue
        except Exception:
            # This thread is also the connection-pool sweeper and the
            # solver's zombie reaper. Letting an exception end it does
            # not cost a prefetch, it costs a daemon that leaks sockets
            # and zombies for the rest of its life - so nothing in here
            # is allowed to be fatal.
            continue


def idle_watchdog(srv):
    while True:
        time.sleep(30)
        try:
            if _idle_check(srv):
                return
        except Exception:
            # An exception here used to end the thread, which leaves a daemon
            # that holds its port and its memory until the machine reboots.
            # Better to try again in thirty seconds.
            continue


def _idle_check(srv):
    """True once the shutdown has been started."""
    with _lock:
        idle = time.time() - _last
        busy = _inflight
    if idle <= IDLE_EXIT or busy:
        return False
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            if json.load(fh).get("pid") == os.getpid():
                os.unlink(STATE_FILE)
    except (OSError, ValueError):
        pass
    threading.Thread(target=srv.shutdown, daemon=True).start()
    return True


class _Stamped:
    """sys.stderr, with seconds-since-start in front of every line.

    proxy.log is read to answer questions like "was the extractor already warm
    when that video was resolved" and "how long after launch did the first
    playback happen". Both are unanswerable from unstamped lines, and this is
    the only place all of them pass through.
    """
    def __init__(self, inner, t0):
        self._inner, self._t0, self._bol = inner, t0, True

    def write(self, text):
        for piece in text.splitlines(keepends=True):
            if self._bol and piece.strip():
                self._inner.write(f"[{time.monotonic() - self._t0:7.2f}] ")
            self._inner.write(piece)
            self._bol = piece.endswith("\n")
        return len(text)

    def flush(self):
        self._inner.flush()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def main():
    sys.stderr = _Stamped(sys.stderr, time.monotonic())
    # 0700, not the umask's 0755: the token that authorises every request
    # this process will serve is one of the files in here.
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    try:
        import stat as _stat
        if _stat.S_IMODE(os.stat(STATE_DIR).st_mode) & 0o077:
            os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass
    st = read_state()
    if st:
        print(f"{st['port']} {st['token']}")
        return 0

    token = stable_token()
    srv = None
    for port in (DEFAULT_PORT, 0):
        try:
            srv = Server(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        print("cannot bind", file=sys.stderr)
        return 1
    srv.token = token
    port = srv.server_address[1]

    tmp = f"{STATE_FILE}.{os.getpid()}.tmp"
    # O_CREAT with the mode, rather than create-then-chmod: the token is in
    # this file, and the gap between the two was a readable one.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"port": port, "token": token, "pid": os.getpid()}, fh)
        os.replace(tmp, STATE_FILE)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    print(f"{port} {token}", flush=True)
    sys.stdout.close()
    # Nothing is warmed here. Warming happens when the picker first says a row
    # is under the cursor - see prefetch_watcher - so a proxy started only to
    # stream a video stays at about 50 MB instead of 190 MB.
    threading.Thread(target=idle_watchdog, args=(srv,), daemon=True).start()
    threading.Thread(target=prefetch_watcher, daemon=True).start()
    try:
        srv.serve_forever()
    finally:
        if time.time() - swept > 30:
            swept = time.time()
            _pool_sweep()
        if _jsc is not None:
            try:
                _jsc.shutdown()
            except Exception:
                pass
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                if json.load(fh).get("pid") == os.getpid():
                    os.unlink(STATE_FILE)
        except (OSError, ValueError):
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
