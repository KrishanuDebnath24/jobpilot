"""How a CV bullet should read — one standard, shared by every agent that writes one.

Three agents put words on a CV: the Gap-closer proposes them, the Rewriter tailors them,
and the Critic judges them. Before this module existed each carried its own loose advice
("be concrete", "name the outcome"), and the results showed it — content that was factually
sound and rhetorically poor in the same four ways every time:

* **No outcomes.** Every bullet described an activity. A reviewer could not tell whether
  tuning inference cost saved 5% or 50%, because no bullet said.
* **Length.** Five bullets of three to four lines each is twenty lines for one role, more
  page than most people give a five-year job, and it is skimmed rather than read.
* **Overlap.** The bullet about OCR/ASR pipelines was the implementation detail of the
  bullet above it; the monitoring bullet repeated the tail of the evaluation bullet. The
  same information was paid for twice in the scarcest space on the page.
* **Uniform rhythm.** Every bullet ended with an em dash or comma and a participial clause
  explaining the why — "…, tuning batch size to balance latency", "…, so grounding was
  measured not assumed". Once, that is a good sentence. Five times in a row it is a tell,
  and reviewers increasingly read it as machine-drafted and discount the whole document.

None of those is a truth problem, so none of the honesty machinery catches them. They are
craft problems, and they need the same treatment honesty got: written down once, applied
everywhere, and checkable.

The standard is deliberately phrased as instructions to a writer rather than as a rubric,
because all three consumers paste it into a prompt. :data:`BULLET_STANDARD` is the full
text; :data:`CRITIC_CHECKLIST` is the same ground restated as things to look for, for the
agent whose job is to find them.
"""

from __future__ import annotations

#: Maximum bullets under any one role or project. Mirrored by ``tailor.MAX_BULLETS``,
#: which enforces it at render time — this is the number the writers are told, so they
#: choose which five rather than having five chosen for them.
MAX_BULLETS_PER_ENTRY = 5

#: Soft word ceiling for one bullet. Two printed lines at the template's body size.
MAX_BULLET_WORDS = 34

BULLET_STANDARD = (
    "HOW TO WRITE A BULLET. These are craft rules, and they matter as much as accuracy: a "
    "true CV that reads badly still loses the interview.\n"
    "• LEAD WITH THE OUTCOME, AND PUT A NUMBER ON IT. An activity is not an achievement. "
    "'Tuned batch size and model choice to balance latency against GPU cost' tells a "
    "reviewer nothing — 'Cut GPU inference cost 40% and p95 latency 25% by tuning batch "
    "size, per-endpoint model selection and frame-sampling rates' tells them everything. "
    "If you do not know the real figure, write a bracketed placeholder the candidate fills "
    "in — '[X]%', '[X] to [Y]', 'from [X] to [Y] minutes'. A bracket is a prompt to supply "
    "a defensible number; silence is a bullet that says nothing. NEVER invent a specific "
    "figure and present it as fact.\n"
    f"• AT MOST {MAX_BULLETS_PER_ENTRY} BULLETS PER ROLE OR PROJECT, and at most about "
    f"{MAX_BULLET_WORDS} words each — two printed lines. Anything past the fifth bullet is "
    "discarded by the renderer, so a sixth is wasted work. Order them most-relevant-first "
    "for this job; the reader stops early, so spend the first bullet well.\n"
    "• EVERY BULLET MUST EARN ITS PLACE. No two bullets may carry the same information. If "
    "one bullet is the implementation detail of another, merge them or drop the weaker. If "
    "two bullets both mention monitoring, or both mention cost, they are one bullet.\n"
    "• VARY THE SENTENCE SHAPE. Do not end bullet after bullet with an em dash or comma "
    "followed by a participial clause ('…, tuning X', '…, enabling Y', '…, so Z was "
    "measured not assumed'). That pattern repeated down a list is the single clearest "
    "signal of machine-drafted text, and reviewers penalise it. Vary how bullets open and "
    "how they close; let some simply end.\n"
    "• OWN THE WORK. Lead with what the candidate did. 'Worked with infrastructure "
    "engineers to size the GPU nodes' hands the achievement to someone else — 'Sized and "
    "secured the VPC-isolated GPU nodes' keeps it. Cut 'helped', 'was responsible for', "
    "'assisted with', 'participated in', 'involved in'.\n"
    "• DO NOT CONTRADICT YOURSELF. Mutually exclusive descriptors in one phrase read as "
    "carelessness: 'on-premises/VPC-isolated' is two different deployments. Pick the one "
    "that was true, or describe the variation plainly ('on-prem and VPC-isolated "
    "deployments, depending on the client').\n"
    "• PLAIN, CONCRETE LANGUAGE, British/Irish spelling. No 'leveraged', 'spearheaded', "
    "'utilised', 'passionate', 'cutting-edge', 'robust', 'seamless', 'best-in-class'."
)

CRITIC_CHECKLIST = (
    "WRITING QUALITY — judge these as strictly as you judge accuracy.\n"
    "• QUANTIFICATION: does each bullet state an outcome, with a figure or a bracketed "
    "placeholder? Flag any bullet that only describes an activity.\n"
    f"• LENGTH: more than {MAX_BULLETS_PER_ENTRY} bullets under one entry, or a bullet "
    f"much over {MAX_BULLET_WORDS} words, is a finding.\n"
    "• REDUNDANCY: do any two bullets carry the same information, or is one the "
    "implementation detail of another? Name both and say which to drop.\n"
    "• RHYTHM: count the bullets ending in an em dash or comma plus a participial clause. "
    "If it is most of them, that is a finding — the document reads as machine-drafted.\n"
    "• OWNERSHIP: flag 'worked with', 'helped', 'was responsible for', 'assisted', and any "
    "phrasing that credits the work to someone else.\n"
    "• CONTRADICTION: flag mutually exclusive descriptors sitting in one phrase.\n"
    "• FILLER: flag 'leveraged', 'spearheaded', 'utilised', 'robust', 'seamless', "
    "'cutting-edge', 'best-in-class', 'passionate'."
)
