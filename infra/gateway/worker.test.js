// Gateway tests, no dependencies: node infra/gateway/worker.test.js
// Stubs the Workers-only APIs (timingSafeEqual, the rate limiter binding, the origin fetch).
import assert from "node:assert/strict";
crypto.subtle.timingSafeEqual = (a, b) => Buffer.from(a).equals(Buffer.from(b));
const seen = [];
let health = { ok: true, embedder: true, models: ["clm-latest"], cache: { pools: {} } };
let jwks = { keys: [] };
globalThis.fetch = async (req) => {
  if (typeof req === "string" && req.endsWith("/cdn-cgi/access/certs")) return Response.json(jwks);
  seen.push(req);
  if (new URL(req.url).pathname === "/health") {
    if (health === "down") throw new Error("origin unreachable");
    return Response.json(health);
  }
  return new Response("ok", { status: 200, headers: { "X-CLM-Latency-Ms": "12" } });
};
const { default: worker } = await import("./worker.js");
let allow = true;
const env = { AGENT_KEYS: JSON.stringify({ alpha: "key-a", beta: "key-b" }), UPSTREAM_KEY: "UP",
              LIMITER: { limit: async ({ key }) => ({ success: allow, key }) } };
const req = (path, auth, method = "POST") => new Request("https://clm.example.com" + path,
  { method, headers: auth ? { Authorization: auth } : {}, body: method === "POST" ? "{}" : undefined });

assert.equal((await worker.fetch(req("/v1/systemone"), env)).status, 401);
assert.equal((await worker.fetch(req("/v1/systemone", "Bearer nope"), env)).status, 401);
assert.equal((await worker.fetch(req("/v1/systemone", "key-a"), env)).status, 401);
assert.equal((await worker.fetch(req("/v1/systemone", "Bearer UP"), env)).status, 401, "upstream key must not work at the edge");
assert.equal(seen.length, 0);

let r = await worker.fetch(req("/v1/systemone", "Bearer key-b"), env);
assert.equal(r.status, 200);
assert.equal(seen[0].headers.get("Authorization"), "Bearer UP");
assert.equal(seen[0].headers.get("X-CLM-Agent"), "beta");
assert.equal(await seen[0].text(), "{}");

allow = false;
assert.equal((await worker.fetch(req("/v1/systemone", "Bearer key-a"), env)).status, 429);
allow = true;

r = await worker.fetch(req("/health", "Bearer whatever", "GET"), env);
assert.equal(r.status, 200);
assert.deepEqual(await r.json(), { ok: true }, "health must not leak models or cache details");
assert.equal(seen.at(-1).headers.get("Authorization"), null, "health forwards without credentials");
health = { ok: true, embedder: false, models: [] };
r = await worker.fetch(req("/health", null, "GET"), env);
assert.equal(r.status, 503); assert.deepEqual(await r.json(), { ok: false });
health = "down";
r = await worker.fetch(req("/health", null, "GET"), env);
assert.equal(r.status, 503); assert.deepEqual(await r.json(), { ok: false });
assert.equal((await worker.fetch(req("/health", null, "POST"), env)).status, 401, "only GET /health is open");

// ── /v1/decisions, against a D1 stand-in backed by real SQLite ─────────────
const { DatabaseSync } = await import("node:sqlite");
function fakeD1() {
  const db = new DatabaseSync(":memory:");
  const stmt = (sql, params = []) => ({
    bind: (...p) => stmt(sql, p),
    run: async () => ({ meta: { changes: Number(db.prepare(sql).run(...params).changes) } }),
    all: async () => ({ results: db.prepare(sql).all(...params) }),
  });
  return { prepare: (sql) => stmt(sql), batch: async (stmts) => Promise.all(stmts.map((s) => s.run())) };
}
const denv = { ...env, DECISIONS: fakeD1(), DECISION_READERS: JSON.stringify(["beta"]) };
const post = (key, body) => worker.fetch(new Request("https://clm.example.com/v1/decisions",
  { method: "POST", headers: { Authorization: `Bearer ${key}` }, body: typeof body === "string" ? body : JSON.stringify(body) }), denv);
const get = async (key, qs = "") => (await worker.fetch(new Request(`https://clm.example.com/v1/decisions${qs}`,
  { headers: { Authorization: `Bearer ${key}` } }), denv)).json();
const record = (id, workflow = "routing/chief") => ({ id, workflow, questions: { route: { type: "choice" } }, state: "s" });
const forwarded = seen.length;

assert.equal((await post("key-a", record("d1"))).status, 200);
assert.deepEqual(await (await post("key-a", record("d1"))).json(), { stored: 0, received: 1 }, "records are idempotent");
assert.deepEqual(await (await post("key-a", { events: [record("d2", "routing/other"),
  { event: "outcome", id: "d1", ok: false, label: "writer", created_at: "t1" }] })).json(), { stored: 2, received: 2 });
await post("key-b", record("b1"));
assert.equal(seen.length, forwarded, "decisions are handled at the edge, never forwarded to the Pod");

let page = await get("key-a");
assert.deepEqual(page.events.map((e) => e.id), ["d1", "d2", "d1"], "an agent reads only its own events");
assert.ok(page.events.every((e) => e.agent === "alpha" && e.received_at), "events are stamped with the posting agent");
assert.equal(page.cursor, null);
assert.deepEqual((await get("key-a", "?workflow=routing/chief")).events.map((e) => [e.id, e.event || "record"]),
  [["d1", "record"], ["d1", "outcome"]], "the workflow filter keeps a record's outcomes");
assert.deepEqual((await get("key-b")).events.map((e) => e.id), ["d1", "d2", "d1", "b1"], "DECISION_READERS read everyone");


assert.deepEqual(await (await post("key-a", { event: "baseline", id: "d1", label: "review", rank: 2 })).json(),
  { stored: 1, received: 1 }, "baseline events are accepted");
assert.equal((await post("key-a", { event: "baseline", id: "d1" })).status, 422, "a baseline needs a label");
assert.equal((await post("key-a", "not json")).status, 422);
assert.equal((await post("key-a", { id: "x" })).status, 422, "neither a record nor an outcome");
assert.equal((await post("key-a", { events: Array(101).fill(record("y")) })).status, 413);
assert.equal((await post("key-a", "x".repeat((1 << 20) + 1))).status, 413);
assert.equal((await post("nope", record("z"))).status, 401);
assert.equal((await worker.fetch(new Request("https://clm.example.com/v1/decisions", { method: "DELETE",
  headers: { Authorization: "Bearer key-a" } }), denv)).status, 405);
assert.equal((await worker.fetch(req("/v1/decisions", "Bearer key-a", "GET"), env)).status, 404, "no D1 binding: 404");
// ── /v1/admin/*: only ADMIN_AGENTS ──────────────────────────────────────────
const aenv = { ...env, ADMIN_AGENTS: JSON.stringify(["beta"]) };
const admin = (key, e = aenv) => worker.fetch(new Request("https://clm.example.com/v1/admin/heads/x",
  { method: "PUT", headers: { Authorization: `Bearer ${key}` }, body: "bytes" }), e);
const before = seen.length;
assert.equal((await admin("key-a")).status, 403, "a non-admin agent is refused at the edge");
assert.equal(seen.length, before, "and never reaches the Pod");
assert.equal((await admin("key-b")).status, 200, "an admin agent is forwarded");
assert.equal(seen.at(-1).headers.get("Authorization"), "Bearer UP");
assert.equal((await admin("key-b", env)).status, 403, "no ADMIN_AGENTS binding: nobody");

// ── personal keys: Access sign-in, device login, revocation ────────────────
const TEAM = "metatheory.cloudflareaccess.com", AUD = "aud-tag-123";
const pair = await crypto.subtle.generateKey({ name: "RSASSA-PKCS1-v1_5", modulusLength: 2048,
  publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" }, true, ["sign", "verify"]);
jwks = { keys: [{ ...(await crypto.subtle.exportKey("jwk", pair.publicKey)), kid: "k1", alg: "RS256" }] };
const enc = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
async function jwt(claims, { kid = "k1", key = pair.privateKey } = {}) {
  const body = `${enc({ alg: "RS256", kid })}.${enc({ aud: [AUD], iss: `https://${TEAM}`, exp: Math.floor(Date.now() / 1000) + 600, ...claims })}`;
  const sig = Buffer.from(await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, new TextEncoder().encode(body))).toString("base64url");
  return `${body}.${sig}`;
}
const kenv = { ...env, DECISIONS: fakeD1(), ACCESS_TEAM_DOMAIN: TEAM, ACCESS_AUD: AUD, ALLOWED_EMAIL_DOMAIN: "metatheory.gg",
               ADMIN_AGENTS: JSON.stringify(["boss@metatheory.gg"]) };
const H = "https://clm.example.com";
const device = (body = { label: "jt's laptop · claude-code" }, e = kenv) =>
  worker.fetch(new Request(H + "/v1/auth/device", { method: "POST", body: JSON.stringify(body) }), e);
const token = (code) => worker.fetch(new Request(H + "/v1/auth/token", { method: "POST", body: JSON.stringify({ device_code: code }) }), kenv);
const browse = async (path, email, opts = {}) => worker.fetch(new Request(H + path, { method: opts.form ? "POST" : "GET",
  headers: { ...(email ? { "Cf-Access-Jwt-Assertion": await jwt({ email, ...(opts.claims || {}) }, opts.jwt) } : {}),
             ...(opts.form ? { Origin: opts.origin ?? H, "Content-Type": "application/x-www-form-urlencoded" } : {}) },
  body: opts.form ? new URLSearchParams(opts.form).toString() : undefined }), kenv);

assert.equal((await device(undefined, env)).status, 404, "no Access configured: logins are off");
let d = await (await device()).json();
assert.match(d.user_code, /^[B-Z]{4}-[B-Z]{4}$/); assert.ok(d.device_code.length >= 40 && d.interval > 0);
assert.equal(d.verification_uri, `${H}/login?code=${d.user_code}`);
assert.equal((await token(d.device_code)).status, 428, "pending until approved");
assert.equal((await token("made-up")).status, 400);

// sign-in checks, before anything is shown
assert.equal((await browse(`/login?code=${d.user_code}`, null)).status, 403, "no Access token");
assert.equal((await browse(`/login?code=${d.user_code}`, "eve@gmail.com")).status, 403, "outside the domain");
assert.equal((await browse(`/login?code=${d.user_code}`, "eve@metatheory.gg.evil.com")).status, 403, "a lookalike domain");
assert.equal((await browse("/login", "jt@metatheory.gg", { claims: { aud: ["other-app"] } })).status, 403, "wrong audience");
assert.equal((await browse("/login", "jt@metatheory.gg", { claims: { exp: 1 } })).status, 403, "expired");
const forged = await crypto.subtle.generateKey({ name: "RSASSA-PKCS1-v1_5", modulusLength: 2048,
  publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" }, true, ["sign", "verify"]);
assert.equal((await browse("/login", "jt@metatheory.gg", { jwt: { key: forged.privateKey } })).status, 403, "bad signature");
let html = await (await browse(`/login?code=${d.user_code}`, "JT@metatheory.gg")).text();
assert.ok(html.includes(d.user_code) && html.includes("jt@metatheory.gg") && html.includes("jt&#39;s laptop"), "the code, the user and the escaped label");

// approving: same-origin form posts only
assert.equal((await browse("/login/approve", "jt@metatheory.gg", { form: { code: d.user_code }, origin: "https://evil.example" })).status, 403);
assert.equal((await token(d.device_code)).status, 428, "a cross-site post approves nothing");
assert.equal((await browse("/login/approve", "jt@metatheory.gg", { form: { code: d.user_code } })).status, 200);
let t = await token(d.device_code);
assert.equal(t.status, 200);
const issued = await t.json();
assert.ok(issued.api_key.startsWith("clm_") && issued.email === "jt@metatheory.gg");
assert.equal((await token(d.device_code)).status, 410, "the key is handed over once");
const stored = await kenv.DECISIONS.prepare("SELECT key_hash FROM api_keys").all();
assert.ok(!JSON.stringify(stored.results).includes(issued.api_key), "only the hash is stored");

// the personal key names the agent
r = await worker.fetch(new Request(H + "/v1/systemone", { method: "POST", headers: { Authorization: `Bearer ${issued.api_key}` }, body: "{}" }), kenv);
assert.equal(r.status, 200); assert.equal(seen.at(-1).headers.get("X-CLM-Agent"), "jt@metatheory.gg");
await worker.fetch(new Request(H + "/v1/decisions", { method: "POST", headers: { Authorization: `Bearer ${issued.api_key}` },
  body: JSON.stringify(record("p1")) }), kenv);
const mine = await (await worker.fetch(new Request(H + "/v1/decisions", { headers: { Authorization: `Bearer ${issued.api_key}` } }), kenv)).json();
assert.deepEqual(mine.events.map((e) => [e.id, e.agent]), [["p1", "jt@metatheory.gg"]], "records carry the person");
assert.equal((await worker.fetch(new Request(H + "/v1/systemone", { method: "POST", headers: { Authorization: "Bearer clm_forged" }, body: "{}" }), kenv)).status, 401);

// revocation: the owner or an admin, nobody else
const keyId = issued.key_id;
assert.ok((await (await browse("/login", "jt@metatheory.gg")).text()).includes("Revoke"));
assert.ok(!(await (await browse("/login", "sam@metatheory.gg")).text()).includes(keyId), "others don't see your keys");
await browse("/login/revoke", "sam@metatheory.gg", { form: { id: keyId } });
assert.equal((await worker.fetch(new Request(H + "/v1/systemone", { method: "POST", headers: { Authorization: `Bearer ${issued.api_key}` }, body: "{}" }), kenv)).status, 200, "a stranger cannot revoke it");
assert.ok((await (await browse("/login", "boss@metatheory.gg")).text()).includes("jt@metatheory.gg"), "admins see every key");
assert.ok((await (await browse("/login/revoke", "boss@metatheory.gg", { form: { id: keyId } })).text()).includes("Key revoked"));
assert.equal((await worker.fetch(new Request(H + "/v1/systemone", { method: "POST", headers: { Authorization: `Bearer ${issued.api_key}` }, body: "{}" }), kenv)).status, 401, "revoked keys stop at once");

// an expired login can't be approved or redeemed
d = await (await device()).json();
await kenv.DECISIONS.prepare("UPDATE device_logins SET expires_at = '2000-01-01' WHERE user_code = ?").bind(d.user_code).run();
assert.ok((await (await browse("/login/approve", "jt@metatheory.gg", { form: { code: d.user_code } })).text()).includes("expired"));
assert.equal((await token(d.device_code)).status, 410);
allow = false;
assert.equal((await device()).status, 429, "device logins are rate-limited per IP");
allow = true;
console.log("worker tests passed");
