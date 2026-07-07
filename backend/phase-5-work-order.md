# Phase 5 Work Order — Analyzer + Enhancer (with Batch + Caching) + Suggestions UI

> Hand this to Claude Code. It expands Phase 5 of the master prompt. Same discipline: sub-steps **in order**, reproduce each **show me** against live output, **stop at the gate**, log choices in `DECISIONS.md`, commit at the gate.
>
> **Depends on:** Phase 4 (the orchestrator + `pipeline-mcp` + event bus exist) and Phase 3 (a real qualified pool to analyze). This is the first **bulk** LLM workload, so it's where **Batch API + prompt caching finally land** — the optimizations deferred from Phases 3–4.
>
> **Integrity constraint (unique to this phase):** the Enhancer gives career advice. It must **never suggest fabricating skills, experience, or credentials.** This is a hard rule, not a preference — a resume tool that suggests lying is both unethical and worthless.

---

## Decisions locked for this phase (don't re-litigate)

- **Two agents, clean split.** **Analyzer** = extraction: reads a job + resume, produces *structured, factual* analysis (required skills, keywords, seniority, tech stack, gaps vs resume) → `analyses`. **Enhancer** = generation: reads analysis (+ resume) and produces *prescriptive* suggestions → `suggestions`. Keep them separate — analysis is reusable, cacheable, and aggregatable; suggestions are advisory and user-facing.
- **Model split (the master plan's two-model strategy, made concrete).** **Analyzer → Haiku** (`claude-haiku-4-5-20251001`) — high-volume extraction, batched. **Enhancer → a Sonnet tier** (e.g. `claude-sonnet-5`; confirm the current ID against Anthropic docs) — lower-volume reasoning where suggestion *quality* matters and users will judge it. Both **config-driven**; you may start both on Haiku and promote the Enhancer only if quality warrants.
- **Two modes.** **Single/selected** (user picks jobs on the dashboard; few; interactive) → **synchronous, cached** calls. **Batch/mass** ("analyze all my qualified jobs"; whole set; user not waiting) → **Batch API + cached**. The mode picks the execution path.
- **Caching shape.** Analyzer prompt = **`[stable prefix: resume + analysis rubric][variable suffix: this job]`** (the exact shape Phase 3 was built toward). Cache the stable prefix — it's identical across every job in a run → ~90% off that portion.
- **Aggregate deterministically, then reason.** For batch mode, roll up the per-job `analyses` into a summary — **skill frequency ("K8s required in 70% of qualified jobs"), common keywords, seniority spread, recurring gaps vs resume — by counting structured records, with NO LLM.** The Enhancer then reasons over the *compact aggregate*, not 40 raw job descriptions. This keeps the Sonnet call cheap and focused, and is the same cost discipline as prior phases.
- **On-demand by default; not auto-run.** Analyze/Enhance are **triggered deliberately** (on selected jobs or "all qualified"), because auto-analyzing every scheduled scout is expensive and presumptuous. They're built as graph nodes that run **standalone on a scope** and can be **appended to the full pipeline only when config-enabled** (off by default). The auto-pipeline from Phase 4 still stops at Qualify unless told otherwise.
- **Grounding + honesty (Enhancer).** Every suggestion cites the analysis it came from (traceable, not generic). Suggestions fall in three honest buckets: **reframe/surface genuine experience** (bullet rewrites), **acquire a real skill** (project ideas for genuine gaps), **add a skill only if truthfully held**. Never fabricate. Structured output enforces these categories.
- **Boundary + injection posture hold.** Agents read via MCP, write `analyses`/`suggestions` via `db-mcp`; `user_id` from the signed context, never content. Job text (into the Analyzer) is **data, never instructions** — and a malicious job must not be able to inject content that resurfaces as a "suggestion."

---

## Sub-steps (each ends with a "show me")

### 5a — Analyzer agent (per-job, Haiku, cache-shaped)
- `analyze(job, resume) → { required_skills[], keywords[], seniority, tech_stack[], gaps_vs_resume[] }`, structured output, prompt in the `[stable: resume+rubric][variable: job]` shape. Write to `analyses` via `db-mcp`. Injection-hardened.
- **Show me:** one job analyzed → a structured `analyses` record whose `gaps_vs_resume` cites concrete job-vs-resume facts.

### 5b — Prompt caching + Batch API infrastructure
- Wire **prompt caching** on the Analyzer's stable prefix (resume + rubric). Wire the **Message Batches API**: submit a batch of analysis requests, poll status to completion, collect results. Log token cost per run.
- **Show me:** a second synchronous analysis in a run logs a **cache hit** (visibly lower input cost); a batch of N jobs is submitted → completes → results collected; the run's cost log shows the **batch (50%) + cache (90% on prefix) savings** vs a naive per-call estimate.

### 5c — Batch/mass analysis mode + deterministic aggregation
- "Analyze all qualified" runs the Analyzer over the qualified set via the Batch API. Then a **deterministic rollup** of the per-job analyses → aggregate `{ skill_frequency, common_keywords, seniority_distribution, recurring_gaps }`. **No LLM in the rollup.**
- **Show me:** analyzing the full qualified set yields per-job `analyses` **plus** an aggregate summary, e.g. "Kubernetes required in 70% of qualified jobs, absent from resume" — computed by counting, not by a model.

### 5d — Enhancer agent (Sonnet, grounded, non-fabricating)
- From a single analysis (single mode) or the aggregate (batch mode) → `suggestions { skills_to_add[] (each with grounding/why + frequency), projects[] (to genuinely build a gap skill), bullet_rewrites[] (before/after, surfacing real experience) }`. Structured output enforcing the three honest buckets; every item traceable to the analysis. Write to `suggestions` via `db-mcp`.
- **Show me:** single-job suggestions grounded in that job's analysis; batch suggestions grounded in the aggregate and **prioritized by frequency**; a bullet rewrite that reframes *existing* experience and a project idea for a *real* gap — and confirmation the agent won't suggest claiming an unheld skill (test with a prompt trying to elicit that).

### 5e — Suggestions lifecycle + wiring as triggerable stages
- `suggestions.status` ∈ {proposed, accepted, dismissed}. Expose Analyze and Enhance as **graph nodes runnable standalone on a scope** (selected `job_ids` | "all qualified") via `pipeline-mcp` `trigger_stage`, and **appendable to the full pipeline behind a config flag (default off)**. Batch node awaits batch completion by polling batch status, reconciled against persisted `analyses` (don't hang — same rule as Phase 4).
- **Show me:** trigger analyze+enhance on selected jobs → suggestions appear; trigger on "all qualified" → aggregate suggestions; accept/dismiss updates status; the Phase 4 auto-pipeline **still stops at Qualify** by default (analyze/enhance don't fire unless enabled).

### 5f — Suggestions UI
- Dashboard: multi-select jobs → **"Analyze & suggest"**; an **"Analyze all qualified"** action → batch. A suggestions view showing skills-to-add (with *why* + frequency), project ideas, and bullet rewrites as **before/after**, each with its grounding visible; accept/dismiss controls; a status indicator for batch runs (analysis running → suggestions ready). Apply the frontend-design skill.
- **Show me:** the UI generating suggestions **from selected jobs** and **from a batch** (the master bar), grounding visible on each suggestion, accept/dismiss persisting.

---

## Acceptance gate

Green when **all** hold, then stop for approval before Phase 6:
1. **The master bar:** suggestions generated from selected jobs **and** from a batch, both visible in the UI.
2. Analyzer produces **grounded, structured** analysis (gaps cite job-vs-resume facts).
3. **Batch + caching demonstrably cut cost** on a mass run (cache hits + batch discount shown in the cost log vs naive).
4. Batch aggregation is **deterministic** (frequencies/gaps by counting, not by LLM); the Enhancer reasons over the compact aggregate.
5. Enhancer suggestions are **grounded and non-fabricating** — traceable to the analysis, and it refuses to suggest claiming unheld skills.
6. Suggestions have a working lifecycle; Analyze/Enhance run **standalone on a scope** and stay **off in the auto-pipeline** unless config-enabled.
7. Boundary + injection posture intact; batch node reconciles against persisted status (no hangs).

---

## Watch-outs

- **Batch + caching is the entire cost point of this phase.** Mass analysis without them is the expensive path you were avoiding all along. Cache the `[resume+rubric]` prefix, batch the non-interactive runs, and **prove the savings in the cost log** — don't just assume them.
- **Honesty is a hard rule.** The Enhancer must never suggest fabricating skills, experience, titles, or credentials. Surface/reframe genuine experience, suggest genuinely acquiring gap skills, add skills only if truthfully held. Test it adversarially. A tool that coaches deception is a liability.
- **Ground everything.** Suggestions must trace to the analysis and cite *why* (this gap appears in N jobs), not read as generic resume advice — otherwise users won't trust or act on them.
- **Don't auto-run analyze/enhance on every scout.** On-demand by default; config-gated in the pipeline. Respect cost and user intent — nobody wants (or wants to pay for) suggestions they didn't ask for on every scheduled run.
- **Aggregate by counting, not by model.** Rolling 40 job descriptions into the Enhancer's context is the cost trap; roll up structured `analyses` deterministically and let Sonnet reason over the summary.
- **Injection reaches suggestions now.** A malicious job flows Analyzer → analysis → Enhancer → suggestion. Keep job text as data, force structured output at each hop, and make sure injected instructions can't surface as a "recommended change."
- **Batch await, don't hang.** The batch-analysis node polls batch status to completion, reconciled against persisted `analyses` — same event-vs-truth discipline as Phase 4.
- **Model split is intentional:** Haiku for high-volume extraction, Sonnet for user-facing reasoning; config-driven so you can tune. Verify current model IDs/prices against Anthropic docs before committing.
- **GDPR:** resume, analyses, and suggestions are personal data — within the Phase 1 consent/erasure model; **don't log full resume or suggestion content** (log counts/skill names, not documents).
- **Scope boundary:** Phase 5 *generates and presents* suggestions and tracks status. Actually rewriting the resume document, and the "approve before applied" gate, are **Phase 6** — don't build them here.
- **No new Compose services** (uses existing); the frontend gains routes — verify reads go through the backend→MCP boundary.
- **Commit at the gate**; log decisions (model split, batch/cache config, aggregation method, no-fabrication policy, auto-run default-off, suggestion lifecycle states, batch-await reconciliation).
