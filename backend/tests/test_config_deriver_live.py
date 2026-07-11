"""Live config-deriver test — one cheap Haiku call that canonicalizes messy resume
titles into market-standard roles. Skips cleanly without a real ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import re

import pytest

from jobpilot_core.config_deriver import propose_search_config
from jobpilot_core.settings import get_settings

S = get_settings()

pytestmark = pytest.mark.skipif(
    not S.anthropic_api_key.startswith("sk-ant-"),
    reason="ANTHROPIC_API_KEY not configured",
)

# Deliberately messy titles: company suffixes, abbreviations, dates, decorations.
MESSY_RESUME = {
    "contact": {"name": "Sam Lee", "location": "Dublin, Ireland"},
    "summary": "Full-stack developer, mostly backend, 7 years.",
    "skills": ["Python", "TypeScript", "React", "Postgres", "Kubernetes"],
    "experience": [
        {"title": "Sr. FS Dev @ Acme Ltd (2019-2023)", "company": "Acme Ltd", "dates": "2019-2023"},
        {"title": "Software Developer II", "company": "Globex", "dates": "2016-2019"},
    ],
    "certifications": [],
}


async def test_llm_canonicalizes_messy_titles():
    prop = await propose_search_config(MESSY_RESUME, hint="senior backend / platform roles", settings=S)

    assert set(prop) >= {"roles", "keywords", "locations"}
    assert prop["roles"], "expected canonical roles from the resume"

    for role in prop["roles"]:
        # Canonical market titles carry no company decoration, dates, or apply-mode noise.
        assert "@" not in role
        assert not re.search(r"\d", role), f"role kept a year/number: {role!r}"
        assert "acme" not in role.lower() and "globex" not in role.lower()

    # Keywords should surface concrete skills; location from the contact block.
    assert any(k.lower() in {"python", "typescript", "react"} for k in prop["keywords"])
    assert any("dublin" in loc.lower() for loc in prop["locations"])
