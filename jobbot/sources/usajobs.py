"""United States: federal government jobs from USAJOBS. Needs a free API key
from https://developer.usajobs.gov/ and the email address you registered with
(USAJOBS asks for it as the User-Agent of every request).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from ..countries import resolve_country
from ..models import Job, clean_line, clean_text, first, to_float, to_iso
from .base import FetchContext, Source, join

API_URL = "https://data.usajobs.gov/api/search"
PAGE_SIZE = 500
_PERIOD = {"PA": "year", "PM": "month", "PW": "week", "PD": "day", "PH": "hour"}


class USAJobs(Source):
    name = "usajobs"
    label = "USAJOBS"
    coverage = "US federal government jobs"
    countries = frozenset({"US"})
    env_vars = ("USAJOBS_API_KEY", "USAJOBS_EMAIL")
    signup = "https://developer.usajobs.gov/ (free)"
    min_interval = 0.5
    incremental = True
    full_refresh_days = 7

    def fetch(self, country: str, ctx: FetchContext) -> Iterator[Job]:
        headers = {
            "Authorization-Key": ctx.env["USAJOBS_API_KEY"],
            "User-Agent": ctx.env["USAJOBS_EMAIL"],
        }
        params: dict[str, Any] = {"ResultsPerPage": PAGE_SIZE, "Page": 1}
        days = ctx.days_since()
        if days is not None and days <= 60:  # DatePosted accepts 0..60 days
            params["DatePosted"] = days
            ctx.incremental = True
        while True:
            data = ctx.http.get_json(API_URL, params=params, headers=headers)
            result = data.get("SearchResult") or {}
            items = result.get("SearchResultItems") or []
            if params["Page"] == 1:
                ctx.note(f"{int(result.get('SearchResultCountAll') or 0):,} announcements match")
            for item in items:
                job = to_job(item)
                if job:
                    yield job
            pages = int((result.get("UserArea") or {}).get("NumberOfPages") or 0)
            if not items or params["Page"] >= pages:
                return
            params["Page"] += 1


def to_job(item: dict[str, Any]) -> Job | None:
    d = item.get("MatchedObjectDescriptor") or {}
    job_id = first(item.get("MatchedObjectId"), d.get("PositionID"))
    if not job_id:
        return None
    places = d.get("PositionLocation") or []
    domestic = [p for p in places if resolve_country(p.get("CountryCode")) in ("US", None)]
    if places and not domestic:
        return None  # posted only for locations abroad
    place = (domestic or [{}])[0]
    pay = (d.get("PositionRemuneration") or [{}])[0]
    low, high = to_float(pay.get("MinimumRange")), to_float(pay.get("MaximumRange"))
    details = (d.get("UserArea") or {}).get("Details") or {}
    return Job(
        source="usajobs",
        source_id=str(job_id),
        country="US",
        title=clean_line(d.get("PositionTitle")) or "(untitled)",
        company=clean_line(join(*dict.fromkeys([d.get("OrganizationName"), d.get("DepartmentName")]), sep=" — ")),
        location=clean_line(d.get("PositionLocationDisplay")),
        city=place.get("CityName"),
        region=place.get("CountrySubDivisionCode"),
        employment_type=join(*(s.get("Name") for s in d.get("PositionSchedule") or [])),
        category=first(*(c.get("Name") for c in d.get("JobCategory") or [])),
        salary_min=low,
        salary_max=high,
        salary_currency="USD" if (low or high) else None,
        salary_period=_PERIOD.get(pay.get("RateIntervalCode")) if (low or high) else None,
        description=clean_text(first(details.get("JobSummary"), d.get("QualificationSummary"))),
        url=d.get("PositionURI"),
        posted_at=to_iso(d.get("PublicationStartDate")),
        expires_at=to_iso(d.get("ApplicationCloseDate")),
    )
