# MCP Servers

Standalone [Model Context Protocol](https://modelcontextprotocol.io) services — the
**only** data path for agents and the app. Each runs as a streamable-HTTP service and
derives the caller's `user_id` from a signed request-context token (validated by an
ASGI middleware); any `user_id` passed as a tool argument is **ignored**. See
`DECISIONS.md` → Phase 1.

Implemented (Phase 1):

- **`db_mcp/`** — scoped CRUD: `get_jobs`, `get_job`, `upsert_jobs`, `search_jobs`
  (keyword fallback until embeddings land), `get_resume`, `get_qualifications`,
  `get_analyses`, `get_suggestions`, `update_suggestion_status`,
  `create_pipeline_run`/`finish_pipeline_run`/`get_pipeline_runs` (Scout run tracking),
  and `delete_user_data` (GDPR hard-delete: rows cascade + storage files).
- **`resume_mcp/`** — `parse_resume` (deterministic PDF/DOCX → typed shape, stored with
  consent) and `get_structured_resume`.

Implemented (Phase 2):

- **`config_mcp/`** — user search config: `get_search_config`, `update_search_config`
  (`roles`, `locations`, `keywords`, `sources`, `target_employers`, `schedule`). Direct
  DB, user-scoped exactly like `db-mcp`.
- **`sources_mcp/`** — the connector hub: `list_sources`, `search_adzuna` (API, NI via `gb`),
  `fetch_greenhouse` (ATS), `search_jooble` / `search_careerjet` / `search_jsearch` (RoI
  aggregators — Jooble/Careerjet free & on by default, JSearch metered & off by default), and
  `run_crawl` (one polite Irish-board crawler with constrained **Haiku** extraction — the only
  LLM path). Holds no DB state but still requires a valid signed context (protects the
  Adzuna/RapidAPI quota / Anthropic budget) and **fail-fasts at boot without a real
  `ANTHROPIC_API_KEY`**. Each connector returns the one raw job shape
  (`jobpilot_core.scout.RawJob`); Scout normalizes + dedupes (with `ats > crawl > aggregator`
  precedence) and persists via `db-mcp`.

Planned later: `pipeline-mcp` (Phase 4+).

Run locally from **this directory** (so `import mcp` resolves to the SDK, not this dir):

```bash
cd mcp && uvicorn db_mcp.server:app --port 8001
cd mcp && uvicorn resume_mcp.server:app --port 8002
cd mcp && uvicorn config_mcp.server:app --port 8003
cd mcp && uvicorn sources_mcp.server:app --port 8004   # needs ANTHROPIC_API_KEY + Adzuna creds
```
