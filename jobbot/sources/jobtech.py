"""Sweden: every ad on Platsbanken, the Swedish Public Employment Service's
job board, from the open JobTech JobStream API (no key needed).

The first crawl downloads the full snapshot of published ads (large: roughly
half a gigabyte for ~45,000 ads). Later crawls ask the change feed for what
was added, updated or removed since the previous crawl.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta, timezone

from ..models import Job, Removed, clean_line, clean_text, first, to_iso
from .base import FetchContext, Source, join, label, work_mode

SNAPSHOT_URL = "https://jobstream.api.jobtechdev.se/v2/snapshot"
STREAM_URL = "https://jobstream.api.jobtechdev.se/v2/stream"
AD_URL = "https://arbetsformedlingen.se/platsbanken/annonser/{id}"
SWEDEN = "i46j_HmG_v64"  # taxonomy concept id for the country Sverige

try:
    from zoneinfo import ZoneInfo

    STOCKHOLM = ZoneInfo("Europe/Stockholm")
except Exception:  # no tz database available (some Windows installs)
    STOCKHOLM = timezone(timedelta(hours=1))


class JobTech(Source):
    name = "jobtech"
    label = "JobTech (Platsbanken)"
    coverage = "every ad on Platsbanken, Sweden's public job board (Arbetsförmedlingen)"
    countries = frozenset({"SE"})
    incremental = True
    full_refresh_days = None  # the change feed reports removed ads

    def fetch(self, country: str, ctx: FetchContext) -> Iterator[Job | Removed]:
        headers = {"Accept": "application/jsonl"}
        if ctx.since is None:
            lines = ctx.http.iter_lines(SNAPSHOT_URL, headers=headers)
        else:
            ctx.incremental = True
            # The API reads this as Swedish local time. Sending the UTC clock
            # time asks for an hour or two more than needed, never less.
            after = ctx.since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            lines = ctx.http.iter_lines(STREAM_URL, params={"updated-after": after}, headers=headers)

        abroad = 0
        for line in lines:
            ad = json.loads(line)
            if ad.get("removed"):
                yield Removed(str(ad["id"]))
                continue
            job = to_job(ad)
            if job is None:
                abroad += 1
            else:
                yield job
        if abroad:
            ctx.note(f"skipped {abroad} ads for jobs outside Sweden")


def to_job(ad: dict) -> Job | None:
    address = ad.get("workplace_address") or {}
    concept = address.get("country_concept_id")
    if (concept and concept != SWEDEN) or (not concept and address.get("country") not in (None, "", "Sverige")):
        return None
    employer = ad.get("employer") or {}
    city = first(address.get("municipality"), (address.get("city") or "").title())
    region = address.get("region")
    hours = label(ad.get("working_hours_type"))
    return Job(
        source="jobtech",
        source_id=str(ad["id"]),
        country="SE",
        title=clean_line(ad.get("headline")) or "(untitled)",
        company=clean_line(first(employer.get("name"), employer.get("workplace"))),
        location=join(city, region),
        city=city,
        region=region,
        work_mode=work_mode(label(ad.get("workplace_model"))),
        employment_type=join(label(ad.get("employment_type")), hours),
        category=label(ad.get("occupation_field")),
        salary_text=clean_line(ad.get("salary_description")),
        description=clean_text((ad.get("description") or {}).get("text")),
        url=first(ad.get("webpage_url"), AD_URL.format(id=ad["id"])),
        posted_at=to_iso(ad.get("publication_date"), STOCKHOLM),
        expires_at=to_iso(ad.get("application_deadline"), STOCKHOLM),
    )
