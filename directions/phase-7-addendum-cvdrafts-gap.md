# Phase 7 Addendum — Close the `cv_drafts` Gate Gap (from the audit)

> Hand this to Claude Code. It closes the one material gate miss the Phase 7 audit found: **gate criterion #2's tailored-CV before/after score-delta coverage is unimplemented**, because Phase 7 (`402e1a0`) was committed *before* Phase 6.5 (`87793d0`) introduced `cv_drafts`. The audit already recorded the fact in memory (`phase7-audit-cvdrafts-gap.md`). This is a **small, read-only** fix — `db-mcp` already exposes the reads. Same discipline: steps **in order**, reproduce each **show me**, commit at the end, update `DECISIONS.md`.
>
> **The whole point:** a Query agent that answers everything *except* "which of my tailored CVs improved my score?" is missing the exact question Phase 6.5 was built to make askable. Closing this makes Phase 7 actually meet its gate.

---

## Decisions locked for this addendum (don't re-litigate)

- **CV-score questions are a SORT over exact numbers — never RAG.** "Which CV improved the most?" is `ORDER BY (after_score − before_score) DESC` over `cv_drafts` — the cardinal rule of Phase 7. It goes to a **structured** tool, not semantic search.
- **Read-only, no new write path, no new scoping surface.** Reuse the existing `db-mcp` reads (`get_cv_drafts` / `get_cv_draft` at `mcp/db_mcp/server.py:290`, which already return before/after scores). The new Query tool wraps those and inherits the same `user_id`-from-session enforcement. **If you find yourself adding a write tool, stop** — that breaks the read-only security invariant.
- **The router must classify fuzzy-sounding CV questions as structured.** "improved," "best," "worst," "did tailoring help" *sound* semantic but are exact sorts. The routing prompt must steer them to the `cv_drafts` tool, not to similarity search. Test this explicitly — it's the most likely failure mode.
- **Deterministic + live tests, matching the audit's rigor.** A deterministic test that the delta sort + scoping are correct against seeded rows, and a live test that a real question routes to the exact tool and returns DB-correct ordering.
- **Keep the gate-closing commit tight.** This commit = the `cv_drafts` fix + the doc gaps that are *actively misleading a fresh session*. **The 7e streaming UI is NOT in this commit** — implement it separately or formally defer it (see Step 5).

---

## Steps (each ends with a "show me")

### 1 — `list_cv_drafts` read tool (delta-sorted) in the Query agent
- Add a read-only tool to `TOOLS` in `packages/core/jobpilot_core/query_agent.py` that wraps `db-mcp`'s `get_cv_drafts`. Return each draft with `before_score`, `after_score`, `delta = after − before`, `status`, and its job reference; support **sort by delta (desc/asc)** and **filter by status** (draft_ready/approved/rejected). No write capability.
- **Show me:** the tool returns the user's `cv_drafts` sorted by score delta, each with before/after/delta and status, **user-scoped** (a second user sees none of the first user's drafts); grep confirms no write path was introduced.

### 2 — Route + routing prompt line
- Add a `_ROUTES` entry in `backend/app/query_runner.py` for the new tool, and a **routing prompt line** instructing the router to send CV-score / improvement / delta questions ("which CV improved most?", "did my tailoring help?", "which got worse?", "how many CVs are waiting for review?") to the **structured** `cv_drafts` tool — explicitly **not** semantic search.
- **Show me:** "which of my CVs improved the most?" routes to the structured tool (not RAG) and returns DB-correct ordering; a deliberately fuzzy variant ("did tailoring help any of my CVs?") **also** routes structured, not semantic.

### 3 — Tests (deterministic + live)
- **Deterministic** (`tests/test_phase7.py`): seed `cv_drafts` with known before/after scores; assert the delta sort is correct; assert user-scoping (user B can't see user A's drafts).
- **Live** (`tests/test_phase7_live.py`): a real "which improved most?" question routes to the exact tool and returns an ordering that **matches a direct `cv_drafts` query**.
- **Show me:** both suites green *including* the new cv_drafts cases (keep the existing 16/16 + 5/5 passing — run the full suites, not just the new tests); the live delta-ordering equals a direct DB query.

### 4 — Doc corrections (same commit)
- **`.env.example`:** add `EMBEDDING_BACKEND`, `EMBEDDING_MODEL`, `QUERY_MODEL` and any other `EMBEDDING_`/`QUERY_` tunables, with the **local `sentence-transformers` default reflected** (matching the documented deviation). Fix the stale header ("This phase makes NO LLM calls" — it does now) and the `EMBEDDING_DIM` comment ("population deferred to Phase 3" — populated in Phase 7).
- **`DECISIONS.md` Phase 7 section:** add the `cv_drafts` delta coverage, note the build-order fact (Phase 7 predates 6.5) and that the gap is now closed.
- **Show me:** `.env.example` matches reality (local embedder, LLM-calls corrected, tunables present, no stale comments); `DECISIONS.md` Phase 7 reflects cv_drafts coverage + the build-order note.

### 5 — Streaming UI (7e): decide, don't bundle
- The work order's 7e says "stream answers"; `Ask.tsx` currently returns a single `POST /query/ask` after a spinner. **Do not put this in the gate-closing commit.** Either (a) implement token-by-token streaming for `Ask.tsx` as a **separate** follow-up, or (b) log it as an **accepted deviation** in `DECISIONS.md` (as was done for the local embedder), with rationale.
- **Show me:** either `Ask.tsx` streams token-by-token (separate commit), **or** `DECISIONS.md` records the non-streaming UI as an accepted deviation.

### 6 — Close the memory note
- Update `phase7-audit-cvdrafts-gap.md` (and `MEMORY.md` if it links) to reflect that the gap is **resolved**, so the next session sees a closed item, not an open one.
- **Show me:** the memory note reads as resolved (with the fixing commit referenced), not as an outstanding gap.

---

## Acceptance gate

Green when **all** hold:
1. **Gate criterion #2 is now met:** "which of my CVs improved the most?" (and status-count variants) are answered from real `cv_drafts` before/after deltas, via a **structured sort verifiably matching a direct DB query — never RAG**.
2. **Read-only invariant intact:** no new write path; the new tool inherits `user_id`-from-session scoping; injection posture unchanged.
3. Deterministic **and** live tests cover `cv_drafts`; the full suites are green (existing 16/16 + 5/5 preserved).
4. `.env.example` and `DECISIONS.md` corrected (embedder, LLM-calls, tunables, cv_drafts coverage, build-order note).
5. The 7e streaming UI is either implemented **separately** or formally deferred — **not** in the gate-closing commit.
6. The memory note is closed.

---

## Watch-outs

- **The fuzzy-word trap is the real risk.** "improved / best / worst / help" sound semantic but are exact sorts. If the router sends these to similarity search, the answers will be plausible and wrong — the defining failure of a data-Q&A agent. Test it explicitly.
- **No write path — reuse the existing reads.** Wrapping `get_cv_drafts` keeps the read-only security win. Adding a write tool "for convenience" is the line you don't cross; state in the commit that none was added.
- **Prove the scoping, don't assume it.** The new tool inherits `db-mcp`'s `user_id` enforcement, but the deterministic test must actually show user B can't read user A's drafts.
- **Keep the commit tight.** cv_drafts fix + actively-misleading doc gaps only; streaming UI separate. A focused commit is easier to review and revert.
- **Don't regress the green suites.** Run the full deterministic + live suites, not just the new cases.
- **Close the memory note** so the next session doesn't re-discover a fixed problem as if it were open.
- **Verification target for "done":** ask "which of my CVs improved the most?" live and confirm the ordering equals a direct `cv_drafts` query — that's the exact-not-RAG proof this phase lives or dies on.
