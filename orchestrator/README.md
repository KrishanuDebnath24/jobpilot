# Orchestrator

A **LangGraph** state machine (not an LLM agent) that advances jobs through pipeline
stages by status and pauses at configured human-in-the-loop gates. It subscribes to the
Redis pub/sub event bus and drives: `scout → qualify → analyze → enhance`, writing a
`pipeline_runs` record and emitting an `agent_events` entry per stage.

_Placeholder in Phase 0 — wired up in Phase 4._
