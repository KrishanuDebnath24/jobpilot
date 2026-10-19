"""Does this job title match what the user is looking for?

The coarse pre-filter every board connector applies before a job is stored. It is
deliberately *coarse*: relevance is the Qualifier's job, scored against the full posting
with the user's résumé in hand. This only has a title, so its job is to keep obvious
noise out of the pool, not to decide what is worth applying to.

Two defects this module exists to fix, both found by reading a real pool rather than by
testing (the connectors previously each carried their own copy of the logic):

- **Substring matching.** ``"git" in "digital"`` is true, so the skill "Git" pulled in
  "eDiscovery/Digital Forensics" and a blockchain post at "Digital Technology Institute".
  "REST" matched "interest" and "forestry". Matching is now on word boundaries.

- **Seniority words used as match tokens.** The résumé deriver contributes roles like
  "Senior Software Engineer", which was split into ``{senior, software, engineer}`` — and
  ``senior`` matches "Senior Credit Controller", "Senior Field Sales Representative" and
  "Compliance Senior Associate". A grade word says nothing about what the job *is*, and
  the deriver already records seniority in its own field, so it is dropped here.

Together those took a real Workable run from 43 titles to 22, removing exactly the noise
and none of the engineering roles.

**Known limit, deliberately left to the Qualifier.** A bare role noun cannot separate
disciplines: "engineer" matches mechanical, civil, electrical and coffee-machine service
engineers as readily as software ones. Requiring a role's full phrase instead was measured
against the live feed and kept *zero* jobs — Irish postings say "Senior CloudOps Engineer",
not "Software Engineer" — so it would trade all the noise for all the signal. Discipline
disambiguation needs the posting body, which is the Qualifier's input, not ours.
"""

from __future__ import annotations

import re

#: Grade/seniority words. These describe *level*, never what the role is, so they must
#: never be the reason a job enters the pool. Kept out of match tokens entirely.
GRADE_WORDS: frozenset[str] = frozenset({
    "senior", "junior", "lead", "staff", "principal", "graduate", "intern", "trainee",
    "associate", "assistant", "entry", "mid", "intermediate", "experienced", "apprentice",
    "head", "chief", "deputy", "vice", "snr", "jnr", "grad",
})

#: Words too short to discriminate; also drops most prepositions and articles.
_MIN_TOKEN_LEN = 3


#: Administrative filler in location labels. Dropped from both sides of a comparison so
#: "Co. Dublin", "County Dublin" and "Dublin" are the same place — they are how three
#: different sources spell one county, and none of them narrows anything.
_LOCATION_NOISE: frozenset[str] = frozenset({"co", "county", "city", "area", "region"})


def _loc_words(text: str | None) -> set[str]:
    """Significant words of a location label, case-folded."""
    return {
        w for w in re.split(r"[^a-z0-9]+", (text or "").lower())
        if len(w) > 1 and w not in _LOCATION_NOISE
    }


def match_tokens(*groups: list[str] | None) -> set[str]:
    """The set of words from roles/keywords that a title may be matched on.

    Phrases are split, short words dropped, and grade words removed — so
    ``["Senior Software Engineer"], ["Python", "Git"]`` yields
    ``{"software", "engineer", "python", "git"}`` with ``senior`` excluded.
    """
    out: set[str] = set()
    for group in groups:
        for phrase in group or []:
            for word in re.split(r"\W+", (phrase or "").lower()):
                if len(word) >= _MIN_TOKEN_LEN and word not in GRADE_WORDS:
                    out.add(word)
    return out


def title_matches(title: str | None, tokens: set[str]) -> bool:
    """True when any token appears in ``title`` as a whole word.

    An empty token set matches everything: no search terms configured means the user has
    not narrowed the search, and silently returning nothing would be worse than broad.
    """
    if not tokens:
        return True
    text = (title or "").lower()
    if not text:
        return False
    return any(re.search(rf"\b{re.escape(tok)}\b", text) for tok in tokens)


def location_matches(location: str | None, locations: list[str] | None) -> bool:
    """True when the job's location satisfies any configured location.

    Matching is **per word**, not substring: a configured location matches when every
    significant word in it appears as a whole word in the job's location. Locations are
    hierarchical and inconsistently punctuated, so the substring test this replaced broke
    on ordinary data — ``"dublin, ireland" in "Dublin, County Dublin, Ireland"`` is False
    because of the intervening county, and a configured "Maynooth, Ireland" missed
    "Maynooth, Co. Kildare, Ireland" for the same reason. Word-set containment keeps the
    hierarchy working ("Dublin" still matches "Dublin, County Dublin, Ireland") without
    depending on how the source happened to order or punctuate the parts.

    Requiring *every* word is what keeps it a filter: "Maynooth Ireland" must not match a
    Dublin job just because both end in "Ireland".

    An empty list matches everything — note that this is why it cannot be the
    Republic-of-Ireland gate; see :mod:`jobpilot_core.roi_location`.
    """
    if not locations:
        return True
    have = _loc_words(location)
    if not have:
        return False
    for loc in locations:
        want = _loc_words(loc)
        if want and want <= have:
            return True
    return False
