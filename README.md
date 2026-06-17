# hybrid_text_fusion over SciFact

A small, self-contained demo of **lexical hybrid search** — the `HybridText`
rank expression on [hev layer](https://hevlayer.com) — over ~5,000 scientific
abstracts from [BEIR/SciFact](https://huggingface.co/datasets/BeIR/scifact).

You type one query string. The Layer gateway tokenizes it, expands **one
full-input BM25 leg plus one fuzzy leg per token**, and fuses the legs with
**reciprocal rank fusion (RRF)** — so results survive typos and morphological
variants without losing BM25's relevance signal. No embeddings, no GPU, no
vector index: `HybridText` is purely lexical.

**Live demo:** <https://hybrid-text.hevlayer.com> — every search shows its
gateway round-trip time, so you can watch one query string fan out into a fused
multi-leg search and come back in milliseconds.

SciFact ships real queries with relevance judgments, so the UI can show
*provable* relevance: pick a bundled query and the known-relevant abstracts are
flagged ✓ in the results.

```
corpus (HF SciFact) ──ingest.py──► Layer gateway ──► Turbopuffer
                                        │
   query ──► POST /v2/namespaces/{ns}/query  rank_by:["content","HybridText", q]
                                        │
                                  {rows, hybrid}  ──► search UI + fusion inspector
```

## What's here

| File | Role |
|------|------|
| `ingest.py` | Download SciFact, declare a BM25 + fuzzy schema, bulk-upsert every abstract through the gateway, and write `static/queries.json`. |
| `server.py` | FastAPI app for local dev: serves the UI and proxies search to the gateway (the API key stays server-side). |
| `src/worker.js` | Cloudflare Worker: same UI + search proxy as `server.py`, for the live deploy. The API key is a Worker secret. |
| `wrangler.jsonc` | Worker config — static assets, gateway vars, and the `hybrid-text.hevlayer.com` custom domain. |
| `static/index.html` | Single-page search UI with a live **fusion inspector** (tokens, legs, fuzziness, RRF constant) and the gateway query time. |
| `eval.py` | Score nDCG@10 / recall@10 over SciFact's qrels — the primary expansion vs the empty-result fallback. |
| `config.py` | Env / `.env` configuration. |

## Prerequisites

A **running Layer gateway** pointed at your Turbopuffer. See
[hevlayer.com](https://hevlayer.com) to set one up.

## Setup

```bash
git clone https://github.com/hev/hybrid-text-fusion-demo
cd hybrid-text-fusion-demo
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: set LAYER_GATEWAY_URL and LAYER_API_KEY
```

`LAYER_API_KEY` is the bearer token your gateway accepts — in the default
`deriveFromStore` auth mode that's your upstream Turbopuffer API key.

## Run

```bash
# 1. Load the corpus (try a slice first to confirm wiring)
python ingest.py --limit 200
python ingest.py              # full ~5,183-doc corpus

# 2. Start the search UI
python server.py              # http://localhost:8000
```

`ingest.py` waits for the index to become searchable before exiting, so once it
prints "Index is searchable" the UI is ready.

## Deploy (Cloudflare Workers)

The live demo runs as a Cloudflare Worker (`src/worker.js`) that mirrors
`server.py`: it serves the UI from `static/` and proxies `/api/search` to the
gateway, keeping the API key server-side. The gateway URL/namespace are plain
`vars` in `wrangler.jsonc`; the bearer token is a Worker **secret**.

```bash
npm install                          # installs wrangler locally

# One-time: set the gateway bearer token as a secret (never committed)
wrangler secret put LAYER_API_KEY    # paste the token at the prompt

wrangler dev                         # local preview (reads .dev.vars)
wrangler deploy                      # ship to hybrid-text.hevlayer.com
```

The custom domain in `wrangler.jsonc` provisions its own DNS record and
certificate on first deploy — the `hevlayer.com` zone just has to live in the
same Cloudflare account. The example chips ship in `static/queries.json` (a
committed snapshot of SciFact's test queries + qrels), so a clean checkout
deploys with working examples without re-running `ingest.py`.

## How it works

- **Ingest.** `ingest.py` posts the Turbopuffer-compatible `upsert_columns`
  write straight to the gateway over plain HTTP, declaring `content` (title +
  abstract) as both `full_text_search` (for the BM25 leg) and `fuzzy` (for the
  per-token fuzzy legs). `title` and `text` are stored for display.
  (The stock `turbopuffer` client also works against the gateway, but it
  gzip-compresses large write bodies and the gateway does not yet decode them,
  so bulk ingest uses uncompressed JSON.)
- **Query.** The UI posts `rank_by: ["content", "HybridText", "<your query>"]`.
  The gateway [tokenizes the input](https://github.com/turbopuffer/alyze)
  (UAX-29 word boundaries, lowercased, ≤15 tokens), builds the BM25 + per-token
  fuzzy legs, and returns the fused `rows` plus a `hybrid` echo describing the
  expansion.
- **Typos.** Every HybridText leg ranks by BM25 over the full query, so when at
  least one token matches a stored term exactly, that anchors relevance and the
  fuzzy legs rescue the misspelled tokens. When *no* token matches exactly (a
  fully-misspelled query), the gateway falls back to neutral-rank fuzzy legs so
  results still surface — flagged `surfaced: true` in the echo — ranked by how
  many tokens each document fuzzy-matches.
- **Fusion inspector.** The right-hand panel renders the `hybrid` echo so you
  can watch one query string become N legs and see the RRF parameters.
- **Query time.** The proxy measures the gateway round-trip and returns it as
  `took_ms`; the UI shows it next to the result count and in the inspector, so
  the cost of fanning out and re-fusing N legs is visible on every search.

## License

[MIT](LICENSE).
