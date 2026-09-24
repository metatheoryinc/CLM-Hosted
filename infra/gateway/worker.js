// CLM gateway: runs on the public hostname in front of the Cloudflare Tunnel to the Pod.
// Each agent sends its own key as "Authorization: Bearer <key>"; the gateway checks it,
// rate-limits per agent, and forwards with the single upstream CLM_API_KEY, which never
// leaves Cloudflare. Revoking an agent is removing its entry from AGENT_KEYS.
//
// Bindings (infra/Pulumi.yaml):
//   AGENT_KEYS    secret, JSON {"<agent name>": "<key>"}
//   UPSTREAM_KEY  secret, the Pod's CLM_API_KEY
//   LIMITER       rate limit, keyed by agent name

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
