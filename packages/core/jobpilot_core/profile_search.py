"""Profile-aware search — derive scout query terms from the user's parsed resume.

Scouting used to be driven purely by the manually-configured ``SearchConfig``; the
resume was only consulted later, by the Qualifier. This module lets Scout retrieve
jobs that match the user's actual profile: it turns the parsed resume into
roles / keywords / locations and **merges** them with the configured terms.

Deterministic (no LLM): job titles come from the resume's experience, keywords from
its skills, locations from its contact block. The user's explicit config is always
preserved — profile terms only *add* coverage (a union, capped), never overwrite.
"""

from __future__ import annotations

import re

# Words that mark a resume line as an actual job title (filters parser noise so we
# don't treat a stray sentence as a "role"). Kept broad but role-shaped.
_ROLE_HINTS = {
    "engineer", "developer", "architect", "manager", "analyst", "scientist",
    "designer", "lead", "consultant", "administrator", "specialist", "director",
    "programmer", "devops", "sre", "administrator", "administrador", "administration",
    "researcher", "technician", "coordinator", "strategist", "product", "data",
    "software", "frontend", "backend", "fullstack", "full-stack", "platform",
    "security", "cloud", "infrastructure", "qa", "test", "machine", "ml",
}

_SENIORITY = {
    "senior": ("senior", "sr.", "sr ", "lead", "staff", "principal", "head", "director"),
    "junior": ("junior", "jr.", "jr ", "graduate", "intern", "trainee", "entry"),
}

# A title line often trails company / dates / location after a separator; keep the
# role phrase before the first of these.
_TITLE_SEPARATORS = re.compile(r"\s*(?:[|@]|,|\bat\b|–|—|-\s|\(|/)\s*", re.IGNORECASE)


def _clean_term(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip(" .,-|·•\t")).strip()


def _looks_like_role(phrase: str) -> bool:
    words = {w for w in re.split(r"\W+", phrase.lower()) if w}
    return bool(words) and len(phrase) <= 60 and bool(words & _ROLE_HINTS)


def _role_from_title(title: str | None) -> str | None:
    """Extract the role phrase from a (possibly messy) experience title line."""
    if not title:
        return None
    head = _TITLE_SEPARATORS.split(title, maxsplit=1)[0]
    head = _clean_term(head)
    # Drop stray leading years / bullets already handled by the parser; require a
    # role-shaped phrase so we don't emit sentences or company names.
    if head and _looks_like_role(head):
        return head
    return None


def infer_seniority(resume: dict) -> str | None:
    """Coarse seniority from the resume's titles (senior/junior/None)."""
    titles = " ".join(
        (e.get("title") or "") for e in (resume.get("experience") or [])
    ).lower()
    for level, needles in _SENIORITY.items():
        if any(n in titles for n in needles):
            return level
    return None


def _dedupe_ci(items: list[str], cap: int) -> list[str]:
    """Case-insensitive de-dupe, order-preserving, capped."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        it = _clean_term(it)
        key = it.lower()
        if it and key not in seen:
            seen.add(key)
            out.append(it)
        if len(out) >= cap:
            break
    return out


def derive_search_terms(
    resume: dict | None, *, max_roles: int = 8, max_keywords: int = 12
) -> dict:
    """Derive ``{roles, keywords, locations, seniority}`` from a parsed resume.

    Empty/None resume yields empty lists. Deterministic and side-effect free."""
    resume = resume or {}
    roles: list[str] = []
    for exp in resume.get("experience") or []:
        role = _role_from_title(exp.get("title"))
        if role:
            roles.append(role)

    keywords = [
        s for s in (resume.get("skills") or [])
        if s and len(_clean_term(s)) <= 40
    ]

    locations: list[str] = []
    contact = resume.get("contact") or {}
    if contact.get("location"):
        locations.append(_clean_term(contact["location"]))

    return {
        "roles": _dedupe_ci(roles, max_roles),
        "keywords": _dedupe_ci(keywords, max_keywords),
        "locations": _dedupe_ci(locations, 4),
        "seniority": infer_seniority(resume),
    }


def merge_search_terms(
    config: dict, derived: dict, *, max_roles: int = 8, max_keywords: int = 12
) -> dict:
    """Union the configured terms with profile-derived ones — config first so the
    user's explicit choices lead, profile terms extend coverage. Locations fall back
    to the profile only when none are configured (a single location drives the
    aggregator query, so we don't dilute an explicit one)."""
    roles = _dedupe_ci(list(config.get("roles") or []) + list(derived.get("roles") or []), max_roles)
    keywords = _dedupe_ci(
        list(config.get("keywords") or []) + list(derived.get("keywords") or []), max_keywords
    )
    locations = list(config.get("locations") or []) or list(derived.get("locations") or [])
    return {"roles": roles, "keywords": keywords, "locations": locations}
