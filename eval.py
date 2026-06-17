#!/usr/bin/env python3
"""RFC 0057 decision gate: does the empty-result fallback (B) help, and does it
regress the clean case?

Scores nDCG@10 and recall@10 over SciFact's test qrels for two expansions,
replayed client-side through the gateway's multi-query passthrough so the
comparison is independent of what's deployed:

  - primary:  the current BM25-anchored expansion — one BM25 leg over the full
              input plus one BM25-ranked fuzzy leg per token. Fuzzy is
              filter-only, so a fully-misspelled query fuses to zero rows.
  - fallback: RFC 0057 Option B — primary, then, when it returns nothing,
              neutral-rank (`id asc`) fuzzy surfacing legs.

Two query sets:
  - clean — SciFact's test queries verbatim. primary and fallback MUST be
            identical here (the regression check; fallback never fires).
  - typo  — every content word of length >= 6 deterministically corrupted, to
            exercise the all-typo failure case B targets.

The residual gap on the typo set (queries fallback still misses) is the
quantified case for Option A.

Usage:
    python eval.py                 # 100 test queries, clean + typo
    python eval.py --limit 0       # all test queries (~300)
"""
from __future__ import annotations

import argparse
import math
import re

import httpx
from datasets import load_dataset

import config

# Mirrors the gateway's HybridText `auto` ladder (RFC 0057 / hybrid_text.rs).
LADDER = [
    {"min_query_chars": 3, "distance": 0},
    {"min_query_chars": 6, "distance": 1},
    {"min_query_chars": 9, "distance": 2},
]
TOP_K = 10
PER_LEG_LIMIT = 50
RANK_CONSTANT = 60
FIELD = config.SEARCH_FIELD


def tokenize(text: str) -> list[str]:
    """Approximate the gateway tokenizer policy: lowercase word-ish tokens,
    drop < 2 chars, dedupe preserving order, cap at 15."""
    out: list[str] = []
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        if len(tok) >= 2 and tok not in out:
            out.append(tok)
    return out[:15]


def typo(text: str) -> str:
    """Deterministically corrupt every alphabetic word of length >= 6 by
    deleting one interior character. No randomness — reproducible runs."""
    def corrupt(m: re.Match[str]) -> str:
        w = m.group(0)
        if len(w) >= 6 and w.isalpha():
            return w[:3] + w[4:]  # drop the 4th char
        return w
    return re.sub(r"[A-Za-z]+", corrupt, text)


def fused_rows(body: dict) -> list[dict]:
    """Unwrap a fused multi-query response (same logic as the gateway)."""
    if isinstance(body.get("rows"), list):
        return body["rows"]
    results = body.get("results")
    if isinstance(results, list) and len(results) == 1 and isinstance(results[0].get("rows"), list):
        return results[0]["rows"]
    return []


def _multi_query(client: httpx.Client, legs: list[dict]) -> list[dict]:
    url = f"{config.GATEWAY_URL}/v2/namespaces/{config.NAMESPACE}/query?stainless_overload=multiQuery"
    body = {"queries": legs, "rerank_by": ["RRF", {"rank_constant": RANK_CONSTANT}]}
    resp = client.post(url, json=body)
    resp.raise_for_status()
    return fused_rows(resp.json())


def _fuzzy(token: str) -> list:
    return [FIELD, "Fuzzy", token, {"max_edit_distance": LADDER}]


def primary(client: httpx.Client, query: str) -> list[dict]:
    tokens = tokenize(query)
    legs = [{"rank_by": [FIELD, "BM25", query], "top_k": PER_LEG_LIMIT}]
    for t in tokens:
        legs.append({"rank_by": [FIELD, "BM25", query], "filters": _fuzzy(t), "top_k": PER_LEG_LIMIT})
    return _multi_query(client, legs)


def fallback(client: httpx.Client, query: str) -> list[dict]:
    rows = primary(client, query)
    if rows:
        return rows
    # RFC 0057 Option B: neutral-rank fuzzy surfacing legs.
    tokens = tokenize(query)
    legs = [{"rank_by": ["id", "asc"], "filters": _fuzzy(t), "top_k": PER_LEG_LIMIT} for t in tokens]
    return _multi_query(client, legs) if legs else []


def ids(rows: list[dict]) -> list[str]:
    return [str(r.get("id")) for r in rows]


def dcg(gains: list[int]) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def ndcg_at_k(ranked: list[str], relevant: set[str], k: int = TOP_K) -> float:
    gains = [1 if rid in relevant else 0 for rid in ranked[:k]]
    idcg = dcg([1] * min(len(relevant), k))
    return dcg(gains) / idcg if idcg else 0.0


def recall_at_k(ranked: list[str], relevant: set[str], k: int = TOP_K) -> float:
    if not relevant:
        return 0.0
    return len(set(ranked[:k]) & relevant) / len(relevant)


def load_test_queries(limit: int) -> list[tuple[str, set[str]]]:
    queries = load_dataset("BeIR/scifact", "queries", split="queries")
    qtext = {str(q["_id"]): q["text"] for q in queries}
    qrels = load_dataset("BeIR/scifact-qrels", split="test")
    relevant: dict[str, set[str]] = {}
    for row in qrels:
        if int(row["score"]) > 0:
            relevant.setdefault(str(row["query-id"]), set()).add(str(row["corpus-id"]))
    items = [(qtext[qid], rels) for qid, rels in relevant.items() if qid in qtext]
    items.sort(key=lambda it: it[0])  # deterministic order
    return items[:limit] if limit > 0 else items


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100, help="number of test queries (0 = all)")
    args = parser.parse_args()

    items = load_test_queries(args.limit)
    print(f"Gateway:   {config.GATEWAY_URL}")
    print(f"Namespace: {config.NAMESPACE}")
    print(f"Queries:   {len(items)} SciFact test queries (with qrels)\n")

    # arm -> set -> accumulators
    agg = {arm: {s: {"ndcg": 0.0, "recall": 0.0, "empty": 0} for s in ("clean", "typo")}
           for arm in ("primary", "fallback")}
    fallback_fired = 0
    fallback_still_empty = 0

    with httpx.Client(headers=config.auth_headers(), timeout=60) as client:
        for i, (qtext_, relevant) in enumerate(items, 1):
            for set_name, text in (("clean", qtext_), ("typo", typo(qtext_))):
                p_rows = primary(client, text)
                f_rows = p_rows if p_rows else fallback(client, text)
                if set_name == "typo" and not p_rows:
                    fallback_fired += 1
                    if not f_rows:
                        fallback_still_empty += 1
                for arm, rows in (("primary", p_rows), ("fallback", f_rows)):
                    rid = ids(rows)
                    agg[arm][set_name]["ndcg"] += ndcg_at_k(rid, relevant)
                    agg[arm][set_name]["recall"] += recall_at_k(rid, relevant)
                    if not rows:
                        agg[arm][set_name]["empty"] += 1
            if i % 25 == 0:
                print(f"  ...{i}/{len(items)}")

    n = len(items)
    print(f"\n{'arm':<10}{'set':<8}{'nDCG@10':>10}{'recall@10':>12}{'empty':>8}")
    print("-" * 48)
    for arm in ("primary", "fallback"):
        for s in ("clean", "typo"):
            a = agg[arm][s]
            print(f"{arm:<10}{s:<8}{a['ndcg']/n:>10.4f}{a['recall']/n:>12.4f}{a['empty']:>8}")

    clean_ident = (
        abs(agg["primary"]["clean"]["ndcg"] - agg["fallback"]["clean"]["ndcg"]) < 1e-9
        and agg["primary"]["clean"]["empty"] == agg["fallback"]["clean"]["empty"]
    )
    print("\nVerdict")
    print(f"  clean set primary == fallback (no regression): {'YES' if clean_ident else 'NO'}")
    print(f"  typo queries where primary was empty:          {fallback_fired}")
    print(f"  ...of those, fallback still empty:             {fallback_still_empty}")
    lift = (agg["fallback"]["typo"]["recall"] - agg["primary"]["typo"]["recall"]) / n
    print(f"  typo recall@10 lift (fallback - primary):      {lift:+.4f}")
    print("\n  Residual = typo queries the fallback surfaces but ranks poorly + the")
    print("  still-empty count above. That residual is the case for Option A.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
