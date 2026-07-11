"""Config-deriver unit tests (no DB, no LLM key).

Exercises the deterministic path: the fallback derivation, the no-key short-circuit,
the empty-resume guard, and the hard cap. The live LLM canonicalization is covered
separately in ``test_config_deriver_live.py`` (skips without a real key).
"""

from __future__ import annotations

from jobpilot_core.config_deriver import (
    _cap,
    _empty_proposal,
    fallback_proposal,
    propose_search_config,
)
from jobpilot_core.settings import Settings

# Explicit empty key forces the deterministic path regardless of env / .env.
NOKEY = Settings(anthropic_api_key="")

RESUME = {
    "contact": {"name": "Jane Doe", "location": "Dublin, Ireland"},
    "summary": "Backend engineer with 6 years building APIs.",
    "skills": ["Python", "FastAPI", "PostgreSQL", "AWS", "Docker"],
    "experience": [
        {"title": "Senior Software Engineer", "company": "Stripe", "dates": "2020-2024"},
        {"title": "Software Engineer", "company": "Intercom", "dates": "2018-2020"},
    ],
    "certifications": ["AWS Certified Developer"],
}


def test_fallback_proposal_shape():
    prop = fallback_proposal(RESUME, NOKEY)
    assert set(prop) == {"roles", "keywords", "locations", "seniority", "notes"}
    assert prop["roles"], "expected at least one role from the experience titles"
    assert any("engineer" in r.lower() for r in prop["roles"])
    assert any(k.lower() == "python" for k in prop["keywords"])
    assert prop["locations"] == ["Dublin, Ireland"]
    assert prop["notes"] is None


async def test_propose_without_key_matches_fallback():
    # With no Anthropic key, the LLM path short-circuits to the deterministic derivation.
    prop = await propose_search_config(RESUME, "senior backend", NOKEY)
    assert prop == fallback_proposal(RESUME, NOKEY)


async def test_empty_resume_yields_empty_proposal():
    assert await propose_search_config(None, None, NOKEY) == _empty_proposal()
    assert await propose_search_config({}, "any hint", NOKEY) == _empty_proposal()


def test_cap_bounds_lists():
    fat = {
        "roles": [f"Role {i}" for i in range(50)],
        "keywords": [f"skill{i}" for i in range(50)],
        "locations": [f"City {i}" for i in range(10)],
        "seniority": "senior",
        "notes": "hi",
    }
    capped = _cap(fat, NOKEY)
    assert len(capped["roles"]) == NOKEY.scout_profile_max_roles
    assert len(capped["keywords"]) == NOKEY.scout_profile_max_keywords
    assert len(capped["locations"]) == 4
    # Non-string / blank entries are dropped by the cap filter.
    dirty = _cap({"roles": ["Engineer", "", None, "  "], "keywords": [], "locations": []}, NOKEY)
    assert dirty["roles"] == ["Engineer"]
