"""Company career sites hosted on Greenhouse, Lever, Ashby and SmartRecruiters.

These public board APIs need no key, but they list one company at a time, so
you choose the companies in ``jobbot.toml``::

    [companies]
    greenhouse = ["stripe", "cloudflare"]   # boards.greenhouse.io/<name>
    lever = ["spotify", "eu:somecompany"]   # jobs.lever.co/<name> ("eu:" = EU-hosted)
    ashby = ["openai"]                      # jobs.ashbyhq.com/<name>
    smartrecruiters = ["BoschGroup"]        # jobs.smartrecruiters.com/<name>

Only openings located in the requested country are kept. Greenhouse gives
free-text locations, so that check is a best guess (see countries.py); add
local place names under ``[locations]`` in jobbot.toml if it misses some.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from typing import Any

from ..countries import resolve_country
from ..models import Job, clean_line, clean_text, first, to_float, to_iso
from .base import CompanyBoardSource, FetchContext, join, label, salary_period, work_mode


class Greenhouse(CompanyBoardSource):
    name = "greenhouse"
    label = "Greenhouse boards"
    coverage = "career sites of companies you list that use Greenhouse"

    def fetch_company(self, company: str, name: str, country: str, ctx: FetchContext) -> Iterator[Job]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs"
        data = ctx.http.get_json(url, params={"content": "true"})
        for posting in data.get("jobs") or []:
            place = (posting.get("location") or {}).get("name") or ""
            # Offices often carry the real places when the location just says "Hybrid".
            offices = [
                (first(o.get("name"), o.get("location")), first(o.get("location"), o.get("name")))
                for o in posting.get("offices") or []
            ]
            local = [office for office, where in offices if office and ctx.in_country(country, [where])]
            if not local and not ctx.in_country(country, [place]):
                continue
            mode = work_mode(place)
            vague = not place or (mode is not None and not ctx.in_country(country, [place]))
            location = "; ".join(local) if vague and local else place
            departments = [d.get("name") for d in posting.get("departments") or [] if d.get("name")]
            yield Job(
                source=self.name,
                source_id=f"{company}:{posting['id']}",
                country=country,
                title=clean_line(posting.get("title")) or "(untitled)",
                company=first(posting.get("company_name"), name),
                location=clean_line(location),
                city=_first_place(location) or _first_place(local[0] if local else None),
                work_mode=mode,
                category=first(*departments),
                description=clean_text(posting.get("content")),
                url=posting.get("absolute_url"),
                posted_at=to_iso(first(posting.get("first_published"), posting.get("updated_at"))),
            )


class Lever(CompanyBoardSource):
    name = "lever"
    label = "Lever boards"
    coverage = "career sites of companies you list that use Lever"

    def fetch_company(self, company: str, name: str, country: str, ctx: FetchContext) -> Iterator[Job]:
        host, slug = ("api.eu.lever.co", company[3:]) if company.startswith("eu:") else ("api.lever.co", company)
        data = ctx.http.get_json(f"https://{host}/v0/postings/{slug}", params={"mode": "json"})
        if not isinstance(data, list):
            raise ValueError(f"unexpected Lever response: {str(data)[:200]}")
        for posting in data:
            categories = posting.get("categories") or {}
            places = categories.get("allLocations") or [categories.get("location")]
            code = (posting.get("country") or "").upper()
            if code != country and not ctx.in_country(country, places):
                continue
            pay = posting.get("salaryRange") or {}
            low, high = to_float(pay.get("min")), to_float(pay.get("max"))
            yield Job(
                source=self.name,
                source_id=f"{slug}:{posting['id']}",
                country=country,
                title=clean_line(posting.get("text")) or "(untitled)",
                company=name,
                location=clean_line(join(*places, sep="; ")),
                city=_first_place(categories.get("location")),
                work_mode=work_mode(posting.get("workplaceType")),
                employment_type=categories.get("commitment"),
                category=first(categories.get("department"), categories.get("team")),
                salary_min=low,
                salary_max=high,
                salary_currency=pay.get("currency") if (low or high) else None,
                salary_period=salary_period(pay.get("interval")) if (low or high) else None,
                description=clean_text(first(posting.get("descriptionPlain"), posting.get("description"))),
                url=first(posting.get("hostedUrl"), posting.get("applyUrl")),
                posted_at=to_iso(posting.get("createdAt")),
            )


class Ashby(CompanyBoardSource):
    name = "ashby"
    label = "Ashby boards"
    coverage = "career sites of companies you list that use Ashby"

    def fetch_company(self, company: str, name: str, country: str, ctx: FetchContext) -> Iterator[Job]:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{company}"
        data = ctx.http.get_json(url, params={"includeCompensation": "true"})
        for posting in data.get("jobs") or []:
            if posting.get("isListed") is False:
                continue
            secondary = posting.get("secondaryLocations") or []
            addresses = [_postal(posting)] + [_postal(s) for s in secondary]
            codes = {resolve_country(a.get("addressCountry")) for a in addresses}
            places = [posting.get("location")] + [s.get("location") for s in secondary]
            if country not in codes and not ctx.in_country(country, places):
                continue
            # City and region only from an address that is in this country.
            here = next((a for a in addresses if resolve_country(a.get("addressCountry")) == country), {})
            low, high, currency, period = _ashby_salary(posting)
            yield Job(
                source=self.name,
                source_id=f"{company}:{posting['id']}",
                country=country,
                title=clean_line(posting.get("title")) or "(untitled)",
                company=name,
                location=clean_line(join(*places, sep="; ")),
                city=first(here.get("addressLocality")),
                region=first(here.get("addressRegion")),
                work_mode=work_mode(posting.get("workplaceType")) or ("remote" if posting.get("isRemote") else None),
                employment_type=_words(posting.get("employmentType")),
                category=first(posting.get("department"), posting.get("team")),
                salary_min=low,
                salary_max=high,
                salary_currency=currency,
                salary_period=period,
                salary_text=((posting.get("compensation") or {}).get("compensationTierSummary")),
                description=clean_text(first(posting.get("descriptionPlain"), posting.get("descriptionHtml"))),
                url=first(posting.get("jobUrl"), posting.get("applyUrl")),
                posted_at=to_iso(posting.get("publishedAt")),
            )


class SmartRecruiters(CompanyBoardSource):
    name = "smartrecruiters"
    label = "SmartRecruiters boards"
    coverage = "career sites of companies you list that use SmartRecruiters"

    def fetch_company(self, company: str, name: str, country: str, ctx: FetchContext) -> Iterator[Job]:
        url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
        offset = 0
        while True:
            params = {"country": country.lower(), "limit": 100, "offset": offset}
            data = ctx.http.get_json(url, params=params)
            items = data.get("content") or []
            for posting in items:
                place = posting.get("location") or {}
                if (place.get("country") or "").upper() != country:
                    continue
                owner = posting.get("company") or {}
                slug = owner.get("identifier") or company
                mode = "remote" if place.get("remote") else "hybrid" if place.get("hybrid") else "onsite"
                yield Job(
                    source=self.name,
                    source_id=f"{company}:{posting['id']}",
                    country=country,
                    title=clean_line(posting.get("name")) or "(untitled)",
                    company=first(owner.get("name"), name),
                    location=clean_line(first(place.get("fullLocation"), join(place.get("city"), place.get("region")))),
                    city=place.get("city"),
                    region=place.get("region"),
                    work_mode=mode,
                    employment_type=label(posting.get("typeOfEmployment")),
                    category=first(label(posting.get("function")), label(posting.get("department"))),
                    url=f"https://jobs.smartrecruiters.com/{slug}/{posting['id']}-{_slug(posting.get('name'))}",
                    posted_at=to_iso(posting.get("releasedDate")),
                )
            offset += len(items)
            if not items or offset >= int(data.get("totalFound") or 0):
                return


# ----------------------------------------------------------------------- helpers


_VAGUE_PLACES = {
    "anywhere", "distributed", "flexible", "global", "hybrid", "in office", "in-office",
    "multiple locations", "n/a", "na", "office", "on-site", "onsite", "remote", "various",
    "worldwide",
}


def _first_place(text: str | None) -> str | None:
    """"Berlin, Germany; Remote" -> "Berlin"; "Hybrid" -> None."""
    if not text:
        return None
    head = re.split(r"[;|]", text)[0].split(",")[0].strip()
    if not head or head.lower() in _VAGUE_PLACES or work_mode(head):
        return None
    return head


def _postal(item: dict[str, Any]) -> dict[str, Any]:
    return ((item.get("address") or {}).get("postalAddress")) or {}


def _ashby_salary(posting: dict[str, Any]):
    for part in (posting.get("compensation") or {}).get("summaryComponents") or []:
        if part.get("compensationType") == "Salary":
            low, high = to_float(part.get("minValue")), to_float(part.get("maxValue"))
            if low or high:
                return low, high, part.get("currencyCode"), salary_period(part.get("interval"))
    return None, None, None, None


def _words(value: str | None) -> str | None:
    """"FullTime" -> "full-time"."""
    if not value:
        return None
    return re.sub(r"(?<=[a-z])(?=[A-Z])", "-", value).lower()


def _slug(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
