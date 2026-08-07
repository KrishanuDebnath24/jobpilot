"""Is this location in the Republic of Ireland?

The app targets the RoI, and every location-filtered source leaks. Server-side facets
are advisory at best: Workable's ``location=ireland`` feed and LinkedIn's
``location=Ireland`` search both return neighbours, and per-employer ATS boards return
the company's whole global board regardless of what you asked for.

The trap this module exists to close is that **Northern Ireland is not the Republic**.
"Belfast, Northern Ireland" contains the substring "ireland", so the obvious
``"ireland" in text.lower()`` check silently admits UK jobs — where the user's work
authorisation, tax position and commute are all different. This app previously imported NI
jobs for exactly that reason — Adzuna cannot serve the Republic and was configured for
``gb`` — which is part of why that connector was removed.

Matching is deliberately asymmetric: an explicit UK/NI signal rejects outright, and only
then do positive RoI signals count. Unknown text returns ``False`` — a source that
cannot say where a job is does not get to add it to an Ireland-only pool.
"""

from __future__ import annotations

import re

#: The 26 counties of the Republic. Names only — see ``_NI`` for the ones that aren't.
_ROI_COUNTIES: frozenset[str] = frozenset({
    "carlow", "cavan", "clare", "cork", "donegal", "dublin", "galway", "kerry",
    "kildare", "kilkenny", "laois", "leitrim", "limerick", "longford", "louth",
    "mayo", "meath", "monaghan", "offaly", "roscommon", "sligo", "tipperary",
    "waterford", "westmeath", "wexford", "wicklow",
})

#: Provinces wholly inside the Republic. Ulster is deliberately absent: six of its nine
#: counties are UK, so "Ulster" alone cannot settle the question. Real boards do use
#: these — PrepayPower and Yuno Energy label Irish roles "Leinster"/"Munster".
_ROI_PROVINCES: frozenset[str] = frozenset({"leinster", "munster", "connacht", "connaught"})

#: Towns/cities worth naming because they appear in listings without a county.
_ROI_PLACES: frozenset[str] = frozenset({
    "dublin", "cork", "galway", "limerick", "waterford", "drogheda", "dundalk",
    "swords", "bray", "navan", "ennis", "tralee", "carlow", "naas", "athlone",
    "letterkenny", "sligo", "clonmel", "greystones", "malahide", "maynooth",
    "celbridge", "leixlip", "balbriggan", "portlaoise", "mullingar", "wexford",
    "killarney", "shannon", "westport", "kilcullen", "sandyford", "enniskerry",
    "blanchardstown", "tallaght", "dun laoghaire", "clonee", "kilkenny",
})

#: Explicit Northern Ireland / UK signals. Any of these rejects, whatever else matches.
_NI: frozenset[str] = frozenset({
    "northern ireland", "n. ireland", "n.ireland", "belfast", "derry", "londonderry",
    "lisburn", "newry", "craigavon", "ballymena", "coleraine", "omagh", "enniskillen",
    "newtownabbey", "bangor, county down", "county antrim", "county armagh",
    "county down", "county fermanagh", "county tyrone", "co. antrim", "co. down",
})
_UK_COUNTRIES: frozenset[str] = frozenset({
    "united kingdom", "great britain", "england", "scotland", "wales", "uk", "gb",
})
_UK_CODES: frozenset[str] = frozenset({"gb", "uk", "gbr"})
_IE_CODES: frozenset[str] = frozenset({"ie", "irl"})

#: Places elsewhere that share a name with an Irish one. Ireland exported its
#: place-names widely — Dublin, Ohio and Cork, Georgia are real, and a bare city token
#: would otherwise admit them. Any of these in the text rejects before the city is read.
_ELSEWHERE: frozenset[str] = frozenset({
    # countries seen in these feeds
    "united states", "usa", "u.s.", "canada", "australia", "new zealand", "india",
    "germany", "france", "spain", "portugal", "netherlands", "belgium", "poland",
    "italy", "switzerland", "austria", "sweden", "denmark", "norway", "finland",
    "czechia", "czech republic", "romania", "hungary", "greece", "turkey", "israel",
    "singapore", "japan", "china", "hong kong", "malaysia", "indonesia", "vietnam",
    "thailand", "philippines", "south korea", "brazil", "mexico", "argentina",
    "chile", "colombia", "peru", "south africa", "nigeria", "kenya", "egypt",
    "morocco", "united arab emirates", "saudi arabia", "qatar", "pakistan",
    # US states that collide with Irish place names, plus the common tech hubs
    "ohio", "georgia", "texas", "california", "virginia", "maryland", "florida",
    "illinois", "massachusetts", "pennsylvania", "michigan", "minnesota", "colorado",
    "arizona", "nevada", "utah", "oregon", "kansas", "kentucky", "tennessee",
    "missouri", "wisconsin", "alabama", "louisiana", "nebraska", "oklahoma",
    "new hampshire", "new jersey", "new mexico", "north carolina", "south carolina",
    "rhode island", "west virginia", "north dakota", "south dakota",
})


#: "<Irish place>, Ireland" — an unambiguous RoI location inside a longer, comma-joined
#: list of locations. Deliberately requires the country word next to the place, so
#: "Dublin, Ohio" and "Cork, Georgia" cannot satisfy it.
_ROI_PAIR_RE = re.compile(
    r"\b(?:" + "|".join(sorted(re.escape(p) for p in (_ROI_COUNTIES | _ROI_PLACES)))
    + r")\b[^,;]*,\s*(?:republic of\s+)?ireland\b"
)


def _words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z]+", text) if w}


def is_roi(
    text: str | None = None,
    *,
    country: str | None = None,
    country_code: str | None = None,
) -> bool:
    """True when the location is in the Republic of Ireland.

    ``country``/``country_code`` are structured fields when a source provides them
    (Workable gives both); ``text`` is the free-form label otherwise. Structured country
    evidence is trusted over the text, because "Dublin, Ohio" and "Cork, Georgia" are
    real places and the text alone would admit them.

    A posting open in several places is Irish if **any** of them is in the Republic —
    "Dublin, Ireland; London, England" is a Dublin job you can apply to. Those are split
    on ``;`` and judged per location, because the single-label rules below reject on any
    foreign country signal and would otherwise throw the Dublin option away.
    """
    if text and ";" in text:
        parts = [p.strip() for p in text.split(";") if p.strip()]
        if len(parts) > 1:
            return any(
                is_roi(p, country=country, country_code=country_code) for p in parts
            )

    blob_all = " ".join(p for p in (text, country) if p).strip().lower()
    # An explicit Northern Ireland signal is decisive and is checked before anything
    # else, including a structured country field. Sources get this wrong in exactly one
    # direction: Workable's Ireland feed returns "Belfast, Ulster, Ireland" with
    # countryName "Ireland". Belfast is the United Kingdom; the city beats the label.
    if any(sig in blob_all for sig in _NI):
        return False

    code = (country_code or "").strip().lower()
    if code:
        if code in _UK_CODES:
            return False
        if code in _IE_CODES:
            return True

    ctry = (country or "").strip().lower()
    if ctry:
        if ctry in _UK_COUNTRIES or "northern ireland" in ctry:
            return False
        if ctry in ("ireland", "republic of ireland", "eire", "éire"):
            return True
        # A named non-Irish country settles it; no need to read the city.
        return False

    blob = (text or "").strip().lower()
    if not blob:
        return False
    if any(sig in blob for sig in _NI):
        return False
    # Positive evidence beats the foreign-country vetoes below. Some sources join several
    # locations with ", " — The Muse emits
    # "Austin, TX, Chicago, IL, Dublin, Ireland, London, United Kingdom" — which cannot be
    # split on the comma, because a comma also separates a city from its country. An
    # explicit "<Irish place>, Ireland" pairing anywhere in the string settles it: the job
    # is open in Ireland whatever else is listed. "Dublin, Ohio" cannot match this.
    if _ROI_PAIR_RE.search(blob):
        return True
    # "Dublin, United Kingdom" and "Dublin, Ohio" would otherwise pass on the city alone.
    if any(re.search(rf"\b{re.escape(c)}\b", blob) for c in _UK_COUNTRIES):
        return False
    if any(re.search(rf"\b{re.escape(c)}\b", blob) for c in _ELSEWHERE):
        return False
    if "ireland" in blob or "éire" in blob:
        return True

    words = _words(blob)
    if words & _ROI_PROVINCES:
        return True
    # Require a county/place token. Bare "remote" is not Ireland.
    return bool(words & _ROI_COUNTIES) or bool(words & _ROI_PLACES) or any(
        p in blob for p in _ROI_PLACES if " " in p
    )
