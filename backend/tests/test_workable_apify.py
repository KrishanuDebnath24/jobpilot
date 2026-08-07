"""Workable + Apify connectors, and the Republic-of-Ireland location filter.

Deterministic: no network. The HTTP paths run against ``httpx.MockTransport`` so the two
things most likely to break silently are actually exercised — the feed's cursor
parameter, and the RoI filtering that keeps a global board out of an Ireland-only pool.
"""

from __future__ import annotations

import httpx
import pytest
from jobpilot_core import ats_detect
from jobpilot_core.roi_location import is_roi
from jobpilot_core.settings import Settings
from sources_mcp.connectors import apify, workable


# --------------------------------------------------------------------------- #
# is_roi
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "Dublin, Ireland", "Cork", "Galway, County Galway, Ireland", "Ireland",
        "Sligo", "Maynooth, Kildare, Ireland", "Leinster", "Munster", "Connacht",
        "Dun Laoghaire", "Kilcullen", "Enniskerry",
    ],
)
def test_is_roi_accepts_republic(text):
    assert is_roi(text) is True


@pytest.mark.parametrize(
    "text",
    [
        # The whole reason this module exists: these all contain "ireland" or read as
        # Irish, and every one of them is the United Kingdom.
        "Belfast, Northern Ireland", "Northern Ireland", "Derry", "Londonderry",
        "Lisburn, County Antrim", "Newry, County Down", "Omagh, County Tyrone",
    ],
)
def test_is_roi_rejects_northern_ireland(text):
    assert is_roi(text) is False


@pytest.mark.parametrize(
    "text",
    ["London, United Kingdom", "Dublin, Ohio", "Cork, Georgia, United States",
     "Remote", "", None, "Barcelona", "EMEA"],
)
def test_is_roi_rejects_elsewhere_and_unknown(text):
    assert is_roi(text) is False


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        # Real Greenhouse/Muse labels. A job open in Dublin *and* elsewhere is still a
        # Dublin job — rejecting it on the foreign half throws away a real opening.
        ("Dublin, Ireland; London, England", True),
        ("Dublin, Ireland; Madrid, Spain; Paris, France", True),
        ("Amsterdam, The Netherlands; Dublin, Ireland; Paris, France", True),
        ("London, England; Paris, France", False),
        # The NI veto still applies per location, so Belfast alone cannot carry it.
        ("Belfast, Northern Ireland; London, England", False),
        ("Belfast, Northern Ireland; Dublin, Ireland", True),
    ],
)
def test_is_roi_handles_multi_location_postings(label, expected):
    assert is_roi(label) is expected


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        # The Muse joins locations with ", ", which cannot be split on the comma because a
        # comma also separates city from country. A "<Irish place>, Ireland" pairing
        # anywhere in the string is decisive. First case is a real record from the pool.
        ("Austin, TX, Chicago, IL, Dublin, Ireland, London, United Kingdom, New York, NY", True),
        ("New York, NY, Cork, Ireland", True),
        ("Austin, TX, London, United Kingdom, New York, NY", False),
        # The pairing must not be satisfiable by a same-named place abroad.
        ("Dublin, Ohio", False),
        ("Cork, Georgia, United States", False),
        # Belfast still cannot ride in on a comma-joined list.
        ("Belfast, Northern Ireland, London, United Kingdom", False),
    ],
)
def test_is_roi_handles_comma_joined_location_lists(label, expected):
    assert is_roi(label) is expected


def test_is_roi_rejects_belfast_even_when_the_source_says_ireland():
    """Real record from Workable's live Ireland feed: "Belfast, Ulster, Ireland" with
    countryName "Ireland". Belfast is the UK; the city has to beat the country label."""
    assert is_roi("Belfast, Ulster, Ireland", country="Ireland") is False
    assert is_roi("Belfast, Ulster, Ireland", country="Ireland", country_code="IE") is False


def test_is_roi_prefers_structured_country_over_text():
    # A board that says the country outright beats a city name that exists twice over.
    assert is_roi("Dublin", country="United States") is False
    assert is_roi("Springfield", country="Ireland") is True
    assert is_roi("anywhere", country_code="IE") is True
    assert is_roi("Dublin", country_code="GB") is False


# --------------------------------------------------------------------------- #
# extract_board_slug — platform routing segments are not employers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.workable.com/view/6SktwALkvNBYQuxUJjikAC/senior-analyst-at-davy",
        "https://apply.workable.com/j/30CB009A83",
        "https://apply.workable.com/j/30CB009A83/apply",
    ],
)
def test_reserved_segments_are_not_slugs(url):
    """Regression: these once yielded board slugs "view" and "j", pointing a later
    stage at boards that do not exist."""
    assert ats_detect.extract_board_slug(url) is None


def test_real_workable_slug_still_extracted():
    url = "https://apply.workable.com/davy/j/30CB009A83/"
    assert ats_detect.detect_ats(url) == "workable"
    assert ats_detect.extract_board_slug(url) == "davy"


# --------------------------------------------------------------------------- #
# Workable field mapping
# --------------------------------------------------------------------------- #
def _feed_record(**over):
    rec = {
        "id": "a4b2-1", "title": "Data Engineer",
        "company": {"title": "Acme", "website": "https://acme.ie"},
        "location": {"city": "Dublin", "subregion": "County Dublin", "countryName": "Ireland"},
        "locations": [{"countryCode": "IE", "city": "Dublin"}],
        "description": "<p>Build <b>pipelines</b>.</p>",
        "requirementsSection": "<ul><li>Python</li></ul>",
        "benefitsSection": "<p>Pension</p>",
        "url": "https://jobs.workable.com/view/TOKEN/data-engineer-at-acme",
        "workplace": "hybrid", "employmentType": "Full-time",
    }
    rec.update(over)
    return rec


def test_feed_mapping_joins_all_body_sections():
    rec = _feed_record()
    label, country, code = workable._feed_location(rec)
    job = workable._feed_to_raw(rec, label)
    assert job.connector == "workable"
    assert job.company == "Acme"
    assert job.location == "Dublin, County Dublin, Ireland"
    assert job.description_completeness == "full"
    # Requirements and benefits must survive; they are what tailoring needs most.
    assert "pipelines" in job.description
    assert "Python" in job.description
    assert "Pension" in job.description
    assert "<" not in job.description  # HTML stripped


def test_feed_location_drops_the_any_placeholder():
    label, _, _ = workable._feed_location(_feed_record(location={"city": "any", "countryName": "Ireland"}))
    assert label == "Ireland"


def test_feed_location_tolerates_string_entries():
    """Live responses mix objects and bare strings in ``locations``; reading
    ``.get`` off a string crashed the whole run."""
    label, country, code = workable._feed_location(
        _feed_record(locations=["Dublin", {"countryCode": "IE"}, "remote"])
    )
    assert (label, country, code) == ("Dublin, County Dublin, Ireland", "Ireland", "IE")

    _, _, code_only_strings = workable._feed_location(_feed_record(locations=["Dublin"]))
    assert code_only_strings is None  # no crash, just no code


def test_board_mapping_prefers_the_application_url():
    rec = {
        "title": "Tax Associate", "shortcode": "30CB009A83",
        "city": "Dublin", "state": "County Dublin", "country": "Ireland",
        "description": "<p>Advise clients.</p>",
        "url": "https://apply.workable.com/j/30CB009A83",
        "application_url": "https://apply.workable.com/j/30CB009A83/apply",
    }
    job = workable._board_to_raw(rec, "davy", "Davy")
    assert job.connector == "workable:davy"
    assert job.source_type == "ats"
    assert job.url.endswith("/apply")  # the form, not the advert
    assert job.external_id == "30CB009A83"


# --------------------------------------------------------------------------- #
# Workable feed over a mocked transport
# --------------------------------------------------------------------------- #
def _mock_httpx(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_feed_paginates_with_pageToken_and_filters_non_roi(monkeypatch):
    """Two pages; page 2 is only reachable via ``pageToken``. Belfast must not survive."""
    seen_params: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen_params.append(params)
        if "pageToken" not in params:
            return httpx.Response(200, json={
                "totalSize": 3,
                "nextPageToken": "CURSOR2",
                "jobs": [
                    _feed_record(id="1", title="Data Engineer"),
                    _feed_record(
                        id="2", title="Data Engineer",
                        location={"city": "Belfast", "countryName": "United Kingdom"},
                        locations=[{"countryCode": "GB"}],
                    ),
                ],
            })
        return httpx.Response(200, json={
            "totalSize": 3, "nextPageToken": None,
            "jobs": [_feed_record(id="3", title="Data Engineer")],
        })

    _mock_httpx(monkeypatch, handler)
    jobs = await workable.search_workable(Settings(), location="ireland")

    assert [j.external_id for j in jobs] == ["1", "3"]  # Belfast dropped
    assert len(seen_params) == 2
    assert seen_params[1]["pageToken"] == "CURSOR2"
    assert seen_params[0]["limit"] == "20"  # the feed 400s above 20


@pytest.mark.asyncio
async def test_board_roi_filter_keeps_global_boards_out(monkeypatch):
    """Acclaro-shaped case: a real board whose jobs are nearly all outside Ireland."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "Acme", "jobs": [
            {"title": "Engineer", "shortcode": "A1", "city": "Dublin", "country": "Ireland",
             "description": "<p>x</p>", "application_url": "https://apply.workable.com/j/A1/apply"},
            {"title": "Engineer", "shortcode": "A2", "city": "Austin", "country": "United States",
             "description": "<p>x</p>", "application_url": "https://apply.workable.com/j/A2/apply"},
            {"title": "Engineer", "shortcode": "A3", "city": "Belfast",
             "country": "United Kingdom", "description": "<p>x</p>",
             "application_url": "https://apply.workable.com/j/A3/apply"},
        ]})

    _mock_httpx(monkeypatch, handler)
    jobs = await workable.fetch_workable_board(Settings(), company="Acme")
    assert [j.external_id for j in jobs] == ["A1"]

    everything = await workable.fetch_workable_board(Settings(), company="Acme", roi_only=False)
    assert len(everything) == 3  # opt-out still available


@pytest.mark.asyncio
async def test_board_404_is_a_named_error(monkeypatch):
    _mock_httpx(monkeypatch, lambda r: httpx.Response(404, text="not found"))
    with pytest.raises(workable.WorkableError, match="board not found"):
        await workable.fetch_workable_board(Settings(), company="nope")


# --------------------------------------------------------------------------- #
# Apify
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_apify_is_off_without_a_token():
    with pytest.raises(apify.ApifyError, match="not configured"):
        await apify.search_apify_linkedin(Settings(apify_api_token=""))


def test_apify_search_url_carries_location_and_terms():
    url = apify._search_url(["Data Engineer"], ["python"], "Ireland")
    assert "location=Ireland" in url
    assert "Data+Engineer+OR+python" in url


def test_apify_maps_full_text_but_an_unapplyable_url():
    rec = {
        "id": 4449187254, "title": "Payroll Specialist", "companyName": "Avoca Ireland",
        "location": "Glencap Commons North, County Wicklow, Ireland",
        "descriptionText": "We are seeking a  detail-oriented   Payroll Specialist.",
        "link": "https://ie.linkedin.com/jobs/view/payroll-specialist-4449187254",
        "applyUrl": "",  # always empty in practice; LinkedIn hides it from guests
    }
    job = apify._to_raw(rec)
    assert job.connector == "apify:linkedin"
    assert job.description_completeness == "full"
    assert "  " not in job.description  # whitespace collapsed
    # The point of record: full text, but not an application form.
    assert ats_detect.detect_ats(job.url) == ats_detect.AGGREGATOR


@pytest.mark.asyncio
async def test_apify_clamps_to_the_cost_ceiling_and_drops_non_roi(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        captured.update(_json.loads(request.content))
        return httpx.Response(200, json=[
            {"id": 1, "title": "Engineer", "companyName": "Acme",
             "location": "Dublin, County Dublin, Ireland",
             "descriptionText": "body", "link": "https://ie.linkedin.com/jobs/view/1"},
            {"id": 2, "title": "Nurse", "companyName": "NHS Ayrshire & Arran",
             "location": "Ayrshire, Scotland, United Kingdom",
             "descriptionText": "body", "link": "https://uk.linkedin.com/jobs/view/2"},
        ])

    _mock_httpx(monkeypatch, handler)
    settings = Settings(apify_api_token="tok", apify_max_results=50)
    jobs = await apify.search_apify_linkedin(settings, max_results=5000)

    assert captured["count"] == 50  # caller cannot raise the ceiling
    assert captured["scrapeCompany"] is False
    assert [j.external_id for j in jobs] == ["1"]  # the Scottish record is dropped
