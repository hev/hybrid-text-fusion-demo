# hybrid-text-fusion-demo — Agent Guide

Engineering and operations guide for the SciFact `HybridText` eval demo. For
build context and conventions read `CLAUDE.md`; for the user-visible shape read
`README.md`. Design of record: **RFC 0057** / **RFC 0022** in `../layer/docs/rfcs/`.

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
or RFC) so the design-preview signal reaches the Layer team.

## Run & gateway

```bash
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env                 # LAYER_GATEWAY_URL + LAYER_API_KEY (upstream Turbopuffer key)
python ingest.py --limit 200         # slice first; then `python ingest.py` for the full ~5,183 corpus
python server.py                     # http://localhost:8000
python eval.py                       # nDCG@10 / recall@10 over qrels
```

Production is a Cloudflare Worker (`src/worker.js`, `hybrid-text.hevlayer.com`):
`npm install`, `wrangler secret put LAYER_API_KEY`, `wrangler deploy`. Keep
`server.py` and the Worker in lockstep.

## Agent rules

- **Reimplement nothing** the gateway owns (CLAUDE.md § The one rule). A gap is a
  finding for `../layer`, not local code.
- Don't commit secrets or the corpus; don't revert unrelated user changes.
