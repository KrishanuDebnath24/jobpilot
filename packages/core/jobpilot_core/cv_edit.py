"""Deterministic edits to an attached CV — add/remove skills and bullets. NO LLM.

The Concierge lets the user tweak a specific job's tailored CV by chat:
"add Kubernetes and drop jQuery", "remove the second Acme bullet, add one about the
migration". Those are structural edits, not generation, so they run in code at zero token
cost — only an explicit *reword* (``cv_rewriter.reword_section``) spends a model call.

Honesty is preserved by routing the result through ``cv_fields.finalize``:

* **Removing** a skill or bullet is always fine.
* **Adding a skill** the résumé doesn't evidence is allowed ONLY as ``user_confirmed`` —
  the candidate has asserted they hold it (policy chosen by the user). It is recorded with
  that basis in ``skill_evidence`` so the trail stays honest about *why* it's on the CV.
  A skill that IS grounded in the résumé is added plainly (no confirmation needed).
* **Adding a bullet** is user-asserted content on a modifiable field; locked facts
  (employer/title/dates/education/contact) are re-imposed by ``finalize`` regardless.

Every edit returns the finalized content plus a summary of what actually changed, so the
caller can tell the user precisely — and reset the draft to ``draft_ready`` for re-approval.
"""

from __future__ import annotations

import copy

from . import cv_fields


def _norm(s) -> str:
    return " ".join(str(s or "").strip().split()).lower()


def _match_role(experience: list[dict], role_ref: str | None, role_index: int | None) -> int | None:
    """Resolve which experience entry a bullet edit targets: an explicit index, or the
    first role whose company/title contains ``role_ref``. ``None`` if it can't be resolved."""
    if role_index is not None and 0 <= role_index < len(experience):
        return role_index
    if role_ref:
        ref = _norm(role_ref)
        for i, r in enumerate(experience):
            hay = _norm(f"{r.get('title')} {r.get('company')}")
            if ref in hay:
                return i
    # a single-role CV is unambiguous
    if len(experience) == 1:
        return 0
    return None


def edit_tailored_cv(
    content: dict,
    base_resume: dict,
    snapshot: dict,
    *,
    add_skills: list[str] | None = None,
    remove_skills: list[str] | None = None,
    add_bullets: list[dict] | None = None,     # [{"text": str, "role_ref"?: str, "role_index"?: int}]
    remove_bullets: list[str] | None = None,   # bullet text (matched loosely) across roles
) -> dict:
    """Apply structural edits and return ``{content, changes, warnings}``. ``content`` is
    finalized (locked facts enforced, skills evidence-gated, headers carried), so it is safe
    to persist and export as-is."""
    cv = copy.deepcopy(content) if isinstance(content, dict) else {}
    changes: list[str] = []
    warnings: list[str] = []

    base_skills = {_norm(s) for s in (base_resume.get("skills") or [])}
    base_text = cv_fields._base_text(base_resume)

    # --- skills ---
    existing = list(cv.get("skills") or [])
    existing_norm = {_norm(s) for s in existing}
    for skill in remove_skills or []:
        n = _norm(skill)
        kept = [s for s in existing if _norm(s) != n]
        if len(kept) != len(existing):
            existing = kept
            existing_norm.discard(n)
            changes.append(f"removed skill '{skill}'")
        else:
            warnings.append(f"skill '{skill}' was not on the CV")
    evidence = [e for e in (cv.get("skill_evidence") or []) if isinstance(e, dict)]
    for skill in add_skills or []:
        n = _norm(skill)
        if not n or n in existing_norm:
            if n in existing_norm:
                warnings.append(f"skill '{skill}' is already on the CV")
            continue
        existing.append(skill.strip())
        existing_norm.add(n)
        # Grounded skills need no citation; ungrounded ones are recorded as user-confirmed.
        if n not in base_skills and n not in base_text:
            evidence = [e for e in evidence if _norm(e.get("skill")) != n]
            evidence.append({"skill": skill.strip(), "basis": cv_fields.USER_CONFIRMED,
                             "evidence": "user confirmed they hold this skill"})
            changes.append(f"added skill '{skill}' (user-confirmed)")
        else:
            changes.append(f"added skill '{skill}'")
    cv["skills"] = existing
    cv["skill_evidence"] = evidence

    # --- bullets ---
    experience = cv.get("experience") or []
    for text in remove_bullets or []:
        n = _norm(text)
        removed = False
        for role in experience:
            bullets = role.get("bullets") or []
            kept = [b for b in bullets if n not in _norm(b)]
            if len(kept) != len(bullets):
                role["bullets"] = kept
                removed = True
        changes.append(f"removed bullet matching '{text}'") if removed else \
            warnings.append(f"no bullet matched '{text}'")
    for spec in add_bullets or []:
        text = (spec.get("text") or "").strip()
        if not text:
            continue
        idx = _match_role(experience, spec.get("role_ref"), spec.get("role_index"))
        if idx is None:
            warnings.append(f"couldn't tell which role to add the bullet to: '{text[:40]}…' "
                            f"(name the company)")
            continue
        # Idempotent: adding a bullet that's already there is a no-op, not a duplicate. The
        # alignment loop re-applies attested bullets every round, and asking twice in chat
        # shouldn't print it twice either.
        if any(_norm(b) == _norm(text) for b in (experience[idx].get("bullets") or [])):
            continue
        experience[idx].setdefault("bullets", []).append(text)
        changes.append(f"added a bullet to {experience[idx].get('company') or f'role {idx}'}")
    cv["experience"] = experience

    # Honest-by-construction: locked facts re-imposed, skills evidence-gated (user_confirmed
    # survives, unsupported ones dropped), original section headers carried through.
    final = cv_fields.finalize(cv, base_resume, snapshot)

    # Report any add that the gate still refused (shouldn't happen for user_confirmed, but a
    # grounded-basis skill with a bad citation would be dropped — tell the user honestly).
    surviving = {_norm(s) for s in (final.get("skills") or [])}
    for skill in add_skills or []:
        if _norm(skill) and _norm(skill) not in surviving:
            warnings.append(f"skill '{skill}' could not be added (failed the honesty gate)")

    return {"content": final, "changes": changes, "warnings": warnings}


def _attested_prose(attested: dict) -> str:
    """The normalised text of every sentence the candidate vouched for."""
    parts: list[str] = []
    for it in (attested.get("new_bullets") or []):
        if isinstance(it, dict) and (it.get("text") or "").strip():
            parts.append(str(it["text"]))
    for it in (attested.get("bullet_rewrites") or []):
        if isinstance(it, dict) and (it.get("after") or "").strip():
            parts.append(str(it["after"]))
    return _norm(" ".join(parts))


def _seed_attested_evidence(cv: dict, base_resume: dict, attested: dict) -> dict:
    """Stamp ``user_confirmed`` on every skill the candidate vouched for, before the gate runs.

    Two live-run bugs live here, both of which silently deleted content the user had asked
    for:

    1. **The skill the Rewriter already listed.** ``edit_tailored_cv`` skips its own evidence
       stamp when a skill is already on the CV (right for a chat edit — nothing to add). But
       the Rewriter, correctly told Kubernetes was attested, *did* list it. So the stamp was
       skipped, ``apply_evidence_gate`` found an added skill with no citation, and deleted it —
       after which the Rewriter substituted a vaguer euphemism ("Container Orchestration").
       The user's explicit choice lost to a gate meant for the model's inventions. So the seed
       must run over the attested skills **unconditionally**, not only when absent.

    2. **The skill named only inside an attested bullet.** Someone who accepts "Migrated 12
       services onto Kafka" without ticking Kafka got a draft claiming Kafka in prose while
       the gate stripped it from the skills array — an incoherent CV the Critic then refused
       to align, correctly and forever. Guarantee a sentence and you guarantee what it names.

    What this deliberately does NOT do: widen ``cv_fields._base_text``. That would admit these
    skills as ``explicit`` — silently asserting they were in the résumé all along. The basis
    recorded is ``user_confirmed``, so the draft keeps saying plainly that this rests on the
    candidate's word. A skill the Rewriter reached for that is neither attested nor named in an
    attested sentence gets no stamp, and the gate still deletes it.
    """
    prose = _attested_prose(attested)
    named = {_norm(it.get("skill")) for it in (attested.get("claimed_skills") or [])
             if isinstance(it, dict) and (it.get("skill") or "").strip()}
    if not (prose or named):
        return cv

    base_skills = {_norm(s) for s in (base_resume.get("skills") or [])}
    base_text = cv_fields._base_text(base_resume)
    evidence = [e for e in (cv.get("skill_evidence") or []) if isinstance(e, dict)]
    have = {_norm(e.get("skill")) for e in evidence}

    def _stamp(label: str, why: str, *, override: bool) -> None:
        n = _norm(label)
        if not n or n in base_skills or n in base_text:
            return  # genuinely on the résumé — needs no citation at all
        if n in have:
            if not override:
                return  # already carries a citation of its own; leave it alone
            # The candidate's attestation OUTRANKS whatever citation the model invented.
            # Found live: the Rewriter, told Kubernetes was attested, wrote its own basis
            # ("Candidate attested to Kubernetes experience during review"). That quote is
            # nowhere in the résumé, so the gate rejected it and deleted the skill — the
            # user's explicit choice destroyed by a citation the model made up on its behalf.
            for i, existing in enumerate(evidence):
                if _norm(existing.get("skill")) == n:
                    evidence[i] = {"skill": label.strip(),
                                   "basis": cv_fields.USER_CONFIRMED, "evidence": why}
                    return
        evidence.append({"skill": label.strip(), "basis": cv_fields.USER_CONFIRMED,
                         "evidence": why})
        have.add(n)

    for it in (attested.get("claimed_skills") or []):
        if isinstance(it, dict) and (it.get("skill") or "").strip():
            _stamp(str(it["skill"]), "you confirmed you hold this skill", override=True)
    if prose:
        for raw in cv.get("skills") or []:
            if _norm(raw) in named:
                continue
            if _norm(raw) and _norm(raw) in prose:
                # Not an override: if the model found a real résumé citation for a term that
                # merely appears in an attested bullet, that stronger basis should stand.
                _stamp(str(raw), "named in a bullet you confirmed", override=False)

    cv = copy.deepcopy(cv)
    cv["skill_evidence"] = evidence
    return cv


def apply_attested(content: dict, base_resume: dict, snapshot: dict, attested: dict | None) -> dict:
    """Force the candidate's attested items onto a draft, deterministically. Returns the
    finalized content (safe to persist/export).

    The prompt asks both agents to include this content, but a prompt is a request: the
    Rewriter can quietly drop a skill, and — more importantly — an attested skill with no
    ``skill_evidence`` entry is *deleted* by ``apply_evidence_gate`` inside every
    ``finalize``. So the loop calls this each round BEFORE judging: it stamps the
    ``user_confirmed`` basis that makes the gate admit the skill, and re-inserts any missing
    bullet. Without it the Critic would grade a CV that silently lacks what the user asked
    for, and the loop would optimise against the wrong document.

    Idempotent, so re-applying every round neither duplicates bullets nor re-warns.
    """
    att = attested or {}
    skills = [it.get("skill") for it in (att.get("claimed_skills") or [])
              if isinstance(it, dict) and (it.get("skill") or "").strip()]
    bullets = [{"text": it.get("text"), "role_ref": it.get("role_ref")}
               for it in (att.get("new_bullets") or [])
               if isinstance(it, dict) and (it.get("text") or "").strip()]
    if not (skills or bullets):
        return cv_fields.finalize(content, base_resume, snapshot)
    # Label skills the attested sentences name BEFORE the gate runs, so a term the Rewriter
    # surfaced from a confirmed bullet survives as user_confirmed rather than being stripped
    # into a draft that claims it in prose but not in the skills list.
    seeded = _seed_attested_evidence(content, base_resume, att)
    return edit_tailored_cv(seeded, base_resume, snapshot,
                            add_skills=skills, add_bullets=bullets)["content"]
