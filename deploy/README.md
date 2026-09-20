# deploy/ — E9 agent baseline (LYR-99)

Kubernetes resources for the frontier-agent baseline: a model given the same
Layer APIs as the demo, a `top_k` and a wall-clock budget. The demo app itself
deploys as a Cloudflare Worker (`wrangler.jsonc`); nothing here serves it.

| File | Resource | State on the shared demo cluster (`layer-prod`) |
| --- | --- | --- |
| `agent-scifact-sol.yaml` | `Agent layer/scifact-sol` — `openai/gpt-5.6-sol` | applied, `Ready` |
| `agent-scifact-luna.yaml` | `Agent layer/scifact-luna` — `openai/gpt-5.6-luna` | applied, `Ready` |
| `apikey.yaml` | `ApiKey layer/scifact-demo` — store grant + `agent.scifact-*` | **not applied** (see below) |

Both Agents bind `indices: [scifact-demo]` (the namespace the live Worker
serves), recallDepth 50, fanout 8, `rankBy: auto`, provenance + trace on, no
`fallback`. `scifact-demo` has no `embed:` attribute, so every leg runs lexical.

**Credential / spend.** The Agents reuse the existing Secret
`layer/chart-openrouter` (key `credential`) via `apiKeySecretRef`. No Secret is
created here. Every agent call bills OpenRouter spend to that credential.

## Apply

    kubectl apply -f deploy/agent-scifact-sol.yaml -f deploy/agent-scifact-luna.yaml
    kubectl get agent scifact-sol scifact-luna -n layer   # PHASE Ready

## Invoke

    curl -X POST "https://aws-us-east-1.hevlayer.com/v2/agents/scifact-luna/query" \
      -H "Authorization: Bearer $LAYER_API_KEY" -H "Content-Type: application/json" \
      -d '{"query": "<claim text>", "top_k": 10}'

`LAYER_API_KEY` is the demo's gateway key (the same bearer `server.py` and the
Worker use).

## Deadline ladder

`spec.budget.deadlineMs` is the budget axis (3000 / 10000 / 30000). The
committed value is 10000; patch per run and restore afterwards:

    kubectl patch agent scifact-luna -n layer --type merge \
      -p '{"spec":{"budget":{"deadlineMs":3000}}}'
    kubectl get agent scifact-luna -n layer \
      -o jsonpath='{.spec.budget.deadlineMs} {.status.phase}{"\n"}'
    # …run…
    kubectl apply -f deploy/agent-scifact-luna.yaml   # back to 10000

The gateway resolves Agents into memory on a periodic tick, so a patch lands
within the refresh interval, not instantly — wait before the first query of a run.

## Entitlement

`apikey.yaml` is the declarative record only. Applying an `ApiKey` mints a new
token, which this demo does not need: the shared gateway uses `deriveFromStore`
auth (the bearer is the store key), and the docs require `agent.<name>` only of
a *minted* key. The cluster also cannot mint CRD-authored keys at present
(`chart/chart-notes` is `Pending`, reason `PepperMissing`).
