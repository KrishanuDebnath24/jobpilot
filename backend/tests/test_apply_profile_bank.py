"""Stage 1 of the apply agent: pre-configured profile + answer bank.

Pure-logic tests — no DB, no HTTP, no model. Everything here guards the one invariant
the feature rests on: a value submitted on an application traces to something the user
configured or confirmed, and a question we cannot answer that way escalates instead of
being guessed.

The locked-category tests are the important ones. They encode the failure mode that
would otherwise be catastrophic and silent: reusing an Irish work-authorisation answer
for a US question because the two sentences are nearly identical.
"""

from __future__ import annotations

import pytest
from jobpilot_core import answer_bank as ab
from jobpilot_core import apply_profile as ap


# --------------------------------------------------------------------------- #
# Canonical field map (Tier A)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "label,expected",
    [
        ("First Name", "first_name"),
        ("Given name *", "first_name"),
        ("Surname", "last_name"),
        ("Email Address", "email"),
        ("Email Address * (required)", "email"),
        ("Mobile", "phone"),
        ("Eircode", "postcode"),
        ("LinkedIn Profile URL", "linkedin_url"),
        ("What is your notice period?", "notice_period_days"),
        ("Expected Salary", "salary_expectation"),
    ],
)
def test_match_field_resolves_common_labels(label, expected):
    assert ap.match_field(label) == expected


def test_autocomplete_beats_label():
    """Machine-declared intent wins over a human string we are pattern-matching."""
    assert ap.match_field("Your details", autocomplete="given-name") == "first_name"


def test_specific_alias_not_shadowed_by_generic():
    """"first name" must not be swallowed by the shorter, more generic "name"."""
    assert ap.match_field("Legal first name") == "first_name"
    assert ap.match_field("Legal last name") == "last_name"


def test_unknown_label_returns_none_rather_than_guessing():
    assert ap.match_field("Describe a time you showed leadership") is None
    assert ap.match_field("") is None
    assert ap.match_field(None) is None


# --------------------------------------------------------------------------- #
# Seeding from the parsed résumé
# --------------------------------------------------------------------------- #
RESUME = {
    "contact": {
        "name": "Krishanu Ghosh",
        "email": "k@example.com",
        "phone": "+353 87 000 0000",
        "location": "Dublin, Ireland",
        "links": [
            "https://linkedin.com/in/example",
            "https://github.com/example",
            "https://example.dev",
        ],
    }
}


def test_seed_lifts_contact_block():
    seeded = ap.seed_from_resume(RESUME)
    assert seeded["first_name"] == "Krishanu"
    assert seeded["last_name"] == "Ghosh"
    assert seeded["email"] == "k@example.com"
    assert seeded["city"] == "Dublin"
    assert seeded["country"] == "Ireland"
    assert seeded["linkedin_url"] == "https://linkedin.com/in/example"
    assert seeded["github_url"] == "https://github.com/example"
    assert seeded["portfolio_url"] == "https://example.dev"


def test_seed_handles_single_token_name_without_inventing_a_surname():
    seeded = ap.seed_from_resume({"contact": {"name": "Prince"}})
    assert seeded["first_name"] == "Prince"
    assert "last_name" not in seeded


def test_seed_tolerates_missing_contact():
    assert ap.seed_from_resume(None) == {}
    assert ap.seed_from_resume({}) == {}
    assert ap.seed_from_resume({"contact": {}}) == {}


def test_merge_seed_never_overwrites_a_user_set_value():
    """A résumé re-upload must not revert a correction the user typed by hand."""
    current = {"first_name": "Kris", "email": None, "city": ""}
    merged = ap.merge_seed(current, ap.seed_from_resume(RESUME))
    assert merged["first_name"] == "Kris"       # user's value survives
    assert merged["email"] == "k@example.com"   # blank gets filled
    assert merged["city"] == ""                 # deliberate empty string is a value


# --------------------------------------------------------------------------- #
# Jurisdiction detection — the guard against cross-country answer reuse
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "question,expected",
    [
        ("Are you legally authorized to work in Ireland?", "IE"),
        ("Are you legally authorized to work in the United States?", "US"),
        ("Do you have the right to work in the UK?", "UK"),
        ("Are you eligible to work in the EU?", "EU"),
    ],
)
def test_detect_jurisdiction(question, expected):
    assert ap.detect_jurisdiction(question) == expected


def test_ambiguous_jurisdiction_is_unanswerable():
    """Two countries in one question cannot be answered from a single stored value."""
    assert ap.detect_jurisdiction("Are you authorized to work in the UK or Ireland?") is None


def test_absent_jurisdiction_returns_none():
    assert ap.detect_jurisdiction("Are you authorized to work?") is None


def test_eligibility_lookup_is_per_jurisdiction():
    elig = {
        "IE": {"authorized": True, "sponsorship_required": False},
        "US": {"authorized": False, "sponsorship_required": True},
    }
    assert ap.eligibility_answer(elig, "IE", "authorized") is True
    assert ap.eligibility_answer(elig, "US", "authorized") is False
    # Unconfigured jurisdiction must not fall back to a permissive default.
    assert ap.eligibility_answer(elig, "CA", "authorized") is None
    assert ap.eligibility_answer(elig, "IE", "visa_type") is None


# --------------------------------------------------------------------------- #
# Locked categories
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "question,category",
    [
        ("Are you legally authorized to work in Ireland?", "work_eligibility"),
        ("Will you now or in the future require sponsorship?", "work_eligibility"),
        ("Have you ever been convicted of a criminal offence?", "criminal_record"),
        ("Do you hold an active security clearance?", "security_clearance"),
        ("Do you have a disability?", "health_disability"),
        ("What is your gender?", "demographics"),
        ("Are you a protected veteran?", "demographics"),
    ],
)
def test_sensitive_questions_are_locked(question, category):
    assert ab.locked_category(question) == category
    assert ab.is_locked(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "What is your notice period?",
        "Expected salary?",
        "Why do you want to work here?",
        "How many years of Python experience do you have?",
    ],
)
def test_ordinary_questions_are_not_locked(question):
    assert ab.is_locked(question) is False


# --------------------------------------------------------------------------- #
# Question normalisation
# --------------------------------------------------------------------------- #
def test_normalise_collapses_wording_variants():
    a = ab.normalise_question("Please tell us — what is your notice period? *")
    b = ab.normalise_question("What is your notice period")
    assert a == b != ""


def test_normalise_drops_parentheticals():
    assert ab.normalise_question("Email Address (required)") == ab.normalise_question("email address")


# --------------------------------------------------------------------------- #
# Resolution ladder
# --------------------------------------------------------------------------- #
def _entry(qid, question, answer, **kw):
    return {
        "id": qid,
        "question_norm": ab.normalise_question(question),
        "answer_value": answer,
        "scope": kw.get("scope", "global"),
        "locked": kw.get("locked", False),
        "question_vec": kw.get("vec"),
    }


def test_exact_match_returns_banked_answer():
    entries = [_entry("e1", "What is your notice period?", "30 days")]
    r = ab.resolve_question("Please tell us, what is your notice period? *", entries)
    assert r.tier is ab.Tier.BANK_EXACT
    assert r.value == "30 days"
    assert r.source_id == "e1"


def test_unknown_question_escalates_rather_than_guessing():
    r = ab.resolve_question("Describe your proudest project", [])
    assert r.tier is ab.Tier.ESCALATE
    assert r.value is None


def test_employer_scope_beats_global():
    entries = [
        _entry("g", "Why do you want to work here?", "Generic answer"),
        _entry("e", "Why do you want to work here?", "Stripe-specific", scope="employer:stripe"),
    ]
    r = ab.resolve_question("Why do you want to work here?", entries, scope="employer:stripe")
    assert r.value == "Stripe-specific"


def test_other_employers_scoped_answer_is_never_reused():
    entries = [_entry("e", "Why do you want to work here?", "Stripe-specific", scope="employer:stripe")]
    r = ab.resolve_question("Why do you want to work here?", entries, scope="employer:intercom")
    assert r.tier is ab.Tier.ESCALATE


# --- the firewall ---------------------------------------------------------- #
def test_locked_question_never_near_matches_across_jurisdictions():
    """The scenario the whole design exists to prevent.

    A stored answer for Ireland must not satisfy a US authorisation question, even
    though an embedder scores the two sentences as nearly identical.
    """
    ie = _entry(
        "ie",
        "Are you legally authorized to work in Ireland?",
        "Yes",
        locked=True,
        vec=[1.0, 0.0, 0.0],
    )
    # An embedder that thinks everything is the same — the adversarial case.
    always_similar = lambda _text: [1.0, 0.0, 0.0]  # noqa: E731

    r = ab.resolve_question(
        "Are you legally authorized to work in the United States?",
        [ie],
        embed=always_similar,
    )
    assert r.tier is ab.Tier.ESCALATE
    assert r.locked_as == "work_eligibility"
    assert r.value is None


def test_locked_question_still_resolves_on_exact_match():
    """Locking disables similarity, not the bank itself."""
    entries = [_entry("ie", "Are you legally authorized to work in Ireland?", "Yes", locked=True)]
    r = ab.resolve_question("Are you legally authorized to work in Ireland?", entries)
    assert r.tier is ab.Tier.BANK_EXACT
    assert r.value == "Yes"


def test_locked_row_is_never_a_near_match_donor():
    """Even an ordinary question must not pull its answer from a locked row."""
    locked_row = _entry("l", "Do you have a disability?", "Prefer not to say",
                        locked=True, vec=[1.0, 0.0, 0.0])
    r = ab.resolve_question(
        "How many years of experience do you have?",
        [locked_row],
        embed=lambda _t: [1.0, 0.0, 0.0],
    )
    assert r.tier is ab.Tier.ESCALATE


# --- near match on ordinary questions -------------------------------------- #
def test_near_match_above_threshold_reuses_answer():
    entries = [_entry("n", "How many years of Python experience?", "6", vec=[1.0, 0.0, 0.0])]
    r = ab.resolve_question(
        "Years of experience with Python?",
        entries,
        embed=lambda _t: [0.99, 0.14, 0.0],
    )
    assert r.tier is ab.Tier.BANK_NEAR
    assert r.value == "6"
    assert r.similarity >= ab.SIMILARITY_THRESHOLD


def test_near_match_below_threshold_escalates():
    entries = [_entry("n", "How many years of Python experience?", "6", vec=[1.0, 0.0, 0.0])]
    r = ab.resolve_question(
        "What is your favourite colour?",
        entries,
        embed=lambda _t: [0.0, 1.0, 0.0],
    )
    assert r.tier is ab.Tier.ESCALATE


def test_no_embedder_skips_near_match_without_error():
    """The near pass is optional; its absence must degrade to escalation, not crash."""
    entries = [_entry("n", "Years of Python?", "6", vec=[1.0, 0.0, 0.0])]
    r = ab.resolve_question("Something unrelated entirely", entries, embed=None)
    assert r.tier is ab.Tier.ESCALATE


def test_cosine_handles_zero_and_mismatched_vectors():
    assert ab.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert ab.cosine([1.0], [1.0, 0.0]) == 0.0
    assert ab.cosine([], []) == 0.0
    assert ab.cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Answer coercion
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,kind,expected",
    [
        ("Yes", "boolean", True),
        ("no", "boolean", False),
        ("30", "number", 30),
        ("2.5", "number", 2.5),
        ("30 days", "text", "30 days"),
        (None, "text", None),
    ],
)
def test_coerce_answer(raw, kind, expected):
    assert ab.coerce_answer(raw, kind) == expected


def test_coerce_returns_raw_rather_than_guessing_on_bad_input():
    """A malformed stored number is something to surface, not silently zero out."""
    assert ab.coerce_answer("not a number", "number") == "not a number"
    assert ab.coerce_answer("maybe", "boolean") == "maybe"
