"""Which applicant tracking system hosts a job's application form.

Stage 2 of the apply agent, and the insight the whole feature rests on: JobPilot pulls
from seven connectors, but five of them only ever hand back a tracking redirect. Follow
it and you land on one of roughly a dozen ATS platforms. Thousands of employers, a dozen
systems — so later stages need a handful of form handlers, not one per job board.

Two halves, deliberately separated:

* :func:`detect_ats` and friends are **pure** — a URL in, an identifier out, no network.
  That makes the interesting logic (which is all pattern-matching) exhaustively testable.
* :func:`resolve_apply_url` does the network hop, taking an injected client so tests never
  reach the internet.

This generalises :func:`jobpilot_core.ats_boards.extract_greenhouse_slugs`, which already
harvests Greenhouse slugs out of aggregator apply links; the same trick works across every
platform below once you know what their URLs look like.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

#: Terminal outcome when the chain lands somewhere we do not recognise. Distinct from
#: ``None`` (never attempted) — an unknown host is a real, recorded answer.
UNKNOWN = "unknown"
#: The chain never left a job board, so there is no application form behind it.
AGGREGATOR = "aggregator"
#: The host refused the request (bot protection, rate limit, auth wall), so we never got
#: to see where the link leads. Emphatically *not* the same as AGGREGATOR: one says "there
#: is no form here", the other says "we were not allowed to look". Conflating them would
#: report jobs as unreachable when they may well be applicable by hand — and would bury
#: the fact that a whole connector is being blocked.
BLOCKED = "blocked"

#: Statuses meaning "this host dislikes the method", worth one retry with GET.
_RETRY_WITH_GET = (400, 404, 405, 501)
#: Statuses meaning "we are being refused", which no retry of ours should try to defeat.
_BLOCKED_STATUSES = (401, 403, 429, 451)

# --------------------------------------------------------------------------- #
# Platform patterns
# --------------------------------------------------------------------------- #
#: ``ats_id -> host suffixes``. Matched against the registered domain, so a subdomain
#: (``acme.myworkdayjobs.com``) matches its platform without listing every tenant.
ATS_HOSTS: dict[str, tuple[str, ...]] = {
    "greenhouse": ("greenhouse.io", "boards.greenhouse.io", "job-boards.greenhouse.io"),
    "lever": ("lever.co", "jobs.lever.co", "hire.lever.co"),
    "workday": ("myworkdayjobs.com", "myworkdaysite.com", "wd1.myworkdayjobs.com"),
    "ashby": ("ashbyhq.com", "jobs.ashbyhq.com"),
    "smartrecruiters": ("smartrecruiters.com",),
    "workable": ("workable.com", "apply.workable.com"),
    "teamtailor": ("teamtailor.com",),
    "personio": ("personio.de", "personio.com", "jobs.personio.de"),
    "recruitee": ("recruitee.com",),
    "bamboohr": ("bamboohr.com", "bamboohr.co.uk"),
    "jobvite": ("jobvite.com",),
    "breezy": ("breezy.hr",),
    "pinpoint": ("pinpointhq.com",),
    "icims": ("icims.com",),
    "taleo": ("taleo.net",),
    "successfactors": ("successfactors.com", "successfactors.eu"),
    "oraclecloud": ("oraclecloud.com",),
    # Irish-market platforms — this pool is Ireland-focused, so these earn their place.
    "rezoomo": ("rezoomo.com",),
    "occupop": ("occupop.com",),
}

#: Hosts that are job boards, not application forms. Landing here means the redirect
#: chain did not complete — worth recording as its own outcome rather than "unknown",
#: because it tells you the listing has no reachable form rather than an unfamiliar one.
AGGREGATOR_HOSTS: frozenset[str] = frozenset({
    "jooble.org", "adzuna.com", "adzuna.ie", "adzuna.co.uk",
    "careerjet.com", "careerjet.ie", "careerjet.co.uk",
    "themuse.com", "indeed.com", "ie.indeed.com", "uk.indeed.com",
    "linkedin.com", "glassdoor.com", "monster.ie", "irishjobs.ie",
    "jobs.ie", "recruitireland.com", "totaljobs.com", "reed.co.uk",
})

#: Employer-slug extractors, per platform. Only defined where the slug is unambiguous in
#: the path — a wrong slug is worse than no slug, so anything doubtful is left out.
_SLUG_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "greenhouse": (
        re.compile(r"/(?:embed/job_board\?for=|boards/)?([A-Za-z0-9_-]+)/jobs/\d+"),
        re.compile(r"job_board\?for=([A-Za-z0-9_-]+)"),
        re.compile(r"^/([A-Za-z0-9_-]+)/jobs/"),
    ),
    "lever": (re.compile(r"^/([A-Za-z0-9_-]+)/"),),
    "ashby": (re.compile(r"^/([A-Za-z0-9_-]+)/"),),
    "recruitee": (),   # slug is the subdomain; handled below
    "teamtailor": (),
    "personio": (),
    "workable": (re.compile(r"^/([A-Za-z0-9_-]+)/"),),
}

#: Platforms where the employer is the subdomain (``acme.recruitee.com``).
_SUBDOMAIN_SLUG: frozenset[str] = frozenset({
    "recruitee", "teamtailor", "personio", "workday", "bamboohr", "pinpoint",
})


def from_connector(connector: str | None) -> tuple[str, str | None] | None:
    """Identify the ATS from the connector that fetched the job, with no network at all.

    This outranks URL inspection, and by a wide margin in practice: Greenhouse boards are
    routinely served from the employer's own domain (``www.mongodb.com``,
    ``careers.toasttab.com``, ``stripe.com``), so host matching files them as unrecognised
    even though Scout fetched them *through the Greenhouse API* and recorded which board
    it asked. The connector is first-hand provenance — believe it over a hostname guess.

    Scout writes ``greenhouse:<board-slug>``, so both the platform and the employer come
    for free. Returns ``None`` when the connector says nothing useful (the aggregators
    name themselves, not the ATS behind the listing).
    """
    if not connector:
        return None
    name, _, slug = connector.partition(":")
    name = name.strip().lower()
    if name in ("greenhouse", "gh"):
        return ("greenhouse", slug.strip() or None)
    if name == "lever":
        return ("lever", slug.strip() or None)
    # Direct-ATS connectors added later can be listed here; aggregator connectors
    # deliberately are not, because the ATS behind their listings is unknown until the
    # redirect is followed.
    return None


def _host(url: str | None) -> str:
    """Lowercased hostname without ``www.``; empty string when unparseable."""
    if not url:
        return ""
    try:
        netloc = urlparse(url.strip()).netloc.lower()
    except ValueError:
        return ""
    host = netloc.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _host_matches(host: str, suffix: str) -> bool:
    """True when ``host`` is ``suffix`` or a subdomain of it.

    Suffix matching rather than substring: ``notgreenhouse.io`` must not match
    ``greenhouse.io``, and ``boards.greenhouse.io`` must.
    """
    return host == suffix or host.endswith("." + suffix)


def is_aggregator(url: str | None) -> bool:
    """True when the URL is a job board rather than an application form."""
    host = _host(url)
    return any(_host_matches(host, agg) for agg in AGGREGATOR_HOSTS) if host else False


def detect_ats(url: str | None) -> str:
    """Identify the ATS behind a URL.

    Returns a platform id, :data:`AGGREGATOR` when the URL is still a job board, or
    :data:`UNKNOWN` for an unrecognised host. Never returns ``None`` — every URL gets a
    verdict, because "we looked and did not recognise it" is information a later stage
    needs to act on.
    """
    host = _host(url)
    if not host:
        return UNKNOWN
    # Specific platforms win over the aggregator check: some boards host ATS pages on
    # their own domain, and a real form beats a "this is a job board" verdict.
    for ats_id, suffixes in ATS_HOSTS.items():
        if any(_host_matches(host, suffix) for suffix in suffixes):
            return ats_id
    if is_aggregator(url):
        return AGGREGATOR
    return UNKNOWN


def extract_board_slug(url: str | None, ats: str | None = None) -> str | None:
    """The employer identifier within an ATS, where the URL states it unambiguously.

    ``https://jobs.lever.co/intercom/abc-123`` → ``"intercom"``. Returns ``None`` rather
    than a guess when the platform buries the employer elsewhere — a wrong slug would
    silently point a later stage at another company's board.
    """
    if not url:
        return None
    ats = ats or detect_ats(url)
    host = _host(url)
    if not host or ats in (UNKNOWN, AGGREGATOR):
        return None

    if ats in _SUBDOMAIN_SLUG:
        parts = host.split(".")
        # Need a real subdomain: "recruitee.com" alone names no employer.
        if len(parts) >= 3 and parts[0] not in ("www", "jobs", "careers", "apply"):
            return parts[0]
        return None

    try:
        path = urlparse(url).path or ""
    except ValueError:
        return None
    for pattern in _SLUG_PATTERNS.get(ats, ()):
        match = pattern.search(path)
        if match and match.group(1):
            slug = match.group(1)
            if slug not in ("jobs", "careers", "embed", "apply"):
                return slug
    return None


# --------------------------------------------------------------------------- #
# Redirect resolution
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ApplyTarget:
    """Where a job's application form lives, and how we got there."""

    url: str            # terminal URL after following redirects
    ats: str            # platform id, AGGREGATOR, or UNKNOWN
    board_slug: str | None
    hops: int
    #: Set when the chain could not be followed — the URL is then the last one reached.
    error: str | None = None

    @property
    def reachable(self) -> bool:
        """True when a real application form was identified behind the listing.

        Deliberately about the *classification*, not whether our request succeeded: a
        recognised ATS URL we happened to time out on still points at a real form, and
        ``error`` remains on the record for diagnostics.
        """
        return self.ats not in (AGGREGATOR, UNKNOWN, BLOCKED)

    @property
    def retryable(self) -> bool:
        """True when the outcome says nothing about the listing, only about our attempt.

        A block or a transport failure is worth another look later (the host may relent,
        or a different network may not be refused). A confirmed aggregator dead-end or an
        unrecognised platform is a fact about the page, so re-fetching it changes nothing.
        """
        return self.ats == BLOCKED or (self.error is not None and self.ats == UNKNOWN)


#: Redirect chains beyond this are treated as a loop rather than followed further.
MAX_HOPS = 8


def target_from_url(url: str, hops: int = 0, error: str | None = None) -> ApplyTarget:
    """Build an :class:`ApplyTarget` from an already-terminal URL. Pure — no network."""
    ats = detect_ats(url)
    return ApplyTarget(
        url=url, ats=ats, board_slug=extract_board_slug(url, ats), hops=hops, error=error
    )


def _failed(url: str, hops: int, error: str, *, blocked: bool = False) -> ApplyTarget:
    """Outcome for a request we could not complete.

    The URL still gets a say. If it already names a platform — ``jobs.lever.co/acme/1``
    is Lever whether or not our HEAD timed out — that is the honest classification, and a
    later stage can still work with it. Only when the URL itself is uninformative (an
    aggregator link, or something unrecognised) does the failure leave us genuinely
    ignorant, and *then* the verdict reflects the failure rather than the address.
    """
    ats = detect_ats(url)
    if ats not in (AGGREGATOR, UNKNOWN):
        return ApplyTarget(
            url=url, ats=ats, board_slug=extract_board_slug(url, ats), hops=hops, error=error
        )
    return ApplyTarget(
        url=url, ats=BLOCKED if blocked else UNKNOWN, board_slug=None, hops=hops, error=error
    )


async def resolve_apply_url(
    client, url: str, *, max_hops: int = MAX_HOPS, connector: str | None = None
) -> ApplyTarget:
    """Follow ``url``'s redirect chain to the page that hosts the application form.

    When ``connector`` identifies the platform outright (a direct-ATS fetch), the answer
    is already known and no request is made at all — the fast path for the largest and
    most reliable slice of the pool.

    ``client`` is an injected httpx-compatible async client, so this is testable without
    network access. Redirects are followed manually rather than with ``follow_redirects``
    because the hop count and the terminal URL are both results we want to record.

    A HEAD request is tried first — it is cheap and most redirectors honour it. Hosts that
    reject HEAD (405/501, and some that simply 404 it) fall back to GET. Any transport
    failure returns the last URL reached with ``error`` set, never raises: one unreachable
    listing must not abort a batch of hundreds.
    """
    current = (url or "").strip()
    if not current:
        return ApplyTarget(url="", ats=UNKNOWN, board_slug=None, hops=0, error="empty url")

    # Provenance beats inspection. A job Scout pulled from the Greenhouse API is on
    # Greenhouse whatever domain the board is served from, so this costs no request and
    # is more reliable than anything the URL could tell us.
    known = from_connector(connector)
    if known:
        ats, slug = known
        return ApplyTarget(
            url=current, ats=ats, board_slug=slug or extract_board_slug(current, ats), hops=0
        )

    seen: set[str] = set()
    for hop in range(max_hops):
        if current in seen:
            return target_from_url(current, hop, "redirect loop")
        seen.add(current)
        try:
            response = await client.head(current, follow_redirects=False)
            # Some hosts simply don't serve HEAD. One retry with GET is a protocol
            # courtesy, not an attempt to get around anything.
            if response.status_code in _RETRY_WITH_GET:
                response = await client.get(current, follow_redirects=False)
        except Exception as exc:  # noqa: BLE001 — a dead listing is data, not a crash
            return _failed(current, hop, f"{type(exc).__name__}: {exc}"[:200])

        status = response.status_code

        # Refused. We do not retry, vary the user agent, or otherwise try to look like
        # someone else — the honest outcome is "we were not allowed to look", which is
        # information, and defeating bot protection is out of scope by design.
        if status in _BLOCKED_STATUSES:
            return _failed(
                current, hop, f"HTTP {status} — host refused the request", blocked=True
            )

        if status >= 400:
            return _failed(current, hop, f"HTTP {status}")

        if status not in (301, 302, 303, 307, 308):
            return target_from_url(current, hop)

        location = response.headers.get("location")
        if not location:
            return target_from_url(current, hop, "redirect without location header")
        # Relative redirects are common on the second hop.
        current = str(response.url.join(location)) if hasattr(response, "url") else location

    return target_from_url(current, max_hops, "too many redirects")
