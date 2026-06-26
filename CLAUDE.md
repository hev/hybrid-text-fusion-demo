# hybrid-text-fusion-demo — build context

The **eval-shaped** demo of hev layer's lexical `HybridText` rank expression over
~5,000 BEIR/SciFact abstracts: one query string fans out into a full-input BM25
leg + one fuzzy leg per token, fused by RRF, scored against SciFact qrels. No
embeddings, no GPU, no vector index. Read `README.md` for the user-visible shape;
the design of record is **RFC 0057** (fuzzy surfacing) and **RFC 0022** (hybrid
fusion) in `../layer/docs/rfcs/`. It is the eval sibling of the UX-shaped
`../shelf` / `../chart` and the fuzzy ancestor of `../drug`.

## ⚠️ IMPORTANT — this repo is a Layer design-preview customer

This repo is a **design-preview customer of hev layer**, not part of the Layer
product. Its job is to *use* Layer the way a real customer would and **report
back** to the Layer team. That feedback loop is a primary responsibility of this
repo, not a side task — the demo working is table stakes; the signal we send the
Layer team is the deliverable.

**When you hit friction, do not fix Layer from here — report it:**

- **A bug, or docs that are wrong / unclear / missing** → file a **GitHub issue**
  on the Layer repo (`hev/layer`) with a minimal repro and the exact page or
  behavior at fault.
- **A missing feature or capability gap** → open an **RFC** in the Layer repo
  (`../layer/docs/rfcs/`), in the existing RFC shape, with this workload as the
  motivating / acceptance case.

**Operations are Layer's job.** This repo has operational access to the shared
Layer cluster, but the goal is that Layer operates *itself* — autoscaling,
scale-to-zero, scheduling, binpacking. Let it. Do **not** hand-tune what Layer is
meant to manage.

- When Layer falls short — autoscaling lags, a pipeline stalls, scale-to-zero
  misbehaves — it is OK to **intervene** to keep the demo healthy. But every
  intervention **must** produce a GitHub issue (bug) or an RFC (missing
  capability). An undocumented manual fix is a process failure: the intervention
  is the symptom, the report is the deliverable.
- **Shared namespace / binpacking.** This repo deploys to a namespace in the
  shared demo cluster alongside the other demos (shelf, shop, chart,
  hybrid-text-fusion-demo, label). Scheduling / binpacking contention may bite.
  Same rule: intervene to stay healthy if you must, but the result is a GH issue
  or an RFC documenting the shortfall — never a silent workaround.

The deliverable of any friction is always a **paper trail in `hev/layer`** (issue
or RFC) so the design-preview signal reaches the Layer team. This demo is the
corpus that drove RFC 0057's fuzzy-surfacing fix — the canonical example of a
finding becoming an RFC.

## The one rule

This demo **reimplements nothing**. Tokenization, the BM25 + per-token fuzzy
legs, RRF fusion, and the `surfaced: true` fallback all live in the gateway; the
app posts `rank_by: ["content","HybridText", q]` and renders the `hybrid` echo.
If you find yourself writing fusion math or a tokenizer, stop — that's a gateway
concern (and a finding for `../layer` if it's missing).

## Stack

Python (`ingest.py` loads SciFact + declares the BM25/fuzzy schema; `server.py`
is the FastAPI dev backend) + a Cloudflare Worker (`src/worker.js`) for the live
deploy at `hybrid-text.hevlayer.com`; `eval.py` scores nDCG@10 / recall@10 over
qrels. Wire shapes are authoritative in `../layer` (`api/query.mdx`); don't
guess field names.
