#!/usr/bin/env python3
"""Load the BEIR/SciFact corpus into a Layer namespace for HybridText search.

SciFact is ~5,183 scientific abstracts that ship with real queries and
relevance judgments, so the demo can show *provable* relevance — not just
keyword hits.

This script:
  1. downloads the SciFact corpus + queries + test qrels from Hugging Face,
  2. declares a full-text (BM25) schema on the search field through the Layer
     gateway and bulk-upserts every abstract, and
  3. writes `queries.json` — real test queries with their known-relevant
     document ids, ranked so the ones that provably surface a relevant
     abstract (clean *and* with an injected typo) come first — for the UI's
     "try these" chips.

Writes go over plain HTTP (`upsert_columns`) so the payload stays uncompressed:
the gateway parses each write body to stamp shard metadata and does not decode
the gzip request bodies the official Turbopuffer client uses for large writes.
HybridText itself is lexical-only (BM25 + per-token fuzzy fused by RRF), so the
namespace is full-text only — no vectors and no embedding step.

Usage:
    python ingest.py                # full corpus
    python ingest.py --limit 200    # quick smoke test on a slice
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
from datasets import load_dataset

import config

HERE = Path(__file__).resolve().parent
QUERIES_OUT = HERE / "static" / "queries.json"

BATCH_SIZE = 256
SAMPLE_QUERIES = 40
# Curation: how the example chips get ordered before they're written. We probe
# the live index with the same HybridText query the UI sends and put the chips
# that surface a known-relevant abstract (clean AND with an injected typo)
# first — the UI shows the first eight, so the demo should lead with queries
# that actually prove relevance and survive a typo.
RECALL_K = 10            # a gold doc must land in the top-K to count as a hit
MAX_CANDIDATES = 120     # cap probes; stops early once SAMPLE_QUERIES are solid

# `content` (title + abstract) is the one field HybridText ranks over:
#   - `full_text_search: True` builds the BM25 index HybridText's full-input
#     leg ranks against.
#   - `fuzzy: True` builds the fuzzy index HybridText's per-token `Fuzzy` legs
#     require. Without it the gateway's fuzzy legs error upstream.
#   - `filterable: False` matters: Turbopuffer makes string attributes
#     filterable by default, which caps values at 4096 bytes — and some
#     abstracts are larger. We never filter on these fields (HybridText uses
#     the full-text/fuzzy indexes, not the exact-match filter index), so we
#     turn filtering off and the size limit with it.
# `title` and `text` are stored for display only.
SCHEMA = {
    "title": {"type": "string", "filterable": False},
    "text": {"type": "string", "filterable": False},
    config.SEARCH_FIELD: {
        "type": "string",
        "full_text_search": True,
        "fuzzy": True,
        "filterable": False,
    },
}


def log(msg: str) -> None:
    print(msg, flush=True)


def build_columns(corpus, limit: int | None):
    """Yield (ids, titles, texts, contents) column batches."""
    ids, titles, texts, contents = [], [], [], []
    seen = 0
    for row in corpus:
        if limit is not None and seen >= limit:
            break
        seen += 1
        doc_id = str(row["_id"])
        title = (row.get("title") or "").strip()
        text = (row.get("text") or "").strip()
        content = ". ".join(part for part in (title, text) if part) or doc_id
        ids.append(doc_id)
        titles.append(title)
        texts.append(text)
        contents.append(content)
        if len(ids) >= BATCH_SIZE:
            yield ids, titles, texts, contents
            ids, titles, texts, contents = [], [], [], []
    if ids:
        yield ids, titles, texts, contents


def ingest(limit: int | None) -> int:
    log("Downloading BEIR/SciFact corpus from Hugging Face…")
    corpus = load_dataset("BeIR/scifact", "corpus", split="corpus")
    log(f"  corpus: {len(corpus):,} documents")

    url = f"{config.GATEWAY_URL}/v2/namespaces/{config.NAMESPACE}"
    total = 0
    with httpx.Client(headers=config.auth_headers(), timeout=60) as client:
        for ids, titles, texts, contents in build_columns(corpus, limit):
            body = {
                "upsert_columns": {
                    "id": ids,
                    "title": titles,
                    "text": texts,
                    config.SEARCH_FIELD: contents,
                },
                # Schema is idempotent; sending it with every batch keeps the
                # script restartable without ordering assumptions.
                "schema": SCHEMA,
            }
            resp = client.post(url, json=body)
            resp.raise_for_status()
            total += len(ids)
            log(f"  upserted {total:,} docs")
    return total


def load_examples() -> list[dict]:
    """Join SciFact test queries with their qrels, sorted by query id."""
    try:
        queries = load_dataset("BeIR/scifact", "queries", split="queries")
        qtext = {str(q["_id"]): q["text"] for q in queries}
        qrels = load_dataset("BeIR/scifact-qrels", split="test")
    except Exception as exc:  # noqa: BLE001 — example chips are a nicety, not core
        log(f"  (skipping example queries: {exc})")
        return []

    relevant: dict[str, list[str]] = {}
    for row in qrels:
        if int(row["score"]) <= 0:
            continue
        relevant.setdefault(str(row["query-id"]), []).append(str(row["corpus-id"]))

    examples = [
        {"id": qid, "text": qtext[qid], "relevant_doc_ids": docids}
        for qid, docids in relevant.items()
        if qid in qtext
    ]
    examples.sort(key=lambda e: int(e["id"]))
    return examples


def make_typo(text: str) -> str:
    """Drop a middle letter from the longest word.

    Mirrors the UI's `makeTypo` (static/index.html) so curation probes the
    exact string a typo chip sends. A single deletion stays within the
    gateway's fuzzy edit distance, so the per-token fuzzy legs can recover it.
    """
    words = text.split()
    best_i, best_len = -1, 0
    for i, w in enumerate(words):
        letters = sum(c.isalpha() for c in w)
        if letters > best_len:
            best_len, best_i = letters, i
    if best_i < 0 or best_len < 5:  # nothing worth corrupting
        return text
    w = words[best_i]
    pos = [i for i, c in enumerate(w) if c.isalpha()]
    drop = pos[len(pos) // 2]
    words[best_i] = w[:drop] + w[drop + 1:]
    return " ".join(words)


def hybrid_top_ids(client: httpx.Client, query: str, top_k: int = RECALL_K) -> list[str]:
    """Top-K doc ids for the same HybridText query the UI's /api/search issues."""
    url = f"{config.GATEWAY_URL}/v2/namespaces/{config.NAMESPACE}/query"
    body = {"rank_by": [config.SEARCH_FIELD, "HybridText", query], "top_k": top_k}
    for attempt in range(3):
        try:
            resp = client.post(url, json=body)
            if resp.status_code == 200:
                return [str(r.get("id")) for r in resp.json().get("rows", [])]
        except httpx.HTTPError:
            pass
        time.sleep(0.4 * (attempt + 1))
    return []


def select_examples(examples: list[dict]) -> list[dict]:
    """Rank relevance-proving chips first, then take the first SAMPLE_QUERIES.

    The UI shows the first eight chips (static/index.html: `ex.slice(0, 8)`),
    so the demo should lead with queries that actually surface a known-relevant
    abstract — and keep doing so once a typo is injected. For each candidate we
    run the same HybridText query the UI sends, clean and typo'd, and call it
    "solid" when a gold doc lands in the top-K both times. Solid chips go first;
    weaker ones are kept but moved to the back. Falls back to id order if the
    gateway answers nothing.
    """
    solid: list[dict] = []
    weak: list[dict] = []
    responded = 0
    with httpx.Client(headers=config.auth_headers(), timeout=30) as client:
        for e in examples[:MAX_CANDIDATES]:
            if len(solid) >= SAMPLE_QUERIES:
                break  # enough relevance-proving chips to fill the sample
            gold = {str(d) for d in e["relevant_doc_ids"]}
            clean_ids = hybrid_top_ids(client, e["text"])
            responded += bool(clean_ids)
            clean_hit = bool(set(clean_ids) & gold)
            typo_hit = False
            if clean_hit:  # only worth checking the typo if the clean query hit
                typo_ids = hybrid_top_ids(client, make_typo(e["text"]))
                responded += bool(typo_ids)
                typo_hit = bool(set(typo_ids) & gold)
            (solid if (clean_hit and typo_hit) else weak).append(e)
            # If the operator is answering nothing, don't burn the whole pool.
            if responded == 0 and len(weak) >= 8:
                log("  HybridText returned no rows; writing chips in id order.")
                return examples[:SAMPLE_QUERIES]
    ordered = solid + weak
    log(f"  curated: {len(solid)} prove relevance (clean + typo), "
        f"{len(weak)} weaker moved to the back")
    return ordered[:SAMPLE_QUERIES]


def wait_until_searchable(sample_query: str, timeout_s: int = 180) -> bool:
    """Poll until the full-text index returns rows (indexing has lag).

    Uses a plain BM25 query: it confirms the index is built and queryable
    without depending on the HybridText operator being deployed on the gateway.
    """
    url = f"{config.GATEWAY_URL}/v2/namespaces/{config.NAMESPACE}/query"
    body = {"rank_by": [config.SEARCH_FIELD, "BM25", sample_query], "top_k": 5}
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            resp = httpx.post(url, json=body, headers=config.auth_headers(), timeout=30)
            if resp.status_code == 200 and resp.json().get("rows"):
                return True
        except httpx.HTTPError:
            pass
        time.sleep(3)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="only ingest the first N documents")
    parser.add_argument("--no-wait", action="store_true",
                        help="skip the post-ingest readiness poll (also skips curation, which needs a queryable index)")
    parser.add_argument("--no-curate", action="store_true",
                        help="write example chips in query-id order instead of ranking the relevance-proving ones first")
    parser.add_argument("--skip-ingest", action="store_true",
                        help="don't re-upload the corpus; only rebuild and curate queries.json")
    args = parser.parse_args()

    if not config.API_KEY:
        log("warning: LAYER_API_KEY is empty — sending unauthenticated requests "
            "(only works if the gateway is in `open` auth mode).")
    log(f"Gateway:   {config.GATEWAY_URL}")
    log(f"Namespace: {config.NAMESPACE}")
    log("")

    if args.skip_ingest:
        log("Skipping corpus ingest (--skip-ingest); refreshing example chips only.")
    else:
        total = ingest(args.limit)
        log(f"\nUpserted {total:,} documents into '{config.NAMESPACE}'.")

    log("\nLoading SciFact test queries + qrels…")
    examples = load_examples()

    # Curation probes the live index, so it can only run once the index is
    # searchable. --no-wait skips the readiness poll, so it also skips curation.
    curate = bool(examples) and not args.no_curate and not args.no_wait
    if examples and not args.no_wait:
        probe = examples[0]["text"]
        log(f"\nWaiting for the index to become searchable (probe: {probe!r})…")
        if not wait_until_searchable(probe):
            log("Timed out waiting for results; the index may still be building. "
                "Writing chips in id order (skipping curation).")
            curate = False

    if curate:
        log("\nCurating example chips (relevance-proving queries first)…")
        sample = select_examples(examples)
    else:
        sample = examples[:SAMPLE_QUERIES]

    QUERIES_OUT.write_text(json.dumps({"examples": sample}, indent=2) + "\n")
    log(f"Wrote {len(sample)} example queries -> {QUERIES_OUT.name}")
    log("\nDone. Start the UI:  python server.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
