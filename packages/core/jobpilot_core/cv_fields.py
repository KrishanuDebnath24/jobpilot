"""Locked vs modifiable CV fields — the integrity spine of tailored CV generation
(Phase 6.5, 6.5a). Pure, no I/O.

Tailoring a CV per job is only legitimate if every improvement comes from presenting
**real** experience in the job's language — never from fabrication. That is enforced in
code here, not merely by prompting the model:

* **LOCKED (immutable, copied verbatim):** contact details; per-role employer name,
  job title, and employment dates/duration; education (institutions, qualifications,
  dates). These are factual claims that can never change.
* **MODIFIABLE (profile-driven):** the professional summary, the skills list, and
  experience **bullet phrasing** (reworded to surface relevant real experience and the
  job's keywords, without changing the underlying facts, scope, or dates), plus section
  ordering.

``validate_locked`` proves the tailored draft's locked fields are byte-identical to the
snapshot; ``enforce_locked`` re-imposes the snapshot verbatim (defense in depth, so an
exported file can never carry drifted locked content); ``validate_no_fabricated_skills``
catches skills that aren't grounded anywhere in the real resume.

**Evidence-gated skills (the alignment loop).** The rewrite/critique loop is allowed to
ADD a skill the base resume's skills list doesn't name — but only against a citation.
For every added skill the rewriter must supply a ``skill_evidence`` entry naming the
resume text that supports it and the basis on which it is claimed:

* ``explicit``     — the skill is named in the resume (a different section/phrasing);
* ``adjacent``     — entailed by named work (e.g. "Docker + ECS" -> containerisation);
* ``transferable`` — the same competence under the job's vocabulary.

``validate_skill_evidence`` re-checks every citation against the REAL resume text and
rejects any skill whose evidence cannot be found there, so the freedom to re-label
genuine experience never becomes the freedom to invent it. ``apply_evidence_gate`` is
the enforcement arm: it drops the rejected skills. A skill already grounded by
``validate_no_fabricated_skills`` needs no citation — that path is unchanged.
"""

from __future__ import annotations

import copy
import re

# The locked set, enumerated for the schema / review UI / docs.
LOCKED_TOP_FIELDS = ("contact", "education")  # whole structures, immutable
LOCKED_EXPERIENCE_FIELDS = ("title", "company", "dates")  # per role: who/what/when
MODIFIABLE_FIELDS = ("summary", "skills", "experience.bullets", "section_order")

# Sections carried through tailoring untouched in content, so the rendered CV keeps every
# header the candidate's real CV had (certifications, projects, languages, ...).
PASSTHROUGH_SECTIONS = ("certifications", "additional_sections")

# How an added skill may be justified. Ordered loosest-evidence-last for review display.
# ``user_confirmed`` is different in kind from the other three: the candidate has explicitly
# asserted (via chat) that they hold the skill, so the CANDIDATE is the guarantor of truth,
# not a résumé citation — the code does NOT check it against résumé text. It stays a distinct,
# named basis so the draft's evidence trail shows plainly which skills are user-asserted vs
# résumé-grounded (auditable, not silent).
USER_CONFIRMED = "user_confirmed"
SKILL_BASES = ("explicit", "adjacent", "transferable", USER_CONFIRMED)

# An evidence citation must overlap the real resume text by at least this many content
# words. Short citations ("Docker") are matched whole; longer ones tolerate the rewriter
# paraphrasing a bullet, without letting a wholly invented sentence through.
_MIN_EVIDENCE_OVERLAP = 0.6
_STOPWORDS = {
    "a", "an", "and", "the", "of", "to", "in", "on", "for", "with", "at", "by", "from",
    "as", "is", "was", "were", "be", "been", "using", "used", "via", "our", "their",
}


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# --------------------------------------------------------------------------- #
# Snapshot: capture the locked facts from the base resume version
# --------------------------------------------------------------------------- #
def extract_locked_snapshot(base_resume: dict) -> dict:
    """The immutable facts of the base resume: contact, education, and each role's
    (title, company, dates). Stored on the draft as ``locked_snapshot_json`` and
    compared against on every validation."""
    return {
        "contact": copy.deepcopy(base_resume.get("contact") or {}),
        "education": copy.deepcopy(base_resume.get("education") or []),
        "experience": [
            {f: (e or {}).get(f) for f in LOCKED_EXPERIENCE_FIELDS}
            for e in (base_resume.get("experience") or [])
        ],
    }


def enforce_locked(tailored: dict, snapshot: dict) -> dict:
    """Return a copy of ``tailored`` with every LOCKED field overwritten by the snapshot
    verbatim — so the persisted/exported draft carries byte-identical locked content no
    matter what the model emitted. Only the modifiable parts (summary, skills, bullets)
    survive from the tailored draft. Roles are aligned by position; a role can never be
    added or removed (the locked role count wins)."""
    out = copy.deepcopy(tailored) if isinstance(tailored, dict) else {}
    out["contact"] = copy.deepcopy(snapshot.get("contact") or {})
    out["education"] = copy.deepcopy(snapshot.get("education") or [])
    tailored_exp = out.get("experience") or []
    fixed: list[dict] = []
    for i, locked in enumerate(snapshot.get("experience") or []):
        te = tailored_exp[i] if i < len(tailored_exp) and isinstance(tailored_exp[i], dict) else {}
        fixed.append({
            "title": locked.get("title"),
            "company": locked.get("company"),
            "dates": locked.get("dates"),
            "bullets": list(te.get("bullets") or []),  # the one modifiable part of a role
        })
    out["experience"] = fixed
    return out


# --------------------------------------------------------------------------- #
# Validation: catch drift + fabrication (defense in depth beyond enforce_locked)
# --------------------------------------------------------------------------- #
def validate_locked(tailored: dict, snapshot: dict) -> dict:
    """``{ok, violations}``: does the tailored draft's locked content equal the snapshot?
    Any drift (a changed date, employer, title, contact detail, education line, or a
    added/removed role) is a violation. This is what catches a touched locked field."""
    violations: list[str] = []
    if (tailored.get("contact") or {}) != (snapshot.get("contact") or {}):
        violations.append("contact details changed")
    if (tailored.get("education") or []) != (snapshot.get("education") or []):
        violations.append("education changed")
    t_exp = tailored.get("experience") or []
    s_exp = snapshot.get("experience") or []
    if len(t_exp) != len(s_exp):
        violations.append(f"role count changed ({len(t_exp)} vs {len(s_exp)}) — roles cannot be added/removed")
    for i, se in enumerate(s_exp):
        if i >= len(t_exp):
            break
        te = t_exp[i] or {}
        for f in LOCKED_EXPERIENCE_FIELDS:
            if (te.get(f) or None) != (se.get(f) or None):
                violations.append(f"experience[{i}].{f} changed: {se.get(f)!r} -> {te.get(f)!r}")
    return {"ok": not violations, "violations": violations}


def _base_text(base_resume: dict) -> str:
    parts: list[str] = list(base_resume.get("skills") or [])
    if base_resume.get("summary"):
        parts.append(base_resume["summary"])
    for e in base_resume.get("experience") or []:
        parts.extend(str(x) for x in (e.get("title"), e.get("company")) if x)
        parts.extend(e.get("bullets") or [])
    for ed in base_resume.get("education") or []:
        parts.append(ed.get("text") or "")
    return _norm(" ".join(p for p in parts if p))


def validate_no_fabricated_skills(tailored: dict, base_resume: dict) -> dict:
    """``{ok, fabricated}``: a tailored skill is legitimate only if it's grounded in the
    real resume — present in the base skills list OR mentioned anywhere in the base text
    (summary/experience/education). A skill that appears nowhere real is fabrication."""
    base_skills = {_norm(s) for s in (base_resume.get("skills") or [])}
    base_text = _base_text(base_resume)
    fabricated: list[str] = []
    for s in tailored.get("skills") or []:
        n = _norm(s)
        if not n:
            continue
        if n in base_skills or n in base_text:
            continue
        fabricated.append(s)
    return {"ok": not fabricated, "fabricated": fabricated}


def drop_fabricated_skills(tailored: dict, base_resume: dict) -> dict:
    """Return a copy with any un-grounded skill removed (kept honest by construction)."""
    fabricated = set(validate_no_fabricated_skills(tailored, base_resume)["fabricated"])
    if not fabricated:
        return tailored
    out = copy.deepcopy(tailored)
    out["skills"] = [s for s in (out.get("skills") or []) if s not in fabricated]
    return out


def validate_tailored(tailored: dict, base_resume: dict, snapshot: dict) -> dict:
    """Combined gate: locked-field integrity + non-fabrication. ``{ok, violations,
    fabricated}``."""
    locked = validate_locked(tailored, snapshot)
    skills = validate_no_fabricated_skills(tailored, base_resume)
    return {
        "ok": locked["ok"] and skills["ok"],
        "violations": locked["violations"],
        "fabricated": skills["fabricated"],
    }


# --------------------------------------------------------------------------- #
# Evidence-gated skill admission — lets the alignment loop ADD a job-required skill
# the base skills list doesn't name, but ONLY against a citation that is verifiably
# in the real resume. This is the check that keeps "re-label real experience in the
# job's vocabulary" from sliding into "claim a skill the candidate lacks".
# --------------------------------------------------------------------------- #
def _content_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9+#.]+", _norm(text)) if w not in _STOPWORDS]


def _evidence_supported(quote: str, base_text: str) -> bool:
    """Is ``quote`` actually present in the base resume? Exact substring wins; otherwise
    at least ``_MIN_EVIDENCE_OVERLAP`` of its content words must appear, so a lightly
    paraphrased citation passes but an invented one does not."""
    q = _norm(quote)
    if not q:
        return False
    if q in base_text:
        return True
    words = _content_words(quote)
    if not words:
        return False
    hits = sum(1 for w in words if w in base_text)
    return (hits / len(words)) >= _MIN_EVIDENCE_OVERLAP


def _evidence_index(cv: dict) -> dict[str, dict]:
    """``{normalized skill: evidence entry}`` from the draft's ``skill_evidence``."""
    out: dict[str, dict] = {}
    for item in cv.get("skill_evidence") or []:
        if isinstance(item, dict) and item.get("skill"):
            out.setdefault(_norm(item["skill"]), item)
    return out


def validate_skill_evidence(cv: dict, base_resume: dict) -> dict:
    """``{ok, admitted, rejected}`` for the draft's skills list.

    A skill is **admitted** when it is either already grounded in the real resume (the
    pre-existing ``validate_no_fabricated_skills`` rule) or carries a ``skill_evidence``
    citation whose quoted text is verifiably present in the resume, on a recognised
    basis. Everything else is **rejected** with a reason — an added skill with no
    citation, a citation quoting text the resume does not contain, or an unknown basis.
    """
    base_skills = {_norm(s) for s in (base_resume.get("skills") or [])}
    base_text = _base_text(base_resume)
    evidence = _evidence_index(cv)

    admitted: list[dict] = []
    rejected: list[dict] = []
    for raw in cv.get("skills") or []:
        n = _norm(raw)
        if not n:
            continue
        if n in base_skills or n in base_text:
            admitted.append({"skill": raw, "basis": "explicit", "evidence": None})
            continue
        ev = evidence.get(n)
        if not ev:
            rejected.append({"skill": raw, "reason": "added skill with no evidence citation"})
            continue
        basis = _norm(ev.get("basis"))
        if basis not in SKILL_BASES:
            rejected.append({"skill": raw, "reason": f"unknown evidence basis {ev.get('basis')!r}"})
            continue
        # User-asserted: the candidate is the guarantor, so no résumé-text check — but it is
        # kept as its own basis so the trail shows it was user-confirmed, not résumé-grounded.
        if basis == USER_CONFIRMED:
            admitted.append({"skill": raw, "basis": basis,
                             "evidence": ev.get("evidence") or "user-confirmed"})
            continue
        quote = ev.get("evidence") or ""
        if not _evidence_supported(quote, base_text):
            rejected.append({
                "skill": raw,
                "reason": "cited evidence does not appear in the real resume",
                "claimed_evidence": quote,
            })
            continue
        admitted.append({"skill": raw, "basis": basis, "evidence": quote})
    return {"ok": not rejected, "admitted": admitted, "rejected": rejected}


def apply_evidence_gate(cv: dict, base_resume: dict) -> dict:
    """Return a copy of ``cv`` with every skill that failed :func:`validate_skill_evidence`
    removed, and ``skill_evidence`` narrowed to the skills that survived. This is the
    enforcement arm — the exported CV can only ever carry evidence-backed skills."""
    result = validate_skill_evidence(cv, base_resume)
    if result["ok"] and not (cv.get("skill_evidence") or []):
        return cv
    kept = {_norm(a["skill"]) for a in result["admitted"]}
    out = copy.deepcopy(cv)
    out["skills"] = [s for s in (out.get("skills") or []) if _norm(s) in kept]
    out["skill_evidence"] = [
        e for e in (out.get("skill_evidence") or [])
        if isinstance(e, dict) and _norm(e.get("skill")) in kept
    ]
    return out


def added_skills(cv: dict, base_resume: dict) -> list[str]:
    """Skills on the draft that the base resume's skills list did not name — the surface
    the reviewer should look hardest at."""
    base_skills = {_norm(s) for s in (base_resume.get("skills") or [])}
    return [s for s in (cv.get("skills") or []) if _norm(s) and _norm(s) not in base_skills]


def carry_passthrough_sections(cv: dict, base_resume: dict) -> dict:
    """Ensure every header the real CV had survives tailoring. Sections the rewriter did
    not return are copied from the base resume verbatim, so certifications/projects/
    languages can never be silently dropped from the rendered document."""
    out = copy.deepcopy(cv) if isinstance(cv, dict) else {}
    for key in PASSTHROUGH_SECTIONS:
        if not out.get(key) and base_resume.get(key):
            out[key] = copy.deepcopy(base_resume[key])
    return out


def finalize(cv: dict, base_resume: dict, snapshot: dict) -> dict:
    """The single honest-by-construction exit used by the alignment loop:
    locked fields re-imposed verbatim → evidence gate applied → original headers carried
    through. Whatever the models emitted, what leaves this function is safe to export."""
    return carry_passthrough_sections(
        apply_evidence_gate(enforce_locked(cv, snapshot), base_resume), base_resume
    )
