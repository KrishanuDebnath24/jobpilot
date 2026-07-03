# Phase 2 Addendum — Aggregator Connectors (Jooble · Careerjet · JSearch)

> Hand this to Claude Code. It slots **between Phase 2 and Phase 3**. Purpose: give the Republic of Ireland real coverage (Adzuna doesn't serve RoI) by adding three aggregator connectors to the existing `sources-mcp`. Same discipline: sub-steps **in order**, reproduce each **show me** against live output, commit at the end, log choices in `DECISIONS.md`.
>
> These connectors **reuse everything from Phase 2** — the connector interface, the deterministic normalize step, the `dedupe_hash` collapse, and the `db-mcp` write path. You are adding *sources*, not a new pipeline. The existing Adzuna (NI-via-`gb`), Greenhouse (ATS), and **irishjobs.ie crawler stay exactly as they are.**

---

## Prerequisites (get these first — all have free tiers)

1. **Jooble API key** — request at `jooble.org/api/about` (fill the form; free). Env: `JOOBLE_API_KEY`.
2. **Careerjet partner account** — `careerjet.com/partners` → get your affiliate/API key (free; rate-limited, liftable on request). Env: `CAREERJET_API_KEY`. The official Python client `careerjet-api` (PyPI) saves you hand-rolling it.
3. **RapidAPI account + JSearch subscription** — subscribe to JSearch on RapidAPI (free tier, then paid). Env: `RAPIDAPI_KEY`. **Confirm the current free-tier request quota before enabling** — this is the one source that costs money at volume.

All three keys go in the backend/`sources-mcp` env only — never the frontend, never git.

---

## Decisions locked for this addendum (don't re-litigate)

- **Deterministic mapping, no LLM.** All three return structured JSON → map straight to the raw job shape. Sending this to a model would repeat the Phase 2 cost trap. LLM stays confined to the crawler's HTML path.
- **Reuse the Phase 2 tail.** Each connector conforms to the existing interface and flows through the *same* normalize → dedupe → `upsert_jobs` path. No new persistence logic.
- **Tagging:** `source_type='api'` for all three; `connector` = `jooble` | `careerjet` | `jsearch`. (Keep `source_type` values as-is; the `connector` field is where the aggregator identity lives.)
- **Dedup priority — direct beats aggregator.** When `dedupe_hash` collapses a job that exists in both a *direct* source (Greenhouse/ATS or company) and an *aggregator*, **keep the direct record** (canonical apply link, richer fields) and discard the aggregator's copy. Aggregators fill coverage gaps; they don't overwrite direct data. Add a simple source-precedence order: `ats > crawl > aggregator`.
- **Cost-gated enablement.** Jooble + Careerjet are free → **on by default** in the search config. JSearch has RapidAPI quota → **off by default**, enabled per-user via the `sources[]` toggle only when they opt in. Bound JSearch to a max pages/run.
- **Config-driven.** `config-mcp` `sources[]` decides which connectors run; extend the enum to include the three new names. Location for RoI is passed as a string/locale per connector (below), not a country-code allowlist.

---

## Sub-steps (each ends with a "show me")

### A1 — Jooble connector
- POST to the Jooble endpoint with the key in the path; body: `{ keywords, location, radius, page }`. **`location` is a free string — pass the config's RoI locations (e.g. "Dublin, Ireland")**; this is exactly what Adzuna's `ie` couldn't do.
- Map the response (`jobs[]`: `title, company, location, snippet, salary, type, link, updated, id`) → raw job shape. `connector='jooble'`.
- **Show me:** `search` via the Jooble connector returns real Dublin/RoI jobs in the common raw shape.

### A2 — Careerjet connector
- Use the official `careerjet-api` client (or GET `public.api.careerjet.net/search`). Set **`locale_code='en_IE'`** (the Ireland site) and pass `keywords`, `location`, `sort`, `pagesize`, `page`.
- It's an affiliate API: it expects `affid`, plus `user_ip`, `user_agent`, and `url`. In this backend/batch context pass the server's representative IP + a descriptive UA (document what you use). Map `jobs[]` → raw shape. `connector='careerjet'`.
- **Show me:** the Careerjet connector returns RoI jobs via `en_IE` in the common raw shape; note in logs the affiliate params sent.

### A3 — JSearch connector (cost-gated; the Indeed/LinkedIn reach)
- GET `jsearch.p.rapidapi.com/search` with headers `X-RapidAPI-Key` + `X-RapidAPI-Host`. Params: `query` (e.g. "software engineer in Dublin, Ireland"), **`country='ie'`**, `page`, `num_pages` (bounded), `date_posted`.
- This pulls **Google for Jobs**, which aggregates Indeed, LinkedIn, Glassdoor, etc. — the compliant path to sources you can't hit directly. Map `data[]` (`job_title, employer_name, job_city/country, job_description, job_apply_link, job_posted_at_datetime_utc`) → raw shape. `connector='jsearch'`.
- Enforce the cost gate: **default-off in `sources[]`, hard `num_pages` cap, and count/log every call** against the RapidAPI quota.
- **Show me:** with JSearch explicitly enabled, the connector returns RoI jobs (including Indeed/LinkedIn-origin listings) in the common raw shape, and the call count is logged; with it disabled (default), Scout skips it and makes zero RapidAPI calls.

### A4 — Wire into config, fan-out, and full cross-source dedup
- Extend `config-mcp` `sources[]` enum with `jooble`, `careerjet`, `jsearch`; update the seeded default config (Jooble + Careerjet on, JSearch off). RoI-focused defaults.
- Confirm the Scout fan-out (the Phase 2g Celery task) now includes the enabled new connectors alongside Adzuna, Greenhouse, and the irishjobs.ie crawler, with per-connector error isolation intact (one failing source ≠ dead run).
- Verify dedup + precedence across the **full** set: a role that appears via an aggregator *and* via Greenhouse/crawler collapses to one row, keeping the direct record.
- **Show me:** one `POST /scout/run` executes all enabled connectors in the background; the `pipeline_runs` row shows per-connector fetched/new/deduped counts; a job present in two+ sources collapses to a single row with the direct source retained; a deliberately broken connector is recorded, not fatal.

---

## Acceptance gate

Green when **all** hold, then proceed to Phase 3:
1. Jooble, Careerjet, and JSearch each land **real RoI jobs** in the raw shape (JSearch when enabled).
2. All three flow through the existing normalize/dedupe/`db-mcp` tail — no bypass, no new persistence path.
3. Cross-source dedup collapses shared jobs across the full connector set (aggregators + ATS + crawler), applying the `ats > crawl > aggregator` precedence.
4. `sources[]` toggles work; **JSearch is off by default** and makes zero RapidAPI calls unless enabled; its calls are bounded and logged.
5. A full Scout run persists a genuine Dublin/RoI-weighted pool; a failing connector degrades gracefully.
6. This addendum adds **no LLM cost** (deterministic mapping only); re-runs stay idempotent (no dupes).

---

## Watch-outs

- **These APIs are the sanctioned path — treat them that way.** Jooble and Careerjet are built to be called and displayed; honor their rate limits and (for Careerjet) its affiliate/attribution model. This is the low-risk half of your sourcing; keep it clean.
- **JSearch is the cost surface.** Freemium then paid — verify the current quota, keep it default-off and page-capped, and watch the call log. The account-level spend cap is Anthropic-only; RapidAPI has its own limits/billing to mind separately.
- **Aggregator overlap is expected, not a bug.** You'll pull the same job from several sources; that's fine because dedup collapses it — but rely on the precedence rule so the *direct* record wins and apply links stay canonical.
- **Data quality varies by aggregator.** Snippets may be truncated and apply links may redirect through the aggregator. Store the raw payload; don't assume every field is present.
- **Still GDPR-bound:** postings are fine to store; don't retain recruiter personal data from listings without a lawful basis.
- **New env keys** (`JOOBLE_API_KEY`, `CAREERJET_API_KEY`, `RAPIDAPI_KEY`) go through the fail-fast config loader with clear errors, and into `.env.example` (blank).
- **Commit at the gate**; log decisions (Careerjet affiliate params used, JSearch page cap, dedup precedence order, default `sources[]` set).
