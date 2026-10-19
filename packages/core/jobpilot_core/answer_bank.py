"""The learned half of pre-configured answers: questions the user has already answered.

ATS screening questions repeat heavily across employers, so answering one once should
answer it everywhere. This module owns how a stored answer is found again:

* exact match on a normalised question string, then
* near match by embedding cosine, above a deliberately high threshold — **but only for
  questions that are not in a locked category**.

The locked-category firewall is the point of the whole module. "Are you authorized to
work in the United States?" and "…in Ireland?" differ by one token and sit almost on top
of each other in embedding space, while having opposite correct answers for the same
person. Any similarity threshold low enough to be useful is also low enough to conflate
them, so sensitive questions are excluded from similarity entirely: they resolve by exact
key against the profile's per-jurisdiction record, or they escalate to the user.

Pure functions over plain data — the embedder is injected, so tests run without a model.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Sequence

from .apply_profile import normalise_label

#: Cosine floor for a near match. High on purpose: a false reuse writes a wrong answer
#: onto a legal declaration, whereas a miss merely asks the user a question.
SIMILARITY_THRESHOLD = 0.92

#: Answer types a form field can carry. Stored as text; this says how to read it back.
ANSWER_KINDS: tuple[str, ...] = ("text", "boolean", "select", "number", "date")


class Tier(str, Enum):
    """Which rung of the resolution ladder supplied a value."""

    PROFILE = "profile"        # A — canonical field map
    BANK_EXACT = "bank_exact"  # B — exact normalised question match
    BANK_NEAR = "bank_near"    # B — embedding near match (non-locked only)
    ESCALATE = "escalate"      # C — ask the user; never guessed
    BLOCKED = "blocked"        # D — hard stop (captcha, ambiguous jurisdiction, …)


# --------------------------------------------------------------------------- #
# Locked categories
# --------------------------------------------------------------------------- #
#: Question topics that must never be similarity-matched. Each entry is a set of
#: substrings; a normalised question containing any of them is locked.
LOCKED_CATEGORIES: dict[str, tuple[str, ...]] = {
    "work_eligibility": (
        "authorized to work", "authorised to work", "legally authorized", "legally authorised",
        "right to work", "eligible to work", "work permit", "work authorization",
        "work authorisation", "visa", "sponsorship", "sponsor", "immigration status",
    ),
    "criminal_record": (
        "criminal", "convicted", "conviction", "background check", "criminal record",
        "offence", "offense",
    ),
    "security_clearance": (
        # Bare "clearance" on purpose: the specific phrasings below miss the ones that
        # actually appear on forms — "Do you hold a current SC clearance?" contains none
        # of them. No ordinary screening question uses the word in another sense.
        "clearance", "vetting", "sc cleared", "dv cleared",
    ),
    "health_disability": (
        "disability", "disabled", "long term condition", "health condition",
        "reasonable adjustment", "reasonable accommodation",
    ),
    "demographics": (
        "gender", "ethnicity", "ethnic", "race", "veteran", "sexual orientation",
        "religion", "age range", "date of birth",
    ),
}


def locked_category(question: str | None) -> str | None:
    """Return the locked category a question falls into, or ``None`` if it is ordinary."""
    norm = normalise_label(question)
    if not norm:
        return None
    for category, needles in LOCKED_CATEGORIES.items():
        if any(needle in norm for needle in needles):
            return category
    return None


def is_locked(question: str | None) -> bool:
    """True when a question may not be resolved by similarity."""
    return locked_category(question) is not None


# --------------------------------------------------------------------------- #
# Normalisation & embedding text
# --------------------------------------------------------------------------- #
#: Politeness and lead-in wording that varies per ATS while the question underneath does
#: not. Stripped from both sides of a comparison, so what survives is the substance.
#: Multi-word phrases are listed before their prefixes so the longer form wins.
_FILLER = re.compile(
    r"\b("
    r"please|kindly|"
    r"we would like to know|we would like to hear|wed like to know|"
    r"let us know|tell us|"
    r"could you|can you|would you|do you|are you|have you|"
    r"in your own words|note that|if applicable|for this role|for this position"
    r")\b"
)


def normalise_question(question: str | None) -> str:
    """The exact-match key: label normalisation plus filler/politeness removal.

    "Please tell us — what is your notice period? *" and "What is your notice period"
    must collapse to the same key, or the bank never gets a hit on the wording variants
    every ATS invents for the same question.
    """
    norm = normalise_label(question)
    if not norm:
        return ""
    norm = _FILLER.sub(" ", norm)
    return re.sub(r"\s+", " ", norm).strip()


def question_embedding_text(question: str) -> str:
    """Text handed to the embedder. Mirrors the ``*_embedding_text`` helpers in
    :mod:`jobpilot_core.embeddings` so question vectors are built the same way as the
    rest of the system's."""
    return normalise_question(question) or (question or "").strip()


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity, guarding the zero-vector case the hashing embedder can emit."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Resolution:
    """One field's outcome. ``value`` is only meaningful when ``tier`` supplied one."""

    tier: Tier
    value: Any | None = None
    #: Bank row id / profile key the value came from — the audit trail for review.
    source_id: str | None = None
    similarity: float | None = None
    #: Why the field escalated or was blocked; shown verbatim in the review UI.
    reason: str | None = None
    locked_as: str | None = None


def resolve_question(
    question: str,
    entries: Sequence[dict],
    *,
    scope: str = "global",
    embed: Callable[[str], list[float]] | None = None,
    threshold: float = SIMILARITY_THRESHOLD,
) -> Resolution:
    """Tier B: find a banked answer for ``question``, or escalate.

    ``entries`` are the user's bank rows as dicts. Scope is honoured strictly — an
    employer-scoped answer ("why do you want to work here?") is never reused for a
    different employer, and a global answer is never shadowed by another employer's.

    Locked questions take the exact-match path only. ``embed`` is injected so tests can
    run deterministically without loading a sentence-transformers model; when it is
    ``None`` the near-match pass is simply skipped.
    """
    norm = normalise_question(question)
    if not norm:
        return Resolution(Tier.ESCALATE, reason="empty question text")

    category = locked_category(question)
    candidates = [e for e in entries if e.get("scope") in (scope, "global")]

    # --- exact ---------------------------------------------------------------
    # Prefer an entry scoped to this employer over the global fallback.
    exact = [e for e in candidates if e.get("question_norm") == norm]
    exact.sort(key=lambda e: 0 if e.get("scope") == scope else 1)
    if exact:
        hit = exact[0]
        return Resolution(
            Tier.BANK_EXACT,
            value=hit.get("answer_value"),
            source_id=str(hit.get("id")) if hit.get("id") else None,
            similarity=1.0,
            locked_as=category,
        )

    # --- locked stops here ---------------------------------------------------
    if category:
        return Resolution(
            Tier.ESCALATE,
            reason=(
                f"{category.replace('_', ' ')} question with no exact stored answer — "
                "similarity matching is disabled for this category"
            ),
            locked_as=category,
        )

    # --- near ----------------------------------------------------------------
    if embed is None:
        return Resolution(Tier.ESCALATE, reason="no stored answer for this question")

    probe = embed(question_embedding_text(question))
    best: tuple[float, dict] | None = None
    for entry in candidates:
        if entry.get("locked"):
            continue  # a locked row is never a near-match donor
        vec = entry.get("question_vec")
        if not vec:
            continue
        score = cosine(probe, vec)
        if best is None or score > best[0]:
            best = (score, entry)

    if best and best[0] >= threshold:
        score, hit = best
        return Resolution(
            Tier.BANK_NEAR,
            value=hit.get("answer_value"),
            source_id=str(hit.get("id")) if hit.get("id") else None,
            similarity=round(score, 4),
        )

    return Resolution(
        Tier.ESCALATE,
        reason="no stored answer for this question",
        similarity=round(best[0], 4) if best else None,
    )


def coerce_answer(value: str | None, kind: str) -> Any:
    """Read a stored text answer back as its declared type.

    Unparseable input returns the raw string rather than a guess — a malformed stored
    number is a thing to surface, not to silently coerce to zero.
    """
    if value is None:
        return None
    if kind == "boolean":
        low = value.strip().lower()
        if low in ("true", "yes", "y", "1"):
            return True
        if low in ("false", "no", "n", "0"):
            return False
        return value
    if kind == "number":
        try:
            return int(value) if "." not in value else float(value)
        except ValueError:
            return value
    return value
