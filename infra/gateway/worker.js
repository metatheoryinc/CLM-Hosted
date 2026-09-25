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

const encoder = new TextEncoder();

// Hash both sides first so the comparison is constant-time and length-independent.
async function sameKey(a, b) {
  const [x, y] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(a)),
    crypto.subtle.digest("SHA-256", encoder.encode(b)),
  ]);
  return crypto.subtle.timingSafeEqual(x, y);
}

async function agentFor(request, agentKeys) {
  const header = request.headers.get("Authorization") || "";
  if (!header.startsWith("Bearer ")) return null;
  const presented = header.slice("Bearer ".length);
  let found = null;
  // check every entry so the time taken does not reveal which agent matched
  for (const [agent, key] of Object.entries(agentKeys)) {
    if ((await sameKey(presented, key)) && found === null) found = agent;
  }
  return found;
}

function error(status, message) {
  return Response.json({ detail: message }, { status });
}

// ── decisions ──────────────────────────────────────────────────────────────
// One row per event: a decision record (clm.decisions.Router), or an outcome or a baseline
// (what the existing decision-maker chose, when it is only known afterwards) for one.
// Rows carry the posting agent; an agent reads its own rows, DECISION_READERS read all.

const MAX_BODY = 1 << 20;
const MAX_EVENTS = 100;
const PAGE = 500;
let schemaReady = false;

async function ensureSchema(db) {
  if (schemaReady) return;
  await db.prepare(`CREATE TABLE IF NOT EXISTS events (
      seq INTEGER PRIMARY KEY AUTOINCREMENT, dedupe TEXT UNIQUE NOT NULL, id TEXT NOT NULL,
      kind TEXT NOT NULL, agent TEXT NOT NULL, workflow TEXT, received_at TEXT NOT NULL, body TEXT NOT NULL)`).run();
  await db.prepare("CREATE INDEX IF NOT EXISTS events_id ON events (id)").run();
  schemaReady = true;
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

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

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

    const agent = await agentFor(request, JSON.parse(env.AGENT_KEYS));
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
