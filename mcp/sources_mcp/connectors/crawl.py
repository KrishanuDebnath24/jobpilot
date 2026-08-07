"""Crawler connector — one polite crawler for a single Irish board (irishjobs.ie),
which has no public API.

Two steps, both **deterministic** (no LLM):

1. Fetch the board's search-results page(s) and pull the individual posting URLs
   straight from the ``/job/…`` links in the HTML.
2. Open each posting page and read the **schema.org ``JobPosting`` JSON-LD** the site
   embeds for Google Jobs — the authoritative title, company, location, and the
   *full* description. That JSON-LD is structured data we field-map (like the ATS/API
   connectors); it is never treated as instructions. If a page has no JSON-LD we fall
   back to its cleaned visible text.

Politeness: honor ``robots.txt``, a per-domain minimum delay, a page cap, and a hard
cap on how many posting pages we open per run.

Earlier versions Haiku-extracted jobs from the *search* page only and never opened the
postings, so descriptions were missing/mislabeled and the links were unreliable. This
version reads each posting, so ``description`` is the real body and ``url`` is the real
posting — and ``description_completeness`` is only ``"full"`` when we actually got it.
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import re
import time
import urllib.robotparser as robotparser
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from jobpilot_core.llm import LLMUsage
from jobpilot_core.roi_location import is_roi
from jobpilot_core.scout import RawJob
from jobpilot_core.settings import Settings

# irishjobs.ie search results are server-rendered HTML (verified crawlable + robots-allowed).
SEARCH_URL_TEMPLATE = "https://www.irishjobs.ie/jobs/{role}/in-{location}"

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


# Per-domain rate-limit state (single-process worker). Politeness, not correctness.
_last_request: dict[str, float] = {}
_rate_lock = asyncio.Lock()


class CrawlBlocked(RuntimeError):
    """robots.txt disallows the target URL."""


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


def _extract_posting_links(page_html: str, base_url: str) -> list[str]:
    """Deterministically pull the individual posting URLs (``/job/…``) out of a
    search-results page, absolutized and de-duplicated in document order."""
    soup = BeautifulSoup(page_html, "html.parser")
    seen: dict[str, None] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0].split("#")[0]
        if not href:
            continue
        path = urlparse(href).path or href
        # A posting is /job/<slug>/<company>-jobNNNNN — exclude apply/similar sub-paths.
        if re.match(r"^/job/[^/]+/[^/]+", path) and "/apply" not in path:
            absolute = urljoin(base_url, href)
            seen.setdefault(absolute, None)
    return list(seen.keys())


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
    description from JSON-LD. Deterministic (no LLM). The returned ``LLMUsage`` is zero
    and kept only for a stable connector signature / cost-logging path."""
    board = board or settings.crawler_board
    ua = settings.crawler_user_agent
    pages = min(max_pages or settings.crawler_max_pages, settings.crawler_max_pages)
    role = (roles or ["software developer"])[0].strip().lower().replace(" ", "-")
    base_url = SEARCH_URL_TEMPLATE.format(role=role, location=location.lower())
    domain = urlparse(base_url).netloc
    usage = LLMUsage(model=settings.scout_extractor_model)  # zero; no LLM used

    out: list[RawJob] = []
    async with httpx.AsyncClient(
        timeout=settings.crawler_timeout_seconds,
        headers=_headers(ua),
        follow_redirects=True,
    ) as client:
        if not await _robots_ok(client, base_url, ua):
            raise CrawlBlocked(f"robots.txt disallows crawling {base_url}")

        # Step 1: collect posting URLs from the search-results pages.
        posting_urls: list[str] = []
        seen_urls: set[str] = set()
        for page in range(1, pages + 1):
            page_url = base_url if page == 1 else f"{base_url}?page={page}"
            await _rate_limit(domain, settings.crawler_min_delay_seconds)
            resp = await _get_with_retry(client, page_url, referer=base_url, ua=ua)
            if resp is None or resp.status_code != 200:
                break
            for u in _extract_posting_links(resp.text, base_url):
                if u not in seen_urls:
                    seen_urls.add(u)
                    posting_urls.append(u)

        posting_urls = posting_urls[: settings.crawler_max_detail_fetches]

        # Step 2: open each posting and read the full description from JSON-LD.
        if not await _robots_ok(client, base_url + "/job", ua):
            # Postings live under /job/ (robots-allowed); guard defensively anyway.
            pass
        for url in posting_urls:
            if not await _robots_ok(client, url, ua):
                continue
            await _rate_limit(domain, settings.crawler_min_delay_seconds)
            resp = await _get_with_retry(client, url, referer=base_url, ua=ua)
            if resp is None or resp.status_code != 200:
                continue

            data = _parse_jobposting_jsonld(resp.text)
            if data and data.get("description"):
                description = data["description"][: settings.crawler_max_text_chars]
                completeness = "full"
                title = data.get("title")
                company = data.get("company")
                loc = data.get("location") or location
                raw_extra = {"date_posted": data.get("date_posted"), "extracted_via": "jsonld"}
            else:
                # No JSON-LD: keep the posting but be honest it's not a clean full body.
                text = _clean_visible_text(resp.text, settings.crawler_max_text_chars)
                if not text:
                    continue
                description = text
                completeness = "snippet"
                title = data.get("title") if data else None
                company = data.get("company") if data else None
                loc = (data.get("location") if data else None) or location
                raw_extra = {"extracted_via": "visible_text"}

            if not title:
                # Derive a title from the URL slug as a last resort (…/job/<slug>/…).
                slug = urlparse(url).path.split("/")
                title = slug[2].replace("-", " ").title() if len(slug) > 2 else None
            if not title:
                continue

            # Republic-of-Ireland gate. Asymmetric on purpose: the board itself is Irish,
            # so provenance carries a posting whose location we could not read (schema.org
            # extraction fails on some pages). Only a location we *did* read and that is
            # explicitly outside the Republic gets dropped — irishjobs.ie is
            # StepStone-owned and lists Northern Ireland too.
            if (loc or "").strip() and not is_roi(loc):
                continue

            out.append(
                RawJob(
                    connector=f"crawl:{board}",
                    source=f"crawl:{board}",
                    source_type="crawl",
                    title=title,
                    company=(company or "").strip() or None,
                    location=(loc or "").strip() or None,
                    url=url,
                    description=description or None,
                    description_completeness=completeness,
                    raw={"board": board, "page_url": url, **raw_extra},
                )
            )
    return out, usage
