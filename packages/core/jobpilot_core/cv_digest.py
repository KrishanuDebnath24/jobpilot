"""Read-only, deterministic digests for the Concierge — NO LLM, so they cost zero tokens.

Two plain-language views the user can ask for on demand:

* :func:`job_digest` — an easy-to-read summary of ONE job, built entirely from the
  Analyzer's already-stored ``analysis`` (required skills, keywords, tech stack, seniority,
  certifications, gaps vs the user's CV) plus a years-of-experience figure scanned from the
  posting text. Nothing is recomputed with a model — the analysis was produced once, at
  analyze time, and this just reshapes it into "skills / experience / role / qualifications".

* :func:`cv_sections` — the modifiable parts of the CV currently attached for a job (profile,
  key skills with their evidence basis, and each role's bullets), so the user can see exactly
  what is there and decide what to add, drop, or reword. A pure read of the stored draft.

Both are the cheap, on-demand path: the data already exists; this only formats it.
"""

from __future__ import annotations

import re

# Same shape of scan the qualifier uses for required years — kept local so a digest never
# needs to import scoring internals. Best-effort; ``None`` when the posting states no number.
_YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:years|yrs)", re.IGNORECASE)


def _required_years(text: str) -> int | None:
    yrs = [int(m) for m in _YEARS_RE.findall(text or "")]
    return min(max(yrs), 15) if yrs else None


def _clean(items) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for it in items or []:
        s = str(it).strip()
        k = s.lower()
        if s and k not in seen:
            seen.add(k)
            out.append(s)
    return out


def job_digest(job: dict, analysis: dict | None) -> dict:
    """Plain-language digest of one job from its stored analysis. ``analysis`` is the
    Analyzer's ``extracted_json`` (may be ``None`` if the job was never analyzed — the
    caller decides whether to offer analysis)."""
    ex = analysis or {}
    role = " @ ".join(x for x in (job.get("title"), job.get("company")) if x) or (job.get("title") or "this role")
    years = _required_years(job.get("description") or "")
    seniority = ex.get("seniority")
    if years and seniority:
        experience = f"{seniority} level, ~{years}+ years"
    elif years:
        experience = f"~{years}+ years"
    else:
        experience = seniority or "not stated"
    return {
        "role": role,
        "location": job.get("location"),
        "analyzed": bool(analysis),
        "experience_needed": experience,
        "skills_required": _clean(ex.get("required_skills")),
        "tech_stack": _clean(ex.get("tech_stack")),
        "keywords": _clean(ex.get("keywords")),
        "qualifications": _clean(ex.get("required_certifications")),
        "gaps_vs_your_cv": _clean(ex.get("gaps_vs_resume")),
    }


def _skill_basis_index(content: dict) -> dict[str, str]:
    idx: dict[str, str] = {}
    for e in content.get("skill_evidence") or []:
        if isinstance(e, dict) and e.get("skill"):
            idx[str(e["skill"]).strip().lower()] = str(e.get("basis") or "").strip()
    return idx


def cv_sections(content: dict, job: dict | None = None) -> dict:
    """The modifiable parts of an attached CV, for review before reframing. Locked facts
    (employer/title/dates) are shown as context but flagged read-only. Each skill is tagged
    with how it's justified so the user can see which are résumé-grounded vs user-confirmed."""
    content = content or {}
    basis = _skill_basis_index(content)
    skills = []
    for s in content.get("skills") or []:
        skills.append({"skill": s, "basis": basis.get(str(s).strip().lower()) or "in your CV"})
    experience = []
    for i, role in enumerate(content.get("experience") or []):
        if not isinstance(role, dict):
            continue
        experience.append({
            "index": i,
            "title": role.get("title"),          # locked
            "company": role.get("company"),      # locked
            "dates": role.get("dates"),          # locked
            "bullets": [b for b in (role.get("bullets") or []) if str(b).strip()],
        })
    return {
        "for_job": " @ ".join(x for x in ((job or {}).get("title"), (job or {}).get("company")) if x) or None,
        "profile": (content.get("summary") or "").strip(),
        "skills": skills,
        "experience": experience,
        "editable": "profile, skills, and experience bullets — employer/title/dates are locked",
    }
