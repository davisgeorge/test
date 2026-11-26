"""What every job source looks like, plus shared helpers."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from math import ceil
from typing import Any, ClassVar

import requests

from ..countries import mentions_country
from ..http import HttpError
from ..models import Job, Removed, utc_now

log = logging.getLogger("jobbot")


@dataclass
class FetchContext:
    """Everything a source needs for one crawl, and what it reports back."""

    http: Any  # jobbot.http.Http, or a stand-in with get_json/post_json/iter_lines
    since: datetime | None = None  # set: only fetch what changed since then (if supported)
    options: dict[str, Any] = field(default_factory=dict)  # [sources.<name>] settings
    companies: list[str] = field(default_factory=list)  # for company job-board sources
    location_keywords: Mapping[str, list[str]] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    now: datetime = field(default_factory=utc_now)

    # Filled in by the source:
    incremental: bool = False  # True if only recent changes were fetched
    complete: bool = True  # False if the source knows it missed listings
    notes: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.notes.append(message)
        log.info("    %s", message)

    def incomplete(self, reason: str) -> None:
        self.complete = False
        self.note(reason)

    def days_since(self) -> int | None:
        """Whole days covered by an incremental fetch, rounded up."""
        if self.since is None:
            return None
        return max(1, ceil((self.now - self.since).total_seconds() / 86400))

    def in_country(self, country: str, texts: Iterable[str | None]) -> bool:
        return mentions_country(texts, country, self.location_keywords)


class Source:
    name: ClassVar[str]
    label: ClassVar[str]
    coverage: ClassVar[str]
    countries: ClassVar[frozenset[str] | None] = None  # None: works for any country
    env_vars: ClassVar[tuple[str, ...]] = ()  # API credentials it needs
    signup: ClassVar[str | None] = None  # where to get those credentials
    needs_companies: ClassVar[bool] = False  # reads a list of company job boards
    min_interval: ClassVar[float] = 0.0  # seconds between requests
    max_requests: ClassVar[int | None] = None  # default request budget per run
    incremental: ClassVar[bool] = False  # can fetch only what changed recently
    # Re-crawl everything this often so removed jobs get closed (None: never needed).
    full_refresh_days: ClassVar[int | None] = 7

    def supports(self, country: str) -> bool:
        return self.countries is None or country in self.countries

    def missing_env(self, env: Mapping[str, str]) -> list[str]:
        return [name for name in self.env_vars if not env.get(name)]

    def fetch(self, country: str, ctx: FetchContext) -> Iterator[Job | Removed]:
        raise NotImplementedError


class CompanyBoardSource(Source):
    """Reads the public job boards of companies listed in the config file."""

    needs_companies = True
    min_interval = 0.5
    full_refresh_days = None  # boards are always read in full

    def fetch(self, country: str, ctx: FetchContext) -> Iterator[Job]:
        for entry in ctx.companies:
            # "slug" or "slug=Display Name"
            company, _, name = (part.strip() for part in entry.partition("="))
            count = 0
            try:
                for job in self.fetch_company(company, name or company_name(company), country, ctx):
                    count += 1
                    yield job
            except (HttpError, requests.RequestException, ValueError) as exc:
                ctx.incomplete(f"{company}: {exc}")
                continue
            log.info("    %s: %d jobs", company, count)

    def fetch_company(self, company: str, name: str, country: str, ctx: FetchContext) -> Iterator[Job]:
        """Yield the jobs of one board. ``name`` is the company's display name."""
        raise NotImplementedError


# ----------------------------------------------------------------------- helpers


def company_name(slug: str) -> str:
    """Board slugs are often lower-case ("acme-corp"); make them readable."""
    if any(ch.isupper() for ch in slug):
        return slug
    return slug.replace("-", " ").replace("_", " ").title()


def work_mode(value: Any) -> str | None:
    text = str(value or "").lower().replace("-", "").replace("_", "").replace(" ", "")
    if "hybrid" in text:
        return "hybrid"
    if any(word in text for word in ("remote", "distans", "telework", "distributed")):
        return "remote"
    if "onsite" in text or "office" in text or "plats" in text:
        return "onsite"
    return None


def salary_period(value: Any) -> str | None:
    text = str(value or "").lower()
    for key, period in (
        ("hour", "hour"),
        ("day", "day"),
        ("week", "week"),
        ("month", "month"),
        ("year", "year"),
        ("annual", "year"),
        ("annum", "year"),
    ):
        if key in text:
            return period
    return None


def label(value: Any) -> str | None:
    """``{"label": "Full-time"}`` -> ``"Full-time"``."""
    if isinstance(value, dict):
        return value.get("label") or value.get("name") or None
    return value or None


def join(*parts: Any, sep: str = ", ") -> str | None:
    text = sep.join(str(p) for p in parts if p)
    return text or None
