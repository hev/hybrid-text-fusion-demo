#!/usr/bin/env python3
"""Search UI backend for the SciFact HybridText demo.

Serves the single-page UI and proxies search requests to the Layer gateway,
injecting the API key server-side so it never reaches the browser. The search
endpoint returns the gateway's `hybrid` echo so the UI can show how one query
string fans out into a BM25 leg + per-token fuzzy legs fused by RRF.

Run:  python server.py   (or: uvicorn server:app --reload)
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import config

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
QUERIES = HERE / "queries.json"

app = FastAPI(title="SciFact · hybrid_text_fusion on Layer")


class SearchRequest(BaseModel):
    query: str
    top_k: int = 10


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def get_config() -> dict:
    return {"namespace": config.NAMESPACE, "field": config.SEARCH_FIELD, "gateway": config.GATEWAY_URL}


@app.get("/api/examples")
def examples() -> dict:
    if QUERIES.exists():
        return json.loads(QUERIES.read_text())
    return {"examples": []}


@app.post("/api/search")
async def search(req: SearchRequest) -> dict:
    query = req.query.strip()
    if not query:
        return {"rows": [], "hybrid": None}

    url = f"{config.GATEWAY_URL}/v2/namespaces/{config.NAMESPACE}/query"
    body = {
        # The Layer-only HybridText rank expression: the gateway tokenizes the
        # input, expands one full-input BM25 leg + one fuzzy leg per token, and
        # fuses with RRF.
        "rank_by": [config.SEARCH_FIELD, "HybridText", query],
        "top_k": max(1, min(req.top_k, 50)),
        "include_attributes": ["title", "text"],
    }
    # Retry transient gateway/edge hiccups (502/503/504 from Cloudflare/ALB, or
    # a connection error) — common for a few seconds after a gateway rollout.
    # Real 4xx (e.g. a 422 validation error) fail immediately.
    transient = {502, 503, 504}
    last_detail = "unknown error"
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(3):
            try:
                resp = await client.post(url, json=body, headers=config.auth_headers())
            except httpx.HTTPError as exc:
                last_detail = f"gateway unreachable: {exc}"
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    return {"rows": data.get("rows", []), "hybrid": data.get("hybrid")}
                if resp.status_code not in transient:
                    raise HTTPException(status_code=resp.status_code, detail=resp.text)
                last_detail = resp.text
            if attempt < 2:
                await asyncio.sleep(0.4 * (attempt + 1))

    raise HTTPException(status_code=502, detail=f"gateway error after retries: {last_detail}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
