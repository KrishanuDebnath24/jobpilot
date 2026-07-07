"""Concierge agentic loop + tool executor (Phase 6, 6b/6c/6d).

The Concierge reaches data/config/pipeline ONLY through the MCP servers, all
user-scoped. This module holds:

  * ``ConciergeExecutor`` — the security boundary. It binds ``user_id`` from the
    session (never from tool arguments), strips any ``user_id`` a tool tries to pass,
    and gates every consequential/destructive tool behind an explicit confirmation.
    Even if injected text convinced the model to call ``delete_user_data``, the
    executor blocks it without a real user yes. This is unit-tested directly.
  * ``run_turn`` — the interactive tool-use loop against a Sonnet tier, with the stable
    system prompt + tool defs cached, retrieved content wrapped as ``<data>``, and the
    conversation persisted through db-mcp. Streams tool-call progress.
"""

from __future__ import annotations

import json
import logging
import uuid

from jobpilot_core import concierge
from jobpilot_core.llm import get_client
from jobpilot_core.settings import get_settings

from . import mcp_client

logger = logging.getLogger("jobpilot.concierge")

# tool -> (service, mcp_tool, arg_mapper|None, wrap_result_as_data)
_ROUTES: dict[str, tuple[str, str, object, bool]] = {
    "get_dashboard": ("db", "get_qualified_jobs", lambda a: {"limit": int(a.get("limit") or 200)}, True),
    "get_suggestions": ("db", "get_suggestions", None, True),
    "get_search_config": ("config", "get_search_config", None, True),
    "update_search_config": ("config", "update_search_config", None, False),
    "get_qualifier_config": ("config", "get_qualifier_config", None, True),
    "update_qualifier_config": ("config", "update_qualifier_config", None, False),
    "get_gates": ("config", "get_gate_config", None, True),
    "update_gate": ("config", "update_gate_config", None, False),
    "get_pipeline_status": ("pipeline", "get_pipeline_status", None, True),
    "run_pipeline": ("pipeline", "run_full_pipeline", lambda a: {}, False),
    "trigger_stage": ("pipeline", "trigger_stage", None, False),
    "approve_gate": ("pipeline", "approve_gate", None, False),
    "skip_gate": ("pipeline", "skip_gate", None, False),
    "delete_user_data": ("db", "delete_user_data", lambda a: {}, False),
}
_LOCAL = {"prepare_resume_apply", "apply_resume"}

_SERVICES = {
    "db": mcp_client.db_mcp,
    "config": mcp_client.config_mcp,
    "pipeline": mcp_client.pipeline_mcp,
    "resume": mcp_client.resume_mcp,
}


async def _default_call(user_id: uuid.UUID, service: str, tool: str, args: dict) -> dict:
    return await _SERVICES[service](user_id, tool, args)


class ConciergeExecutor:
    """Dispatch a Concierge tool call under the trust boundary. ``user_id`` is fixed at
    construction from the session; a tool argument can never change it."""

    def __init__(self, user_id: uuid.UUID, call=None):
        self.user_id = user_id
        self._call = call or _default_call

    async def execute(self, name: str, args: dict, confirmed: bool = False) -> tuple[dict, bool]:
        """Returns ``(result, wrap_as_data)``. A consequential/destructive tool called
        without ``confirmed`` returns a ``confirmation_required`` result and performs
        NO action."""
        args = dict(args or {})
        args.pop("user_id", None)  # trust boundary: identity never comes from tool args
        if name not in concierge.TOOL_NAMES:
            return {"error": f"unknown tool {name!r}"}, False
        if concierge.authorize(name, confirmed) == "confirm":
            return {"status": "confirmation_required",
                    "prompt": concierge.confirmation_prompt(name, args)}, False

        if name == "prepare_resume_apply":
            from .apply_runner import prepare_apply
            return await prepare_apply(self.user_id), True
        if name == "apply_resume":
            from .apply_runner import apply_to_resume
            return await apply_to_resume(self.user_id, args.get("confirmed_skills")), False

        service, tool, mapper, is_data = _ROUTES[name]
        mcp_args = mapper(args) if mapper else args
        result = await self._call(self.user_id, service, tool, mcp_args)
        return (result if isinstance(result, dict) else {"result": result}), is_data


# --------------------------------------------------------------------------- #
# Turn loop
# --------------------------------------------------------------------------- #
def _last_pending(history: list[dict]) -> dict | None:
    """The pending consequential action proposed by the Concierge on the previous turn
    (recorded in the assistant message metadata), awaiting the user's yes/no."""
    for m in reversed(history):
        if m.get("role") == "assistant":
            pending = (m.get("tool_calls_json") or {}).get("pending_confirmation")
            return pending or None
    return None


def _to_anthropic_messages(history: list[dict]) -> list[dict]:
    """Persisted transcript → Messages API turns (user/assistant text only; tool blocks
    are per-turn and not replayed)."""
    out: list[dict] = []
    for m in history:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            out.append({"role": m["role"], "content": m["content"]})
    return out


async def run_turn(user_id: uuid.UUID, chat_session_id: uuid.UUID, user_text: str) -> dict:
    """Run one Concierge turn. Persists the exchange, drives the tool-use loop, and
    returns the reply plus a list of tool-call events (for streaming)."""
    settings = get_settings()
    history = (await mcp_client.db_mcp(
        user_id, "get_messages", {"chat_session_id": str(chat_session_id)}))["messages"]

    pending = _last_pending(history)
    confirmed_action = pending if (pending and concierge.is_affirmative(user_text)) else None

    await mcp_client.db_mcp(user_id, "append_message", {
        "chat_session_id": str(chat_session_id), "role": "user", "content": user_text})

    messages = _to_anthropic_messages(history) + [{"role": "user", "content": user_text}]
    executor = ConciergeExecutor(user_id)
    client = get_client(settings)
    events_out: list[dict] = []
    new_pending: dict | None = None
    resp = None

    for _ in range(settings.concierge_max_tool_iterations):
        resp = await client.messages.create(
            model=settings.concierge_model,
            max_tokens=settings.concierge_max_tokens,
            system=concierge.system_blocks(),
            tools=concierge.TOOLS,
            messages=messages,
            thinking={"type": "disabled"},  # interactive chat: low latency, no scratchpad
        )
        messages.append({"role": "assistant", "content": resp.content})
        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if resp.stop_reason != "tool_use" or not tool_uses:
            break
        tool_results = []
        for tu in tool_uses:
            confirmed = bool(confirmed_action and confirmed_action.get("tool") == tu.name)
            # On confirmation reuse the ORIGINALLY-proposed args (prevents arg drift).
            args = confirmed_action["args"] if confirmed else (tu.input or {})
            result, is_data = await executor.execute(tu.name, args, confirmed=confirmed)
            events_out.append({"tool": tu.name,
                               "status": result.get("status", "ok") if isinstance(result, dict) else "ok"})
            if isinstance(result, dict) and result.get("status") == "confirmation_required":
                new_pending = {"tool": tu.name, "args": tu.input or {}}
            content = concierge.wrap_retrieved(tu.name, json.dumps(result)) if is_data else json.dumps(result)
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": content})
        messages.append({"role": "user", "content": tool_results})
        confirmed_action = None  # consumed for this turn

    reply = "".join(b.text for b in (resp.content if resp else []) if getattr(b, "type", None) == "text")
    await mcp_client.db_mcp(user_id, "append_message", {
        "chat_session_id": str(chat_session_id), "role": "assistant", "content": reply,
        "tool_calls_json": {"pending_confirmation": new_pending} if new_pending else None})
    logger.info("concierge.turn tools=%d pending=%s", len(events_out), bool(new_pending))
    return {"reply": reply, "events": events_out, "pending": new_pending}
