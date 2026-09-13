"""Embedding client: qwen3-embedding-8b via OpenRouter (4096-dim)."""
from __future__ import annotations

import os
import time as _time

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/embeddings"
MODEL = "qwen/qwen3-embedding-8b"
DIM = 4096
BATCH_SIZE = 50
# A healthy embed is sub-second; the old flat timeout=120.0 meant a single hung
# response body (OpenRouter occasionally stalls a connection) blocked for 120s,
# ×3 retries = 360s per batch — which silently wedged radar-scan and every other
# embedding-dependent cron (the "scripts error / hang" the operator saw). Use a
# structured timeout (10s connect / 45s read) so a stuck read fails fast and the
# retry below reconnects. Mirrors the investment profile's embed client.
_HTTP_TIMEOUT = httpx.Timeout(45.0, connect=10.0)


def _skip() -> bool:
    return os.environ.get("CORTEX_SKIP_EMBED") == "1"


def embed_texts(texts: list[str], max_retries: int = 3) -> list[list[float]]:
    if _skip():
        return [[0.0] * DIM for _ in texts]
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise ValueError("OPENROUTER_API_KEY not set")
    out: list[list[float]] = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        for attempt in range(max_retries):
            try:
                resp = httpx.post(
                    OPENROUTER_URL,
                    headers={"Authorization": f"Bearer {key}"},
                    json={"model": MODEL, "input": batch},
                    timeout=_HTTP_TIMEOUT,
                )
                resp.raise_for_status()
                data = sorted(resp.json()["data"], key=lambda d: d["index"])
                out.extend([d["embedding"] for d in data])
                break
            except (httpx.HTTPError, KeyError) as e:
                if attempt == max_retries - 1:
                    raise RuntimeError(f"embed failed after {max_retries}: {e}") from e
                _time.sleep(2 ** attempt)
    return out


def embed_text(text: str) -> list[float]:
    return embed_texts([text])[0]
