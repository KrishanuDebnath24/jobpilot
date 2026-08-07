"""Stage 2 of the apply agent: finding where a job's application form actually lives.

Pure-logic tests plus a resolver exercised against a fake client — no network. The
detection half matters more than it looks: five of seven connectors hand back a tracking
redirect, so getting the terminal platform wrong means a later stage drives the wrong
form, and getting an employer slug wrong means pointing at another company's board.
"""

from __future__ import annotations

import pytest
from jobpilot_core import ats_detect as ad


# --------------------------------------------------------------------------- #
# Platform detection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://boards.greenhouse.io/intercom/jobs/4012345", "greenhouse"),
        ("https://job-boards.greenhouse.io/stripe/jobs/5", "greenhouse"),
        ("https://jobs.lever.co/intercom/8f2a-1", "lever"),
        ("https://acme.myworkdayjobs.com/en-US/careers/job/Dublin/Engineer_R-1", "workday"),
        ("https://jobs.ashbyhq.com/openai/abc-123", "ashby"),
        ("https://jobs.smartrecruiters.com/Acme/74400", "smartrecruiters"),
        ("https://apply.workable.com/acme/j/ABC123/", "workable"),
        ("https://acme.teamtailor.com/jobs/1234-engineer", "teamtailor"),
        ("https://acme.recruitee.com/o/backend-engineer", "recruitee"),
        ("https://acme.bamboohr.com/careers/42", "bamboohr"),
        ("https://careers.icims.com/jobs/1234/engineer/job", "icims"),
        ("https://acme.taleo.net/careersection/jobdetail.ftl?job=1", "taleo"),
        ("https://www.rezoomo.com/job/12345/", "rezoomo"),
    ],
)
def test_detect_known_platforms(url, expected):
    assert ad.detect_ats(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://ie.jooble.org/jdp/12345",
        "https://www.adzuna.ie/details/98765",
        "https://www.careerjet.ie/jobad/ie123",
        "https://www.themuse.com/jobs/acme/engineer",
        "https://www.indeed.com/viewjob?jk=abc",
        "https://www.linkedin.com/jobs/view/123456",
        "https://www.irishjobs.ie/job/12345",
    ],
)
def test_aggregators_are_flagged_not_treated_as_forms(url):
    """Landing on a job board means the chain didn't complete — a distinct outcome from
    an unrecognised host, because it says there's no reachable form behind the listing."""
    assert ad.detect_ats(url) == ad.AGGREGATOR
    assert ad.is_aggregator(url) is True


def test_unknown_host_is_recorded_not_guessed():
    assert ad.detect_ats("https://careers.some-random-company.ie/apply/7") == ad.UNKNOWN


def test_every_url_gets_a_verdict():
    """Never ``None``: "we looked and didn't recognise it" is information downstream needs."""
    for url in (None, "", "not a url", "ftp://x", "https://"):
        assert ad.detect_ats(url) in (ad.UNKNOWN, ad.AGGREGATOR)


def test_suffix_matching_does_not_match_lookalike_domains():
    """Substring matching would make ``notgreenhouse.io`` a Greenhouse board."""
    assert ad.detect_ats("https://notgreenhouse.io/acme/jobs/1") == ad.UNKNOWN
    assert ad.detect_ats("https://greenhouse.io.evil.example/x") == ad.UNKNOWN
    assert ad.detect_ats("https://boards.greenhouse.io/acme/jobs/1") == "greenhouse"


def test_www_and_port_and_userinfo_are_stripped():
    assert ad.detect_ats("https://www.jobs.lever.co/acme/1") == "lever"
    assert ad.detect_ats("https://jobs.lever.co:443/acme/1") == "lever"


# --------------------------------------------------------------------------- #
# Employer slug extraction
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://boards.greenhouse.io/intercom/jobs/4012345", "intercom"),
        ("https://jobs.lever.co/intercom/8f2a-1", "intercom"),
        ("https://jobs.ashbyhq.com/openai/abc-123", "openai"),
        ("https://apply.workable.com/acme/j/ABC123/", "acme"),
        ("https://acme.recruitee.com/o/backend-engineer", "acme"),
        ("https://acmecorp.teamtailor.com/jobs/1234", "acmecorp"),
        ("https://acme.myworkdayjobs.com/en-US/careers/job/x", "acme"),
    ],
)
def test_extract_board_slug(url, expected):
    assert ad.extract_board_slug(url) == expected


def test_slug_is_none_rather_than_a_guess():
    """A wrong slug silently points a later stage at another company's board, so an
    ambiguous URL must yield nothing at all."""
    assert ad.extract_board_slug("https://ie.jooble.org/jdp/1") is None
    assert ad.extract_board_slug("https://careers.unknown.ie/apply") is None
    assert ad.extract_board_slug(None) is None
    # Bare platform domain names no employer.
    assert ad.extract_board_slug("https://recruitee.com/o/job") is None


def test_generic_path_segments_are_not_mistaken_for_employers():
    assert ad.extract_board_slug("https://jobs.lever.co/jobs/123") is None
    assert ad.extract_board_slug("https://jobs.teamtailor.com/jobs/1") is None


# --------------------------------------------------------------------------- #
# Redirect resolution (fake client — no network)
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status_code: int, location: str | None = None, url: str = ""):
        self.status_code = status_code
        self.headers = {"location": location} if location else {}
        self.url = FakeURL(url)


class FakeURL:
    """Minimal stand-in for httpx.URL — only ``join`` is used, for relative redirects."""

    def __init__(self, value: str):
        self.value = value

    def join(self, other: str) -> str:
        if other.startswith("http"):
            return other
        from urllib.parse import urljoin

        return urljoin(self.value, other)

    def __str__(self) -> str:
        return self.value


class FakeClient:
    """Serves a scripted redirect chain and records how it was called."""

    def __init__(self, chain: dict[str, FakeResponse], head_unsupported: bool = False):
        self.chain = chain
        self.head_unsupported = head_unsupported
        self.head_calls: list[str] = []
        self.get_calls: list[str] = []

    async def head(self, url: str, follow_redirects: bool = False):
        self.head_calls.append(url)
        if self.head_unsupported:
            return FakeResponse(405, url=url)
        return self.chain.get(url, FakeResponse(200, url=url))

    async def get(self, url: str, follow_redirects: bool = False):
        self.get_calls.append(url)
        return self.chain.get(url, FakeResponse(200, url=url))


@pytest.mark.asyncio
async def test_resolves_aggregator_redirect_to_the_real_ats():
    """The core Stage 2 scenario: Jooble link in, Lever form out."""
    start = "https://ie.jooble.org/jdp/999"
    final = "https://jobs.lever.co/intercom/8f2a-1"
    client = FakeClient({start: FakeResponse(302, final, url=start)})

    target = await ad.resolve_apply_url(client, start)

    assert target.url == final
    assert target.ats == "lever"
    assert target.board_slug == "intercom"
    assert target.hops == 1
    assert target.reachable is True
    assert target.error is None


@pytest.mark.asyncio
async def test_follows_a_multi_hop_chain():
    a = "https://www.adzuna.ie/details/1"
    b = "https://adzuna-tracker.example/click?x=1"
    c = "https://boards.greenhouse.io/stripe/jobs/77"
    client = FakeClient({
        a: FakeResponse(302, b, url=a),
        b: FakeResponse(301, c, url=b),
    })

    target = await ad.resolve_apply_url(client, a)

    assert target.url == c
    assert target.ats == "greenhouse"
    assert target.board_slug == "stripe"
    assert target.hops == 2


@pytest.mark.asyncio
async def test_direct_ats_url_needs_no_hops():
    url = "https://boards.greenhouse.io/intercom/jobs/1"
    target = await ad.resolve_apply_url(FakeClient({}), url)
    assert target.url == url
    assert target.ats == "greenhouse"
    assert target.hops == 0


@pytest.mark.asyncio
async def test_relative_redirect_is_joined_against_the_current_url():
    start = "https://careers.example.com/jobs/1"
    client = FakeClient({start: FakeResponse(302, "/apply/1", url=start)})
    target = await ad.resolve_apply_url(client, start)
    assert target.url == "https://careers.example.com/apply/1"


@pytest.mark.asyncio
async def test_falls_back_to_get_when_head_is_rejected():
    """Plenty of redirectors answer HEAD with 405 — the hop still has to be followed."""
    start = "https://ie.jooble.org/jdp/1"
    final = "https://jobs.lever.co/acme/1"
    client = FakeClient({start: FakeResponse(302, final, url=start)}, head_unsupported=True)

    target = await ad.resolve_apply_url(client, start)

    assert target.ats == "lever"
    assert client.head_calls and client.get_calls  # tried HEAD, then GET


@pytest.mark.asyncio
async def test_redirect_loop_is_detected_rather_than_spun_on():
    a, b = "https://x.example/1", "https://x.example/2"
    client = FakeClient({a: FakeResponse(302, b, url=a), b: FakeResponse(302, a, url=b)})
    target = await ad.resolve_apply_url(client, a)
    assert target.error == "redirect loop"


@pytest.mark.asyncio
async def test_chain_longer_than_max_hops_gives_up():
    chain = {
        f"https://h.example/{i}": FakeResponse(302, f"https://h.example/{i + 1}", url=f"https://h.example/{i}")
        for i in range(20)
    }
    target = await ad.resolve_apply_url(chain and FakeClient(chain), "https://h.example/0", max_hops=3)
    assert target.error == "too many redirects"
    assert target.hops == 3


@pytest.mark.asyncio
async def test_transport_failure_returns_data_not_an_exception():
    """One dead listing must never abort a batch of hundreds."""

    class Boom:
        async def head(self, url, follow_redirects=False):
            raise ConnectionError("host unreachable")

        async def get(self, url, follow_redirects=False):
            raise ConnectionError("host unreachable")

    target = await ad.resolve_apply_url(Boom(), "https://dead.example/1")
    assert target.error is not None
    assert "ConnectionError" in target.error
    assert target.reachable is False


@pytest.mark.asyncio
async def test_chain_that_never_leaves_the_aggregator_is_marked_unreachable():
    url = "https://ie.jooble.org/jdp/5"
    target = await ad.resolve_apply_url(FakeClient({}), url)
    assert target.ats == ad.AGGREGATOR
    assert target.reachable is False


@pytest.mark.asyncio
async def test_empty_url_is_handled():
    target = await ad.resolve_apply_url(FakeClient({}), "")
    assert target.error == "empty url"
    assert target.reachable is False


@pytest.mark.asyncio
async def test_redirect_without_location_header_is_reported():
    start = "https://x.example/1"
    client = FakeClient({start: FakeResponse(302, None, url=start)})
    target = await ad.resolve_apply_url(client, start)
    assert target.error == "redirect without location header"


def test_reachable_reflects_classification_not_fetch_success():
    """``reachable`` answers "is there a form here", not "did our request work".

    Those came apart in practice: a Lever URL that timed out is still a Lever form, and
    treating the timeout as "unreachable" hid applicable jobs. The error stays on the
    record as diagnostics rather than changing the verdict.
    """
    assert ad.target_from_url("https://jobs.lever.co/acme/1").reachable is True
    assert ad.target_from_url("https://ie.jooble.org/1").reachable is False
    assert ad.target_from_url("https://unknown.example/1").reachable is False
    # A transient failure against a known platform does not demote it.
    errored = ad.target_from_url("https://jobs.lever.co/acme/1", error="boom")
    assert errored.reachable is True
    assert errored.error == "boom"


# --------------------------------------------------------------------------- #
# Refusal vs. dead end — the distinction found empirically against Adzuna
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [401, 403, 429, 451])
@pytest.mark.asyncio
async def test_refusal_is_blocked_not_a_dead_end(status):
    """A host refusing us says nothing about whether the job has a form.

    This was a real bug: Adzuna's CloudFront answers 403, and because the terminal URL
    was still an adzuna.co.uk address the result got filed as AGGREGATOR — reporting
    "no application form behind this listing" for jobs that are perfectly applicable.
    """
    url = "https://www.adzuna.co.uk/jobs/land/ad/123"
    client = FakeClient({url: FakeResponse(status, url=url)})

    target = await ad.resolve_apply_url(client, url)

    assert target.ats == ad.BLOCKED
    assert target.ats != ad.AGGREGATOR
    assert target.reachable is False
    assert target.retryable is True     # worth another look later
    assert str(status) in (target.error or "")


@pytest.mark.asyncio
async def test_blocked_response_is_not_retried_with_a_different_identity():
    """We do not vary the request to get around a refusal — one HEAD, then stop."""
    url = "https://blocked.example/1"
    client = FakeClient({url: FakeResponse(403, url=url)})
    await ad.resolve_apply_url(client, url)
    assert len(client.head_calls) == 1
    assert client.get_calls == []


@pytest.mark.asyncio
async def test_method_not_allowed_still_retries_with_get():
    """A protocol courtesy, distinct from circumventing a block."""
    url = "https://picky.example/1"
    final = "https://jobs.lever.co/acme/1"
    client = FakeClient({url: FakeResponse(302, final, url=url)}, head_unsupported=True)
    target = await ad.resolve_apply_url(client, url)
    assert target.ats == "lever"
    assert client.get_calls  # the fallback ran


@pytest.mark.asyncio
async def test_server_error_yields_unknown_not_a_platform_guess():
    """A broken page tells us nothing about the platform behind it, so guessing from the
    failing URL's host would file a dead link as a confirmed verdict."""
    url = "https://ie.jooble.org/jdp/1"
    client = FakeClient({url: FakeResponse(500, url=url)})
    target = await ad.resolve_apply_url(client, url)
    assert target.ats == ad.UNKNOWN
    assert target.error == "HTTP 500"
    assert target.reachable is False


@pytest.mark.asyncio
async def test_genuine_aggregator_dead_end_is_not_retryable():
    """A 200 that stays on a job board really is a dead end — re-fetching won't help."""
    url = "https://ie.jooble.org/jdp/1"
    target = await ad.resolve_apply_url(FakeClient({}), url)
    assert target.ats == ad.AGGREGATOR
    assert target.retryable is False


@pytest.mark.asyncio
async def test_transport_failure_is_retryable():
    class Boom:
        async def head(self, url, follow_redirects=False):
            raise TimeoutError("slow")

        async def get(self, url, follow_redirects=False):
            raise TimeoutError("slow")

    target = await ad.resolve_apply_url(Boom(), "https://slow.example/1")
    assert target.retryable is True
    assert target.reachable is False


# --------------------------------------------------------------------------- #
# Connector provenance — beats URL inspection, and costs no request
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "connector,ats,slug",
    [
        ("greenhouse:mongodb", "greenhouse", "mongodb"),
        ("greenhouse:intercom", "greenhouse", "intercom"),
        ("greenhouse", "greenhouse", None),
        ("gh", "greenhouse", None),
    ],
)
def test_from_connector_identifies_platform_and_employer(connector, ats, slug):
    assert ad.from_connector(connector) == (ats, slug)


@pytest.mark.parametrize("connector", ["adzuna", "jooble", "careerjet", "muse", "crawl:irishjobs", None, ""])
def test_aggregator_connectors_reveal_nothing_about_the_ats(connector):
    """An aggregator names itself, not the system behind the listing — so it must not
    short-circuit resolution."""
    assert ad.from_connector(connector) is None


@pytest.mark.asyncio
async def test_custom_domain_greenhouse_is_recognised_via_connector():
    """The case that made this necessary.

    Greenhouse boards are commonly served from the employer's own domain, so host
    matching alone files 148 of this pool's jobs as "unrecognised" despite Scout having
    fetched them straight from the Greenhouse API.
    """
    url = "https://www.mongodb.com/careers/job/12345"
    assert ad.detect_ats(url) == ad.UNKNOWN          # host alone cannot tell

    client = FakeClient({})
    target = await ad.resolve_apply_url(client, url, connector="greenhouse:mongodb")

    assert target.ats == "greenhouse"
    assert target.board_slug == "mongodb"
    assert target.reachable is True
    assert client.head_calls == [] and client.get_calls == []   # no request at all


@pytest.mark.asyncio
async def test_connector_shortcut_makes_no_network_call():
    client = FakeClient({})
    await ad.resolve_apply_url(client, "https://stripe.com/jobs/1", connector="greenhouse:stripe")
    assert client.head_calls == []
    assert client.get_calls == []


@pytest.mark.asyncio
async def test_aggregator_connector_still_follows_the_chain():
    start = "https://ie.jooble.org/jdp/1"
    final = "https://jobs.lever.co/acme/1"
    client = FakeClient({start: FakeResponse(302, final, url=start)})
    target = await ad.resolve_apply_url(client, start, connector="jooble")
    assert target.ats == "lever"
    assert client.head_calls  # the hop was made


# --------------------------------------------------------------------------- #
# A failed fetch must not erase what the URL already tells us
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_timeout_on_a_recognised_ats_url_keeps_the_platform():
    """Found live: a jobs.lever.co URL timed out and was filed as "unknown".

    The address names the platform. Our inability to HEAD it is incidental — the form is
    still a Lever form, and a later stage can still work with it.
    """
    url = "https://jobs.lever.co/whoop/abc-123"

    class Boom:
        async def head(self, u, follow_redirects=False):
            raise TimeoutError("slow")

        async def get(self, u, follow_redirects=False):
            raise TimeoutError("slow")

    target = await ad.resolve_apply_url(Boom(), url)

    assert target.ats == "lever"
    assert target.board_slug == "whoop"
    assert target.reachable is True
    assert target.error is not None      # the failure is still on the record


@pytest.mark.asyncio
async def test_failure_on_an_uninformative_url_stays_unknown():
    """The other half: when the address says nothing, a failure leaves us ignorant."""
    url = "https://jobviewtrack.com/click/xyz"

    class Boom:
        async def head(self, u, follow_redirects=False):
            raise TimeoutError("slow")

        async def get(self, u, follow_redirects=False):
            raise TimeoutError("slow")

    target = await ad.resolve_apply_url(Boom(), url)
    assert target.ats == ad.UNKNOWN
    assert target.reachable is False


@pytest.mark.asyncio
async def test_403_on_a_recognised_ats_url_keeps_the_platform():
    url = "https://boards.greenhouse.io/acme/jobs/1"
    client = FakeClient({url: FakeResponse(403, url=url)})
    target = await ad.resolve_apply_url(client, url)
    assert target.ats == "greenhouse"
    assert target.reachable is True


@pytest.mark.asyncio
async def test_403_on_an_aggregator_url_is_blocked_not_a_dead_end():
    url = "https://www.adzuna.co.uk/jobs/land/ad/1"
    client = FakeClient({url: FakeResponse(403, url=url)})
    target = await ad.resolve_apply_url(client, url)
    assert target.ats == ad.BLOCKED
    assert target.reachable is False


def test_lever_connector_is_recognised():
    assert ad.from_connector("lever:whoop") == ("lever", "whoop")
