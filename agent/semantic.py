"""Semantic ranking — meaning-based retrieval with a graceful fallback.

Everything that recalls past material (playbooks, lessons, cross-conversation
snippets) is keyword-matched by default: deterministic, free, offline. When a
local embedding model is available (Ollama), this module upgrades those lookups
to *semantic* ranking so a fully reworded request still finds the right
playbook ("push my site live" ≈ "deploy hugo blog to netlify").

Contract: `rerank()` returns None when no embedder is available — callers keep
their keyword path unchanged, so behaviour without Ollama is identical to
before, and every test remains deterministic.
"""
from __future__ import annotations

import math

MIN_SIM = 0.35          # cosine floor: below this a "match" is noise


def _embed(texts: list) -> list | None:
    try:
        from . import rag
        return rag.embed_texts(list(texts))
    except Exception:
        return None


def cosine(a, b) -> float:
    try:
        num = sum(x * y for x, y in zip(a, b))
        da = math.sqrt(sum(x * x for x in a))
        db = math.sqrt(sum(y * y for y in b))
        return num / (da * db) if da and db else 0.0
    except Exception:
        return 0.0


def rerank(query: str, candidates: list, text_of, limit: int,
           min_sim: float = MIN_SIM) -> list | None:
    """Order `candidates` by semantic similarity to `query`.

    text_of(c) -> the text that represents a candidate.
    Returns the top `limit` candidates above `min_sim`, best first — or None
    when embeddings are unavailable (caller falls back to keyword ranking).
    A single batched call embeds the query and every candidate together.
    """
    if not candidates:
        return []
    texts = [query] + [text_of(c) or "" for c in candidates]
    vecs = _embed(texts)
    if not vecs or len(vecs) != len(texts):
        return None
    q = vecs[0]
    scored = sorted(((cosine(q, v), c) for v, c in zip(vecs[1:], candidates)),
                    key=lambda x: -x[0])
    return [c for s, c in scored if s >= min_sim][:max(1, limit)]
