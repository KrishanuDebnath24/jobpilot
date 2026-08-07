"""The user's pre-configured application facts, and the canonical form-field map.

Stage 1 of the apply agent. Two responsibilities:

* :func:`seed_from_resume` — lift what the résumé parser already extracted (name, email,
  phone, location, links) into a draft profile, so a user confirms rather than retypes.
  Seeding never overwrites a value the user has set; it only fills blanks.
* :data:`FIELD_ALIASES` / :func:`match_field` — Tier A of the resolution ladder. An ATS
  form field whose label or ``autocomplete`` attribute matches a known alias resolves to
  a named profile key deterministically, with no model and no embedding involved.

Everything here is pure: no I/O, no session, no LLM. That is what makes it testable and
what keeps the honesty guarantee cheap to verify.
"""

from __future__ import annotations

import re
from typing import Any

# --------------------------------------------------------------------------- #
# Canonical fields
# --------------------------------------------------------------------------- #
#: Profile columns an ATS field may resolve to (Tier A targets).
PROFILE_FIELDS: tuple[str, ...] = (
    "first_name", "last_name", "email", "phone",
    "address_line1", "city", "region", "postcode", "country",
    "linkedin_url", "github_url", "portfolio_url",
    "notice_period_days", "earliest_start_date",
    "salary_expectation", "salary_currency",
    "willing_to_relocate", "work_mode_preference",
)

#: Locked categories never resolve by similarity — exact configured key or escalate.
#: Kept here (not in ``answer_bank``) because the profile owns the underlying values.
LOCKED_PROFILE_KEYS: frozenset[str] = frozenset({"eligibility_json", "demographics_json"})

#: Label/attribute aliases → profile key. Lowercased, punctuation-stripped on both sides.
#: Ordered longest-first at match time so "first name" cannot be swallowed by "name".
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "first_name": ("first name", "given name", "forename", "fname", "firstname"),
    "last_name": ("last name", "surname", "family name", "lname", "lastname"),
    "email": ("email", "email address", "e mail", "your email"),
    "phone": ("phone", "phone number", "mobile", "telephone", "contact number", "tel"),
    "address_line1": ("address", "street address", "address line 1"),
    "city": ("city", "town", "city or town"),
    "region": ("state", "province", "county", "region", "state province"),
    "postcode": ("postcode", "postal code", "zip", "zip code", "eircode"),
    "country": ("country", "country of residence"),
    "linkedin_url": ("linkedin", "linkedin url", "linkedin profile", "linkedin profile url"),
    "github_url": ("github", "github url", "github profile"),
    "portfolio_url": ("portfolio", "website", "personal website", "portfolio url", "personal site"),
    "notice_period_days": ("notice period", "what is your notice period"),
    "earliest_start_date": ("start date", "earliest start date", "availability date", "when can you start"),
    "salary_expectation": ("salary expectation", "expected salary", "desired salary", "salary requirements"),
    "salary_currency": ("salary currency", "currency"),
    "willing_to_relocate": ("willing to relocate", "open to relocation", "relocate"),
    "work_mode_preference": ("work preference", "remote preference", "work arrangement", "working model"),
}

#: HTML ``autocomplete`` tokens are an unambiguous signal when a form bothers to set them.
AUTOCOMPLETE_MAP: dict[str, str] = {
    "given-name": "first_name",
    "family-name": "last_name",
    "email": "email",
    "tel": "phone",
    "tel-national": "phone",
    "street-address": "address_line1",
    "address-line1": "address_line1",
    "address-level2": "city",
    "address-level1": "region",
    "postal-code": "postcode",
    "country-name": "country",
    "url": "portfolio_url",
}

_PUNCT = re.compile(r"[^a-z0-9\s]+")
_WS = re.compile(r"\s+")


def normalise_label(text: str | None) -> str:
    """Lowercase, strip punctuation and collapse whitespace.

    Also drops a trailing required-marker asterisk and any parenthetical, so
    "Email Address * (required)" and "email address" reduce to the same key.
    """
    if not text:
        return ""
    s = text.strip().lower()
    s = re.sub(r"\((?:[^)]*)\)", " ", s)      # drop parentheticals
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


def match_field(label: str | None, autocomplete: str | None = None) -> str | None:
    """Tier A: resolve a form field to a profile key, or ``None`` to fall through.

    ``autocomplete`` wins when present — it is machine-declared intent rather than a
    human-readable string we are pattern-matching. Alias matching is exact against the
    normalised label first, then a longest-alias containment pass so "Your email address"
    still resolves while "first name" is never shadowed by the shorter "name".
    """
    if autocomplete:
        token = normalise_label(autocomplete).replace(" ", "-")
        if token in AUTOCOMPLETE_MAP:
            return AUTOCOMPLETE_MAP[token]

    norm = normalise_label(label)
    if not norm:
        return None

    for key, aliases in FIELD_ALIASES.items():
        if norm in aliases:
            return key

    # Containment fallback, longest alias first so specific beats generic.
    ranked = sorted(
        ((alias, key) for key, aliases in FIELD_ALIASES.items() for alias in aliases),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    for alias, key in ranked:
        if alias in norm:
            return key
    return None


# --------------------------------------------------------------------------- #
# Seeding from the parsed résumé
# --------------------------------------------------------------------------- #
_LINK_PATTERNS: tuple[tuple[str, str], ...] = (
    ("linkedin_url", "linkedin.com"),
    ("github_url", "github.com"),
)


def _split_name(full: str | None) -> tuple[str | None, str | None]:
    """Best-effort split of a single ``name`` string into first/last.

    The résumé schema stores one ``name``; ATS forms almost always want two fields. A
    single token yields a first name and no surname rather than guessing one.
    """
    if not full or not full.strip():
        return None, None
    parts = [p for p in full.strip().split() if p]
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])


def _split_location(loc: str | None) -> tuple[str | None, str | None]:
    """``"Dublin, Ireland"`` → ``("Dublin", "Ireland")``. Unsplittable input → city only."""
    if not loc or not loc.strip():
        return None, None
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], parts[-1]


def seed_from_resume(structured: dict[str, Any] | None) -> dict[str, Any]:
    """Derive profile fields from a parsed résumé's ``contact`` block.

    Returns only the keys it could genuinely populate — callers merge this over blanks.
    Nothing is inferred: every value here is copied from text the parser read off the
    user's own document.
    """
    contact = (structured or {}).get("contact") or {}
    seeded: dict[str, Any] = {}

    first, last = _split_name(contact.get("name"))
    if first:
        seeded["first_name"] = first
    if last:
        seeded["last_name"] = last
    if contact.get("email"):
        seeded["email"] = contact["email"]
    if contact.get("phone"):
        seeded["phone"] = contact["phone"]

    city, country = _split_location(contact.get("location"))
    if city:
        seeded["city"] = city
    if country:
        seeded["country"] = country

    for raw in contact.get("links") or []:
        url = str(raw).strip()
        if not url:
            continue
        low = url.lower()
        for key, needle in _LINK_PATTERNS:
            if needle in low and key not in seeded:
                seeded[key] = url
                break
        else:
            seeded.setdefault("portfolio_url", url)

    return seeded


def merge_seed(current: dict[str, Any], seeded: dict[str, Any]) -> dict[str, Any]:
    """Fill blanks in ``current`` from ``seeded``; never overwrite a user-set value.

    A re-upload of the résumé must not silently revert a correction the user made by
    hand, so a present value — including one deliberately set to empty string — wins.
    """
    out = dict(current)
    for key, value in seeded.items():
        if out.get(key) is None:
            out[key] = value
    return out


# --------------------------------------------------------------------------- #
# Eligibility (locked, per-jurisdiction)
# --------------------------------------------------------------------------- #
#: ISO-3166 alpha-2 plus the aliases ATS forms actually write.
JURISDICTION_ALIASES: dict[str, tuple[str, ...]] = {
    "IE": ("ireland", "republic of ireland", "roi", "ie"),
    "UK": ("united kingdom", "uk", "great britain", "britain", "gb", "england", "scotland", "wales"),
    "EU": ("european union", "eu", "eea", "european economic area"),
    "US": ("united states", "usa", "us", "united states of america", "america"),
    "CA": ("canada", "ca"),
    "AU": ("australia", "au"),
}


def detect_jurisdiction(question: str | None) -> str | None:
    """Extract the jurisdiction an eligibility question is asking about.

    Returns ``None`` when it cannot be determined with certainty — which the caller must
    treat as "escalate to the user", never as a default. This function existing at all is
    the guard against answering a US authorisation question with an Irish answer.
    """
    norm = normalise_label(question)
    if not norm:
        return None
    hits = {
        code
        for code, aliases in JURISDICTION_ALIASES.items()
        if any(re.search(rf"\b{re.escape(a)}\b", norm) for a in aliases)
    }
    # Ambiguous ("...in the UK or Ireland?") is not answerable from a single stored value.
    return hits.pop() if len(hits) == 1 else None


def eligibility_answer(profile_eligibility: dict, jurisdiction: str, key: str) -> Any | None:
    """Read one locked eligibility value, e.g. ``("IE", "authorized")``.

    Missing jurisdiction or missing key both return ``None`` so the caller escalates
    rather than assuming a permissive default.
    """
    block = (profile_eligibility or {}).get(jurisdiction)
    if not isinstance(block, dict):
        return None
    return block.get(key)
