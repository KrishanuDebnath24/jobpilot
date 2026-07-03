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

from .settings import Settings

logger = logging.getLogger("jobpilot.llm")

# USD per 1M tokens. Keep in sync with claude.com/pricing; used only to log an
# *estimated* cost for visibility — billing is authoritative.
_PRICING: dict[str, tuple[float, float]] = {
    # model_id: (input_per_mtok, output_per_mtok)
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_DEFAULT_PRICE = (1.0, 5.0)  # assume the cheap tier if a model is unlisted


@dataclass
class LLMUsage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    @property
    def cost_usd(self) -> float:
        pin, pout = _PRICING.get(self.model, _DEFAULT_PRICE)
        return self.input_tokens / 1e6 * pin + self.output_tokens / 1e6 * pout

    def add(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.calls += 1

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "calls": self.calls,
            "estimated_cost_usd": round(self.cost_usd, 6),
        }


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


def _log(model: str, usage_obj) -> LLMUsage:
    u = LLMUsage(model=model)
    u.add(getattr(usage_obj, "input_tokens", 0) or 0, getattr(usage_obj, "output_tokens", 0) or 0)
    logger.info(
        "llm.call model=%s input_tokens=%d output_tokens=%d est_cost_usd=%.6f",
        u.model, u.input_tokens, u.output_tokens, u.cost_usd,
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
    resp = await client.messages.create(
        model=mdl,
        max_tokens=min(max_tokens or 256, settings.llm_max_output_tokens),
        system=system or "You are a terse assistant. Answer in as few words as possible.",
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return LLMResult(data=text, text=text, usage=_log(mdl, resp.usage), stop_reason=resp.stop_reason)


async def extract_json(
    *,
    system: str,
    user_text: str,
    tool_name: str,
    tool_description: str,
    input_schema: dict,
    settings: Settings,
    model: str | None = None,
    max_tokens: int | None = None,
) -> LLMResult:
    """Force a single tool call with ``input_schema`` and return its validated
    arguments as ``data``. Extract-only: the model must respond via the tool, so
    untrusted page text cannot steer generation or tool selection.
    """
    client = get_client(settings)
    mdl = _model(settings, model)
    resp = await client.messages.create(
        model=mdl,
        max_tokens=min(max_tokens or settings.llm_max_output_tokens, settings.llm_max_output_tokens),
        system=system,
        messages=[{"role": "user", "content": user_text}],
        tools=[{"name": tool_name, "description": tool_description, "input_schema": input_schema}],
        tool_choice={"type": "tool", "name": tool_name},
    )
    usage = _log(mdl, resp.usage)
    data: Any = None
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            data = block.input
            break
    return LLMResult(data=data, usage=usage, stop_reason=resp.stop_reason)
