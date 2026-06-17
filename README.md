# hybrid_text_fusion over SciFact

A small, self-contained demo of **lexical hybrid search** — the `HybridText`
rank expression on [hev layer](https://hevlayer.com) — over ~5,000 scientific
abstracts from [BEIR/SciFact](https://huggingface.co/datasets/BeIR/scifact).

You type one query string. The Layer gateway tokenizes it, expands **one
full-input BM25 leg plus one fuzzy leg per token**, and fuses the legs with
**reciprocal rank fusion (RRF)** — so results survive typos and morphological
variants without losing BM25's relevance signal. No embeddings, no GPU, no
vector index: `HybridText` is purely lexical.

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
| `ingest.py` | Download SciFact, declare a BM25 + fuzzy schema, bulk-upsert every abstract through the gateway, and write `queries.json`. |
| `server.py` | FastAPI app: serves the UI and proxies search to the gateway (the API key stays server-side). |
| `static/index.html` | Single-page search UI with a live **fusion inspector** (tokens, legs, fuzziness, RRF constant). |
| `eval.py` | Score nDCG@10 / recall@10 over SciFact's qrels — the primary expansion vs the empty-result fallback. |
| `config.py` | Env / `.env` configuration. |

## Prerequisites

A **running Layer gateway** pointed at your Turbopuffer. The gateway resolves
its upstream endpoint + API key from a Kubernetes `VectorStore` resource, so it
needs a cluster context with a `VectorStore` (and its credential `Secret`).
Minimal shape:

```yaml
apiVersion: hevlayer.com/v1alpha1
kind: VectorStore
metadata: { name: default }
spec:
  kind: Turbopuffer
  default: true
  endpoint: { url: "https://api.turbopuffer.com" }
  credential: { secretRef: { name: turbopuffer-creds, key: api-key } }
  # inboundAuth defaults to deriveFromStore: the bearer token the gateway
  # accepts is your upstream Turbopuffer API key.
---
apiVersion: v1
kind: Secret
metadata: { name: turbopuffer-creds }
stringData: { api-key: "<your-turbopuffer-api-key>" }
```

Aerospike, S3, and PostgreSQL are **not** required for this demo — they're
optional/degradable for upsert + query. See the gateway's own docs for the full
boot story.

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

## How it works

- **Ingest.** `ingest.py` posts the Turbopuffer-compatible `upsert_columns`
  write straight to the gateway over plain HTTP, declaring `content` (title +
  abstract) as both `full_text_search` (for the BM25 leg) and `fuzzy` (for the
  per-token fuzzy legs). `title` and `text` are stored for display.
  (The stock `turbopuffer` client also works against the gateway, but it
  gzip-compresses large write bodies and the gateway does not yet decode them,
  so bulk ingest uses uncompressed JSON.)
- **Query.** The UI posts `rank_by: ["content", "HybridText", "<your query>"]`.
  The gateway tokenizes the input (UAX-29 word boundaries, lowercased, ≤15
  tokens), builds the BM25 + per-token fuzzy legs, and returns the fused `rows`
  plus a `hybrid` echo describing the expansion.
- **Typos.** Every HybridText leg ranks by BM25 over the full query, so when at
  least one token matches a stored term exactly, that anchors relevance and the
  fuzzy legs rescue the misspelled tokens. When *no* token matches exactly (a
  fully-misspelled query), the gateway falls back to neutral-rank fuzzy legs so
  results still surface — flagged `surfaced: true` in the echo — ranked by how
  many tokens each document fuzzy-matches.
- **Fusion inspector.** The right-hand panel renders the `hybrid` echo so you
  can watch one query string become N legs and see the RRF parameters.

## License

[MIT](LICENSE).
