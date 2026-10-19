"""config-mcp — user search configuration over HTTP (streamable MCP).

Same security core as db-mcp: the caller's ``user_id`` comes ONLY from the signed
request context (validated by :class:`UserContextMiddleware`). A second user can
never read or write another user's config — scoping is bound by the runtime, not
by any argument the caller supplies.
"""

from __future__ import annotations

from jobpilot_core import repositories
from jobpilot_core.db import session_scope
from jobpilot_core.mcp_auth import UserContextMiddleware, require_user_id
from jobpilot_core.settings import get_settings
from mcp.server.fastmcp import FastMCP

settings = get_settings()
mcp = FastMCP("config-mcp", stateless_http=True, json_response=True, host="0.0.0.0", port=8003)


@mcp.tool()
async def get_search_config() -> dict:
    """Return the caller's search config (a default empty shape if none exists)."""
    uid = require_user_id()
    async with session_scope() as session:
        cfg = await repositories.get_search_config(session, uid)
        return {"config": repositories.search_config_to_dict(cfg)}


@mcp.tool()
async def update_search_config(
    roles: list[str] | None = None,
    locations: list[str] | None = None,
    keywords: list[str] | None = None,
    sources: list[str] | None = None,
    target_employers: list[str] | None = None,
    sites: list[str] | None = None,
    schedule: str | None = None,
) -> dict:
    """Create-or-update the caller's search config (partial update: only provided
    fields are written). ``sources`` = enabled connectors; ``target_employers`` =
    ATS boards to poll."""
    uid = require_user_id()
    fields = {
        "roles": roles,
        "locations": locations,
        "keywords": keywords,
        "sources": sources,
        "target_employers": target_employers,
        "sites": sites,
        "schedule": schedule,
    }
    async with session_scope() as session:
        cfg = await repositories.upsert_search_config(session, uid, fields)
        return {"config": repositories.search_config_to_dict(cfg)}


# --------------------------------------------------------------------------- #
# Qualifier config (Phase 3)
# --------------------------------------------------------------------------- #
@mcp.tool()
async def get_qualifier_config() -> dict:
    """Return the caller's qualifier config (a sensible default shape if none exists)."""
    uid = require_user_id()
    async with session_scope() as session:
        cfg = await repositories.get_qualifier_config(session, uid)
        return {"config": repositories.qualifier_config_to_dict(cfg)}


@mcp.tool()
async def update_qualifier_config(
    weights_json: dict | None = None,
    threshold: float | None = None,
    must_have: list[str] | None = None,
    nice_to_have: list[str] | None = None,
) -> dict:
    """Create-or-update the caller's qualifier config (partial update). ``weights_json``
    = {skills,experience,title_keyword,location}; ``must_have`` missing → hard-disqualify."""
    uid = require_user_id()
    fields = {
        "weights_json": weights_json,
        "threshold": threshold,
        "must_have": must_have,
        "nice_to_have": nice_to_have,
    }
    async with session_scope() as session:
        cfg = await repositories.upsert_qualifier_config(session, uid, fields)
        return {"config": repositories.qualifier_config_to_dict(cfg)}


@mcp.tool()
async def ensure_qualifier_config(resume_skills: list[str] | None = None) -> dict:
    """Seed a conservative default (skills → nice-to-haves, no must-haves, moderate
    threshold) ONLY if the caller has no qualifier config yet. Idempotent."""
    from jobpilot_core.qualifier import default_qualifier_config

    uid = require_user_id()
    async with session_scope() as session:
        existing = await repositories.get_qualifier_config(session, uid)
        if existing is not None:
            return {"config": repositories.qualifier_config_to_dict(existing), "seeded": False}
        cfg = await repositories.upsert_qualifier_config(
            session, uid, default_qualifier_config(resume_skills or [])
        )
        return {"config": repositories.qualifier_config_to_dict(cfg), "seeded": True}


# --------------------------------------------------------------------------- #
# Gate config (Phase 6) — human-in-the-loop review toggles
# --------------------------------------------------------------------------- #
@mcp.tool()
async def get_gate_config() -> dict:
    """The caller's effective gate config (stored overrides merged onto the defaults):
    which pipeline transitions pause for review. Includes the default set for the UI."""
    from jobpilot_core import gates

    uid = require_user_id()
    async with session_scope() as session:
        stored = await repositories.get_gate_configs(session, uid)
    return {
        "gates": gates.effective_gates(stored),
        "defaults": dict(gates.GATE_DEFAULTS),
        "keys": list(gates.GATE_KEYS),
    }


@mcp.tool()
async def update_gate_config(gate_key: str, enabled: bool) -> dict:
    """Enable/disable a single review gate for the caller. Rejects unknown keys."""
    from jobpilot_core import gates

    uid = require_user_id()
    if gate_key not in gates.GATE_KEYS:
        return {"error": f"unknown gate {gate_key!r} (expected {list(gates.GATE_KEYS)})"}
    async with session_scope() as session:
        await repositories.upsert_gate_config(session, uid, gate_key, bool(enabled))
        stored = await repositories.get_gate_configs(session, uid)
    return {"gates": gates.effective_gates(stored)}


# --------------------------------------------------------------------------- #
# Application profile + answer bank (apply agent, stage 1)
#
# These live in config-mcp rather than a new service because they are exactly what this
# service already owns: per-user configuration, no browser and no LLM involved. The
# browser-driving apply-mcp arrives only when Playwright does.
# --------------------------------------------------------------------------- #
@mcp.tool()
async def get_application_profile() -> dict:
    """The caller's pre-configured application facts. Returns an empty shell (not an
    error) when none has been saved yet, so the editor renders the same either way."""
    uid = require_user_id()
    async with session_scope() as session:
        profile = await repositories.get_application_profile(session, uid)
        return {"profile": repositories.application_profile_to_dict(profile)}


@mcp.tool()
async def update_application_profile(fields: dict) -> dict:
    """Partial update of the application profile.

    A key absent from ``fields`` is left untouched; a key present with ``null`` clears
    it. Unknown keys are ignored rather than rejected, so a newer frontend cannot 400
    against an older backend.
    """
    uid = require_user_id()
    async with session_scope() as session:
        profile = await repositories.upsert_application_profile(session, uid, fields or {})
        return {"profile": repositories.application_profile_to_dict(profile)}


@mcp.tool()
async def seed_application_profile() -> dict:
    """Fill blank profile fields from the caller's parsed résumé.

    Only blanks are filled — a value the user typed by hand survives a résumé re-upload.
    Every seeded value is copied from text the parser read off the user's own document;
    nothing is inferred.
    """
    from jobpilot_core import apply_profile

    uid = require_user_id()
    async with session_scope() as session:
        resume = await repositories.get_latest_uploaded_resume(session, uid)
        if resume is None or not resume.parsed_json:
            return {"seeded": False, "reason": "no parsed résumé on file"}

        seeded = apply_profile.seed_from_resume(resume.parsed_json)
        if not seeded:
            return {"seeded": False, "reason": "résumé contained no usable contact details"}

        existing = await repositories.get_application_profile(session, uid)
        current = repositories.application_profile_to_dict(existing)
        merged = apply_profile.merge_seed(
            {k: current.get(k) for k in seeded}, seeded
        )
        merged["seeded_from_resume_id"] = resume.id
        profile = await repositories.upsert_application_profile(session, uid, merged)
        return {
            "seeded": True,
            "fields": sorted(seeded),
            "profile": repositories.application_profile_to_dict(profile),
        }


@mcp.tool()
async def get_answer_bank() -> dict:
    """List the caller's banked screening answers (newest first, vectors omitted)."""
    uid = require_user_id()
    async with session_scope() as session:
        return {"entries": await repositories.get_answer_bank(session, uid)}


@mcp.tool()
async def upsert_answer(
    question_text: str,
    answer_value: str,
    answer_kind: str = "text",
    scope: str = "global",
) -> dict:
    """Store or re-confirm one screening answer.

    The normalised question is derived here, and the locked category is decided by
    content rather than by the caller — so a client cannot mark a work-eligibility
    question as ordinary and thereby opt it into similarity matching.
    """
    from jobpilot_core import answer_bank as ab

    uid = require_user_id()
    question_text = (question_text or "").strip()
    if not question_text:
        return {"error": "question_text is required"}
    if answer_kind not in ab.ANSWER_KINDS:
        return {"error": f"unknown answer_kind {answer_kind!r} (expected {list(ab.ANSWER_KINDS)})"}

    norm = ab.normalise_question(question_text)
    if not norm:
        return {"error": "question_text normalised to nothing"}

    locked = ab.is_locked(question_text)
    vec = None
    if not locked:
        # Locked rows get no vector at all: absent an embedding they can never be
        # selected as a near match, which is the firewall enforced in storage as well
        # as in the resolver.
        try:
            from jobpilot_core import embeddings

            vec = embeddings.embed_texts(
                settings, [ab.question_embedding_text(question_text)]
            )[0]
        except Exception:  # noqa: BLE001 - embedding is an optimisation, never required
            vec = None

    async with session_scope() as session:
        entry = await repositories.upsert_answer_bank_entry(
            session, uid,
            question_text=question_text,
            question_norm=norm,
            answer_value=answer_value,
            answer_kind=answer_kind,
            scope=scope,
            locked=locked,
            question_vec=vec,
        )
        return {
            "entry": repositories.answer_bank_entry_to_dict(entry),
            "locked_as": ab.locked_category(question_text),
        }


# --------------------------------------------------------------------------- #
# Autofill (apply agent, stage 3)
#
# The browser extension scrapes a live ATS form and posts the field descriptors here;
# this resolves each one against the profile and the bank and hands back a plan. Still
# no browser and no LLM on this side — the extension owns the DOM, and every value in the
# response traces to a row the user wrote.
# --------------------------------------------------------------------------- #
def _question_embedder():
    """An embed callable for the near-match pass, or ``None`` if the model is unavailable.

    Returned as a closure rather than embedding up front because most fields resolve at
    Tier A or by exact match and never need a vector at all; loading the model for a form
    that turns out to be all-profile would be pure latency.
    """
    from jobpilot_core import embeddings

    def embed(text: str) -> list[float]:
        return embeddings.embed_texts(settings, [text])[0]

    return embed


@mcp.tool()
async def plan_application(
    fields: list[dict],
    employer_slug: str | None = None,
    company: str | None = None,
) -> dict:
    """Resolve a scraped form against the caller's profile and answer bank.

    ``fields`` are plain descriptors — ``{ref, label, type, name, autocomplete, required,
    options}`` — read off the live page. Returns one plan entry per field carrying the
    value, the tier that supplied it and the source record, so the extension can show the
    provenance of everything it fills rather than asking the user to trust it.

    Nothing is written here: planning a form must be safe to repeat.
    """
    from jobpilot_core import apply_fill

    uid = require_user_id()
    if not isinstance(fields, list):
        return {"error": "fields must be a list of field descriptors"}

    scope = apply_fill.employer_scope(employer_slug, company)
    async with session_scope() as session:
        profile = repositories.application_profile_to_dict(
            await repositories.get_application_profile(session, uid)
        )
        entries = await repositories.get_answer_bank(session, uid, include_vectors=True)

    embed = None
    if any(e.get("question_vec") for e in entries):
        # Only pay for the model when there is something for it to match against.
        try:
            embed = _question_embedder()
        except Exception:  # noqa: BLE001 - near-match is an optimisation, never required
            embed = None

    plan = apply_fill.plan_form(fields, profile, entries, scope=scope, embed=embed)
    plan["scope"] = scope
    plan["profile_complete"] = bool(profile.get("first_name") and profile.get("email"))
    # Returned so the caller can name an uploaded CV after the applicant rather than
    # sending a recruiter a file called "cv.pdf".
    plan["applicant_name"] = " ".join(
        p for p in (profile.get("first_name"), profile.get("last_name")) if p
    ) or None
    return plan


@mcp.tool()
async def record_application_answers(
    answers: list[dict],
    employer_slug: str | None = None,
    company: str | None = None,
) -> dict:
    """Bank what the user actually submitted, so the next form asks less.

    ``answers`` are ``{question, value, kind, tier, source_id, edited}`` read back off the
    form at submit time. Only values the user typed or corrected are stored — anything
    that came out of the profile or the bank unchanged gets a usage bump instead, because
    reuse and re-authoring are different events.

    The locked flag is decided here from the question's content, exactly as in
    ``upsert_answer``: a sensitive question banked from a form must not be able to enter
    the similarity pool just because it arrived by a different route.
    """
    from jobpilot_core import answer_bank as ab
    from jobpilot_core import apply_fill

    uid = require_user_id()
    if not isinstance(answers, list):
        return {"error": "answers must be a list"}

    scope = apply_fill.employer_scope(employer_slug, company)
    to_bank = apply_fill.bankable_answers(answers, scope=scope, company=company)
    reused = apply_fill.used_answer_ids(answers)

    # Embed the ordinary new questions in one batch — the near-match pass is what makes
    # a differently-worded version of the same question resolve next time.
    unlocked = [item for item in to_bank if not item["locked_as"]]
    vectors: dict[str, list[float]] = {}
    if unlocked:
        try:
            from jobpilot_core import embeddings

            texts = [ab.question_embedding_text(i["question_text"]) for i in unlocked]
            for item, vec in zip(unlocked, embeddings.embed_texts(settings, texts)):
                vectors[item["question_text"]] = vec
        except Exception:  # noqa: BLE001 - a missing vector costs a future match, nothing more
            vectors = {}

    stored: list[dict] = []
    async with session_scope() as session:
        for item in to_bank:
            entry = await repositories.upsert_answer_bank_entry(
                session, uid,
                question_text=item["question_text"],
                question_norm=ab.normalise_question(item["question_text"]),
                answer_value=item["answer_value"],
                answer_kind=item["answer_kind"],
                scope=item["scope"],
                locked=bool(item["locked_as"]),
                question_vec=vectors.get(item["question_text"]),
            )
            stored.append(repositories.answer_bank_entry_to_dict(entry))

        bumped = 0
        if reused:
            import uuid as _uuid

            parsed = []
            for raw in reused:
                try:
                    parsed.append(_uuid.UUID(raw))
                except (ValueError, AttributeError):
                    continue
            bumped = await repositories.mark_answers_used(session, uid, parsed)

    return {"learned": len(stored), "reused": bumped, "entries": stored, "scope": scope}


@mcp.tool()
async def delete_answer(entry_id: str) -> dict:
    """Delete one banked answer by id (scoped to the caller)."""
    import uuid as _uuid

    uid = require_user_id()
    try:
        parsed = _uuid.UUID(entry_id)
    except (ValueError, AttributeError):
        return {"error": "entry_id is not a valid uuid"}
    async with session_scope() as session:
        return {"deleted": await repositories.delete_answer_bank_entry(session, uid, parsed)}


app = UserContextMiddleware(mcp.streamable_http_app(), settings)
