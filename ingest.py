#!/usr/bin/env python3
"""Load the BEIR/SciFact corpus into a Layer namespace for HybridText search.

SciFact is ~5,183 scientific abstracts that ship with real queries and
relevance judgments, so the demo can show *provable* relevance — not just
keyword hits.

This script:
  1. downloads the SciFact corpus + queries + test qrels from Hugging Face,
  2. declares a full-text (BM25) schema on the search field through the Layer
     gateway and bulk-upserts every abstract, and
  3. writes `queries.json` — a sample of real test queries with their
     known-relevant document ids — for the UI's "try these" chips.

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
QUERIES_OUT = HERE / "queries.json"

BATCH_SIZE = 256
SAMPLE_QUERIES = 40

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


def build_examples() -> list[dict]:
    """Join SciFact test queries with their qrels into UI example chips."""
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
    sample = examples[:SAMPLE_QUERIES]
    QUERIES_OUT.write_text(json.dumps({"examples": sample}, indent=2))
    log(f"  wrote {len(sample)} example queries -> {QUERIES_OUT.name}")
    return sample


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
    parser.add_argument("--no-wait", action="store_true", help="skip the post-ingest readiness poll")
    args = parser.parse_args()

    if not config.API_KEY:
        log("warning: LAYER_API_KEY is empty — sending unauthenticated requests "
            "(only works if the gateway is in `open` auth mode).")
    log(f"Gateway:   {config.GATEWAY_URL}")
    log(f"Namespace: {config.NAMESPACE}")
    log("")

    total = ingest(args.limit)
    log(f"\nUpserted {total:,} documents into '{config.NAMESPACE}'.")

    log("\nBuilding example queries…")
    sample = build_examples()

    if args.no_wait:
        log("\nDone. Start the UI:  python server.py")
        return 0

    probe = sample[0]["text"] if sample else "cancer"
    log(f"\nWaiting for the BM25 index to become searchable (probe: {probe!r})…")
    if wait_until_searchable(probe):
        log("Index is searchable. Start the UI:  python server.py")
        return 0
    log("Timed out waiting for results; the index may still be building. "
        "Try the UI in a moment:  python server.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
