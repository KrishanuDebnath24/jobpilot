"""Title/location pre-filter shared by the board connectors.

Every case here is a real title from the live pool. The two bugs under test both let
obvious noise into an Ireland-only software-engineering search, and neither was visible
from a unit test written against tidy inputs — they were found by reading the pool.
"""

from __future__ import annotations

import pytest
from jobpilot_core.job_match import (
    GRADE_WORDS,
    location_matches,
    match_tokens,
    title_matches,
)

# What the user's config plus the résumé deriver actually produced.
ROLES = ["Software Engineer", "Backend Engineer", "Senior Software Engineer"]
KEYWORDS = ["python", "FastAPI", "PostgreSQL", "Docker", "AWS", "Kubernetes", "REST", "Git"]


@pytest.fixture
def tokens():
    return match_tokens(ROLES, KEYWORDS)


def test_grade_words_never_become_match_tokens(tokens):
    """"Senior Software Engineer" must not contribute "senior". A grade word describes
    level, not the job, and the deriver already records seniority separately."""
    assert "senior" not in tokens
    assert not (tokens & GRADE_WORDS)
    assert {"software", "engineer", "python", "kubernetes"} <= tokens


@pytest.mark.parametrize(
    "title",
    [
        # All matched only on "senior" before the fix.
        "Senior Credit Controller",
        "Senior Field Sales Representative - National Broadband Ireland",
        "Compliance Senior Associate",
        "Internal Audit, Senior Associate",
        "Pension Support Senior Associate",
        "Senior CSA Quantity Surveyor - Pre Construction Estimating",
        "Senior Product Manager",
        "Senior AI Deployment Consultant",
    ],
)
def test_seniority_alone_does_not_admit_a_job(title, tokens):
    assert title_matches(title, tokens) is False


@pytest.mark.parametrize(
    "title",
    [
        # "git" is a substring of "digital"; "rest" of "interest"/"forestry".
        "Associate, eDiscovery/Digital Forensics",
        "UNPAID VOLUNTEER - Blockchain Developer (Digital Technology Institute)",
        "Interest Rate Products Analyst",
        "Forestry Operations Supervisor",
    ],
)
def test_substring_collisions_no_longer_match(title, tokens):
    assert title_matches(title, tokens) is False


@pytest.mark.parametrize(
    "title",
    [
        "Senior CloudOps Engineer",
        "Senior RAG Engineer",
        "AI Automations Product Engineer",
        "Backend Engineer",
        "Software Engineer, Platform",
        "Python Developer",
        "Kubernetes Platform Specialist",
    ],
)
def test_genuinely_relevant_titles_survive(title, tokens):
    assert title_matches(title, tokens) is True


def test_word_boundary_does_not_swallow_longer_words(tokens):
    """"Engineering Manager" is a different role from "Engineer" and no longer matches on
    that token — it can still enter via another token if one is present."""
    assert title_matches("Engineering Manager, Server Networking", tokens) is False
    assert title_matches("Software Engineering Manager", tokens) is True  # via "software"


def test_no_terms_configured_matches_everything():
    """An unnarrowed search must stay broad rather than silently return nothing."""
    assert title_matches("Anything At All", set()) is True
    assert title_matches("", set()) is True


def test_empty_title_never_matches(tokens):
    assert title_matches(None, tokens) is False
    assert title_matches("", tokens) is False


# --------------------------------------------------------------------------- #
# location_matches — substring is correct here, unlike for titles
# --------------------------------------------------------------------------- #
def test_location_matching_is_hierarchical():
    assert location_matches("Dublin, County Dublin, Ireland", ["Dublin"]) is True
    assert location_matches("Cork, Ireland", ["Dublin"]) is False
    assert location_matches("anywhere", []) is True
    assert location_matches(None, ["Dublin"]) is False


def test_location_matches_is_not_the_roi_gate():
    """Documenting the trap: this happily passes Northern Ireland on a Belfast query and
    passes everything when unset, which is why roi_location exists separately."""
    assert location_matches("Belfast, Northern Ireland", ["Belfast"]) is True
    assert location_matches("Belfast, Northern Ireland", []) is True
