// CLM gateway: runs on the public hostname in front of the Cloudflare Tunnel to the Pod.
// Each agent sends its own key as "Authorization: Bearer <key>"; the gateway checks it,
// rate-limits per agent, and forwards with the single upstream CLM_API_KEY, which never
// leaves Cloudflare. Revoking an agent is removing its entry from AGENT_KEYS.
//
// It also collects routing decisions (clm.decisions.HttpSink) at /v1/decisions, in D1.
//
// Bindings (infra/Pulumi.yaml):
//   AGENT_KEYS         secret, JSON {"<agent name>": "<key>"}
//   UPSTREAM_KEY       secret, the Pod's CLM_API_KEY
//   LIMITER            rate limit, keyed by agent name
//   DECISIONS          D1 database for /v1/decisions (optional: absent -> 404)
//   DECISION_READERS   JSON ["<agent name>", ...] that may read every agent's decisions
//   ADMIN_AGENTS       JSON ["<agent name>", ...] that may use /v1/admin/* (uploading heads)
//   ACCESS_TEAM_DOMAIN the Cloudflare Zero Trust team domain (<team>.cloudflareaccess.com)
//   ACCESS_AUD         the aud tag of the Access application guarding /login (absent: logins are off)
//   ALLOWED_EMAIL_DOMAIN  e.g. metatheory.gg: only these Google accounts get personal keys
//
// Personal keys: a person signs in at /login (Google, through Cloudflare Access) and gets a key
// tied to their email, which then names their agent in logs, rate limits and decision reads.
// Agents get one without copy-paste through a device login: POST /v1/auth/device returns a short
// code to approve at /login, and POST /v1/auth/token hands over the key once. Keys are stored
// only as SHA-256 hashes and last until revoked at /login (by their owner or an admin agent).

const encoder = new TextEncoder();

// Hash both sides first so the comparison is constant-time and length-independent.
async function sameKey(a, b) {
  const [x, y] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(a)),
    crypto.subtle.digest("SHA-256", encoder.encode(b)),
  ]);
  return crypto.subtle.timingSafeEqual(x, y);
}

async function agentFor(request, env, ctx) {
  const header = request.headers.get("Authorization") || "";
  if (!header.startsWith("Bearer ")) return null;
  const presented = header.slice("Bearer ".length);
  let found = null;
  // check every entry so the time taken does not reveal which agent matched
  for (const [agent, key] of Object.entries(JSON.parse(env.AGENT_KEYS))) {
    if ((await sameKey(presented, key)) && found === null) found = agent;
  }
  if (found !== null || !presented.startsWith(KEY_PREFIX) || !env.DECISIONS) return found;
  // a personal key: looked up by its hash, so the table never holds a usable key
  await ensureSchema(env.DECISIONS);
  const hash = await sha256hex(presented);
  const row = (await env.DECISIONS.prepare("SELECT email, revoked_at FROM api_keys WHERE key_hash = ?")
    .bind(hash).all()).results[0];
  if (!row || row.revoked_at) return null;
  const now = new Date(), stale = new Date(now - 10 * 60 * 1000).toISOString();
  const touch = env.DECISIONS.prepare(
    "UPDATE api_keys SET last_used_at = ? WHERE key_hash = ? AND (last_used_at IS NULL OR last_used_at < ?)")
    .bind(now.toISOString(), hash, stale).run();
  if (ctx?.waitUntil) ctx.waitUntil(touch); else await touch;
  return row.email;
}

function error(status, message) {
  return Response.json({ detail: message }, { status });
}

// ── personal keys: Google sign-in (Cloudflare Access) and device login ─────

const KEY_PREFIX = "clm_";
const CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ";       // no vowels or look-alikes
const DEVICE_TTL_MS = 10 * 60 * 1000;
const POLL_SECONDS = 3;

const b64url = (bytes) => btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const fromB64url = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), (c) => c.charCodeAt(0));
const randomBytes = (n) => crypto.getRandomValues(new Uint8Array(n));
async function sha256hex(s) {
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(s)))]
    .map((b) => b.toString(16).padStart(2, "0")).join("");
}
function userCode() {
  const out = [];
  while (out.length < 8) for (const b of randomBytes(16)) if (b < 240 && out.length < 8) out.push(CODE_ALPHABET[b % 20]);
  return out.slice(0, 4).join("") + "-" + out.slice(4).join("");
}
const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const isAdmin = (env, email) => JSON.parse(env.ADMIN_AGENTS || "[]").includes(email);

// The Access JWT, verified here as well as at the edge: signature (the team's published keys),
// audience, issuer, expiry, and an email in the allowed domain.
let certs = { at: 0, keys: [] };
async function accessEmail(request, env) {
  if (!env.ACCESS_AUD || !env.ACCESS_TEAM_DOMAIN) return null;
  const cookie = (request.headers.get("Cookie") || "").match(/(?:^|;\s*)CF_Authorization=([^;]+)/);
  const token = request.headers.get("Cf-Access-Jwt-Assertion") || (cookie && cookie[1]);
  if (!token) return null;
  try {
    const [h, p, sig] = token.split(".");
    const header = JSON.parse(new TextDecoder().decode(fromB64url(h)));
    const claims = JSON.parse(new TextDecoder().decode(fromB64url(p)));
    if (Date.now() - certs.at > 3600_000 || !certs.keys.some((k) => k.kid === header.kid)) {
      const r = await fetch(`https://${env.ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs`);
      certs = { at: Date.now(), keys: (await r.json()).keys || [] };
    }
    const jwk = certs.keys.find((k) => k.kid === header.kid);
    if (!jwk || header.alg !== "RS256") return null;
    const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
    if (!(await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, fromB64url(sig), encoder.encode(`${h}.${p}`)))) return null;
    const aud = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
    if (!aud.includes(env.ACCESS_AUD) || claims.iss !== `https://${env.ACCESS_TEAM_DOMAIN}`) return null;
    if (!claims.exp || claims.exp * 1000 < Date.now()) return null;
    const email = String(claims.email || "").toLowerCase();
    const domain = String(env.ALLOWED_EMAIL_DOMAIN || "").toLowerCase();
    return domain && email.endsWith("@" + domain) ? email : null;
  } catch {
    return null;
  }
}

function page(title, body, status = 200) {
  return new Response(`<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>${escapeHtml(title)}</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:760px;margin:48px auto;padding:0 20px;color:#18202d;background:#f4f6f9}
h1{font-size:28px;margin:0 0 8px}.code{font:600 34px ui-monospace,monospace;letter-spacing:.12em;margin:16px 0}
table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #d9dee6}td,th{padding:8px 10px;border-bottom:1px solid #d9dee6;text-align:left;font-size:14px}
button{font:600 15px system-ui;padding:9px 18px;border-radius:6px;border:0;background:#2b55d6;color:#fff;cursor:pointer}
button.quiet{background:#e3e7ee;color:#18202d}.muted{color:#586275;font-size:14px}</style></head><body>${body}</body></html>`,
    { status, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
                          "X-Frame-Options": "DENY", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'" } });
}

async function keysPage(env, email, note = "") {
  const all = isAdmin(env, email);
  const { results } = await env.DECISIONS.prepare(
    `SELECT id, email, label, created_at, last_used_at, revoked_at FROM api_keys ${all ? "" : "WHERE email = ?"} ORDER BY created_at DESC`)
    .bind(...(all ? [] : [email])).all();
  const rows = results.map((k) => `<tr><td>${all ? escapeHtml(k.email) + "<br>" : ""}${escapeHtml(k.label || "key")}</td>
    <td>${escapeHtml(k.created_at.slice(0, 10))}</td><td>${escapeHtml((k.last_used_at || "never").slice(0, 16).replace("T", " "))}</td>
    <td>${k.revoked_at ? "revoked " + escapeHtml(k.revoked_at.slice(0, 10)) :
      `<form method="post" action="/login/revoke"><input type="hidden" name="id" value="${escapeHtml(k.id)}"><button class="quiet">Revoke</button></form>`}</td></tr>`).join("");
  return page("CLM keys", `<h1>CLM keys</h1><p class="muted">Signed in as ${escapeHtml(email)}${all ? " (admin: all keys)" : ""}.</p>
    ${note ? `<p>${note}</p>` : ""}
    <p>To connect an agent, run <code>/clm:login</code> in Claude Code, or <code>python3 integrations/codex/install.py</code> for Codex.</p>
    ${rows ? `<table><tr><th>Key</th><th>Created</th><th>Last used</th><th></th></tr>${rows}</table>` : "<p>No keys yet.</p>"}`);
}

function sameOrigin(request) {
  const origin = request.headers.get("Origin");
  return origin === new URL(request.url).origin;
}

async function loginRoutes(request, env, url) {
  if (!env.DECISIONS || !env.ACCESS_AUD) return error(404, "personal keys are not configured");
  const email = await accessEmail(request, env);
  if (!email) return page("Sign in required", "<h1>Sign in required</h1><p>Sign in with your Metatheory Google account.</p>", 403);
  await ensureSchema(env.DECISIONS);
  const db = env.DECISIONS, now = new Date().toISOString();
  if (request.method === "GET" && url.pathname === "/login") {
    const code = (url.searchParams.get("code") || "").toUpperCase();
    const d = code && (await db.prepare("SELECT label, status, expires_at FROM device_logins WHERE user_code = ?").bind(code).all()).results[0];
    if (d && d.status === "pending" && d.expires_at > now) {
      return page("Connect an agent", `<h1>Connect an agent</h1><p>Approve this sign-in only if you just started it.</p>
        <div class="code">${escapeHtml(code)}</div><p class="muted">${escapeHtml(d.label || "an agent")}</p>
        <form method="post" action="/login/approve"><input type="hidden" name="code" value="${escapeHtml(code)}">
        <button>Approve and issue a key for ${escapeHtml(email)}</button></form>`);
    }
    return keysPage(env, email, code ? "That code has expired or was already used. Start the login again." : "");
  }
  if (request.method !== "POST" || !sameOrigin(request)) return error(403, "forbidden");
  const form = await request.formData();
  if (url.pathname === "/login/approve") {
    const r = await db.prepare("UPDATE device_logins SET status = 'approved', email = ? WHERE user_code = ? AND status = 'pending' AND expires_at > ?")
      .bind(email, String(form.get("code") || "").toUpperCase(), now).run();
    if (!r.meta?.changes) return keysPage(env, email, "That code has expired or was already used. Start the login again.");
    return page("Approved", "<h1>Approved</h1><p>Your agent has its key. You can close this tab and go back to the terminal.</p>");
  }
  if (url.pathname === "/login/revoke") {
    const id = String(form.get("id") || "");
    const r = isAdmin(env, email)
      ? await db.prepare("UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL").bind(now, id).run()
      : await db.prepare("UPDATE api_keys SET revoked_at = ? WHERE id = ? AND email = ? AND revoked_at IS NULL").bind(now, id, email).run();
    return keysPage(env, email, r.meta?.changes ? "Key revoked. Agents using it stop working now." : "No such key.");
  }
  return error(404, "not found");
}

async function deviceRoutes(request, env, url) {
  if (!env.DECISIONS || !env.ACCESS_AUD) return error(404, "personal keys are not configured");
  if (request.method !== "POST") return error(405, "use POST");
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  if (!(await env.LIMITER.limit({ key: `ip:${ip}` })).success) return error(429, "rate limit exceeded");
  await ensureSchema(env.DECISIONS);
  const db = env.DECISIONS, now = new Date();
  let body = {};
  try { body = await request.json(); } catch {}
  if (url.pathname === "/v1/auth/device") {
    const deviceCode = b64url(randomBytes(32)), code = userCode();
    await db.prepare(`INSERT INTO device_logins (device_hash, user_code, label, status, created_at, expires_at)
                      VALUES (?, ?, ?, 'pending', ?, ?)`)
      .bind(await sha256hex(deviceCode), code, String(body.label || "").slice(0, 120), now.toISOString(),
            new Date(+now + DEVICE_TTL_MS).toISOString()).run();
    const verify = `${url.origin}/login?code=${code}`;
    return Response.json({ device_code: deviceCode, user_code: code, verification_uri: verify,
                           verification_uri_complete: verify, expires_in: DEVICE_TTL_MS / 1000, interval: POLL_SECONDS },
                         { headers: { "Cache-Control": "no-store" } });
  }
  if (url.pathname === "/v1/auth/token") {
    const hash = await sha256hex(String(body.device_code || ""));
    const d = (await db.prepare("SELECT status, email, label, expires_at FROM device_logins WHERE device_hash = ?").bind(hash).all()).results[0];
    if (!d) return Response.json({ error: "invalid_device_code" }, { status: 400 });
    if (d.status === "consumed") return Response.json({ error: "already_used" }, { status: 410 });
    if (d.expires_at < now.toISOString()) return Response.json({ error: "expired_token" }, { status: 410 });
    if (d.status !== "approved") return Response.json({ error: "authorization_pending", interval: POLL_SECONDS }, { status: 428 });
    // hand the key over exactly once, even if two polls race
    const claimed = await db.prepare("UPDATE device_logins SET status = 'consumed' WHERE device_hash = ? AND status = 'approved'").bind(hash).run();
    if (!claimed.meta?.changes) return Response.json({ error: "already_used" }, { status: 410 });
    const key = KEY_PREFIX + b64url(randomBytes(32)), id = b64url(randomBytes(9));
    await db.prepare("INSERT INTO api_keys (id, key_hash, email, label, created_at) VALUES (?, ?, ?, ?, ?)")
      .bind(id, await sha256hex(key), d.email, d.label, now.toISOString()).run();
    return Response.json({ api_key: key, email: d.email, key_id: id }, { headers: { "Cache-Control": "no-store" } });
  }
  return error(404, "not found");
}

// ── decisions ──────────────────────────────────────────────────────────────
// One row per event: a decision record (clm.decisions.Router), or an outcome or a baseline
// (what the existing decision-maker chose, when it is only known afterwards) for one.
// Rows carry the posting agent; an agent reads its own rows, DECISION_READERS read all.

const MAX_BODY = 1 << 20;
const MAX_EVENTS = 100;
const PAGE = 500;
let schemaReady = null;

async function ensureSchema(db) {
  if (schemaReady === db) return;
  await db.prepare(`CREATE TABLE IF NOT EXISTS events (
      seq INTEGER PRIMARY KEY AUTOINCREMENT, dedupe TEXT UNIQUE NOT NULL, id TEXT NOT NULL,
      kind TEXT NOT NULL, agent TEXT NOT NULL, workflow TEXT, received_at TEXT NOT NULL, body TEXT NOT NULL)`).run();
  await db.prepare("CREATE INDEX IF NOT EXISTS events_id ON events (id)").run();
  await db.prepare(`CREATE TABLE IF NOT EXISTS api_keys (
      id TEXT PRIMARY KEY, key_hash TEXT UNIQUE NOT NULL, email TEXT NOT NULL, label TEXT,
      created_at TEXT NOT NULL, last_used_at TEXT, revoked_at TEXT)`).run();
  await db.prepare(`CREATE TABLE IF NOT EXISTS device_logins (
      device_hash TEXT PRIMARY KEY, user_code TEXT UNIQUE NOT NULL, label TEXT, status TEXT NOT NULL,
      email TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL)`).run();
  schemaReady = db;
}

function parseEvent(e) {
  if (!e || typeof e !== "object" || typeof e.id !== "string" || !e.id || e.id.length > 128) return null;
  if (e.event === "outcome") return { kind: "outcome", dedupe: `o:${e.id}:${e.created_at || ""}:${e.label || ""}:${e.ok}` };
  if (e.event === "baseline" && typeof e.label === "string") return { kind: "baseline", dedupe: `b:${e.id}:${e.label}` };
  if (e.questions && typeof e.questions === "object") return { kind: "record", dedupe: `r:${e.id}` };
  return null;
}

async function postDecisions(request, env, agent) {
  const raw = await request.text();
  if (raw.length > MAX_BODY) return error(413, `body over ${MAX_BODY} bytes`);
  let body;
  try { body = JSON.parse(raw); } catch { return error(422, "body is not JSON"); }
  const events = Array.isArray(body?.events) ? body.events : [body];
  if (events.length > MAX_EVENTS) return error(413, `more than ${MAX_EVENTS} events`);
  const parsed = events.map(parseEvent);
  const bad = parsed.findIndex((p) => p === null);
  if (bad >= 0) return error(422, `event ${bad} is not a decision record, outcome or baseline`);
  await ensureSchema(env.DECISIONS);
  const now = new Date().toISOString();
  const stmt = env.DECISIONS.prepare(
    "INSERT OR IGNORE INTO events (dedupe, id, kind, agent, workflow, received_at, body) VALUES (?, ?, ?, ?, ?, ?, ?)");
  const results = await env.DECISIONS.batch(events.map((e, i) => stmt.bind(
    `${agent}:${parsed[i].dedupe}`, e.id, parsed[i].kind, agent,
    typeof e.workflow === "string" ? e.workflow : null, now, JSON.stringify({ ...e, agent, received_at: now }))));
  return Response.json({ stored: results.filter((r) => r.meta?.changes).length, received: events.length });
}

async function getDecisions(url, env, agent) {
  await ensureSchema(env.DECISIONS);
  const readAll = JSON.parse(env.DECISION_READERS || "[]").includes(agent);
  const cursor = Number(url.searchParams.get("cursor") || 0) || 0;
  const workflow = url.searchParams.get("workflow");
  const where = ["seq > ?"], params = [cursor];
  if (!readAll) { where.push("agent = ?"); params.push(agent); }
  if (workflow) {
    where.push("(workflow = ? OR id IN (SELECT id FROM events WHERE kind = 'record' AND workflow = ?))");
    params.push(workflow, workflow);
  }
  const { results } = await env.DECISIONS.prepare(
    `SELECT seq, body FROM events WHERE ${where.join(" AND ")} ORDER BY seq LIMIT ${PAGE}`)
    .bind(...params).all();
  const next = results.length === PAGE ? results[results.length - 1].seq : null;
  return Response.json({ events: results.map((r) => JSON.parse(r.body)), cursor: next },
                       { headers: { "Cache-Control": "no-store" } });
}

// Anthropic Messages API pass-through for agents (Mipmap's summarizer): any request is
// forwarded as-is, the provider key stays a Worker secret. With AI_GATEWAY_URL set
// (https://gateway.ai.cloudflare.com/v1/<account>/<gateway>/anthropic) it goes through
// Cloudflare AI Gateway for logs and analytics, authenticated with AI_GATEWAY_TOKEN
// (cf-aig-authorization) and the key stored there (BYOK) unless ANTHROPIC_API_KEY is set.
async function anthropicProxy(request, env, url, agent) {
  const base = env.AI_GATEWAY_URL || "https://api.anthropic.com";
  if (!env.ANTHROPIC_API_KEY && !(env.AI_GATEWAY_URL && env.AI_GATEWAY_TOKEN)) {
    return error(404, "anthropic proxy is not configured");
  }
  const headers = new Headers(request.headers);
  headers.delete("Authorization");
  headers.delete("x-api-key");
  if (env.ANTHROPIC_API_KEY) headers.set("x-api-key", env.ANTHROPIC_API_KEY);
  if (env.AI_GATEWAY_URL && env.AI_GATEWAY_TOKEN) headers.set("cf-aig-authorization", `Bearer ${env.AI_GATEWAY_TOKEN}`);
  if (!headers.has("anthropic-version")) headers.set("anthropic-version", "2023-06-01");
  const target = `${base.replace(/\/$/, "")}/${url.pathname.slice("/v1/anthropic/".length)}${url.search}`;
  const response = await fetch(new Request(target, { method: request.method, headers, body: request.body, duplex: "half" }));
  console.log(JSON.stringify({ agent, path: url.pathname, status: response.status, upstream: "anthropic" }));
  return response;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // the key pages (signed in through Cloudflare Access) and the device login, before key auth
    if (url.pathname === "/login" || url.pathname.startsWith("/login/")) return loginRoutes(request, env, url);
    if (url.pathname.startsWith("/v1/auth/")) return deviceRoutes(request, env, url);

    // the website lives on Cloudflare Pages (SITE_ORIGIN), not in the Pod image, so a site
    // update is `tools/deploy-site.sh` with no image rebuild
    const sitePath = {
      "/": "/home", "/index.html": "/", "/playground": "/", "/playground/": "/",
      "/home.css": "/home.css", "/home.js": "/home.js", "/app.css": "/app.css", "/app.js": "/app.js",
      "/guides/claude-code": "/guide-claude-code", "/guides/claude-code/": "/guide-claude-code",
      "/guides/codex": "/guide-codex", "/guides/codex/": "/guide-codex",
      "/guides/mipmap": "/guide-mipmap", "/guides/mipmap/": "/guide-mipmap",
    }[url.pathname];
    if (sitePath && env.SITE_ORIGIN && (request.method === "GET" || request.method === "HEAD")) {
      const headers = new Headers();
      for (const name of ["Accept", "Accept-Language", "If-Modified-Since", "If-None-Match"]) {
        const value = request.headers.get(name); if (value) headers.set(name, value);
      }
      return fetch(new Request(new URL(sitePath + url.search, env.SITE_ORIGIN), { method: request.method, headers }));
    }

    // unauthenticated liveness for uptime checks: only whether the Pod and its encoder
    // are up, not the models or cache details clm-serve's /health reports
    if (url.pathname === "/health" && request.method === "GET") {
      let ok = false;
      try {
        const upstream = await fetch(new Request(url, { headers: {} }));
        ok = upstream.ok && (await upstream.json()).embedder === true;
      } catch {}
      return Response.json({ ok }, { status: ok ? 200 : 503, headers: { "Cache-Control": "no-store" } });
    }

    const agent = await agentFor(request, env, ctx);
    if (agent === null) return error(401, "invalid API key");

    const { success } = await env.LIMITER.limit({ key: agent });
    if (!success) return error(429, "rate limit exceeded");

    // every agent is forwarded with the upstream key, so admin routes are gated here
    if (url.pathname.startsWith("/v1/admin/") && !JSON.parse(env.ADMIN_AGENTS || "[]").includes(agent)) {
      return error(403, "admin routes are limited to the gateway's admin agents");
    }

    if (url.pathname === "/v1/decisions") {
      if (!env.DECISIONS) return error(404, "decision collection is not configured");
      if (request.method === "POST") return postDecisions(request, env, agent);
      if (request.method === "GET") return getDecisions(url, env, agent);
      return error(405, "use POST or GET");
    }

    if (url.pathname.startsWith("/v1/anthropic/")) return anthropicProxy(request, env, url, agent);

    const headers = new Headers(request.headers);
    headers.set("Authorization", `Bearer ${env.UPSTREAM_KEY}`);
    headers.set("X-CLM-Agent", agent);
    // a Worker on a route that fetches its own hostname goes to the origin (the tunnel)
    const response = await fetch(new Request(request, { headers }));
    console.log(JSON.stringify({ agent, path: url.pathname, status: response.status,
                                 latency_ms: response.headers.get("X-CLM-Latency-Ms") }));
    return response;
  },
};
