"""Adzuna: a job search engine that aggregates most job boards in 19
countries. Needs a free API key from https://developer.adzuna.com/.

Free keys are rate limited (about 25 requests a minute and 250 a day at the
time of writing), so the crawl pages newest-first and stops at the request
budget (``[sources.adzuna] max_requests`` or ``--max-requests``). Ask Adzuna
for a higher limit if you need the full backlog of a large country.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from ..http import HttpError
from ..models import Job, clean_line, clean_text, first, to_float, to_iso
from .base import FetchContext, Source, join

API_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
PAGE_SIZE = 50
CURRENCY = {
    "AT": "EUR", "AU": "AUD", "BE": "EUR", "BR": "BRL", "CA": "CAD", "CH": "CHF",
    "DE": "EUR", "ES": "EUR", "FR": "EUR", "GB": "GBP", "IN": "INR", "IT": "EUR",
    "MX": "MXN", "NL": "EUR", "NZ": "NZD", "PL": "PLN", "SG": "SGD", "US": "USD",
    "ZA": "ZAR",
}
_TIME = {"full_time": "full-time", "part_time": "part-time"}


class Adzuna(Source):
    name = "adzuna"
    label = "Adzuna"
    coverage = "listings aggregated from most job boards in 19 countries"
    countries = frozenset(CURRENCY)
    env_vars = ("ADZUNA_APP_ID", "ADZUNA_APP_KEY")
    signup = "https://developer.adzuna.com/ (free)"
    min_interval = 2.5  # free tier allows ~25 requests per minute
    max_requests = 250  # free tier daily allowance
    incremental = True
    full_refresh_days = 7

    def fetch(self, country: str, ctx: FetchContext) -> Iterator[Job]:
        params: dict[str, Any] = {
            "app_id": ctx.env["ADZUNA_APP_ID"],
            "app_key": ctx.env["ADZUNA_APP_KEY"],
            "results_per_page": PAGE_SIZE,
            "sort_by": "date",
            "content-type": "application/json",
        }
        if ctx.options.get("what"):
            params["what"] = ctx.options["what"]
        days = ctx.days_since()
        if days is not None:
            params["max_days_old"] = days
            ctx.incremental = True

        page, read, total = 1, 0, None
        while True:
            url = API_URL.format(country=country.lower(), page=page)
            try:
                data = ctx.http.get_json(url, params=params)
            except HttpError as exc:
                if page > 1 and exc.status in (400, 404):
                    ctx.incomplete(f"Adzuna stopped paging at page {page} (HTTP {exc.status})")
                    return
                raise
            results = data.get("results") or []
            if total is None:
                total = int(data.get("count") or 0)
                ctx.note(f"{total:,} listings match")
            for result in results:
                job = to_job(result, country)
                if job:
                    yield job
            read += len(results)
            if not results or read >= total:
                return
            page += 1


def to_job(result: dict[str, Any], country: str) -> Job | None:
    if not result.get("id"):
        return None
    area = (result.get("location") or {}).get("area") or []
    predicted = str(result.get("salary_is_predicted", "0")) == "1"
    low = None if predicted else to_float(result.get("salary_min"))
    high = None if predicted else to_float(result.get("salary_max"))
    return Job(
        source="adzuna",
        source_id=str(result["id"]),
        country=country,
        title=clean_line(result.get("title")) or "(untitled)",
        company=clean_line((result.get("company") or {}).get("display_name")),
        location=clean_line((result.get("location") or {}).get("display_name")),
        city=area[-1] if len(area) >= 2 else None,
        region=area[1] if len(area) >= 3 else None,
        employment_type=join(_TIME.get(result.get("contract_time")), result.get("contract_type")),
        category=(result.get("category") or {}).get("label"),
        salary_min=low,
        salary_max=high,
        salary_currency=CURRENCY.get(country) if (low or high) else None,
        salary_period="year" if (low or high) else None,
        description=clean_text(result.get("description")),
        url=first(result.get("redirect_url")),
        posted_at=to_iso(result.get("created")),
    )
