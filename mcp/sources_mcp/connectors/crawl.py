"""Crawler connector — one polite crawler for an Irish board that has no public API.

Which board is a *setting* (``crawler_board``), because a crawl target can be taken away
without notice and this one was. irishjobs.ie (and its sibling jobs.ie, both StepStone)
now answer **403 to every request, robots.txt included**, from a browser User-Agent as
readily as from ours — measured 2026-09-18. There is no code fix for that: the site has
withdrawn consent, and working around bot protection is the thing this project has
already declined to do elsewhere (see the stage-2 resolver). The board stays in the
registry because the block may lift.

``jobsireland.ie`` — the Department of Social Protection's own board — is the working
target. ``robots.txt`` disallows nothing, its results are server-rendered, and unlike the
other candidates its **keyword search actually filters server-side** (``accountant``
returns 2, ``analyst`` returns 25). Two things about it are load-bearing and were measured
rather than assumed:

- Address it **without** ``www``. ``www.jobsireland.ie`` answers 500 on every
  ``job-Details`` id; the bare host serves the same ids 200.
- Its listing lives behind ``/Jobsireland.API/JobsIreland/BrowseJobs``, which returns an
  HTML fragment, not JSON. ``PageSize`` bounds a call and ``PageNumber`` is ignored, so one
  request per search term returns that term's whole result set.

Also ruled out, so nobody re-tries them: ``jobs.ie`` 403s every path including its
homepage (same owner as irishjobs.ie); ``publicjobs.ie`` and ``recruitireland.com`` render
their listings client-side or ignore the keyword entirely.

"""

from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import re
import time
import urllib.robotparser as robotparser
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from jobpilot_core.job_match import GRADE_WORDS, match_tokens, title_matches
from jobpilot_core.llm import LLMUsage
from jobpilot_core.roi_location import is_roi
from jobpilot_core.scout import RawJob
from jobpilot_core.settings import Settings

# Browser-shaped request headers. The site throttles/hangs bare requests; a full,
# mainstream header set is what a normal browser sends and is what reliably returns
# the server-rendered HTML. Politeness is still enforced via robots + rate limit + caps.
def _headers(ua: str, referer: str | None = None) -> dict[str, str]:
    h = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IE,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
    }
    if referer:
        h["Referer"] = referer
    return h


logger = logging.getLogger("jobpilot.sources")

# Per-domain rate-limit state (single-process worker). Politeness, not correctness.
_last_request: dict[str, float] = {}
_rate_lock = asyncio.Lock()


class CrawlBlocked(RuntimeError):
    """robots.txt disallows the target URL."""


class CrawlUnavailable(RuntimeError):
    """The board refused or failed to serve its search page.

    Raised rather than returning an empty list, because those two outcomes mean opposite
    things: "no Irish jobs matched today" is a result, "the site 403s us now" is an
    outage that needs a human. Returning ``[]`` for the second is how irishjobs.ie's
    block went unnoticed — the connector reported 0 jobs and no error for weeks.
    """


async def _rate_limit(domain: str, delay: float) -> None:
    async with _rate_lock:
        wait = delay - (time.monotonic() - _last_request.get(domain, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request[domain] = time.monotonic()


async def _robots_ok(client: httpx.AsyncClient, url: str, ua: str) -> bool:
    p = urlparse(url)
    try:
        r = await client.get(f"{p.scheme}://{p.netloc}/robots.txt")
        if r.status_code != 200:
            return True  # no robots.txt -> crawling permitted by convention
        rp = robotparser.RobotFileParser()
        rp.parse(r.text.splitlines())
        return rp.can_fetch(ua, url)
    except httpx.HTTPError:
        return True


async def _get_with_retry(
    client: httpx.AsyncClient, url: str, *, referer: str | None, ua: str, tries: int = 3
) -> httpx.Response | None:
    """GET with a few retries — the board occasionally disconnects mid-handshake on the
    first hit. Returns the response, or None if every attempt failed."""
    for attempt in range(tries):
        try:
            return await client.get(url, headers=_headers(ua, referer))
        except httpx.HTTPError:
            if attempt == tries - 1:
                return None
            await asyncio.sleep(1.0 * (attempt + 1))
    return None


def _extract_posting_links(
    page_html: str, base_url: str, posting_path_re: re.Pattern[str]
) -> list[str]:
    """Deterministically pull the individual posting URLs out of a search-results page,
    absolutized and de-duplicated in document order. The shape of a posting path differs
    per board, so the board supplies the pattern."""
    soup = BeautifulSoup(page_html, "html.parser")
    seen: dict[str, None] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0].split("#")[0]
        if not href:
            continue
        path = urlparse(href).path or href
        if posting_path_re.match(path) and "/apply" not in path and "/save" not in path:
            absolute = urljoin(base_url, href)
            seen.setdefault(absolute, None)
    return list(seen.keys())


def _slug_text(url: str) -> str:
    """The words in a posting URL, as a title-ish string for the pre-filter below.
    Trailing ids and separators become spaces: ``/company/cpl/job/learner-coordinator-33390``
    -> ``company cpl job learner coordinator``."""
    words = re.split(r"[^A-Za-z]+", urlparse(url).path)
    return " ".join(w for w in words if len(w) > 1)


def _strip_html(value: str | None) -> str:
    if not value:
        return ""
    text = BeautifulSoup(_html.unescape(value), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _location_str(job_location) -> str | None:
    """Flatten schema.org ``jobLocation`` (Place → PostalAddress) to a readable string.
    Handles a single object or a list; returns the first usable one."""
    items = job_location if isinstance(job_location, list) else [job_location]
    for item in items:
        if not isinstance(item, dict):
            continue
        addr = item.get("address")
        if isinstance(addr, dict):
            country = addr.get("addressCountry")
            if isinstance(country, dict):
                country = country.get("name")
            parts = [addr.get("addressLocality"), addr.get("addressRegion"), country]
            # Dedup at the comma-token level: a field may itself hold "Dublin, County
            # Dublin" while another repeats "Dublin", so split, drop case-insensitive
            # duplicates, preserve order -> "Dublin, County Dublin, IE".
            seen: set[str] = set()
            clean: list[str] = []
            for p in parts:
                if not isinstance(p, str):
                    continue
                for tok in p.split(","):
                    tok = tok.strip()
                    if tok and tok.lower() not in seen:
                        seen.add(tok.lower())
                        clean.append(tok)
            if clean:
                return ", ".join(clean)
        elif isinstance(addr, str) and addr.strip():
            return addr.strip()
    return None


def _org_name(hiring_org) -> str | None:
    if isinstance(hiring_org, dict):
        name = hiring_org.get("name")
        return name.strip() if isinstance(name, str) and name.strip() else None
    if isinstance(hiring_org, str) and hiring_org.strip():
        return hiring_org.strip()
    return None


def _parse_jobposting_jsonld(page_html: str) -> dict | None:
    """Find the schema.org ``JobPosting`` JSON-LD block and return the fields we map.
    Returns None if the page carries no JobPosting JSON-LD."""
    soup = BeautifulSoup(page_html, "html.parser")
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        if '"JobPosting"' not in raw and "'JobPosting'" not in raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        # A JobPosting may also be nested under @graph.
        for item in list(candidates):
            if isinstance(item, dict) and isinstance(item.get("@graph"), list):
                candidates.extend(item["@graph"])
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return {
                    "title": (item.get("title") or "").strip() or None,
                    "company": _org_name(item.get("hiringOrganization")),
                    "location": _location_str(item.get("jobLocation")),
                    "description": _strip_html(item.get("description")),
                    "date_posted": item.get("datePosted"),
                }
    return None


def _clean_visible_text(page_html: str, cap: int) -> str:
    """Fallback description: the page's visible text, chrome removed, capped."""
    soup = BeautifulSoup(page_html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "head", "nav", "footer", "header"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()[:cap]


def _parse_jobsireland(page_html: str) -> dict | None:
    """Field-map a jobsireland.ie posting.

    No ``JobPosting`` JSON-LD here either, but the page is consistent enough to read by
    anchor rather than by class name (the markup is Orchard CMS utility soup, and the
    class names carry no meaning). The flattened text runs

        Back <title> <employer> Ref: #JOB-<id> <address> No of positions: …

    so the employer is what sits between the title and the reference, and the address is
    what sits between the reference and the position count. The body is the one element
    that is genuinely marked: ``span.margin-reset``, with the structured career-level and
    candidate-requirement block (``div.experiences``) appended when present — that block
    holds the years-of-experience and qualification minimums the qualifier reasons about.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    # The first h3 that is not a section label is the job title.
    section_labels = {"application details", "job description", "job details"}
    title = None
    for heading in soup.find_all("h3"):
        text = heading.get_text(" ", strip=True)
        if text and text.strip().lower() not in section_labels:
            title = text.strip()
            break

    parts = []
    main = soup.select_one("span.margin-reset")
    if main:
        parts.append(main.get_text(" ", strip=True))
    requirements = soup.select_one("div.experiences")
    if requirements:
        parts.append(requirements.get_text(" ", strip=True))
    description = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()

    flat = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    ref = None
    company = None
    location = None
    ref_match = re.search(r"Ref:\s*#JOB-(\d+)", flat)
    if ref_match:
        ref = ref_match.group(1)
        if title:
            between = flat[: ref_match.start()]
            cut = between.rfind(title)
            if cut != -1:
                company = between[cut + len(title):].strip(" -|")
                # "Company Details Confidential" is the site's label for a withheld employer.
                company = re.sub(r"^Company Details\s*", "", company).strip()
        after = flat[ref_match.end():]
        stop = re.search(r"No of positions:|Published On:|Closing On:", after)
        location = after[: stop.start()].strip(" ,-") if stop else None

    if not title and not description:
        return None
    return {
        "title": title,
        "company": company or None,
        "location": location or None,
        "description": description,
        "date_posted": None,
        "reference": ref,
    }


def _collect_links(page_html: str, base_url: str, pattern: re.Pattern[str]) -> list[dict]:
    """Postings as bare links — for a board whose search page says nothing else useful."""
    return [{"url": url, "title": None}
            for url in _extract_posting_links(page_html, base_url, pattern)]


def _collect_jobsireland_rows(page_html: str, base_url: str) -> list[dict]:
    """Postings from a jobsireland results fragment, keeping the row's title.

    The row already carries the title, so the pre-filter below can judge relevance without
    opening the posting — and this board's links are query strings (``?id=``), which the
    generic link extractor deliberately strips.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    out: dict[str, dict] = {}
    for anchor in soup.find_all("a", href=True):
        match = re.search(r"/job-Details\?id=(\d+)", anchor["href"], re.I)
        if not match:
            continue
        url = urljoin(base_url, f"/en-US/job-Details?id={match.group(1)}")
        if url in out:
            continue
        # aria-label reads "View details of <title>"; fall back to the row's heading.
        label = anchor.get("aria-label") or ""
        title = re.sub(r"^View details of\s*", "", label, flags=re.I).strip() or None
        if not title:
            row = anchor.find_parent(["li", "div"])
            heading = row.find(["h2", "h3", "h4"]) if row else None
            title = heading.get_text(" ", strip=True) if heading else None
        out[url] = {"url": url, "title": title}
    return list(out.values())


@dataclass(frozen=True)
class _Board:
    """One crawlable board: where its search lives, how to read its results page, and how
    to read a posting. Adding a board is adding an entry here, not editing the crawl."""

    name: str
    search_urls: Callable[[list[str], str, int], list[str]]
    collect: Callable[[str, str], list[dict]]
    parse: Callable[[str], dict | None]


def _irishjobs_urls(terms: list[str], location: str, pages: int) -> list[str]:
    role = (terms[0] if terms else "software developer").replace(" ", "-")
    base = f"https://www.irishjobs.ie/jobs/{role}/in-{location}"
    return [base if page == 1 else f"{base}?page={page}" for page in range(1, pages + 1)]


#: jobsireland.ie must be addressed without the ``www`` host: ``www.jobsireland.ie``
#: answers 500 on every job-Details id while the bare host serves them 200. Measured
#: 2026-09-18 across the whole result set, so this is the site's routing, not a fluke.
_JOBSIRELAND_BASE = "https://jobsireland.ie"


def _jobsireland_urls(terms: list[str], location: str, pages: int) -> list[str]:
    """One search per term, because this board's keyword is an AND over the phrase.

    Measured: "Finance Data Analyst" returns 0, "Data Analyst" returns 1, "analyst"
    returns 25 — the same collapse the aggregator connectors document. So the caller's
    primary role is split into its significant words and each is asked separately, with
    the union taken below. ``PageSize`` is what actually bounds a call (``PageNumber`` is
    ignored), so one request per term returns that term's whole result set.
    """
    size = max(10, min(50, pages * 25))
    return [
        f"{_JOBSIRELAND_BASE}/Jobsireland.API/JobsIreland/BrowseJobs"
        f"?{urlencode({'Keyword': term, 'PageSize': size})}"
        for term in (terms or [""])
    ]


_BOARDS: dict[str, _Board] = {
    "jobsireland": _Board(
        name="jobsireland",
        search_urls=_jobsireland_urls,
        collect=_collect_jobsireland_rows,
        parse=_parse_jobsireland,
    ),
    "irishjobs": _Board(
        name="irishjobs",
        search_urls=_irishjobs_urls,
        collect=lambda html, base: _collect_links(
            html, base, re.compile(r"^/job/[^/]+/[^/]+")
        ),
        parse=_parse_jobposting_jsonld,
    ),
}


def _search_terms(roles: list[str] | None, keywords: list[str] | None) -> list[str]:
    """What to search a free-text board with: the primary role, then its words.

    jobsireland matches the keyword as an **ordered phrase inside the title** — measured:
    "Business Analyst" returns 8, "Graduate Business Analyst" returns 1, and the reversed
    "Analyst Business" returns 0, so it is a substring match and not an AND over words.

    That makes the whole role worth asking for first: when it is a real job title the
    phrase is exactly right, and a single word can hit the page ceiling ("business" already
    returns 49 against a cap of 50) and lose those precise matches. It is not sufficient on
    its own, because a résumé-derived role is often a self-description rather than a title
    — "Finance Data Analyst" returns 0 where "analyst" returns 25 — so the role's own words
    follow it, longest first, and the union is taken.

    Capped at four searches: each is one polite rate-limited request, and reach past that
    comes from the ATS connectors, not from asking this board more ways.
    """
    primary = (roles or keywords or [""])[0].strip()
    words = [w for w in re.split(r"\W+", primary.lower())
             if len(w) > 2 and w not in GRADE_WORDS]
    # Longest first: "analyst" discriminates better than "data" on these boards.
    ordered = sorted(dict.fromkeys(words), key=len, reverse=True)
    terms = [primary] if primary and len(words) > 1 else []
    for word in ordered:
        if word not in {t.lower() for t in terms}:
            terms.append(word)
    return terms[:4] or [""]


async def run_crawl(
    settings: Settings,
    *,
    board: str | None = None,
    roles: list[str] | None = None,
    keywords: list[str] | None = None,
    location: str = "dublin",
    max_pages: int | None = None,
) -> tuple[list[RawJob], LLMUsage]:
    """Crawl the board's search page(s), then open each posting to read its full
    description. Deterministic (no LLM). The returned ``LLMUsage`` is zero and kept only
    for a stable connector signature / cost-logging path.

    Raises :class:`CrawlUnavailable` when the board will not serve its search page, so a
    block is reported as an error rather than as an empty, successful-looking run.
    """
    name = (board or settings.crawler_board).strip().lower()
    if name not in _BOARDS:
        raise CrawlUnavailable(
            f"unknown crawler board {name!r}; known boards: {', '.join(sorted(_BOARDS))}"
        )
    site = _BOARDS[name]
    ua = settings.crawler_user_agent
    pages = min(max_pages or settings.crawler_max_pages, settings.crawler_max_pages)
    search_urls = site.search_urls(_search_terms(roles, keywords), location.lower(), pages)
    base_url = search_urls[0]
    domain = urlparse(base_url).netloc
    usage = LLMUsage(model=settings.scout_extractor_model)  # zero; no LLM used

    # The coarse title pre-filter the board connectors apply, run here *before* a posting
    # is opened: a board search is broad (one of them indexes on a single word), so
    # without it the detail-fetch budget goes to chefs and HGV drivers. No terms -> keep
    # all, same convention as everywhere else.
    terms = match_tokens(roles, keywords)

    out: list[RawJob] = []
    async with httpx.AsyncClient(
        timeout=settings.crawler_timeout_seconds,
        headers=_headers(ua),
        follow_redirects=True,
    ) as client:
        if not await _robots_ok(client, base_url, ua):
            raise CrawlBlocked(f"robots.txt disallows crawling {base_url}")

        # Step 1: collect postings from the search-results pages.
        found: dict[str, dict] = {}
        for index, page_url in enumerate(search_urls):
            await _rate_limit(domain, settings.crawler_min_delay_seconds)
            resp = await _get_with_retry(client, page_url, referer=base_url, ua=ua)
            if resp is None or resp.status_code != 200:
                if index == 0:
                    # The first page failing is the board refusing us, not the end of the
                    # results. irishjobs.ie answers 403 here to every request now.
                    status = "no response" if resp is None else f"HTTP {resp.status_code}"
                    raise CrawlUnavailable(
                        f"{name}: search page unavailable ({status}) at {page_url}"
                    )
                break
            for entry in site.collect(resp.text, page_url):
                found.setdefault(entry["url"], entry)

        matched = [
            entry for entry in found.values()
            if title_matches(entry.get("title") or _slug_text(entry["url"]), terms)
        ]
        postings = matched[: settings.crawler_max_detail_fetches]
        logger.info(
            "crawl.search board=%s searches=%d -> %d postings, %d title-matched, opening %d",
            name, len(search_urls), len(found), len(matched), len(postings),
        )

        # Step 2: open each posting and read the full description.
        for entry in postings:
            url = entry["url"]
            if not await _robots_ok(client, url, ua):
                continue
            await _rate_limit(domain, settings.crawler_min_delay_seconds)
            resp = await _get_with_retry(client, url, referer=base_url, ua=ua)
            if resp is None or resp.status_code != 200:
                continue

            data = site.parse(resp.text)
            if data and data.get("description"):
                description = data["description"][: settings.crawler_max_text_chars]
                completeness = "full"
                title = data.get("title") or entry.get("title")
                company = data.get("company")
                loc = data.get("location") or location
                raw_extra = {
                    "date_posted": data.get("date_posted"),
                    "reference": data.get("reference"),
                    "extracted_via": (
                        "jsonld" if site.parse is _parse_jobposting_jsonld else "markup"
                    ),
                }
            else:
                # No structured block: keep the posting but be honest it's not a clean body.
                text = _clean_visible_text(resp.text, settings.crawler_max_text_chars)
                if not text:
                    continue
                description = text
                completeness = "snippet"
                title = (data.get("title") if data else None) or entry.get("title")
                company = data.get("company") if data else None
                loc = (data.get("location") if data else None) or location
                raw_extra = {"extracted_via": "visible_text"}

            if not title:
                # Derive a title from the URL slug as a last resort.
                segments = [part for part in urlparse(url).path.split("/") if part]
                title = segments[-1].replace("-", " ").title() if segments else None
            if not title:
                continue

            # Republic-of-Ireland gate. Asymmetric on purpose: the board itself is Irish,
            # so provenance carries a posting whose location we could not read (extraction
            # fails on some pages). Only a location we *did* read and that is explicitly
            # outside the Republic gets dropped — irishjobs.ie is StepStone-owned and lists
            # Northern Ireland too.
            if (loc or "").strip() and not is_roi(loc):
                continue

            out.append(
                RawJob(
                    connector=f"crawl:{name}",
                    source=f"crawl:{name}",
                    source_type="crawl",
                    title=title,
                    company=(company or "").strip() or None,
                    location=(loc or "").strip() or None,
                    url=url,
                    description=description or None,
                    description_completeness=completeness,
                    raw={"board": name, "page_url": url, **raw_extra},
                )
            )
    logger.info("crawl.board=%s -> %d jobs", name, len(out))
    return out, usage
