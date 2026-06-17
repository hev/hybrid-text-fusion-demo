/**
 * Cloudflare Worker backend for the SciFact HybridText demo.
 *
 * Mirrors server.py: serves the single-page UI from ./static and proxies search
 * to the Layer gateway, injecting the API key server-side so it never reaches
 * the browser. The search response carries `took_ms` — the gateway round-trip
 * measured at the edge — so the UI can show how fast hybrid_text_fusion answers.
 *
 * Static assets (index.html, queries.json) are served before this Worker runs;
 * the Worker only handles the /api/* routes below.
 */
import queriesData from "../static/queries.json";

const TRANSIENT = new Set([502, 503, 504]);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);

    if (pathname === "/api/config") {
      return json({
        namespace: env.LAYER_NAMESPACE,
        field: env.LAYER_SEARCH_FIELD,
        gateway: env.LAYER_GATEWAY_URL,
      });
    }

    if (pathname === "/api/examples") {
      return json(queriesData);
    }

    if (pathname === "/api/search") {
      if (request.method !== "POST") {
        return new Response("Method not allowed", { status: 405 });
      }
      return search(request, env);
    }

    return new Response("Not found", { status: 404 });
  },
};

async function search(request, env) {
  let req;
  try {
    req = await request.json();
  } catch {
    return json({ rows: [], hybrid: null });
  }

  const query = (req.query || "").trim();
  if (!query) return json({ rows: [], hybrid: null });
  const topK = Math.max(1, Math.min(Number(req.top_k) || 10, 50));

  const gateway = env.LAYER_GATEWAY_URL.replace(/\/+$/, "");
  const url = `${gateway}/v2/namespaces/${env.LAYER_NAMESPACE}/query`;
  const body = JSON.stringify({
    // The Layer-only HybridText rank expression: the gateway tokenizes the
    // input, expands one full-input BM25 leg + one fuzzy leg per token, and
    // fuses with RRF.
    rank_by: [env.LAYER_SEARCH_FIELD, "HybridText", query],
    top_k: topK,
    include_attributes: ["title", "text"],
  });
  const headers = { "Content-Type": "application/json" };
  if (env.LAYER_API_KEY) headers["Authorization"] = `Bearer ${env.LAYER_API_KEY}`;

  // Retry transient gateway/edge hiccups (502/503/504 or a dropped connection),
  // common for a few seconds after a gateway rollout. Real 4xx fail immediately.
  let lastDetail = "unknown error";
  for (let attempt = 0; attempt < 3; attempt++) {
    let resp;
    const t0 = Date.now();
    try {
      resp = await fetch(url, { method: "POST", headers, body });
    } catch (exc) {
      lastDetail = `gateway unreachable: ${exc}`;
      if (attempt < 2) await sleep(400 * (attempt + 1));
      continue;
    }
    const tookMs = Date.now() - t0;

    if (resp.status === 200) {
      const data = await resp.json();
      return json({ rows: data.rows || [], hybrid: data.hybrid || null, took_ms: tookMs });
    }
    if (!TRANSIENT.has(resp.status)) {
      return new Response(await resp.text(), { status: resp.status });
    }
    lastDetail = await resp.text();
    if (attempt < 2) await sleep(400 * (attempt + 1));
  }

  return new Response(`gateway error after retries: ${lastDetail}`, { status: 502 });
}
