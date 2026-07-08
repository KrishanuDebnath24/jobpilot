"""Embedding orchestration (Phase 7, 7a) — runs in the Celery worker.

Backfill + embed-on-write are the same loop: pull the user's rows that have no
embedding yet (via db-mcp ``list_unembedded``), compute vectors locally with the
configured :class:`~jobpilot_core.embeddings.Embedder` (sentence-transformers by
default), and write them back through db-mcp ``set_embeddings``. The heavy model lives
here in the worker/backend image, never in the MCP services — db-mcp only stores and
searches float vectors.

Embedding is **asynchronous to the write path**: Scout/Analyze/Enhance/resume-upload
just enqueue ``embed.run`` after they persist, so a new row gets embedded shortly after
without blocking the write. Cost/latency is logged in ``jobpilot_core.embeddings``.
"""

from __future__ import annotations

import logging
import uuid

from jobpilot_core.embeddings import EMBED_KINDS, embed_texts
from jobpilot_core.settings import get_settings

from . import mcp_client

logger = logging.getLogger("jobpilot.embed")

_MAX_BATCHES_PER_KIND = 40  # safety cap: batch * this bounds rows embedded per kind/run


def enqueue_embed(user_id: uuid.UUID, kinds: list[str] | None = None) -> None:
    """Fire-and-forget: queue an ``embed.run`` for this user on the data-plane queue
    (embed-on-write). Best-effort — a broker hiccup must never fail the write that
    triggered it (the row stays NULL and is picked up by the next embed run/backfill)."""
    try:
        from .celery_app import celery

        celery.send_task("embed.run", args=[str(user_id), kinds],
                         queue=get_settings().data_queue)
    except Exception as exc:  # noqa: BLE001
        logger.warning("embed.enqueue failed: %s", exc)


async def _embed_kind(user_id: uuid.UUID, kind: str, settings) -> int:
    """Embed every unembedded row of one kind (in batches). Returns rows embedded."""
    batch = max(1, settings.embedding_batch_size)
    embedded = 0
    for _ in range(_MAX_BATCHES_PER_KIND):
        items = (await mcp_client.db_mcp(
            user_id, "list_unembedded", {"kind": kind, "limit": batch}))["items"]
        if not items:
            break
        vectors = embed_texts(settings, [it["text"] for it in items])
        payload = [{"id": it["id"], "embedding": vec} for it, vec in zip(items, vectors)]
        res = await mcp_client.db_mcp(user_id, "set_embeddings", {"kind": kind, "items": payload})
        embedded += int(res.get("written", 0))
        if len(items) < batch:
            break
    return embedded


async def run_embed(user_id: uuid.UUID, kinds: list[str] | None = None) -> dict:
    """Backfill/refresh embeddings for the given kinds (default: all). Idempotent — only
    NULL-embedding rows are touched, so re-running is a cheap no-op once caught up."""
    settings = get_settings()
    targets = [k for k in (kinds or EMBED_KINDS) if k in EMBED_KINDS]
    embedded: dict[str, int] = {}
    for kind in targets:
        try:
            embedded[kind] = await _embed_kind(user_id, kind, settings)
        except Exception as exc:  # noqa: BLE001 - one kind failing must not sink the rest
            logger.warning("embed.kind %s failed: %s", kind, exc)
            embedded[kind] = 0
    coverage = (await mcp_client.db_mcp(user_id, "embedding_coverage", {}))["coverage"]
    summary = {"embedded": embedded, "coverage": coverage}
    logger.info("embed.summary embedded=%s", embedded)
    return summary
