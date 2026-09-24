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
console.log("worker tests passed");
