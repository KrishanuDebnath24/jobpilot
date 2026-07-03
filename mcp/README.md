# MCP Servers

Standalone [Model Context Protocol](https://modelcontextprotocol.io) services — the
**only** data path for agents and the app. Each runs as a streamable-HTTP service and
derives the caller's `user_id` from a signed request-context token (validated by an
ASGI middleware); any `user_id` passed as a tool argument is **ignored**. See
`DECISIONS.md` → Phase 1.

Implemented (Phase 1):

- **`db_mcp/`** — scoped CRUD: `get_jobs`, `get_job`, `upsert_jobs`, `search_jobs`
  (keyword fallback until embeddings land), `get_resume`, `get_qualifications`,
  `get_analyses`, `get_suggestions`, `update_suggestion_status`, and
  `delete_user_data` (GDPR hard-delete: rows cascade + storage files).
- **`resume_mcp/`** — `parse_resume` (deterministic PDF/DOCX → typed shape, stored with
  consent) and `get_structured_resume`.

Planned later: `config-mcp`, `pipeline-mcp`, `sources-mcp` (Phase 2+).

Run locally from **this directory** (so `import mcp` resolves to the SDK, not this dir):

```bash
cd mcp && uvicorn db_mcp.server:app --port 8001
cd mcp && uvicorn resume_mcp.server:app --port 8002
```
