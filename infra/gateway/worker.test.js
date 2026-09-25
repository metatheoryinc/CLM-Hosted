// Gateway tests, no dependencies: node infra/gateway/worker.test.js
// Stubs the Workers-only APIs (timingSafeEqual, the rate limiter binding, the origin fetch).
import assert from "node:assert/strict";
crypto.subtle.timingSafeEqual = (a, b) => Buffer.from(a).equals(Buffer.from(b));
const seen = [];
let health = { ok: true, embedder: true, models: ["clm-latest"], cache: { pools: {} } };
globalThis.fetch = async (req) => {
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
console.log("worker tests passed");
