"""ATS board coverage — which Greenhouse boards to poll.

Greenhouse's public Job Board API is *per board*
(``boards-api.greenhouse.io/v1/boards/<slug>/jobs``): there is no endpoint that answers
"who is hiring in Dublin". So the board list is curated, merged with the user's
configured ``target_employers``.

Slugs are just data mapped into an API path; a bad slug 404s and is recorded as a
per-board error, never fatal.

**On harvesting.** This module used to derive slugs from the apply links of jobs already
fetched, on the theory that aggregator listings would point at ATS boards we did not
know about. Measured against the real pool, that returned nothing: **0 of 398 aggregator
apply URLs contained any ATS domain at all**, and the harvester produced 0 boards over
its lifetime. The aggregators it fed on have since been removed, so the code went with
them. Board discovery is a research activity done deliberately and verified before a
slug is added here — see the note on verification below.

**On verification.** Every slug below was confirmed live against the boards API, and
then confirmed to actually be that company by checking the job locations. That second
check is not optional: name-derived slugs resolve to the *wrong company* often enough to
matter. ``theorchard`` is Sony Music in New York, not the Celbridge firm; ``easygo`` is a
Melbourne gaming company, not the Maynooth EV-charging one; ``pmg`` is in Dallas. All
returned HTTP 200 with real jobs. A 200 proves a board exists, not that it is the right
board.
"""

from __future__ import annotations

from collections.abc import Iterable

# Verified Irish Greenhouse boards: each resolved live against the boards API *and*
# confirmed to be the named company by inspecting job locations. The trailing comment is
# the Irish job count at the time of verification (2026-08-07) — a rough sense of yield,
# not a contract.
#
# Deliberately excluded despite resolving cleanly, because they carry no Irish roles:
# accenturefederalservices (617 jobs, all US federal), dmgmedia (London), nuitee
# (Barcelona/EMEA), scalapaysrl (Milan).
DEFAULT_GREENHOUSE_BOARDS: list[str] = [
    "phonewatchireland",  # 38
    "prepaypower",        # 17
    "lotusworks",         #  6
    "amach",              #  8
    "learnupon",          #  8
    "yunoenergy",         # 11
    "goal",               #  5
    "tines",              #  4
    "nearform",           #  4
    "portwest",           #  4
    "homesecure",         #  4
    "payslipltd",         #  4
    "cityswift",          #  3
    "inizio",             #  3
    "accountsiq",         #  3
    "esw",                #  2
    "flipdish",           #  1
    "poppulo",            #  1
    # Larger employers with a real Dublin presence. These are global boards, so the
    # connector's Republic-of-Ireland filter is what keeps them from swamping the pool.
    "intercom",
    "stripe",
    "datadog",
    "twilio",
    "letsgetchecked",
    "mongodb",
    "cloudflare",
    "udemy",
]


def merge_boards(
    configured: Iterable[str] | None,
    *,
    include_defaults: bool = True,
    cap: int = 40,
) -> list[str]:
    """Union of the user's configured employers and the curated defaults —
    de-duplicated (case-insensitive), configured-first, capped for politeness/time."""
    seen: set[str] = set()
    out: list[str] = []
    groups: list[Iterable[str]] = [configured or []]
    if include_defaults:
        groups.append(DEFAULT_GREENHOUSE_BOARDS)
    for group in groups:
        for slug in group:
            key = (slug or "").strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(key)
            if len(out) >= cap:
                return out
    return out
