// Resident YouTube JS-challenge solver.
//
// Why this exists
// ---------------
// Every stream resolution has to answer YouTube's `n` (and sometimes `sig`)
// challenge, which means running a function that only exists inside the ~2 MB
// player JS. yt-dlp does that by piping the player plus a bundled parser into
// a fresh `deno run` per video. Measured on this machine: 664-830 ms, and the
// entire cost is re-parsing the same player again for every single video.
//
// The parse result is good for as long as YouTube ships that player - about a
// week. So this holds it. Deno starts once, keeps the compiled challenge
// functions in a Map keyed by player URL, and answers over a pipe:
//
//   preprocess a new player   ~630 ms   (once per player, ~weekly)
//   compile a cached one       ~30 ms   (once per process start)
//   answer two challenges       ~4 ms   (per video)
//
// Protocol: one JSON object per line on stdin, one per line on stdout.
//   {"id":N,"boot":{"lib":"...","core":"..."}}      -> {"id":N,"ok":true}
//   {"id":N,"key":URL,"requests":[...]}             -> {"id":N,"ok":false,"need":"player"}
//   {"id":N,"key":URL,"pre":"...","requests":[...]} -> {"id":N,"ok":true,"responses":[...]}
//   {"id":N,"key":URL,"player":"...","requests":[]} -> ... plus "pre" for caching
//
// It is given no Deno permissions at all: the caller sends the solver source
// down the pipe rather than letting this read it, so a compromised player
// cannot reach the network or the filesystem from in here.

const IDLE_MS = Number(Deno.args[0] ?? 1800) * 1000;
const MAX_PLAYERS = Number(Deno.args[1] ?? 2);

let jsc = null;                 // the ejs core entry point, once booted
const players = new Map();      // player_url -> {n, sig}
let lastUsed = performance.now();

const enc = new TextEncoder();
function reply(obj) {
  // Looped on the return value, not written once and hoped for: a
  // preprocessed player is ~4 MB going down a 64 KB pipe, and writeSync is
  // allowed to take less than it was given. A short write there would put
  // half a JSON document on the wire, which the parent reads as a corrupt
  // reply and answers by killing this process.
  const bytes = enc.encode(JSON.stringify(obj) + "\n");
  let off = 0;
  while (off < bytes.length) {
    const n = Deno.stdout.writeSync(bytes.subarray(off));
    if (n <= 0) break;
    off += n;
  }
}

function boot(src) {
  // `new Function` rather than eval so the bundled lib's own top-level
  // declarations cannot collide with anything in this module.
  jsc = new Function(
    `${src.lib}\nObject.assign(globalThis, lib);\n${src.core}\nreturn jsc;`,
  )();
  if (typeof jsc !== "function") throw new Error("core script exposed no jsc()");
}

// The preprocessed player is a program that assigns the two solver functions
// onto the object it is handed. That is exactly what ejs's own core does with
// it; doing it here is what lets the result be kept.
function compile(pre) {
  const fns = { n: null, sig: null };
  new Function("_result", pre)(fns);
  if (!fns.n && !fns.sig) throw new Error("preprocessed player yielded no functions");
  return fns;
}

function remember(key, fns) {
  players.set(key, fns);
  while (players.size > MAX_PLAYERS) players.delete(players.keys().next().value);
}

function solve(fns, requests) {
  return requests.map((r) => {
    const fn = fns[r.type];
    if (!fn) return { type: "error", error: `no ${r.type} function in this player` };
    try {
      return {
        type: "result",
        data: Object.fromEntries(r.challenges.map((c) => [c, fn(c)])),
      };
    } catch (e) {
      return { type: "error", error: e instanceof Error ? `${e.message}\n${e.stack}` : `${e}` };
    }
  });
}

function handle(msg) {
  lastUsed = performance.now();
  if (msg.boot) {
    boot(msg.boot);
    return { id: msg.id, ok: true };
  }
  if (!jsc) return { id: msg.id, ok: false, error: "not booted" };

  let fns = players.get(msg.key);
  let pre = null;
  if (!fns && msg.pre) {
    fns = compile(msg.pre);
    remember(msg.key, fns);
  }
  if (!fns && msg.player) {
    // Only path that pays the ~630 ms parse. `pre` goes back with the answer
    // so the caller can put it on disk and skip this after a restart.
    const out = jsc({ type: "player", player: msg.player, requests: [], output_preprocessed: true });
    if (out.type === "error") throw new Error(out.error);
    pre = out.preprocessed_player;
    if (!pre) throw new Error("player preprocessing returned nothing");
    fns = compile(pre);
    remember(msg.key, fns);
  }
  if (!fns) return { id: msg.id, ok: false, need: "player" };

  const out = { id: msg.id, ok: true, responses: solve(fns, msg.requests || []) };
  if (pre) out.pre = pre;
  return out;
}

if (IDLE_MS > 0) {
  // Two jobs. Letting go of a compiled player is one - V8 will not return
  // that memory any other way, so the process itself has to end. Being tidy
  // is the other: stdin closing is the normal way this exits, but a parent
  // killed without closing the pipe would otherwise leave this resident.
  //
  // The check is frequent enough to honour a short timeout without polling
  // pointlessly on a long one.
  const every = Math.min(30_000, Math.max(1_000, Math.floor(IDLE_MS / 4)));
  const timer = setInterval(() => {
    if (performance.now() - lastUsed > IDLE_MS) Deno.exit(0);
  }, every);
  Deno.unrefTimer(timer);
}

// Lines can be megabytes (a raw player is ~2 MB), so this reads raw chunks and
// splits on newlines itself rather than using a line-oriented reader with a
// buffer ceiling. No JSON value we emit or accept contains a bare newline.
let buf = "";
const dec = new TextDecoder();
for await (const chunk of Deno.stdin.readable) {
  buf += dec.decode(chunk, { stream: true });
  let nl;
  while ((nl = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, nl);
    buf = buf.slice(nl + 1);
    if (!line.trim()) continue;
    // Exactly one reply per line, whatever happens. Replying from inside the
    // try meant a throw after a partial write could send a second one, and
    // the pipe has no framing beyond order - so the caller after that would
    // read somebody else's answer.
    let msg = null;
    let out;
    try {
      msg = JSON.parse(line);
      out = handle(msg);
    } catch (e) {
      out = { id: msg?.id ?? null, ok: false, error: e instanceof Error ? e.message : String(e) };
    }
    reply(out);
  }
}
