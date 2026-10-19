"""The shared writing standard, project proposals, and the propose→critique→propose loop.

The standard exists because the same four craft defects appeared in generated bullets
every time — no outcomes, too long, overlapping content, and a uniform participial rhythm
that reads as machine-drafted. None of those is a truth problem, so none of the honesty
machinery caught them. These tests pin that the standard actually reaches every agent that
writes a bullet, that projects are in scope for proposals, and that the critic loop is
wired the way it claims.

Deterministic: no model is called anywhere here. What is checked is prompt assembly, item
bookkeeping and the refine control flow — the parts that break silently.
"""

from __future__ import annotations

import pytest
from jobpilot_core import cv_critic, cv_rewriter, gap_closer, gap_critic, writing


# --------------------------------------------------------------------------- #
# One standard, reaching every writer
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name,text",
    [
        ("gap_closer", gap_closer._BRIEF),
        ("cv_rewriter", cv_rewriter.INTEGRITY),
    ],
)
def test_every_agent_that_writes_a_bullet_carries_the_standard(name, text):
    assert "LEAD WITH THE OUTCOME" in text, f"{name} lost the quantification rule"
    assert "VARY THE SENTENCE SHAPE" in text, f"{name} lost the rhythm rule"
    assert "EVERY BULLET MUST EARN ITS PLACE" in text, f"{name} lost the redundancy rule"
    assert "OWN THE WORK" in text, f"{name} lost the ownership rule"


def test_the_critic_is_told_to_look_for_the_same_defects():
    """A standard the writers follow and the reviewer does not check is half a standard."""
    rubric = cv_critic._RUBRIC
    for needle in ("QUANTIFICATION", "REDUNDANCY", "RHYTHM", "OWNERSHIP", "CONTRADICTION"):
        assert needle in rubric


def test_the_bullet_cap_is_one_number_everywhere():
    """The renderer discards past the cap, so a writer told a different number would waste
    output that is thrown away before anyone reads it."""
    from jobpilot_core import tailor

    assert writing.MAX_BULLETS_PER_ENTRY == tailor.MAX_BULLETS
    assert str(writing.MAX_BULLETS_PER_ENTRY) in writing.BULLET_STANDARD


def test_the_standard_names_the_specific_tells_it_is_trying_to_stop():
    """Generic advice ('be concise') produced generically bad bullets. The rules name the
    actual failing phrasings, which is what makes them actionable."""
    standard = writing.BULLET_STANDARD
    assert "participial clause" in standard
    assert "Worked with" in standard          # the ownership giveaway
    assert "on-premises/VPC-isolated" in standard  # the contradiction example
    assert "[X]" in standard                  # the bracketed-placeholder convention


def test_placeholders_are_offered_but_invention_is_not():
    """A bracket asks the candidate for a defensible number. A fabricated figure presented
    as fact is the one thing the quantification rule must not licence."""
    assert "NEVER invent a specific" in writing.BULLET_STANDARD


# --------------------------------------------------------------------------- #
# Projects are in scope for proposals
# --------------------------------------------------------------------------- #
def test_project_rewrites_are_a_proposal_kind():
    assert "project_rewrites" in gap_closer._KINDS
    assert gap_closer._PREFIX["project_rewrites"] == "pr"
    assert "project_rewrites" in gap_closer._SCHEMA["required"]


def test_the_brief_tells_the_agent_to_propose_on_projects():
    assert "PROPOSE FOR PROJECTS TOO" in gap_closer._BRIEF


RESUME_WITH_PROJECTS = {
    "skills": ["Python"],
    "experience": [
        {"title": "Engineer", "company": "Acme", "dates": "2021-2024",
         "bullets": ["Did a thing."]},
        # Seven bullets — one past the digest cap, so the cap itself is observable.
        {"title": "Data Engineer", "company": "LTIMindtree", "dates": "2019-2021",
         "bullets": [f"Shipped increment {i}." for i in range(1, 8)]},
    ],
    "certifications": ["AWS Certified Solutions Architect"],
    "projects": [{"name": "Multi-Agent Platform", "tech": "Python, LangGraph",
                  "dates": "Feb. 2026 - Present",
                  "bullets": ["Built a LangGraph control plane.",
                              "Routed data through MCP services."]}],
    "additional_sections": [{"heading": "Publications", "items": ["Paper on retrieval."]}],
}

#: Every function that renders the résumé *for a model*. Four separate functions with four
#: sets of callers, and the recurring bug of this codebase is that content moves to a new
#: field and only some of them are updated — so they are enumerated once, here, and every
#: coverage test below iterates the whole list. A fifth view added without being registered
#: here is the next instance of the same bug.
#:
#: Note the two distinct functions named ``_resume_block``: ``analyzer``'s and
#: ``qualifier``'s are different digests with different caps, not an import of one another.
def _model_facing_resume_views():
    from jobpilot_core import analyzer, cv_fields, qualifier

    return (
        ("analyzer._resume_block (Analyzer, Enhancer, Gap-closer)", analyzer._resume_block, True),
        ("qualifier._resume_block (Qualifier refine)", qualifier._resume_block, True),
        ("cv_rewriter._full_resume_block (Rewriter, Critic)", cv_rewriter._full_resume_block, False),
        ("cv_fields._base_text (fabrication gate)", cv_fields._base_text, False),
    )


@pytest.mark.parametrize("label,render,_capped", _model_facing_resume_views())
def test_every_view_shows_every_section_the_cv_carries(label, render, _capped):
    """The bug this class of test exists for, which has now occurred four times: content
    moves to a new field, a résumé view still reads the old one, and an agent is left
    working on content it has never seen. Being *told* to propose on projects is worthless
    without being *shown* one — the only way to comply would be to invent a project name.

    The fourth instance was the worst, because ``qualifier._resume_block`` gates the whole
    pipeline: a skill evidenced only by a project or a credential scored as ``missing``, the
    job fell below threshold, and it left the pool before any other agent saw it. Nothing
    surfaced — you cannot notice jobs you were never shown.

    Case-insensitive because ``_base_text`` normalises to lowercase.
    """
    block = render(RESUME_WITH_PROJECTS).lower()
    for needle, what in (
        ("multi-agent platform", "the project name"),
        ("langgraph control plane", "the project bullets"),
        ("aws certified solutions architect", "the certifications"),
        ("paper on retrieval", "the extra headed sections"),
    ):
        assert needle in block, f"{label} hides {what}"


@pytest.mark.parametrize("label,render,capped", _model_facing_resume_views())
def test_six_bullets_per_role_reach_the_digests_and_all_of_them_reach_the_rest(
    label, render, capped
):
    """The digests cap bullets per role; the tailoring view and the honesty haystack must
    not, because an agent can only reword what it was shown and a gate that cannot see a
    bullet will delete the skill it evidences as a fabrication."""
    block = render(RESUME_WITH_PROJECTS).lower()
    assert "shipped increment 6." in block, f"{label} truncates before the sixth bullet"
    if capped:
        assert "shipped increment 7." not in block, f"{label} is no longer capped at six"
    else:
        assert "shipped increment 7." in block, f"{label} must render the whole CV, uncapped"


def test_a_project_evidenced_skill_is_admissible_to_the_gates():
    """The third consumer of the same data: the fabrication haystack. A skill named only in
    a typed project must not be deleted as invented."""
    from jobpilot_core import cv_fields

    draft = {**RESUME_WITH_PROJECTS, "skills": ["Python", "LangGraph"]}
    assert cv_fields.validate_no_fabricated_skills(draft, RESUME_WITH_PROJECTS)["ok"]


def test_project_items_get_stable_ids_and_start_undecided():
    shaped = gap_closer.with_item_ids({
        "project_rewrites": [
            {"project_ref": "Platform", "before": "a", "after": "b"},
            {"project_ref": "Platform", "before": "c", "after": "d"},
        ],
    })
    assert [i["id"] for i in shaped["project_rewrites"]] == ["pr0", "pr1"]
    assert all(i["accepted"] is None for i in shaped["project_rewrites"])


def test_an_accepted_project_rewrite_is_collected_for_the_rewriter():
    shaped = gap_closer.with_item_ids({
        "project_rewrites": [{"project_ref": "Platform", "before": "a", "after": "b"}],
    })
    shaped["project_rewrites"][0]["accepted"] = True
    assert not gap_closer.is_empty(shaped)
    assert gap_closer.accepted_items(shaped)["project_rewrites"][0]["after"] == "b"


def test_an_accepted_project_rewrite_reaches_the_rewriter_prompt():
    """The attested block is how a user-vouched item becomes trusted input. A kind missing
    from it is a kind the user can accept and never see on the CV."""
    block = cv_rewriter.attested_block({
        "project_rewrites": [{"project_ref": "Multi-Agent Platform", "after": "Reworded."}],
    })
    assert "Multi-Agent Platform" in block
    assert "Reworded." in block


# --------------------------------------------------------------------------- #
# The proposal critic
# --------------------------------------------------------------------------- #
SHAPED = {
    "claimed_skills": [{"id": "sk0", "skill": "Kubernetes", "closes": "k8s",
                        "in_resume": False, "interview_prep": "Ran the migration."}],
    "bullet_rewrites": [{"id": "rw0", "role_ref": "Acme", "before": "did work",
                         "after": "Cut latency 30%.", "interview_prep": "Know the numbers."}],
    "new_bullets": [{"id": "nb0", "role_ref": "Acme", "text": "Built X.",
                     "interview_prep": "Describe X."}],
    "project_rewrites": [{"id": "pr0", "project_ref": "Platform", "before": "built it",
                          "after": "Built it, 12 services.", "interview_prep": "Walk the design."}],
}


def test_the_critic_sees_every_item_with_its_id():
    """Findings are keyed by item id, so an item absent from the block can never be
    critiqued — and would look like a pass rather than an omission."""
    block = gap_critic.proposals_block(SHAPED)
    for item_id in ("sk0", "rw0", "nb0", "pr0"):
        assert f"[{item_id}]" in block
    assert "Platform" in block  # project rewrites are included, not just role ones


def test_needs_revision_is_false_for_a_clean_pass():
    """A 'ship' verdict with nothing to fix must skip the second propose call — otherwise
    the reviewed variant always costs three calls, including when it changes nothing."""
    assert not gap_critic.needs_revision(
        {"verdict": "ship", "findings": [], "drop_item_ids": []})


@pytest.mark.parametrize("critique", [
    {"verdict": "revise", "findings": [], "drop_item_ids": []},
    {"verdict": "ship", "findings": [{"item_id": "rw0"}], "drop_item_ids": []},
    {"verdict": "ship", "findings": [], "drop_item_ids": ["nb0"]},
])
def test_needs_revision_is_true_whenever_there_is_anything_to_act_on(critique):
    assert gap_critic.needs_revision(critique)


def test_the_critique_block_names_the_items_to_drop_explicitly():
    block = gap_critic.critique_block({
        "verdict": "revise",
        "instructions": "Quantify everything.",
        "findings": [{"item_id": "rw0", "category": "no_outcome",
                      "problem": "activity not outcome", "fix": "add a figure"}],
        "drop_item_ids": ["nb0"],
    })
    assert "Quantify everything." in block
    assert "rw0" in block and "add a figure" in block
    assert "DROP these entirely" in block and "nb0" in block
    # The second pass replaces the set wholesale, so it must be told to re-emit everything.
    assert "COMPLETE proposal set" in block


def test_the_critic_grades_defensibility_not_just_prose():
    """The reason this is a separate agent from cv_critic: a proposal is judged on whether
    the candidate could survive being asked about it, which a finished CV is not."""
    assert "DEFENSIBILITY" in gap_critic._RUBRIC
    assert "interview_prep" in gap_critic._RUBRIC
    categories = gap_critic._SCHEMA["properties"]["findings"]["items"]["properties"]["category"]["enum"]
    assert "indefensible" in categories and "implausible" in categories


def test_the_critic_is_told_to_be_sparing():
    """A reviewer that flags everything makes the second pass churn good content."""
    assert "sparing" in gap_critic._RUBRIC


# --------------------------------------------------------------------------- #
# The second pass reuses the cached prefix
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_critique_rides_in_the_variable_half_not_the_cached_prefix(monkeypatch):
    """Both passes share a résumé and a brief. Putting the critique in the prefix would
    change it between calls and pay the cache-write cost twice for one job."""
    seen: list[dict] = []

    async def fake_extract_json(**kwargs):
        seen.append(kwargs)

        class _R:
            data = {"claimed_skills": [], "bullet_rewrites": [],
                    "new_bullets": [], "project_rewrites": []}
            usage = type("U", (), {"input_tokens": 0, "output_tokens": 0,
                                   "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 0})()
        return _R()

    monkeypatch.setattr(gap_closer, "extract_json", fake_extract_json)
    from jobpilot_core.settings import Settings

    settings = Settings(anthropic_api_key="test")
    resume = {"skills": ["Python"], "experience": []}

    await gap_closer.propose_gap_close({}, resume, settings)
    await gap_closer.propose_gap_close({}, resume, settings, critique="CRITIQUE TEXT HERE")

    assert seen[0]["cached_prefix"] == seen[1]["cached_prefix"]
    assert "CRITIQUE TEXT HERE" not in seen[0]["variable_text"]
    assert "CRITIQUE TEXT HERE" in seen[1]["variable_text"]
