"""Pluggable embeddings for semantic search (Phase 7, 7a).

The default backend is **local sentence-transformers** (zero API cost; the price is a
heavy ``torch`` dependency), chosen over a hosted API (Voyage). It is pluggable via
``EMBEDDING_BACKEND``: a dependency-free **hashing** backend (bag-of-words hashed into
the vector space, L2-normalised) is used for tests / offline runs and as a graceful
fallback if sentence-transformers can't be imported — so ``jobpilot_core`` never *hard*
-requires torch, and the semantic-search *mechanism* (embed → store → nearest-neighbour)
is exercisable without it.

The Postgres pgvector columns are fixed at ``embedding_dim`` (1024) from Phase 1. A
model whose native dimension is smaller (all-MiniLM-L6-v2 is 384) is **zero-padded** to
1024 by :func:`fit_to_dim`; appending zeros leaves every pairwise dot product and norm
unchanged, so cosine/L2 ordering is identical — no migration needed to swap models.

This module holds no per-user state and does no DB I/O. Cost/latency is logged here;
the *writes* go through db-mcp (worker computes vectors, db-mcp stores them), keeping
torch out of the MCP service images.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from functools import lru_cache
from typing import Protocol, runtime_checkable

from .settings import Settings

logger = logging.getLogger("jobpilot.embeddings")

_TOKEN = re.compile(r"[a-z0-9]+")


# --------------------------------------------------------------------------- #
# Vector shaping
# --------------------------------------------------------------------------- #
def fit_to_dim(vec: list[float], target_dim: int) -> list[float]:
    """Shape a vector to exactly ``target_dim``: zero-pad if shorter (preserves cosine
    ordering — appended zeros change no dot product or norm), truncate if longer."""
    n = len(vec)
    if n == target_dim:
        return list(vec)
    if n < target_dim:
        return list(vec) + [0.0] * (target_dim - n)
    return list(vec[:target_dim])


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = sum(x * x for x in vec) ** 0.5
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


# --------------------------------------------------------------------------- #
# Embedder implementations
# --------------------------------------------------------------------------- #
@runtime_checkable
class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one ``dim``-length vector per input text (order preserved)."""
        ...


class HashingEmbedder:
    """Dependency-free deterministic embedder: bag-of-words hashed into ``dim`` buckets
    (term-frequency), L2-normalised. No semantic understanding — but identical text maps
    to an identical vector and lexical overlap raises cosine similarity, which is enough
    to test the retrieval mechanism deterministically and offline. Native dim == the DB
    dim, so no padding is needed."""

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim
        self.name = f"hashing-{dim}"

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _TOKEN.findall((text or "").lower()):
            h = int.from_bytes(hashlib.md5(tok.encode()).digest()[:4], "big")
            vec[h % self.dim] += 1.0
        return _l2_normalize(vec)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]


class SentenceTransformerEmbedder:
    """Local sentence-transformers model (lazy-loaded on first use). Native dim is the
    model's; callers pad to the DB dim via :func:`fit_to_dim`."""

    def __init__(self, model_name: str, batch_size: int = 64) -> None:
        self.name = model_name
        self._model_name = model_name
        self._batch_size = batch_size
        self._model = None  # lazy

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # heavy import (torch)

            self._model = SentenceTransformer(self._model_name)
        return self._model

    @property
    def dim(self) -> int:
        return int(self._load().get_sentence_embedding_dimension())

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        # normalize_embeddings so cosine distance is well-behaved.
        arr = model.encode(
            texts, batch_size=self._batch_size, normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [list(map(float, row)) for row in arr]


@lru_cache(maxsize=4)
def _build_embedder(backend: str, model_name: str, dim: int, batch_size: int) -> Embedder:
    if backend == "hashing":
        return HashingEmbedder(dim=dim)
    try:
        emb = SentenceTransformerEmbedder(model_name, batch_size=batch_size)
        emb._load()  # surface an import/download failure now, not mid-run
        return emb
    except Exception as exc:  # noqa: BLE001 - degrade to hashing rather than crash the worker
        logger.warning(
            "embeddings: sentence-transformers unavailable (%s); falling back to hashing", exc)
        return HashingEmbedder(dim=dim)


def get_embedder(settings: Settings) -> Embedder:
    """The configured embedder (cached). Falls back to the hashing backend if the
    sentence-transformers model can't be loaded."""
    return _build_embedder(
        settings.embedding_backend, settings.embedding_model,
        settings.embedding_dim, settings.embedding_batch_size,
    )


# --------------------------------------------------------------------------- #
# The embed operation (with cost/latency logging) — vectors fitted to the DB dim
# --------------------------------------------------------------------------- #
def embed_texts(settings: Settings, texts: list[str]) -> list[list[float]]:
    """Embed ``texts`` and shape each vector to ``embedding_dim``. Logs count, latency,
    model, and cost (0 for the local model — honest, not omitted). Returns [] for []."""
    if not texts:
        return []
    embedder = get_embedder(settings)
    capped = [(t or "")[: settings.embedding_max_chars] for t in texts]
    t0 = time.monotonic()
    raw = embedder.embed(capped)
    vecs = [fit_to_dim(v, settings.embedding_dim) for v in raw]
    dt_ms = (time.monotonic() - t0) * 1000
    logger.info(
        "embeddings.embed model=%s count=%d dim=%d latency_ms=%.1f est_cost_usd=%.6f",
        embedder.name, len(vecs), settings.embedding_dim, dt_ms, 0.0,
    )
    return vecs


# --------------------------------------------------------------------------- #
# Text builders — what we embed for each content kind. Kept compact + deterministic.
# --------------------------------------------------------------------------- #
def job_embedding_text(job: dict) -> str:
    parts = [job.get("title"), job.get("company"), job.get("location"), job.get("description")]
    return "\n".join(p for p in parts if p).strip()


def analysis_embedding_text(analysis: dict) -> str:
    ex = analysis.get("extracted_json") or {}
    bits: list[str] = []
    for key in ("required_skills", "keywords", "tech_stack"):
        vals = ex.get(key) or []
        if isinstance(vals, list) and vals:
            bits.append(f"{key}: " + ", ".join(str(v) for v in vals))
    if ex.get("seniority") or analysis.get("seniority"):
        bits.append(f"seniority: {ex.get('seniority') or analysis.get('seniority')}")
    gaps = (analysis.get("gaps_json") or {}).get("gaps") or ex.get("gaps_vs_resume") or []
    if gaps:
        bits.append("gaps: " + ", ".join(str(g) for g in gaps))
    return "\n".join(bits).strip()


def suggestion_embedding_text(suggestion: dict) -> str:
    bits: list[str] = []
    skills = suggestion.get("skills_to_add") or []
    if skills:
        bits.append("skills_to_add: " + ", ".join(str(s) for s in skills))
    projects = (suggestion.get("projects_json") or {}).get("projects") or []
    for p in projects:
        if isinstance(p, dict) and p.get("title"):
            bits.append(f"project: {p['title']}")
    rewrites = (suggestion.get("bullet_rewrites_json") or {}).get("bullet_rewrites") or []
    for r in rewrites:
        if isinstance(r, dict) and r.get("after"):
            bits.append(f"bullet: {r['after']}")
    return "\n".join(bits).strip()


def resume_embedding_text(resume: dict) -> str:
    parsed = resume.get("parsed_json") or {}
    bits: list[str] = []
    skills = parsed.get("skills") or []
    if skills:
        bits.append("skills: " + ", ".join(str(s) for s in skills))
    for exp in parsed.get("experience") or []:
        if isinstance(exp, dict):
            if exp.get("title"):
                bits.append(str(exp["title"]))
            for b in exp.get("bullets") or []:
                bits.append(str(b))
    text = "\n".join(bits).strip()
    return text or (resume.get("raw_text") or "").strip()


# kind -> text builder, for the generic embed-backfill loop.
TEXT_BUILDERS = {
    "jobs": job_embedding_text,
    "analyses": analysis_embedding_text,
    "suggestions": suggestion_embedding_text,
    "resumes": resume_embedding_text,
}
EMBED_KINDS = tuple(TEXT_BUILDERS)
