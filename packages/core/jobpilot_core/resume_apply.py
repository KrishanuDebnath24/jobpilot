"""Apply accepted suggestions to the resume (Phase 6, 6e) — honest + versioned.

This is where the Phase 5 no-fabrication rule is enforced **at write time**. Given a
resume and the user's *accepted* suggestions, assemble a **new resume version** with:

  1. **Bullet rewrites** applied directly — they reframe experience the resume
     already contains. A rewrite is applied only when its ``before`` line matches an
     existing bullet; a rewrite with no matching bullet is **dropped** (never invent
     an accomplishment).
  2. **Skills added only where the user confirms they hold them** — a skill is added
     only if it appears in an accepted ``skills_to_add`` bucket AND the caller passes
     it in ``confirmed_skills``. Everything else is **withheld** (surfaced, not added).
  3. **Projects surfaced as guidance, never inserted** — project ideas are advice for
     building a real gap skill; they are returned separately, not written into the
     resume.

Pure functions only (no DB, no LLM). The original resume dict is never mutated — a
deep copy is returned — so the caller can persist the new version while preserving
the original (Phase 6 versioning + Phase 1 erasure model).
"""

from __future__ import annotations

import copy


def _norm(s: str) -> str:
    return " ".join((s or "").strip().split()).lower()


def _accepted_rewrites(accepted: list[dict]) -> list[dict]:
    out: list[dict] = []
    for sug in accepted or []:
        for rw in ((sug.get("bullet_rewrites_json") or {}).get("bullet_rewrites") or []):
            if rw.get("before") and rw.get("after"):
                out.append(rw)
    return out


def _accepted_skills(accepted: list[dict]) -> list[str]:
    seen: dict[str, str] = {}
    for sug in accepted or []:
        for skill in (sug.get("skills_to_add") or []):
            if skill and _norm(skill) not in seen:
                seen[_norm(skill)] = skill.strip()
    return list(seen.values())


def _accepted_projects(accepted: list[dict]) -> list[dict]:
    out: list[dict] = []
    for sug in accepted or []:
        for proj in ((sug.get("projects_json") or {}).get("projects") or []):
            if proj.get("title"):
                out.append(proj)
    return out


def apply_suggestions(
    resume: dict, accepted: list[dict], confirmed_skills: list[str] | None
) -> dict:
    """Assemble the new resume structure. Deep-copies ``resume`` (original untouched).

    Honesty invariants (enforced here, not merely requested of a model):
    - bullet rewrites replace an EXISTING bullet matched by ``before``; unmatched
      rewrites are discarded;
    - a skill is added ONLY if it is both accepted AND in ``confirmed_skills``;
    - projects are never inserted into the resume.
    """
    new = copy.deepcopy(resume) if resume else {}
    confirmed = {_norm(s) for s in (confirmed_skills or [])}

    # 1) Bullet rewrites — replace matching existing bullets (reframe, don't invent).
    rewrites = _accepted_rewrites(accepted)
    by_before = {_norm(rw["before"]): rw["after"] for rw in rewrites}
    applied: list[dict] = []
    for exp in new.get("experience") or []:
        bullets = exp.get("bullets") or []
        for i, bullet in enumerate(bullets):
            after = by_before.get(_norm(str(bullet)))
            if after is not None and _norm(after) != _norm(str(bullet)):
                applied.append({"before": bullet, "after": after})
                bullets[i] = after
        exp["bullets"] = bullets

    # 2) Skills — add only confirmed ones; withhold the rest.
    existing = {_norm(s) for s in (new.get("skills") or [])}
    added: list[str] = []
    withheld: list[str] = []
    for skill in _accepted_skills(accepted):
        if _norm(skill) not in confirmed:
            withheld.append(skill)  # user hasn't confirmed they hold it → never add
            continue
        if _norm(skill) in existing:
            continue
        new.setdefault("skills", []).append(skill)
        existing.add(_norm(skill))
        added.append(skill)

    return {
        "resume": new,
        "applied_rewrites": applied,
        "added_skills": added,
        "withheld_skills": withheld,           # confirmed-only guard surfaced these
        "project_guidance": _accepted_projects(accepted),  # guidance, NOT inserted
    }


def render_resume_text(structured: dict) -> str:
    """Plain-text rendering of a structured resume, for a downloadable version."""
    s = structured or {}
    lines: list[str] = []
    contact = s.get("contact") or {}
    if contact.get("name"):
        lines.append(str(contact["name"]))
    contact_bits = [contact.get(k) for k in ("email", "phone", "location") if contact.get(k)]
    if contact_bits:
        lines.append(" | ".join(str(b) for b in contact_bits))
    if s.get("summary"):
        lines += ["", "SUMMARY", str(s["summary"])]
    if s.get("skills"):
        lines += ["", "SKILLS", ", ".join(s["skills"])]
    if s.get("experience"):
        lines += ["", "EXPERIENCE"]
        for exp in s["experience"]:
            head = " | ".join(
                str(x) for x in (exp.get("title"), exp.get("company"),
                                 exp.get("location"), exp.get("dates")) if x
            )
            if head:
                lines.append(head)
            for bullet in (exp.get("bullets") or []):
                lines.append(f"  - {bullet}")
    if s.get("education"):
        lines += ["", "EDUCATION"]
        for edu in s["education"]:
            if edu.get("text"):
                lines.append(str(edu["text"]))
    return "\n".join(lines).strip() + "\n"
