"""Backend -> MCP client over streamable HTTP.

Every call mints a short-lived signed context token carrying the authenticated
user_id and sends it as a Bearer header. The MCP service derives user_id from that
token alone. This is the path that proves "the app touches job/resume data only
through MCP", not via direct ORM.
"""

from __future__ import annotations

import uuid
from typing import Any

from jobpilot_core.security import mint_mcp_context_token
from jobpilot_core.settings import get_settings
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def _extract(result: Any) -> Any:
    if getattr(result, "isError", False):
        text = result.content[0].text if result.content else "MCP tool error"
        raise RuntimeError(f"MCP tool error: {text}")
    sc = getattr(result, "structuredContent", None)
    if sc is not None:
        if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
            return sc["result"]
        return sc
    if result.content:
        import json

        return json.loads(result.content[0].text)
    return None


async def call_tool(
    service_url: str,
    user_id: uuid.UUID,
    tool: str,
    arguments: dict | None = None,
) -> Any:
    settings = get_settings()
    token = mint_mcp_context_token(user_id, settings)
    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(service_url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments or {})
            return _extract(result)


async def db_mcp(user_id: uuid.UUID, tool: str, arguments: dict | None = None) -> Any:
    return await call_tool(get_settings().db_mcp_url, user_id, tool, arguments)


async def resume_mcp(user_id: uuid.UUID, tool: str, arguments: dict | None = None) -> Any:
    return await call_tool(get_settings().resume_mcp_url, user_id, tool, arguments)
