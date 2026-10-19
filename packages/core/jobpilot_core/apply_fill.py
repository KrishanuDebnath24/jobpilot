"""Turn a scraped form into a filled-in plan — the A/B/C/D ladder over real fields.

Stage 3 of the apply agent, and the first place the two stores built in stage 1 are
actually spent. The browser extension scrapes an ATS form into plain field descriptors
and posts them here; this module decides, per field, whether a value exists that the user
has already stood behind. Nothing in this file talks to a browser, a model or a database:
the extension owns the DOM, config-mcp owns the session, and what is left is a pure
function from (form, profile, bank) to a plan. That is deliberate — the honesty guarantee
is only worth as much as the ease of testing it.

The ladder, in strict order per field:

* **Locked first.** Before anything else, a question in a locked category is routed to the
  per-jurisdiction eligibility record or escalated. Ordering matters: ``match_field``'s
  containment fallback is generous by design, and a work-eligibility question must never
  be able to fall into it. The firewall is only a firewall if it runs before the fuzzy
  step, not after.
* **A — profile.** ``apply_profile.match_field`` resolves label/autocomplete to a named
  profile key, deterministically.
* **B — bank.** Exact normalised-question match, then embedding near-match for ordinary
  questions only.
* **C — escalate.** No confident value: the field is left empty and flagged for the user.
  Whatever they type is banked on submit, so the question is Tier B forever after.
* **D — blocked.** A captcha, a password, or an eligibility question whose jurisdiction
  cannot be read off the text. Never worked around.

One rule that is easy to lose in a refactor: a resolved value that does not correspond to
an offered option **escalates rather than fills**. Writing "4 weeks" into a select that
only offers "Immediately / 1 month / 3 months" would either silently select nothing or
silently select the wrong thing, and both are worse than asking.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Callable, Iterable, Sequence

from . import answer_bank as ab
from . import apply_profile as ap


def _folded(text: str | None) -> str:
    """Normalised, with accents folded to ASCII.

    ``normalise_label`` strips anything outside ``[a-z0-9 ]``, which turns "résumé" into
    "r sum " and means a perfectly ordinary CV upload label matches nothing. Folding is
    done here rather than in ``normalise_label`` on purpose: that function derives
    ``question_norm``, the bank's stored primary key, and changing how a stored key is
    computed would orphan every row already written under the old rule.
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return ap.normalise_label(stripped)


def _mentions(haystack: str, needle: str) -> bool:
    """Whole-word containment, so "cv" does not match inside another word."""
    return re.search(rf"\b{re.escape(needle)}\b", haystack) is not None

#: What the extension should do with a field. Kept separate from :class:`answer_bank.Tier`
#: because the tier records *where a value came from* while the action records *what to do*
#: — a CV upload has an action but no tier, and an escalation has a tier but no value.
ACTION_FILL = "fill"
ACTION_ATTACH_CV = "attach_cv"
ACTION_ASK = "ask"
ACTION_SKIP = "skip"
ACTION_BLOCKED = "blocked"

#: Input types that carry no answer and must never be touched by an autofill.
_UNTOUCHABLE_TYPES = frozenset({"password", "hidden", "submit", "button", "reset", "image"})

#: Field types that take a file. Only a CV slot is ever populated, and only from a
#: document the user already has on file — cover-letter slots escalate.
_FILE_TYPES = frozenset({"file"})

#: Labels on a file input that mean "your CV". Anything else (cover letter, portfolio,
#: transcript) is left for the user: attaching the wrong document is a silent failure the
#: user would not notice until after submitting.
_CV_LABELS = (
    "resume", "cv", "curriculum vitae", "upload resume", "attach resume",
    "upload cv", "attach cv",
)

#: Anything captcha-shaped stops the run. Matched as substrings of the label or field name.
_CAPTCHA_MARKERS = ("captcha", "recaptcha", "hcaptcha", "i am not a robot", "im not a robot")

#: Questions whose answer is about *this employer* and must never be reused elsewhere.
#: "Why do you want to work here?" banked globally would be submitted, verbatim and
#: wrong, to the next twenty companies.
_EMPLOYER_SCOPED_PATTERNS = (
    "why do you want to work",
    "why are you interested",
    "why this company",
    "why us",
    "what interests you about",
    "why would you like to join",
    "what attracts you",
    "our mission",
    "our company",
    "our values",
    "our product",
    "how did you hear about",
)

#: Option labels meaning yes / no, for coercing a stored boolean onto a real select.
_YES = ("yes", "y", "true", "i am", "i do", "i have", "authorised", "authorized")
_NO = ("no", "n", "false", "i am not", "i do not", "i dont", "i have not", "i havent")


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #
def employer_scope(slug: str | None, company: str | None = None) -> str:
    """The bank scope key for one employer, or ``"global"`` when unidentifiable.

    Prefers the ATS board slug over the display name: ``apply_board_slug`` is what the
    platform itself calls the employer, whereas ``company`` varies per connector
    ("Stripe" / "Stripe, Inc." / "Stripe Payments Europe") and would split one employer's
    answers across several scopes.
    """
    key = (slug or "").strip().lower() or ap.normalise_label(company).replace(" ", "-")
    return f"employer:{key}" if key else "global"


def is_employer_specific(question: str | None, company: str | None = None) -> bool:
    """True when an answer to ``question`` is only valid for this employer.

    Matched against the *plain* normalised label as well as the bank's key, because
    ``normalise_question`` strips politeness lead-ins — "why **do you** want to work
    here" reduces to "why want to work here", and a pattern written the way a human
    would write it would then never fire.
    """
    plain = _folded(question)
    norm = ab.normalise_question(question)
    if not (plain or norm):
        return False
    if any(p in plain or p in norm for p in _EMPLOYER_SCOPED_PATTERNS):
        return True
    # A question naming the company is about the company, however it is phrased.
    company_norm = _folded(company)
    return bool(company_norm) and len(company_norm) > 2 and company_norm in plain


def scope_for(question: str | None, scope: str, company: str | None = None) -> str:
    """Where an answer to ``question`` should be banked: employer scope or global."""
    return scope if is_employer_specific(question, company) else "global"


# --------------------------------------------------------------------------- #
# Profile values to form-ready strings
# --------------------------------------------------------------------------- #
def profile_value(profile: dict, key: str) -> str | None:
    """Read one profile key back as the string a form field would carry.

    Returns ``None`` for anything unset, which the caller must escalate — a blank profile
    field is a question the user has not answered yet, not an empty answer.
    """
    raw = (profile or {}).get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool):
        return "Yes" if raw else "No"
    if key == "notice_period_days":
        # Stored as an integer; forms ask in prose. "30 days" reads correctly as free
        # text and still has a shot at matching an option like "30 days".
        return f"{raw} days"
    return str(raw)


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #
def _option_pairs(options: Iterable[Any] | None) -> list[tuple[str, str]]:
    """Normalise the extension's option list to ``[(value, label)]``.

    Accepts bare strings as well as ``{"value": ..., "label": ...}`` so a scraper that
    only found text nodes still works.
    """
    out: list[tuple[str, str]] = []
    for opt in options or []:
        if isinstance(opt, dict):
            value = str(opt.get("value", opt.get("label", "")))
            label = str(opt.get("label", opt.get("value", "")))
        else:
            value = label = str(opt)
        if label.strip() or value.strip():
            out.append((value, label))
    return out


def match_option(value: Any, options: Iterable[Any] | None) -> tuple[str, str] | None:
    """Find the offered option that means ``value``, or ``None`` to escalate.

    Exact normalised match first, then yes/no equivalence, then containment. Deliberately
    conservative: an option is only chosen when the match is unambiguous, because a
    near-miss here silently submits an answer the user never gave.
    """
    pairs = _option_pairs(options)
    if not pairs:
        return None
    wanted = ap.normalise_label(str(value))
    if not wanted:
        return None

    for pair in pairs:
        if ap.normalise_label(pair[1]) == wanted or ap.normalise_label(pair[0]) == wanted:
            return pair

    # Boolean equivalence: a stored "Yes" must land on an option reading "I am authorised".
    family = _YES if wanted in _YES else _NO if wanted in _NO else None
    if family:
        hits = [p for p in pairs if ap.normalise_label(p[1]) in family]
        if len(hits) == 1:
            return hits[0]
        # Prefix match ("Yes, I am authorised to work...") — only when exactly one option
        # starts that way, so "Yes" never has to choose between two yes-flavoured options.
        prefixed = [
            p for p in pairs
            if any(ap.normalise_label(p[1]).startswith(f) for f in family)
        ]
        return prefixed[0] if len(prefixed) == 1 else None

    contained = [p for p in pairs if wanted in ap.normalise_label(p[1])]
    return contained[0] if len(contained) == 1 else None


# --------------------------------------------------------------------------- #
# Field descriptors
# --------------------------------------------------------------------------- #
def _label_of(field: dict) -> str:
    """The human question for a field: its label, falling back to placeholder/name.

    ``name`` is the last resort and often machine-ish ("job_application[answers][3]"),
    but a bad label only ever costs a match — it can never produce a wrong one, because
    every downstream comparison is against the same string.
    """
    for key in ("label", "aria_label", "placeholder", "name"):
        text = (field.get(key) or "").strip()
        if text:
            return text
    return ""


def _is_captcha(field: dict) -> bool:
    haystack = _folded(f"{_label_of(field)} {field.get('name') or ''}")
    return any(marker in haystack for marker in _CAPTCHA_MARKERS)


def _is_cv_field(field: dict) -> bool:
    norm = _folded(f"{_label_of(field)} {field.get('name') or ''}")
    return any(_mentions(norm, needle) for needle in _CV_LABELS)


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def _entry(field: dict, action: str, **extra: Any) -> dict:
    """One planned field, as the extension consumes it."""
    out: dict[str, Any] = {
        "ref": field.get("ref"),
        "label": _label_of(field),
        "type": (field.get("type") or "text").lower(),
        "required": bool(field.get("required")),
        "action": action,
        "value": None,
        "option_value": None,
        "tier": None,
        "source": None,
        "source_id": None,
        "similarity": None,
        "locked_as": None,
        "reason": None,
        # Whether an answer to this field is worth remembering. A blocked *question* is
        # very much worth banking once the user answers it — "are you authorised to work
        # in the UK or Ireland?" resolves by exact match forever after. A captcha is not:
        # its answer is a one-time random string, and banking it would fill the bank with
        # noise that can never match anything again.
        "bankable": True,
    }
    out.update(extra)
    return out


def _resolved_fill(field: dict, value: Any, **extra: Any) -> dict:
    """Turn a resolved value into a fill, or into an ask when no option carries it."""
    options = field.get("options")
    if options:
        chosen = match_option(value, options)
        if chosen is None:
            return _entry(
                field,
                ACTION_ASK,
                reason=(
                    f"stored answer {str(value)!r} is not one of the options this form "
                    "offers — pick the right one and it will be re-banked"
                ),
                **extra,
            )
        return _entry(field, ACTION_FILL, value=chosen[1], option_value=chosen[0], **extra)
    return _entry(field, ACTION_FILL, value=str(value), **extra)


def _plan_locked(field: dict, question: str, category: str, profile: dict) -> dict | None:
    """Locked-category routing. Returns ``None`` to fall through to the bank's exact pass.

    Only work eligibility has a structured home in the profile. Demographics escalate
    unless the exact question has been answered before: the profile default is "prefer
    not to say", and choosing that on the user's behalf is still choosing.
    """
    if category != "work_eligibility":
        return None

    jurisdiction = ap.detect_jurisdiction(question)
    if jurisdiction is None:
        return _entry(
            field,
            ACTION_BLOCKED,
            locked_as=category,
            tier=ab.Tier.BLOCKED.value,
            reason=(
                "this asks about work eligibility but names no single country, so no "
                "stored answer can be known to apply to it — answer this one yourself"
            ),
        )

    norm = ab.normalise_question(question)
    key = (
        "sponsorship_required"
        if any(w in norm for w in ("sponsor", "sponsorship", "visa support"))
        else "authorized"
    )
    stored = ap.eligibility_answer(profile.get("eligibility_json") or {}, jurisdiction, key)
    if stored is None:
        return _entry(
            field,
            ACTION_ASK,
            locked_as=category,
            tier=ab.Tier.ESCALATE.value,
            reason=(
                f"no eligibility recorded for {jurisdiction} — set it in your application "
                "profile, or answer here"
            ),
        )

    return _resolved_fill(
        field,
        "Yes" if stored else "No",
        tier=ab.Tier.PROFILE.value,
        source=f"eligibility.{jurisdiction}.{key}",
        locked_as=category,
    )


def plan_field(
    field: dict,
    profile: dict,
    entries: Sequence[dict],
    *,
    scope: str = "global",
    embed: Callable[[str], list[float]] | None = None,
) -> dict:
    """Resolve one scraped field. Never raises: an unrecognised field becomes an ask."""
    ftype = (field.get("type") or "text").lower()

    if ftype in _UNTOUCHABLE_TYPES:
        return _entry(
            field, ACTION_SKIP, bankable=False, reason=f"{ftype} field — never autofilled"
        )

    if _is_captcha(field):
        return _entry(
            field,
            ACTION_BLOCKED,
            tier=ab.Tier.BLOCKED.value,
            bankable=False,
            reason="captcha — this one is yours, and no attempt is made to work around it",
        )

    if ftype in _FILE_TYPES:
        if _is_cv_field(field):
            return _entry(field, ACTION_ATTACH_CV, source="cv")
        return _entry(
            field,
            ACTION_ASK,
            reason="file upload that isn't your CV — attach it yourself",
        )

    question = _label_of(field)
    if not question:
        return _entry(field, ACTION_ASK, reason="field has no readable label")

    # --- locked first: the fuzzy tiers must not get a chance at these ---------
    category = ab.locked_category(question)
    if category:
        routed = _plan_locked(field, question, category, profile)
        if routed is not None:
            return routed

    # --- A: profile ----------------------------------------------------------
    if not category:
        key = ap.match_field(question, field.get("autocomplete"))
        if key:
            value = profile_value(profile, key)
            if value is not None:
                return _resolved_fill(
                    field, value, tier=ab.Tier.PROFILE.value, source=f"profile.{key}"
                )
            # A known profile field the user has left blank is a genuine gap, and saying
            # which one turns the ask into a one-time fix rather than a recurring prompt.
            return _entry(
                field,
                ACTION_ASK,
                tier=ab.Tier.ESCALATE.value,
                reason=f"your profile has no {key.replace('_', ' ')} yet",
            )

    # --- B: answer bank ------------------------------------------------------
    res = ab.resolve_question(question, entries, scope=scope, embed=embed)
    if res.tier in (ab.Tier.BANK_EXACT, ab.Tier.BANK_NEAR) and res.value is not None:
        return _resolved_fill(
            field,
            res.value,
            tier=res.tier.value,
            source="answer_bank",
            source_id=res.source_id,
            similarity=res.similarity,
            locked_as=res.locked_as,
        )

    # --- C: escalate ---------------------------------------------------------
    return _entry(
        field,
        ACTION_ASK,
        tier=ab.Tier.ESCALATE.value,
        similarity=res.similarity,
        locked_as=res.locked_as or category,
        reason=res.reason or "no stored answer for this question",
    )


def plan_form(
    fields: Sequence[dict],
    profile: dict,
    entries: Sequence[dict],
    *,
    scope: str = "global",
    embed: Callable[[str], list[float]] | None = None,
) -> dict:
    """Plan a whole form. Returns the per-field plan plus a countable summary.

    The summary is what the dashboard and the extension's banner both read, so coverage
    ("11 of 14 filled from what you've already told us") is computed once, here, rather
    than by each surface counting tiers for itself.
    """
    planned = [
        plan_field(f, profile, entries, scope=scope, embed=embed) for f in fields or []
    ]
    counted = [p for p in planned if p["action"] != ACTION_SKIP]
    bank_tiers = (ab.Tier.BANK_EXACT.value, ab.Tier.BANK_NEAR.value)
    summary = {
        "total": len(counted),
        "filled": sum(1 for p in counted if p["action"] == ACTION_FILL),
        "from_profile": sum(
            1 for p in counted
            if p["action"] == ACTION_FILL and p["tier"] == ab.Tier.PROFILE.value
        ),
        "from_bank": sum(
            1 for p in counted
            if p["action"] == ACTION_FILL and p["tier"] in bank_tiers
        ),
        "asked": sum(1 for p in counted if p["action"] == ACTION_ASK),
        "blocked": sum(1 for p in counted if p["action"] == ACTION_BLOCKED),
        "attachments": sum(1 for p in counted if p["action"] == ACTION_ATTACH_CV),
        "required_gaps": sum(
            1 for p in counted
            if p["required"] and p["action"] in (ACTION_ASK, ACTION_BLOCKED)
        ),
    }
    return {"fields": planned, "summary": summary}


# --------------------------------------------------------------------------- #
# Learning: what came back off a submitted form
# --------------------------------------------------------------------------- #
#: Values too short or too generic to be worth banking as an answer to anything.
_TRIVIAL = frozenset({"", "-", "n a", "na", "select", "please select", "choose", "none"})


def bankable_answers(
    answers: Sequence[dict],
    *,
    scope: str = "global",
    company: str | None = None,
) -> list[dict]:
    """Filter what the user actually submitted down to what belongs in the bank.

    ``answers`` are ``{question, value, kind, tier, source_id, edited}`` as read back off
    the form at submit time. Three things are dropped:

    * **Unedited profile-tier fields.** Name and email are facts on the profile, not
      questions; re-banking them would duplicate the profile into the bank and give two
      places to correct an address.
    * **Empty and placeholder values.** "— Select —" is the absence of an answer.
    * **Unedited bank hits.** A value that came out of the bank and went in unchanged
      needs a usage bump, not a rewrite. Those are returned by :func:`used_answer_ids`.

    Everything surviving is something the user typed or corrected with the form in front
    of them — which is exactly the standard the never-infer rule asks for. Employer-
    specific questions are scoped so they are never replayed at another company.
    """
    skip_when_unedited = (
        ab.Tier.PROFILE.value,
        ab.Tier.BANK_EXACT.value,
        ab.Tier.BANK_NEAR.value,
    )
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in answers or []:
        question = (item.get("question") or "").strip()
        raw = item.get("value")
        value = "" if raw is None else str(raw).strip()
        if not question or ap.normalise_label(value) in _TRIVIAL:
            continue
        if item.get("tier") in skip_when_unedited and not item.get("edited"):
            continue

        norm = ab.normalise_question(question)
        if not norm:
            continue
        target = scope_for(question, scope, company)
        if (norm, target) in seen:
            continue
        seen.add((norm, target))

        kind = item.get("kind") or "text"
        if kind not in ab.ANSWER_KINDS:
            kind = "text"
        out.append(
            {
                "question_text": question,
                "answer_value": value,
                "answer_kind": kind,
                "scope": target,
                "locked_as": ab.locked_category(question),
            }
        )
    return out


def used_answer_ids(answers: Sequence[dict]) -> list[str]:
    """Bank rows that supplied a value the user submitted unchanged — a usage bump.

    Separated from :func:`bankable_answers` because reuse and re-authoring are different
    events: one proves an answer is still right, the other replaces it.
    """
    bank_tiers = (ab.Tier.BANK_EXACT.value, ab.Tier.BANK_NEAR.value)
    return [
        str(item["source_id"])
        for item in answers or []
        if item.get("source_id")
        and not item.get("edited")
        and item.get("tier") in bank_tiers
    ]
