# MCP Servers

Standalone [Model Context Protocol](https://modelcontextprotocol.io) servers — the
**only** data path for agents. Every tool takes an authenticated `user_id` and scopes
all reads/writes to it (the security boundary; see the build plan §6).

Planned servers (built from Phase 1 onward):

- `db-mcp` — scoped CRUD + semantic `search_jobs`
- `config-mcp` — search & qualifier config
- `pipeline-mcp` — pipeline status / triggers / gates
- `sources-mcp` — connector hub (Adzuna, ATS endpoints, polite crawlers)
- `resume-mcp` — resume parse + structured resume + embedding

_Placeholder in Phase 0 — no servers implemented yet._
