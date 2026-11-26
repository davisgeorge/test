"""Germany: the Federal Employment Agency's job board (Bundesagentur für
Arbeit, "Jobsuche"), via its public API. The client id below is the public one
documented at https://jobsuche.api.bund.dev; no registration is needed.

One query can only page through the first 10,000 results, but the board lists
~800,000 jobs. So the crawl splits the search into smaller slices (by
occupational field, then temp-agency yes/no, contract type and private agency
yes/no) until each slice fits in that window. Slices that are still too big
are cut by geography into overlapping circles around cities. The crawl
reports how much of the total it reached.
"""

from __future__ import annotations

from collections.abc import Iterator
from math import ceil
from typing import Any

from ..http import HttpError
from ..models import Job, clean_line, first, to_float, to_iso
from .base import FetchContext, Source, join

API_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
HEADERS = {"X-API-Key": "jobboerse-jobsuche"}
JOB_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"
WINDOW = 10_000  # deepest result reachable by paging one query
PAGE_SIZE = 500

# The API's "wo" (place) is a geocoded search with a radius, not a region
# filter: "Hessen" finds a village called Hessen. So geography is cut into
# overlapping circles around cities that together cover the country.
CIRCLE_KM = 150
CIRCLE_CENTRES = (
    "Flensburg", "Kiel", "Hamburg", "Rostock", "Bremen", "Hannover", "Berlin", "Cottbus",
    "Münster", "Aachen", "Kassel", "Magdeburg", "Leipzig", "Dresden", "Görlitz", "Köln",
    "Trier", "Frankfurt am Main", "Erfurt", "Würzburg", "Nürnberg", "Saarbrücken",
    "Stuttgart", "Freiburg im Breisgau", "Konstanz", "München", "Regensburg", "Passau",
)
# Each step narrows the query further. Values come from the facet counts the
# API returns with every search; the tuple is the fallback.
SPLITS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("berufsfeld", ()),
    ("zeitarbeit", ("false", "true")),
    ("befristung", ("1", "2", "3")),
    ("pav", ("false", "true")),
    ("wo", CIRCLE_CENTRES),
)
OFFER_TYPES = {1: "job", 2: "self-employment", 4: "apprenticeship", 34: "internship/trainee"}
_OFFER_NAMES = {
    "ARBEIT": "job",
    "SELBSTAENDIGKEIT": "self-employment",
    "AUSBILDUNG": "apprenticeship",
    "PRAKTIKUM_TRAINEE": "internship/trainee",
}
_CONTRACT = {"UNBEFRISTET": "permanent", "BEFRISTET": "fixed-term"}
_PAY_PERIOD = {
    "STUNDENLOHN": "hour",
    "MONATSGEHALT": "month",
    "MONATSLOHN": "month",
    "JAHRESGEHALT": "year",
}


class Arbeitsagentur(Source):
    name = "arbeitsagentur"
    label = "Bundesagentur für Arbeit"
    coverage = "Germany's national job board (Bundesagentur für Arbeit), ~800k jobs"
    countries = frozenset({"DE"})
    min_interval = 0.2
    incremental = True
    full_refresh_days = 7

    def fetch(self, country: str, ctx: FetchContext) -> Iterator[Job]:
        base: dict[str, Any] = {"wo": "Deutschland"}  # the board also lists jobs abroad
        days = ctx.days_since()
        if days is not None and days <= 100:  # the API accepts 0..100 days
            base["veroeffentlichtseit"] = days
            ctx.incremental = True
        for offer in ctx.options.get("offer_types", [1]):
            crawl = _Crawl(ctx, {**base, "angebotsart": offer})
            yield from crawl.run()
            kind = OFFER_TYPES.get(int(offer), f"type {offer}")
            reached = crawl.fetched + crawl.abroad
            ctx.note(
                f"{kind} listings: reached {reached:,} of {crawl.listed:,}"
                + (f" ({crawl.abroad:,} located abroad, skipped)" if crawl.abroad else "")
            )
            if crawl.unreachable:
                ctx.incomplete(f"{crawl.unreachable:,} {kind} listings sat in slices too big to page through")
            elif crawl.listed and reached < 0.98 * crawl.listed:
                ctx.incomplete(f"only reached {reached / crawl.listed:.1%} of {kind} listings")


class _Crawl:
    def __init__(self, ctx: FetchContext, params: dict[str, Any]) -> None:
        self.ctx = ctx
        self.params = params
        self.listed = 0
        self.fetched = 0
        self.abroad = 0  # listed on the German board but located in another country
        self.unreachable = 0

    def run(self) -> Iterator[Job]:
        yield from self._slice(self.params, level=0, seen=None)

    def _page(self, params: dict[str, Any], page: int) -> dict[str, Any]:
        query = {**params, "page": page, "size": PAGE_SIZE}
        return self.ctx.http.get_json(API_URL, params=query, headers=HEADERS)

    def _slice(self, params: dict[str, Any], level: int, seen: set[str] | None) -> Iterator[Job]:
        data = self._page(params, 1)
        total = int(data.get("maxErgebnisse") or 0)
        if level == 0:
            self.listed = total
        if total <= WINDOW or level == len(SPLITS):
            if total > WINDOW:
                self.unreachable += total - WINDOW
            yield from self._read_all(params, data, total, seen)
            return

        name, fallback = SPLITS[level]
        facet = ((data.get("facetten") or {}).get(name) or {}).get("counts") or {}
        values = list(facet) or list(fallback)
        if not values:  # nothing to split on: take what the window allows
            self.unreachable += total - WINDOW
            yield from self._read_all(params, data, total, seen)
            return
        extra: dict[str, Any] = {}
        if name == "wo":
            extra["umkreis"] = CIRCLE_KM
            seen = set() if seen is None else seen  # circles overlap: skip jobs already read
        for value in values:
            if facet and not facet.get(value):
                continue
            yield from self._slice({**params, name: value, **extra}, level + 1, seen)

    def _read_all(self, params, data, total: int, seen: set[str] | None) -> Iterator[Job]:
        pages = ceil(min(total, WINDOW) / PAGE_SIZE)
        for page in range(1, pages + 1):
            if page > 1:
                try:
                    data = self._page(params, page)
                except HttpError as exc:
                    if exc.status != 400:
                        raise
                    # The listing grew past the paging window while we read it.
                    self.unreachable += max(0, min(total, WINDOW) - (page - 1) * PAGE_SIZE)
                    return
            items = data.get("ergebnisliste") or data.get("stellenangebote") or []
            if not items:
                break
            for item in items:
                job = to_job(item)
                if job is None:
                    self.abroad += 1
                    continue
                if seen is not None:
                    if job.source_id in seen:
                        continue
                    seen.add(job.source_id)
                self.fetched += 1
                yield job


def _region(value: str | None) -> str | None:
    """API regions look like "NORDRHEIN_WESTFALEN"."""
    if not value:
        return None
    return "-".join(part.capitalize() for part in value.split("_"))


def to_job(item: dict[str, Any]) -> Job | None:
    refnr = first(item.get("referenznummer"), item.get("refnr"))
    if not refnr:
        return None
    places = item.get("stellenlokationen") or []
    addresses = [p.get("adresse") or {} for p in places] or [item.get("arbeitsort") or {}]
    in_germany = [a for a in addresses if (a.get("land") or "DEUTSCHLAND").upper() == "DEUTSCHLAND"]
    if not in_germany:
        return None  # listed in Germany's board, but the job is abroad
    address = in_germany[0]
    city = first(address.get("ort"))
    region = _region(address.get("region"))

    pay_kind = item.get("artDerVerguetung")
    if pay_kind == "FESTGEHALT":
        low = high = to_float(item.get("festgehalt"))
    elif pay_kind == "GEHALTSSPANNE":
        low, high = to_float(item.get("gehaltsspanneVon")), to_float(item.get("gehaltsspanneBis"))
    else:
        low = high = None
    offer = item.get("stellenangebotsart")
    return Job(
        source="arbeitsagentur",
        source_id=str(refnr),
        country="DE",
        title=clean_line(first(item.get("stellenangebotsTitel"), item.get("titel"), item.get("beruf")))
        or "(untitled)",
        company=clean_line(first(item.get("firma"), item.get("arbeitgeber"))),
        location=join(city, region),
        city=city,
        region=region,
        employment_type=join(
            _OFFER_NAMES.get(offer, offer),
            _CONTRACT.get(item.get("vertragsdauer")),
            "minijob" if item.get("istGeringfuegigeBeschaeftigung") else None,
        ),
        category=first(item.get("hauptberuf"), item.get("beruf")),
        salary_min=low,
        salary_max=high,
        salary_currency="EUR" if (low or high) else None,
        salary_period=_PAY_PERIOD.get(item.get("verguetungsangabe")) if (low or high) else None,
        url=JOB_URL.format(refnr=refnr),
        posted_at=to_iso(
            first(
                item.get("datumErsteVeroeffentlichung"),
                (item.get("veroeffentlichungszeitraum") or {}).get("von"),
                item.get("aktuelleVeroeffentlichungsdatum"),
            )
        ),
        expires_at=to_iso((item.get("veroeffentlichungszeitraum") or {}).get("bis")),
    )
