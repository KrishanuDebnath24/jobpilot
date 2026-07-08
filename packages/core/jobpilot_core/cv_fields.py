"""Locked vs modifiable CV fields — the integrity spine of tailored CV generation
(Phase 6.5, 6.5a). Pure, no I/O.

Tailoring a CV per job is only legitimate if every improvement comes from presenting
**real** experience in the job's language — never from fabrication. That is enforced in
code here, not merely by prompting the model:

* **LOCKED (immutable, copied verbatim):** contact details; per-role employer name,
  job title, and employment dates/duration; education (institutions, qualifications,
  dates). These are factual claims that can never change.
* **MODIFIABLE (profile-driven):** the professional summary, the skills list (emphasis
  + ordering only — never invented), and experience **bullet phrasing** (reworded to
  surface relevant real experience and the job's keywords, without changing the
  underlying facts, scope, or dates), plus section ordering.

``validate_locked`` proves the tailored draft's locked fields are byte-identical to the
snapshot; ``enforce_locked`` re-imposes the snapshot verbatim (defense in depth, so an
exported file can never carry drifted locked content); ``validate_no_fabricated_skills``
catches skills that aren't grounded anywhere in the real resume.
"""

from __future__ import annotations

import copy
import re

# The locked set, enumerated for the schema / review UI / docs.
LOCKED_TOP_FIELDS = ("contact", "education")  # whole structures, immutable
LOCKED_EXPERIENCE_FIELDS = ("title", "company", "dates")  # per role: who/what/when
MODIFIABLE_FIELDS = ("summary", "skills", "experience.bullets", "section_order")


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
