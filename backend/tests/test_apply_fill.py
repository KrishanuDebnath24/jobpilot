"""Stage 3 of the apply agent: the ladder run over a real form's fields.

Pure-logic tests — no DB, no browser, no model. Stage 1's tests guard the resolution
primitives; these guard what happens when those primitives meet a form, which is where
the interesting failures live:

* an eligibility question must not be answerable by the generic alias map just because
  the alias map runs second and happens to match a substring,
* a resolved value that no offered option carries must escalate rather than pick the
  nearest thing, and
* an answer the user typed with the form in front of them must be banked, while one that
  came out of the bank unchanged must not be re-written over itself.

The embedder is injected everywhere it is needed, so the whole file runs offline.
"""

from __future__ import annotations

import pytest
from jobpilot_core import answer_bank as ab
from jobpilot_core import apply_fill as af


def field(**kw):
    """A scraped field descriptor with sane defaults, so tests state only what matters."""
    return {
        "ref": kw.pop("ref", "jp-1"),
        "label": kw.pop("label", ""),
        "type": kw.pop("type", "text"),
        "name": kw.pop("name", None),
        "autocomplete": kw.pop("autocomplete", None),
        "required": kw.pop("required", False),
        "options": kw.pop("options", None),
        **kw,
    }


PROFILE = {
    "first_name": "Krishanu",
    "last_name": "Sen",
    "email": "k@example.com",
    "phone": "+353 1 234 5678",
    "city": "Dublin",
    "country": "Ireland",
    "linkedin_url": "https://linkedin.com/in/example",
    "notice_period_days": 30,
    "willing_to_relocate": False,
    "eligibility_json": {
        "IE": {"authorized": True, "sponsorship_required": False},
        "US": {"authorized": False, "sponsorship_required": True},
    },
}


def bank(question, answer, **kw):
    return {
        "id": kw.pop("id", "row-1"),
        "question_text": question,
        "question_norm": ab.normalise_question(question),
        "answer_value": answer,
        "answer_kind": kw.pop("answer_kind", "text"),
        "scope": kw.pop("scope", "global"),
        "locked": kw.pop("locked", False),
        "question_vec": kw.pop("question_vec", None),
    }


# --------------------------------------------------------------------------- #
# Tier A — the profile
# --------------------------------------------------------------------------- #
def test_profile_fields_fill_from_the_profile():
    plan = af.plan_field(field(label="Email Address *", type="email"), PROFILE, [])
    assert plan["action"] == af.ACTION_FILL
    assert plan["value"] == "k@example.com"
    assert plan["tier"] == ab.Tier.PROFILE.value
    assert plan["source"] == "profile.email"


def test_autocomplete_beats_a_vague_label():
    plan = af.plan_field(
        field(label="Your details", autocomplete="given-name"), PROFILE, []
    )
    assert plan["value"] == "Krishanu"


def test_a_blank_profile_field_escalates_and_says_which_one():
    plan = af.plan_field(field(label="GitHub URL"), PROFILE, [])
    assert plan["action"] == af.ACTION_ASK
    assert "github url" in plan["reason"]


def test_notice_period_is_rendered_in_the_units_a_form_asks_for():
    plan = af.plan_field(field(label="What is your notice period?"), PROFILE, [])
    assert plan["value"] == "30 days"


def test_a_stored_boolean_reads_as_yes_or_no():
    plan = af.plan_field(field(label="Willing to relocate?"), PROFILE, [])
    assert plan["value"] == "No"


# --------------------------------------------------------------------------- #
# The locked firewall
# --------------------------------------------------------------------------- #
def test_eligibility_answers_the_jurisdiction_actually_asked_about():
    ie = af.plan_field(
        field(label="Are you authorised to work in Ireland?"), PROFILE, []
    )
    us = af.plan_field(
        field(label="Are you authorized to work in the United States?"), PROFILE, []
    )
    assert ie["value"] == "Yes"
    assert us["value"] == "No"
    assert ie["locked_as"] == us["locked_as"] == "work_eligibility"


def test_sponsorship_reads_the_sponsorship_key_not_the_authorisation_one():
    plan = af.plan_field(
        field(label="Will you require visa sponsorship in Ireland?"), PROFILE, []
    )
    # Authorised in IE but needing no sponsorship: answering this from `authorized`
    # would say "Yes" and be exactly backwards.
    assert plan["value"] == "No"
    assert plan["source"] == "eligibility.IE.sponsorship_required"


def test_an_ambiguous_jurisdiction_is_blocked_not_guessed():
    plan = af.plan_field(
        field(label="Are you authorised to work in the UK or Ireland?"), PROFILE, []
    )
    assert plan["action"] == af.ACTION_BLOCKED
    assert plan["tier"] == ab.Tier.BLOCKED.value


def test_an_unconfigured_jurisdiction_escalates():
    plan = af.plan_field(
        field(label="Are you authorised to work in Canada?"), PROFILE, []
    )
    assert plan["action"] == af.ACTION_ASK
    assert "CA" in plan["reason"]


def test_a_locked_question_never_falls_through_to_the_alias_map():
    """The ordering guarantee. ``match_field`` does a generous containment pass, so a
    locked question reaching Tier A at all is the bug this test exists to catch."""
    plan = af.plan_field(
        field(label="What country are you legally authorised to work in?"), PROFILE, []
    )
    # "country" is a profile alias; resolving this to profile.country would answer a
    # legal question with a residence fact.
    assert plan["source"] != "profile.country"
    assert plan["value"] != "Ireland"


def test_demographics_escalate_rather_than_defaulting_for_the_user():
    plan = af.plan_field(field(label="What is your gender?"), PROFILE, [])
    assert plan["action"] == af.ACTION_ASK
    assert plan["locked_as"] == "demographics"


def test_a_locked_question_answered_before_is_reused_by_exact_match():
    entries = [bank("What is your gender?", "Prefer not to say", locked=True)]
    plan = af.plan_field(field(label="What is your gender?"), PROFILE, entries)
    assert plan["action"] == af.ACTION_FILL
    assert plan["value"] == "Prefer not to say"


# --------------------------------------------------------------------------- #
# Tier B — the bank
# --------------------------------------------------------------------------- #
def test_a_banked_answer_fills_a_question_the_profile_cannot():
    entries = [bank("How many years of Python experience do you have?", "6")]
    plan = af.plan_field(
        field(label="How many years of Python experience do you have?"), PROFILE, entries
    )
    assert plan["tier"] == ab.Tier.BANK_EXACT.value
    assert plan["value"] == "6"
    assert plan["source_id"] == "row-1"


def test_an_employer_scoped_answer_is_not_reused_at_another_employer():
    entries = [bank("Why do you want to work here?", "I love Acme", scope="employer:acme")]
    plan = af.plan_field(
        field(label="Why do you want to work here?"),
        PROFILE,
        entries,
        scope="employer:globex",
    )
    assert plan["action"] == af.ACTION_ASK


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #
YES_NO = [{"value": "1", "label": "Yes"}, {"value": "0", "label": "No"}]


def test_a_boolean_lands_on_the_matching_option():
    plan = af.plan_field(
        field(label="Are you authorised to work in Ireland?", type="select", options=YES_NO),
        PROFILE,
        [],
    )
    assert plan["action"] == af.ACTION_FILL
    assert plan["option_value"] == "1"


def test_a_wordy_option_still_matches_by_prefix():
    options = [
        {"value": "a", "label": "Yes, I am authorised to work in Ireland"},
        {"value": "b", "label": "No, I would require sponsorship"},
    ]
    plan = af.plan_field(
        field(label="Are you authorised to work in Ireland?", type="select", options=options),
        PROFILE,
        [],
    )
    assert plan["option_value"] == "a"


def test_two_yes_flavoured_options_escalate_rather_than_choose():
    options = [
        {"value": "a", "label": "Yes, immediately"},
        {"value": "b", "label": "Yes, with notice"},
        {"value": "c", "label": "No"},
    ]
    plan = af.plan_field(
        field(label="Are you authorised to work in Ireland?", type="select", options=options),
        PROFILE,
        [],
    )
    assert plan["action"] == af.ACTION_ASK


def test_a_stored_value_no_option_carries_escalates():
    options = [
        {"value": "i", "label": "Immediately"},
        {"value": "m", "label": "1 month"},
        {"value": "q", "label": "3 months"},
    ]
    plan = af.plan_field(
        field(label="What is your notice period?", type="select", options=options),
        PROFILE,
        [],
    )
    assert plan["action"] == af.ACTION_ASK
    assert "not one of the options" in plan["reason"]


# --------------------------------------------------------------------------- #
# Fields that are not questions
# --------------------------------------------------------------------------- #
def test_a_cv_upload_is_an_attachment_not_a_question():
    plan = af.plan_field(field(label="Upload your résumé", type="file"), PROFILE, [])
    assert plan["action"] == af.ACTION_ATTACH_CV


def test_a_cover_letter_upload_is_left_to_the_user():
    plan = af.plan_field(field(label="Cover letter", type="file"), PROFILE, [])
    assert plan["action"] == af.ACTION_ASK


def test_a_captcha_stops_rather_than_being_worked_around():
    plan = af.plan_field(field(label="Enter the captcha text"), PROFILE, [])
    assert plan["action"] == af.ACTION_BLOCKED
    # Its answer is a one-time random string; banking it is noise that can never match.
    assert plan["bankable"] is False


def test_a_blocked_question_is_still_worth_banking_once_answered():
    """The distinction captchas force. An ambiguous eligibility question blocks, but the
    answer the user gives to it resolves that exact wording by exact match forever after —
    so unlike a captcha it must stay bankable."""
    plan = af.plan_field(
        field(label="Are you authorised to work in the UK or Ireland?"), PROFILE, []
    )
    assert plan["action"] == af.ACTION_BLOCKED
    assert plan["bankable"] is True


def test_a_password_field_is_never_touched():
    plan = af.plan_field(field(label="Create a password", type="password"), PROFILE, [])
    assert plan["action"] == af.ACTION_SKIP


# --------------------------------------------------------------------------- #
# Whole-form summary
# --------------------------------------------------------------------------- #
def test_summary_counts_what_the_panel_reports():
    fields = [
        field(ref="a", label="First Name"),
        field(ref="b", label="Email", type="email"),
        field(ref="c", label="How did you hear about us?"),
        field(ref="d", label="Upload CV", type="file"),
        field(ref="e", label="Create a password", type="password"),
        field(ref="f", label="Portfolio", required=True),
    ]
    plan = af.plan_form(fields, PROFILE, [])
    summary = plan["summary"]
    assert summary["total"] == 5  # the password field is skipped, not counted
    assert summary["from_profile"] == 2
    assert summary["attachments"] == 1
    assert summary["asked"] == 2
    assert summary["required_gaps"] == 1  # only the portfolio gap is marked required


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #
def test_employer_scope_prefers_the_board_slug_over_the_display_name():
    assert af.employer_scope("acme-inc", "Acme, Inc.") == "employer:acme-inc"
    assert af.employer_scope(None, "Acme, Inc.") == "employer:acme-inc"
    assert af.employer_scope(None, None) == "global"


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Why do you want to work here?", True),
        ("What interests you about this role at Acme?", True),
        ("How did you hear about us?", True),
        ("How many years of Python experience do you have?", False),
        ("What is your notice period?", False),
    ],
)
def test_employer_specific_questions_are_recognised(question, expected):
    assert af.is_employer_specific(question, "Acme") is expected


# --------------------------------------------------------------------------- #
# Learning from a submitted form
# --------------------------------------------------------------------------- #
def test_a_newly_typed_answer_is_banked():
    banked = af.bankable_answers(
        [{"question": "Do you hold a current SC clearance?", "value": "No", "kind": "select"}]
    )
    assert len(banked) == 1
    assert banked[0]["answer_value"] == "No"
    assert banked[0]["scope"] == "global"
    assert banked[0]["locked_as"] == "security_clearance"


def test_an_unedited_bank_hit_is_a_reuse_not_a_rewrite():
    answers = [
        {
            "question": "Years of Python?",
            "value": "6",
            "tier": ab.Tier.BANK_EXACT.value,
            "source_id": "row-1",
            "edited": False,
        }
    ]
    assert af.bankable_answers(answers) == []
    assert af.used_answer_ids(answers) == ["row-1"]


def test_a_corrected_bank_hit_overwrites_and_is_not_counted_as_reuse():
    answers = [
        {
            "question": "Years of Python?",
            "value": "8",
            "tier": ab.Tier.BANK_EXACT.value,
            "source_id": "row-1",
            "edited": True,
        }
    ]
    assert af.bankable_answers(answers)[0]["answer_value"] == "8"
    assert af.used_answer_ids(answers) == []


def test_profile_facts_are_not_duplicated_into_the_bank():
    answers = [
        {"question": "Email Address", "value": "k@example.com", "tier": ab.Tier.PROFILE.value},
    ]
    assert af.bankable_answers(answers) == []


def test_placeholder_values_are_not_answers():
    answers = [
        {"question": "Country", "value": "— Select —"},
        {"question": "Notice period", "value": "   "},
    ]
    assert af.bankable_answers(answers) == []


def test_an_employer_question_is_banked_under_that_employer():
    banked = af.bankable_answers(
        [{"question": "Why do you want to work here?", "value": "Because…"}],
        scope="employer:acme",
        company="Acme",
    )
    assert banked[0]["scope"] == "employer:acme"


def test_an_ordinary_question_stays_global_even_on_an_employer_form():
    banked = af.bankable_answers(
        [{"question": "How many years of Python experience do you have?", "value": "6"}],
        scope="employer:acme",
        company="Acme",
    )
    assert banked[0]["scope"] == "global"
