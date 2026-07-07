# Phase 2 Addendum 2.2 (Revised) — Full-Text Coverage & Honest Aggregation

> Hand this to Claude Code. **This supersedes the hydration-focused 2.2.** Same window (before Phase 6), same discipline: steps **in order**, reproduce each **show me**, commit at the end, log choices in `DECISIONS.md`.
>
> **Why the reframe:** hydration was implemented, tested live, and **removed** — the aggregators hand out tracker/redirect URLs (`jobviewtrack.com`, `jooble.org/jdp`, `adzuna.co.uk/…/land`), not employer pages, and every destination was robots-disallowed or bot-blocked (403/429). It hydrated **0 jobs** and wasn't worth its footprint. So we stop trying to *fetch* missing text and instead **prioritize sources that already have it, and make snippets lose every contest to full text.**

---

## Already done — do NOT rebuild

- `description_completeness` (`full` | `snippet`) set by each connector.
- `limited_data` marker in the Qualifier, surfaced in the dashboard.
- Hydration machinery removed (logged in `DECISIONS.md`).

This addendum builds on those flags.

---

## Core principle

**Full-text-primary.** Full-text sources *define* the pool's quality; snippet sources *extend* it as clearly-flagged breadth. **"Snippet" is a quality signal, not a delete signal.** The goal is that a snippet never wins a contest against full text — in dedup, in the aggregate, or in the UI — so snippets only ever *add coverage* and never *dilute*. You keep RoI breadth (which the snippet aggregators carry) without letting thin text corrupt the outputs.

---

## Decisions locked for this addendum (don't re-litigate)

- **Full-text sources (ATS + JSearch) carry quality; snippet aggregators (Jooble, Careerjet) stay as demoted breadth.**
- **Drop Adzuna for RoI.** It doesn't serve the Republic (Belfast-via-`gb` only) — raising its caps buys Northern Ireland, not Dublin. It's the one genuinely low-value source here.
- **Cap increases follow full-text yield, in this order:** grow `target_employers` first (biggest *free* win — every Irish employer added = full-text ATS jobs), then `JSEARCH_MAX_PAGES` (metered — watch quota), leave Adzuna, **hold the Jooble/Careerjet caps** until the aggregate is full-text-safe.
- **Dedup completeness tiebreaker:** within the existing `ats > crawl > aggregator` precedence, a **`full` record beats a `snippet` record** for the same `dedupe_hash`.
- **Aggregate from full text only.** Phase 5c frequency stats compute over `full` jobs (or heavily down-weight `snippet`); `limited_data` excluded/discounted. Don't infer skills from text you don't have.
- **Bounded pagination only for full-text sources.** JSearch may paginate (capped). **Aggregator pagination stays off** until the aggregate is full-text-safe — more snippets otherwise amplify the dilution you're fixing.
- **Prune by measured unique contribution after dedup, not by the "snippet" label.** A source that adds little *unique* post-dedup is the drop candidate — even a full-text one.
- **Scraping Indeed/LinkedIn (incl. via Apify) is compliant-coverage-last.** Exhaust JSearch + ATS first. Apify hosts the scraper but **doesn't launder the ToS** — it outsources the mechanics of scraping Indeed/LinkedIn, not the legality. If ever adopted: isolated behind the connector interface, **default-off**, a consciously-flipped switch. **Indeed is a gray-area call; LinkedIn is advised against for a product.** This is a business-risk decision, not a technical one — read Apify's terms + each site's ToS.

---

## Steps (each ends with a "show me")

### 1 — Dedup completeness tiebreaker
- Extend precedence so that, within a tier, `full` beats `snippet` for the same `dedupe_hash`; record the survivor's `description_completeness`.
- **Show me:** the same Dublin role arriving full-text (JSearch/ATS) **and** as a Jooble snippet collapses to the **full-text** record; the survivor is marked `full`.

### 2 — Full-text-only aggregate
- Compute the Phase 5c frequency stats over `full` jobs only (or snippets heavily down-weighted). The aggregate states its basis: "computed from N full-text jobs of M qualified."
- **Show me:** the frequency numbers **rise** when snippets are excluded vs included (proving the earlier undercount), and the UI shows the basis line.

### 3 — Full-text-weighted cap strategy
- Add a batch of Irish employers to `target_employers`. Bump `JSEARCH_MAX_PAGES` 1→2/3 with per-call quota logging. Leave Adzuna. Hold Jooble/Careerjet caps.
- **Show me:** a Scout run's `fetched` counts shift toward full-text sources; JSearch quota use is logged; the **full-text share** of the qualified pool rises versus the prior run.

### 4 — JSearch coverage measurement
- One run with JSearch enabled and paginated; measure (by `dedupe_hash` overlap) how many previously snippet-only roles now have a full-text source.
- **Show me:** a before/after count — "X of the 85 snippet roles now arrive full-text via JSearch" — quantifying the compliant recovery.

### 5 — Measured pruning
- Instrument **per-source unique contribution after dedup** (jobs that survive dedup and exist *only* via that source).
- **Show me:** a per-connector report of unique-after-dedup contribution, and a documented keep/drop decision per source from the numbers (e.g. drop Adzuna for RoI; keep Jooble only if it adds real unique breadth).

### Optional / gated (NOT a required build step) — Apify Indeed/LinkedIn
- Consider **only if** steps 3–4 leave a real, measured coverage gap. If pursued: a new connector behind the interface, **default-off**, with its own cap/quota guards; **Indeed only** as the less-exposed target; **LinkedIn not recommended** for a product. Read Apify's terms and each site's ToS first. Document the decision + rationale in `DECISIONS.md`.

---

## Acceptance gate

Green when **all** hold:
1. Dedup tiebreaker: `full` beats `snippet` for the same role.
2. Aggregate computed from full text (or snippets down-weighted), **basis stated in the UI**, and demonstrably different from the snippet-inclusive numbers.
3. Caps raised on full-text sources (`target_employers` grown, JSearch paginated with quota logging); Adzuna left; snippet caps held.
4. JSearch measurement quantifies recovered full-text coverage.
5. Per-source unique-contribution instrumented; keep/drop decisions documented.
6. Apify, if adopted at all, is isolated + default-off + documented as a business-risk decision — **LinkedIn not enabled** for the product.

---

## Watch-outs

- **Snippet ≠ delete.** Demote snippets everywhere; drop a source only on measured low *unique* contribution, not on the label.
- **Don't raise snippet caps until the aggregate is full-text-safe** — otherwise you amplify the exact dilution you just fixed.
- **`target_employers` is the cheapest full-text lever** — grow it before paying JSearch.
- **JSearch is metered** — pagination costs quota; log every call and keep the cap.
- **Adzuna for RoI is dead weight** — don't spend caps on Northern Ireland while building for Dublin.
- **Apify doesn't launder the ToS.** It outsources the mechanics of scraping Indeed/LinkedIn, not the legality; LinkedIn is the sharp line; keep any such connector default-off and consciously flipped. (Not legal advice — a business-risk call.)
- **State the aggregate's basis** ("N full-text of M") so users know the frequency stats aren't computed from teasers.
- **GDPR unchanged; commit at the gate; log source keep/drop decisions and any Apify decision.**
