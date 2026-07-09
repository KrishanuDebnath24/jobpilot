"""Thin Anthropic client wrapper — the single choke point for LLM calls.

Every call is routed through here so that (a) the model is read from config, not
hard-coded, and (b) token usage + an estimated cost are logged for cost
visibility. Phase 2's only LLM work is *extraction* (unstructured HTML -> typed
fields), so :func:`extract_json` forces a tool call with a strict input schema —
extract-only, structured output, no free-form generation. Job/board text is
treated strictly as data; it never selects tools or changes control flow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from anthropic import AsyncAnthropic

from . import tracing
from .settings import Settings

logger = logging.getLogger("jobpilot.llm")

# USD per 1M tokens. Keep in sync with claude.com/pricing; used only to log an
# *estimated* cost for visibility — billing is authoritative.
_PRICING: dict[str, tuple[float, float]] = {
    # model_id: (input_per_mtok, output_per_mtok)
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # Phase 5 Enhancer tier (intro pricing; keep in sync with claude.com/pricing).
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
}
_DEFAULT_PRICE = (1.0, 5.0)  # assume the cheap tier if a model is unlisted

# Anthropic cache/batch cost multipliers (relative to base input price).
_CACHE_READ_MULT = 0.1    # cached-prefix reads cost ~0.1x base input
_CACHE_WRITE_MULT = 1.25  # first write of a 5-minute cache entry costs ~1.25x
_BATCH_MULT = 0.5         # Batch API is 50% of standard price


@dataclass
class LLMUsage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    # Phase 5: cache-prefix accounting (resume+rubric prefix), separate from full-price
    # input so the cost log can show the ~90%-off savings on the cached portion.
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    calls: int = 0
    batch: bool = False  # this usage was a Batch API run (50% price)

    @property
    def cost_usd(self) -> float:
        pin, pout = _PRICING.get(self.model, _DEFAULT_PRICE)
        cost = (
            self.input_tokens / 1e6 * pin
            + self.cache_read_input_tokens / 1e6 * pin * _CACHE_READ_MULT
            + self.cache_creation_input_tokens / 1e6 * pin * _CACHE_WRITE_MULT
            + self.output_tokens / 1e6 * pout
        )
        return cost * (_BATCH_MULT if self.batch else 1.0)

    @property
    def naive_cost_usd(self) -> float:
        """What the same work would cost with NO cache and NO batch — every input
        token at full price. Lets the run log quantify the batch+cache savings."""
        pin, pout = _PRICING.get(self.model, _DEFAULT_PRICE)
        total_in = self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens
        return total_in / 1e6 * pin + self.output_tokens / 1e6 * pout

    def add(
        self,
        input_tokens: int,
        output_tokens: int,
        *,
        cache_read: int = 0,
        cache_creation: int = 0,
    ) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_input_tokens += cache_read
        self.cache_creation_input_tokens += cache_creation
        self.calls += 1

    def as_dict(self) -> dict:
        d = {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "calls": self.calls,
            "batch": self.batch,
            "estimated_cost_usd": round(self.cost_usd, 6),
        }
        naive = self.naive_cost_usd
        d["naive_cost_usd"] = round(naive, 6)
        d["savings_usd"] = round(max(0.0, naive - self.cost_usd), 6)
        d["savings_pct"] = round(100 * (1 - self.cost_usd / naive), 1) if naive > 0 else 0.0
        return d


@dataclass
class LLMResult:
    data: Any
    usage: LLMUsage
    text: str | None = None
    stop_reason: str | None = None
    _fields: dict = field(default_factory=dict)


def _model(settings: Settings, override: str | None) -> str:
    return override or settings.scout_extractor_model


def get_client(settings: Settings) -> AsyncAnthropic:
    """Construct a client, failing fast if the key is missing/placeholder."""
    return AsyncAnthropic(api_key=settings.require_anthropic_key())


def _log(model: str, usage_obj, *, batch: bool = False) -> LLMUsage:
    u = LLMUsage(model=model, batch=batch)
    u.add(
        getattr(usage_obj, "input_tokens", 0) or 0,
        getattr(usage_obj, "output_tokens", 0) or 0,
        cache_read=getattr(usage_obj, "cache_read_input_tokens", 0) or 0,
        cache_creation=getattr(usage_obj, "cache_creation_input_tokens", 0) or 0,
    )
    logger.info(
        "llm.call model=%s input=%d cache_read=%d cache_write=%d output=%d batch=%s est_cost_usd=%.6f",
        u.model, u.input_tokens, u.cache_read_input_tokens, u.cache_creation_input_tokens,
        u.output_tokens, batch, u.cost_usd,
    )
    return u


async def complete_text(
    prompt: str,
    settings: Settings,
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    system: str | None = None,
) -> LLMResult:
    """Minimal text round-trip (used by the 2a health check)."""
    client = get_client(settings)
    mdl = _model(settings, model)
    with tracing.span("llm.call", model=mdl, kind="text"):
        resp = await client.messages.create(
            model=mdl,
            max_tokens=min(max_tokens or 256, settings.llm_max_output_tokens),
            system=system or "You are a terse assistant. Answer in as few words as possible.",
            messages=[{"role": "user", "content": prompt}],
        )
        usage = _log(mdl, resp.usage)
        tracing.annotate_current_llm(usage)
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return LLMResult(data=text, text=text, usage=usage, stop_reason=resp.stop_reason)


def build_forced_tool_params(
    *,
    system: str,
    cached_prefix: str | None,
    variable_text: str,
    tool_name: str,
    tool_description: str,
    input_schema: dict,
    model: str,
    max_tokens: int,
    temperature: float | None = None,
) -> dict:
    """Assemble the Messages-API params for a forced single-tool-call.

    When ``cached_prefix`` is given, the user turn is split ``[stable prefix (cached)]
    [variable]`` and a ``cache_control`` breakpoint is placed on the prefix — identical
    across every job in a run, so it reads from cache at ~0.1x after the first write
    (Phase 5 caching). The same shape is reused for both sync and Batch API requests.
    """
    if cached_prefix is not None:
        content = [
            {"type": "text", "text": cached_prefix, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": variable_text},
        ]
    else:
        content = variable_text
    params: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "tools": [{"name": tool_name, "description": tool_description, "input_schema": input_schema}],
        "tool_choice": {"type": "tool", "name": tool_name},
    }
    if temperature is not None:
        params["temperature"] = temperature
    return params


def _tool_input(content_blocks, tool_name: str) -> Any:
    for block in content_blocks:
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            return block.input
    return None


async def extract_json(
    *,
    system: str,
    user_text: str | None = None,
    tool_name: str,
    tool_description: str,
    input_schema: dict,
    settings: Settings,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    cached_prefix: str | None = None,
    variable_text: str | None = None,
) -> LLMResult:
    """Force a single tool call with ``input_schema`` and return its validated
    arguments as ``data``. Extract-only: the model must respond via the tool, so
    untrusted text cannot steer generation or tool selection. ``temperature=0`` gives
    reproducible scoring. Pass ``cached_prefix`` + ``variable_text`` (instead of
    ``user_text``) to cache a stable prompt prefix across a run (Phase 5).
    """
    client = get_client(settings)
    mdl = _model(settings, model)
    params = build_forced_tool_params(
        system=system,
        cached_prefix=cached_prefix,
        variable_text=variable_text if variable_text is not None else (user_text or ""),
        tool_name=tool_name,
        tool_description=tool_description,
        input_schema=input_schema,
        model=mdl,
        # Default to the extraction cap; callers that need more (the Enhancer's
        # multi-bucket output) pass an explicit, larger max_tokens.
        max_tokens=max_tokens or settings.llm_max_output_tokens,
        temperature=temperature,
    )
    with tracing.span("llm.call", model=mdl, kind="tool", tool=tool_name):
        resp = await client.messages.create(**params)
        usage = _log(mdl, resp.usage)
        tracing.annotate_current_llm(usage)
    return LLMResult(data=_tool_input(resp.content, tool_name), usage=usage,
                     stop_reason=resp.stop_reason)


# --------------------------------------------------------------------------- #
# Message Batches API (Phase 5, 5b): submit N forced-tool-call requests, poll to
# completion, collect results keyed by custom_id. 50% of standard price; keyed
# results (arrive in any order — never trust position).
# --------------------------------------------------------------------------- #
async def submit_batch(settings: Settings, requests: list[dict]) -> str:
    """Create a batch from ``[{"custom_id": str, "params": {...}}]`` and return its id.
    ``params`` is a full Messages-API param dict (from ``build_forced_tool_params``)."""
    client = get_client(settings)
    with tracing.span("llm.batch.submit", count=len(requests)):
        batch = await client.messages.batches.create(requests=requests)
    logger.info("llm.batch.submitted id=%s count=%d", batch.id, len(requests))
    return batch.id


async def get_batch_status(settings: Settings, batch_id: str) -> str:
    client = get_client(settings)
    batch = await client.messages.batches.retrieve(batch_id)
    return batch.processing_status  # "in_progress" | "canceling" | "ended"


async def collect_batch_results(
    settings: Settings, batch_id: str, tool_name: str, model: str
) -> tuple[dict[str, Any], LLMUsage]:
    """Stream a *finished* batch's results, returning ``{custom_id: tool_input}`` for
    succeeded requests plus aggregate usage (batch-priced). Failed/expired requests
    are skipped (the caller leaves those jobs un-analyzed, re-attempted next run)."""
    client = get_client(settings)
    out: dict[str, Any] = {}
    usage = LLMUsage(model=model, batch=True)
    with tracing.span("llm.batch.collect", model=model, batch=True) as sp:
        async for result in await client.messages.batches.results(batch_id):
            rtype = result.result.type
            if rtype != "succeeded":
                logger.warning("llm.batch.result custom_id=%s type=%s (skipped)", result.custom_id, rtype)
                continue
            msg = result.result.message
            u = msg.usage
            usage.add(
                getattr(u, "input_tokens", 0) or 0,
                getattr(u, "output_tokens", 0) or 0,
                cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
                cache_creation=getattr(u, "cache_creation_input_tokens", 0) or 0,
            )
            data = _tool_input(msg.content, tool_name)
            if data is not None:
                out[result.custom_id] = data
        sp.record_llm(usage).set(succeeded=len(out))
    logger.info("llm.batch.collected id=%s succeeded=%d est_cost_usd=%.6f",
                batch_id, len(out), usage.cost_usd)
    return out, usage
