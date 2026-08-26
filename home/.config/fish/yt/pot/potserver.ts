// Localhost-only HTTP front end for bgutil-ytdlp-pot-provider's SessionManager.
//
// Why yt needs a PO token at all
// ------------------------------
// Every stream resolution asks YouTube for a proof-of-origin token. Minting one
// means running BotGuard, which is a browser-shaped JavaScript workload. The
// provider ships two ways to do that: a script invoked per request, and a
// long-lived HTTP server. yt-dlp's plugin prefers the server and falls back to
// the script, and the difference is not small - the script costs a fresh Deno
// process plus a jsdom environment on *every* video, measured at ~3.9s, against
// ~3ms once a warm server holds the minter in memory.
//
// Why not the provider's own server/src/main.ts
// ---------------------------------------------
// It binds [::] / 0.0.0.0 - every interface on the machine. Upstream calls that
// temporary and says localhost is the plan. Nothing off this machine has any
// business minting tokens here, so this binds 127.0.0.1 and nothing else. It
// also drops express for Deno.serve, so the only imports are the provider's own
// modules, reached through the sibling `src` symlink.
//
// Run it from this directory - the node_modules symlink here is how Deno
// resolves the provider's dependencies. ytlib.warm_pot() does exactly that.
import { SessionManager } from "./src/session_manager.ts";
import { VERSION } from "./src/utils.ts";

const opt = (name: string, fallback: string) => {
  const i = Deno.args.indexOf(name);
  return i >= 0 && Deno.args[i + 1] ? Deno.args[i + 1] : fallback;
};
const PORT = Number(opt("--port", "4416"));
const IDLE_MS = Number(opt("--idle-minutes", "30")) * 60_000;

const sessions = new SessionManager();
let lastUsed = performance.now();

// Minting the first token of a session costs a BotGuard challenge and an
// IntegrityToken - the four seconds that made the first video of a session
// visibly slower than the rest, because yt-dlp asks for a token in the middle
// of resolving a stream and waits for the answer. Everything after it is
// served from SessionManager's caches in ~3ms.
//
// Nothing about that work depends on *which* video: it depends on the visitor
// id, which the proxy already holds before it resolves anything. So the proxy
// POSTs it here as soon as the picker opens and the mint happens against an
// idle machine instead of against a keypress. One warm per binding - a second
// request for one already in flight or already done is a no-op, not a second
// mint.
//
// The binding the proxy sends is the one yt-dlp will ask for on the current
// settings, so the token itself is usually reusable too. It does not have to
// be: SessionManager caches the minter independently of the binding, so even a
// binding that turns out not to match still leaves the expensive half done.
const warmed = new Set<string>();

function warm(contentBinding: string) {
  if (warmed.has(contentBinding)) return;
  warmed.add(contentBinding);
  sessions.generatePoToken(contentBinding)
    .then(() => console.log(`pre-warmed a minter for ${contentBinding.slice(0, 24)}...`))
    .catch((e) => {
      // Left out of `warmed` so a real request can try again: a failed warm
      // must not turn into a session with no token at all.
      warmed.delete(contentBinding);
      console.error("pre-warm failed: " + (e instanceof Error ? e.message : String(e)));
    });
}

// Nothing else reaps this process and it holds ~175 MB resident, so it lets
// itself go once a session is clearly over. Starting it again is automatic.
if (IDLE_MS > 0) {
  setInterval(() => {
    if (performance.now() - lastUsed > IDLE_MS) {
      console.log("idle, exiting");
      Deno.exit(0);
    }
  }, 60_000);
}

const json = (o: unknown, status = 200) =>
  new Response(JSON.stringify(o), {
    status,
    headers: { "content-type": "application/json" },
  });

async function readBody(req: Request): Promise<Record<string, unknown>> {
  try {
    return (await req.json()) ?? {};
  } catch {
    return {};
  }
}

Deno.serve({
  hostname: "127.0.0.1",
  port: PORT,
  onListen: () => console.log(`POT server ${VERSION} on 127.0.0.1:${PORT}`),
}, async (req) => {
  const path = new URL(req.url).pathname;

  // The plugin pings before a batch of requests and refuses a server whose
  // version it does not recognise, so this must report the provider's own.
  if (req.method === "GET" && path === "/ping") {
    return json({ server_uptime: performance.now() / 1000, version: VERSION });
  }

  if (req.method === "POST" && path === "/get_pot") {
    lastUsed = performance.now();
    const b = await readBody(req);
    for (const dead of ["data_sync_id", "visitor_data", "disable_innertube"]) {
      if (b[dead]) return json({ error: `${dead} is deprecated` }, 400);
    }
    // Nothing authenticates a caller here - yt-dlp's plugin has no notion of
    // a token, so any process on this machine can POST /get_pot. That is
    // acceptable for minting a token it could mint itself, but not for
    // talking a warm minter into trusting a forged certificate, so this one
    // stays off regardless of what the request asks for.
    if (b.disable_tls_verification) {
      return json({ error: "disable_tls_verification is not permitted" }, 400);
    }
    try {
      return json(await sessions.generatePoToken(
        b.content_binding as string | undefined,
        b.proxy as string,
        (b.bypass_cache as boolean) || false,
        b.source_address as string | undefined,
        false,   // disable_tls_verification: refused above
        b.challenge,
        b.innertube_context,
      ));
    } catch (e) {
      console.error(e instanceof Error ? e.stack : String(e));
      return json({ error: String(e) }, 500);
    }
  }

  // Not part of the provider's protocol and yt-dlp never calls it; this is
  // ours. Returns immediately - the caller is warming, not waiting.
  if (req.method === "POST" && path === "/warm") {
    lastUsed = performance.now();
    const b = await readBody(req);
    const cb = b.content_binding;
    if (typeof cb !== "string" || !cb) {
      return json({ error: "content_binding required" }, 400);
    }
    warm(cb);
    return json({ warming: true }, 202);
  }

  if (req.method === "POST" && path === "/invalidate_caches") {
    sessions.invalidateCaches();
    warmed.clear();     // or the next warm is skipped for a binding we dropped
    return new Response(null, { status: 204 });
  }
  if (req.method === "POST" && path === "/invalidate_it") {
    sessions.invalidateIT();
    warmed.clear();
    return new Response(null, { status: 204 });
  }
  return new Response("bgutil POT provider (localhost only)\n", { status: 400 });
});
