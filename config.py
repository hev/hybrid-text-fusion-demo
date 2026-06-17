"""Shared configuration for the SciFact HybridText demo.

Reads settings from the environment, falling back to a sibling `.env` file so
the demo runs with no extra dependencies. Real environment variables always win
over `.env`.
"""
from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # setdefault: a real env var overrides the file.
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

GATEWAY_URL = os.environ.get("LAYER_GATEWAY_URL", "http://localhost:8080").rstrip("/")
API_KEY = os.environ.get("LAYER_API_KEY", "")
NAMESPACE = os.environ.get("LAYER_NAMESPACE", "scifact-demo")
# The single field HybridText ranks over (must be full_text_search in the schema).
SEARCH_FIELD = os.environ.get("LAYER_SEARCH_FIELD", "content")


def auth_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    return headers
