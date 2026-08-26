"""A resident Deno process for YouTube's JS challenges.

Every stream resolution has to answer YouTube's `n` challenge, and the only
place the answer function exists is inside the ~2 MB player JS. yt-dlp solves
that by piping the player plus a bundled JavaScript parser into a fresh
`deno run` for every single video: measured here at 664-830 ms, essentially
all of it re-parsing a player that has not changed since last week.

This registers a JS-challenge provider that keeps one Deno process alive with
the parsed player already compiled, so the per-video cost is the two function
calls it actually needs:

    preprocess a new player   ~900 ms   once per player (YouTube rotates ~weekly)
    compile a cached one      ~550 ms   once per proxy start
    answer two challenges       ~1 ms   per video

It costs about 130 MB of resident memory while it is up, which is why it is
optional (`jsc_resident=0`) and why the preprocessing - the part that peaks at
330 MB - is done in a throwaway process rather than in the resident one.

Anything that goes wrong here rejects the request, and yt-dlp's own deno
provider picks it up exactly as if this module had never been imported.
"""

import hashlib
import json
import os
import subprocess
import sys
import threading

from yt_dlp.extractor.youtube.jsc._builtin.deno import DenoJCP
from yt_dlp.extractor.youtube.jsc._builtin.ejs import ScriptVariant
from yt_dlp.extractor.youtube.jsc.provider import (
    JsChallengeProviderRejectedRequest,
    JsChallengeProviderResponse,
    JsChallengeResponse,
    JsChallengeType,
    NChallengeOutput,
    SigChallengeOutput,
    register_preference,
    register_provider,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_JS = os.path.join(HERE, "ytjsc.js")
CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "yt", "jsc")

# A preprocessed player is ~4 MB. Two is enough to cover the day YouTube
# rotates: the one in use and the one just replaced.
KEEP_PLAYERS = 2
PRE_TIMEOUT = 180.0            # one-off preprocessing of a new player
CALL_TIMEOUT = 60.0            # a solve against an already-compiled player

# Deno is given no permissions at all. The solver source arrives down the pipe
# rather than being read from disk, so the untrusted player JS this runs has
# no filesystem and no network to reach for.
DENO_ARGS = ["run", "--ext=js", "--no-prompt", "--no-remote", "--no-lock",
             "--no-npm", "--node-modules-dir=none", "--no-config", "--cached-only"]

# V8's lite mode drops the optimising compiler. Measured on the resident
# process: 131 MB -> 101 MB, peak 147 MB -> 101 MB, and the compile of a
# cached player is *faster* (291ms -> 246ms) because there is no JIT to warm
# up. A solve goes from 0.29ms to 0.35ms, which is nothing next to the ~1s
# resolve it sits inside.
#
# Explicitly NOT used for the one-off preprocessing below: that is 2 MB of
# JavaScript through a parser and a code generator, exactly the workload the
# optimiser exists for, and lite mode takes it from 690ms to 2401ms.
RESIDENT_FLAGS = ["--v8-flags=--lite-mode"]


class _Solver:
    """The resident process. One per interpreter; guarded by its own lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.next_id = 0
        self.loaded = set()      # player_url values this process has compiled
        self.dead = False        # a failure bad enough to stop trying

    # -- process ---------------------------------------------------------
    def _spawn(self, exe, boot, idle):
        p = subprocess.Popen([exe, *DENO_ARGS, *RESIDENT_FLAGS, SERVER_JS,
                              str(idle), str(KEEP_PLAYERS)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True,
                             start_new_session=False)
        self.proc, self.loaded = p, set()
        r = self._raw(boot, PRE_TIMEOUT)
        if not r.get("ok"):
            raise RuntimeError(f"solver boot failed: {r.get('error')}")

    def _alive(self):
        return self.proc is not None and self.proc.poll() is None

    def close(self):
        p, self.proc = self.proc, None
        self.loaded = set()
        if p is None:
            return
        for pipe in (p.stdin, p.stdout):
            # Both, not just stdin: the read end left open kept a pipe fd per
            # solver restart, and the solver restarts every time it idles out.
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # kill() without a wait() leaves a zombie, and reap() cannot help
            # because self.proc has already been cleared - so it stayed one
            # for the life of the daemon.
            p.kill()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    # -- messaging -------------------------------------------------------
    def _raw(self, msg, timeout):
        """One request, one reply. Kills the process rather than leaving a
        half-read pipe behind, because a desynchronised pipe would hand the
        next caller somebody else's answer."""
        self.next_id += 1
        msg["id"] = self.next_id
        line = json.dumps(msg)
        got = []

        # The process is read once, here: a timeout below calls close(),
        # which clears self.proc while this thread may still be between the
        # write and the read - and an AttributeError there is not one of the
        # exceptions this used to catch, so it surfaced as a traceback on the
        # daemon's stderr instead of the timeout it already was.
        proc = self.proc

        def pump():
            try:
                proc.stdin.write(line + "\n")
                proc.stdin.flush()
                got.append(proc.stdout.readline())
            except Exception as e:
                got.append(e)

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive() or not got or isinstance(got[0], Exception) or not got[0]:
            self.close()
            raise RuntimeError("solver stopped responding")
        r = json.loads(got[0])
        if r.get("id") != msg["id"]:
            self.close()
            raise RuntimeError("solver replied out of order")
        return r

    def solve(self, exe, boot, key, requests, get_player, cache, idle):
        """[{type,challenges}] -> the server's `responses` list.

        The lock is held for the whole exchange: the pipe carries one reply
        per request with no framing beyond order, so two callers interleaving
        on it would each get the other's answer.
        """
        with self.lock:
            if self.dead:
                raise RuntimeError("solver disabled after an earlier failure")
            # Three passes at most. The extra one is for the process idling
            # out in the window between _alive() saying yes and the write
            # reaching it - which is not a rare race, because the whole point
            # of a short idle timeout is that it fires between videos.
            last = None
            for _ in range(3):
                try:
                    if not self._alive():
                        self.close()
                        self._spawn(exe, {"boot": boot}, idle)
                    if key not in self.loaded:
                        pre = cache.load(key)
                        if pre is None:
                            pre = _preprocess(exe, boot, get_player())
                            cache.store(key, pre)
                        r = self._raw({"key": key, "pre": pre, "requests": requests},
                                      PRE_TIMEOUT)
                        if not r.get("ok"):
                            raise RuntimeError(r.get("error") or "solver rejected the player")
                        self.loaded.add(key)
                        return r["responses"]
                    r = self._raw({"key": key, "requests": requests}, CALL_TIMEOUT)
                    if r.get("need"):
                        # Dropped by the server's own rotation past KEEP_PLAYERS.
                        self.loaded.discard(key)
                        continue
                    if not r.get("ok"):
                        raise RuntimeError(r.get("error") or "solver failed")
                    return r["responses"]
                except (BrokenPipeError, OSError, ValueError) as e:
                    last = e
                    self.close()
            raise RuntimeError(f"solver would not stay up ({last})"
                               if last else "solver kept losing the player")


def _preprocess(exe, boot, player):
    """Parse a new player in a throwaway process.

    Deliberately not done in the resident one: parsing peaks around 330 MB and
    V8 hands very little of that back, so doing it here is the difference
    between ~130 MB resident and ~330 MB.
    """
    p = subprocess.Popen([exe, *DENO_ARGS, SERVER_JS, "0", "1"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True)   # full JIT here
    try:
        out, _ = p.communicate(
            json.dumps({"id": 1, "boot": boot}) + "\n"
            + json.dumps({"id": 2, "key": "p", "player": player, "requests": []}) + "\n",
            timeout=PRE_TIMEOUT)
    except subprocess.TimeoutExpired:
        # kill() alone leaves it a zombie: the pipes are still open and
        # nothing has waited on it. communicate() after the kill is what the
        # subprocess docs ask for, and it is the only reaper this child gets.
        p.kill()
        try:
            p.communicate(timeout=10)
        except Exception:
            pass
        raise RuntimeError("preprocessing a new player timed out")
    lines = [ln for ln in (out or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        raise RuntimeError("preprocessing produced no result")
    r = json.loads(lines[1])
    if not r.get("ok") or not r.get("pre"):
        raise RuntimeError(r.get("error") or "preprocessing returned nothing")
    return r["pre"]


class _PreCache:
    """Preprocessed players on disk, newest KEEP_PLAYERS kept.

    yt-dlp has this same cache built in and leaves it switched off because
    "files are large and we do not support rotation" - so this rotates.
    """

    def _path(self, key):
        return os.path.join(CACHE_DIR,
                            hashlib.sha256(key.encode()).hexdigest()[:32] + ".js")

    def load(self, key):
        try:
            with open(self._path(key), encoding="utf-8") as fh:
                return fh.read() or None
        except OSError:
            return None

    def store(self, key, pre):
        try:
            os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
            path = self._path(key)
            tmp = f"{path}.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(pre)
            os.replace(tmp, path)
            # The file is raw JS, so the URL it belongs to lives beside it -
            # that is the only thing warm() has to go on at start-up.
            with open(path + ".url", "w", encoding="utf-8") as fh:
                fh.write(key)
            self._rotate()
        except OSError:
            pass

    def _rotate(self):
        try:
            files = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR)
                     if f.endswith(".js")]
            files.sort(key=lambda f: os.stat(f).st_mtime, reverse=True)
            for f in files[KEEP_PLAYERS:]:
                os.unlink(f)
                try:
                    os.unlink(f + ".url")
                except OSError:
                    pass
        except OSError:
            pass


_SOLVER = _Solver()
_CACHE = _PreCache()


def _idle():
    """Seconds the resident process may sit unused before it exits.

    Short on purpose. V8 gives almost nothing back once it has compiled a
    player - dropping it and forcing a collection only took 108 MB to 93 MB -
    so the only way to stop paying for a solver nobody is using is to let the
    process go. Coming back costs a 15 ms spawn and a ~250 ms compile, and
    that lands on a speculative resolve rather than on the user, because
    prefetching is what wakes it. A 30 minute video therefore holds nothing.
    """
    try:
        import ytlib
        return max(0, int(float(ytlib.cfg("jsc_idle_minutes") or 5) * 60))
    except Exception:
        return 300


@register_provider
class ResidentJCP(DenoJCP):
    PROVIDER_NAME = "deno-resident"
    JS_RUNTIME_NAME = "deno"
    # Nothing here writes to yt-dlp's own cache; _PreCache does the rotation
    # that the built-in one explicitly does not.
    _ENABLE_PREPROCESSED_PLAYER_CACHE = False

    def is_available(self, /):
        if _SOLVER.dead or os.environ.get("YT_JSC_RESIDENT") == "0":
            return False
        return os.path.exists(SERVER_JS) and super().is_available()

    def _real_bulk_solve(self, /, requests):
        if not requests:
            return
        exe = self.runtime_info.path
        if self._lib_script.variant is ScriptVariant.DENO_NPM:
            # That variant pulls meriyah in over `npm:`, which needs a
            # populated Deno npm cache and permissions this deliberately does
            # not grant. Let the built-in provider have it.
            raise JsChallengeProviderRejectedRequest(
                "resident solver needs the self-contained lib script", expected=True)
        boot = {"lib": self._lib_script.code, "core": self._core_script.code}

        grouped = {}
        for r in requests:
            grouped.setdefault(r.input.player_url, []).append(r)

        for player_url, group in grouped.items():
            payload = [{"type": r.type.value, "challenges": r.input.challenges}
                       for r in group]
            vid = next((r.video_id for r in group), None)
            try:
                responses = _SOLVER.solve(
                    exe, boot, player_url, payload,
                    lambda: self._get_player(vid, player_url), _CACHE, _idle())
            except Exception as e:
                # Rejected, not errored: the point of this provider is to be
                # invisible when it cannot help, and an error would be
                # reported to the user for something yt-dlp is about to do
                # correctly anyway.
                raise JsChallengeProviderRejectedRequest(
                    f"resident solver unavailable ({e})", expected=True) from e
            if len(responses) != len(group):
                raise JsChallengeProviderRejectedRequest(
                    "resident solver returned the wrong number of answers", expected=True)
            for request, data in zip(group, responses):
                if data.get("type") != "result" or not isinstance(data.get("data"), dict):
                    yield JsChallengeProviderResponse(
                        request, None, RuntimeError(data.get("error") or "no answer"))
                    continue
                out = (NChallengeOutput(data["data"])
                       if request.type is JsChallengeType.N
                       else SigChallengeOutput(data["data"]))
                yield JsChallengeProviderResponse(
                    request, JsChallengeResponse(request.type, out))


@register_preference(ResidentJCP)
def _prefer_resident(provider, requests):
    # Above the built-in deno provider's 1000, so this is tried first and the
    # built-in one is still right there when it rejects.
    return 2000


def warm(get_player_url=None):
    """Compile the cached player now rather than on the first video.

    Called from a background thread at proxy start-up. Only ever loads a
    player that is already preprocessed on disk - it will not go to the
    network, so a rotation is discovered by the first real resolve.
    """
    try:
        import yt_dlp                                   # noqa: F401
        from yt_dlp.utils._jsruntime import DenoJsRuntime
        info = DenoJsRuntime().info
        if not info or not info.supported:
            return False
        files = sorted(
            (os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR)
             if f.endswith(".js")),
            key=lambda f: os.stat(f).st_mtime, reverse=True)
        if not files:
            return False
        key = _key_of(files[0])
        if not key:
            return False
        # The preprocessed player is ~3.9 MB and this is called on a timer now
        # - the solver drops itself after a few minutes idle and something has
        # to notice - so find out whether there is anything to do before
        # reading it.
        with _SOLVER.lock:
            if _SOLVER._alive() and key in _SOLVER.loaded:
                return True
        with open(files[0], encoding="utf-8") as fh:
            pre = fh.read()
        from yt_dlp_ejs.yt import solver
        boot = {"lib": solver.lib(), "core": solver.core()}
        with _SOLVER.lock:
            if not _SOLVER._alive():
                _SOLVER._spawn(info.path, {"boot": boot}, _idle())
            if key not in _SOLVER.loaded:
                r = _SOLVER._raw({"key": key, "pre": pre, "requests": []}, PRE_TIMEOUT)
                if r.get("ok"):
                    _SOLVER.loaded.add(key)
        return True
    except Exception:
        return False


def _key_of(path):
    """The player URL a cache file belongs to, from its sidecar."""
    try:
        with open(path + ".url", encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def reap():
    """Clear up after a solver that timed out on its own.

    Popen.poll() is a non-blocking waitpid, so calling it is what stops an
    exited child sitting as a zombie until the proxy itself ends - which, with
    a five minute idle timeout and a half hour proxy, is most of the time.
    Cheap enough to call on a timer: one syscall, and nothing at all once the
    child has been reaped.
    """
    p = _SOLVER.proc
    if p is not None and p.poll() is not None:
        # Only take the lock once there is something to do; a solve in
        # progress holds it for as long as an extraction.
        if _SOLVER.lock.acquire(blocking=False):
            try:
                if _SOLVER.proc is p:
                    _SOLVER.proc = None
                    _SOLVER.loaded = set()
            finally:
                _SOLVER.lock.release()


def shutdown():
    _SOLVER.close()
