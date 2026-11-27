"""Runs every usable source for a country and records what it found."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import Config
from .http import BudgetExceeded, Http
from .models import Removed, iso, parse_iso, utc_now
from .sources import ALL_SOURCES, FetchContext, Source
from .store import Store

log = logging.getLogger("jobbot")

OVERLAP = timedelta(hours=1)  # start incremental fetches a little early so nothing slips through
MAX_INCREMENTAL_GAP = timedelta(days=30)  # after a longer break, crawl everything again
COMMIT_EVERY = 1000
LOG_EVERY = 5000
PROGRESS_EVERY = 250

Progress = Callable[[dict], None]


@dataclass
class Availability:
    source: Source
    ready: bool
    reason: str = ""
    needs: str | None = None  # "key" or "companies" when not ready
    missing: tuple[str, ...] = ()  # environment variables still to set


def availability(country: str, config: Config, env: Mapping[str, str]) -> list[Availability]:
    """Every source that covers ``country``, and whether it can run right now."""
    found = []
    for source in ALL_SOURCES:
        if not source.supports(country):
            continue
        missing = source.missing_env(env)
        if missing:
            reason = f"set {' and '.join(missing)} — get a key at {source.signup}"
            found.append(Availability(source, False, reason, "key", tuple(missing)))
        elif source.needs_companies and not config.companies.get(source.name):
            reason = f"add companies to [companies] {source.name} in jobbot.toml"
            found.append(Availability(source, False, reason, "companies"))
        else:
            found.append(Availability(source, True))
    return found


@dataclass
class CrawlResult:
    source: str
    mode: str = "full"  # "full", "incremental" (since the last crawl) or "recent" (--since-days)
    status: str = "ok"  # "ok", "partial", "error" or "skipped"
    fetched: int = 0
    new: int = 0
    closed: int = 0
    requests: int = 0
    notes: list[str] = field(default_factory=list)
    initial: bool = False  # first crawl of this source for this country


@dataclass
class RunResult:
    country: str
    started_at: str
    crawls: list[CrawlResult]

    @property
    def ran(self) -> list[CrawlResult]:
        return [c for c in self.crawls if c.status != "skipped"]

    @property
    def returning_sources(self) -> list[str]:
        """Sources that had been crawled before: their new jobs are really new."""
        return [c.source for c in self.ran if not c.initial]


def make_http(source: Source, max_requests: int | None) -> Http:
    return Http(min_interval=source.min_interval, max_requests=max_requests)


def run(
    country: str,
    store: Store,
    config: Config,
    *,
    only: Sequence[str] | None = None,
    full: bool = False,
    since_days: float | None = None,
    max_requests: int | None = None,
    env: Mapping[str, str] | None = None,
    http_factory: Callable[[Source, int | None], object] = make_http,
    progress: Progress | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> RunResult:
    """Crawl every ready source for ``country``.

    ``full`` re-downloads everything; ``since_days`` asks only for jobs posted
    or changed in that many recent days (for sources that support it).
    ``progress`` is called with counts as the crawl goes; ``should_stop``
    returning True ends the crawl early (nothing gets closed then).
    """
    env = os.environ if env is None else env
    started = iso(utc_now())
    crawls = []
    plan = [item for item in availability(country, config, env) if not only or item.source.name in only]
    for item in plan:
        if not item.ready:
            crawls.append(CrawlResult(item.source.name, mode="-", status="skipped", notes=[item.reason]))
            continue
        if should_stop and should_stop():
            break
        crawls.append(
            _crawl(
                item.source, country, store, config, env, full, since_days, max_requests, http_factory,
                progress, should_stop,
            )
        )
    return RunResult(country, started, crawls)


def _since(source: Source, country: str, store: Store, now: datetime) -> datetime | None:
    """Where an incremental crawl should start, or None for a full crawl."""
    if not source.incremental:
        return None
    last_full = store.last_crawl(source.name, country, modes=("full",))
    if last_full is None:
        return None  # never had a complete listing yet
    if source.full_refresh_days is not None and now - parse_iso(last_full["started_at"]) >= timedelta(
        days=source.full_refresh_days
    ):
        return None  # time for a full crawl, which also closes jobs that disappeared
    last_ok = store.last_crawl(source.name, country, modes=("full", "incremental"))
    since = parse_iso(last_ok["started_at"]) - OVERLAP
    if now - since > MAX_INCREMENTAL_GAP:
        return None
    return since


def _crawl(
    source, country, store, config, env, full, since_days, max_requests, http_factory, progress=None, should_stop=None
) -> CrawlResult:
    now = utc_now()
    started = iso(now)
    if since_days is not None:
        since = now - timedelta(days=since_days) if source.incremental else None
    else:
        since = None if full else _since(source, country, store, now)
    options = config.source_options(source.name)
    budget = max_requests if max_requests is not None else options.get("max_requests", source.max_requests)
    http = http_factory(source, budget)
    ctx = FetchContext(
        http=http,
        since=since,
        options=options,
        companies=config.companies.get(source.name, []),
        location_keywords=config.locations,
        env=env,
        now=now,
    )
    result = CrawlResult(source.name, initial=not store.has_data(source.name, country))
    planned = "full" if not ctx.since else "recent" if since_days is not None else "incremental"
    crawl_id = store.start_crawl(source.name, country, planned, started)
    what = f"changes since {iso(ctx.since)}" if ctx.since else "everything"
    log.info("%s: fetching %s for %s", source.name, what, country)

    def report(done: bool = False) -> None:
        if progress:
            progress({"source": source.name, "fetched": result.fetched, "new": result.new,
                      "closed": result.closed, "done": done, "initial": result.initial})

    report()
    error = None
    try:
        for item in source.fetch(country, ctx):
            if should_stop and should_stop():
                ctx.incomplete("stopped on request")
                break
            if isinstance(item, Removed):
                result.closed += store.close_job(f"{source.name}:{item.source_id}", started)
                continue
            if item.country != country:
                continue
            result.fetched += 1
            result.new += store.upsert(item, started, initial=result.initial)
            if result.fetched % COMMIT_EVERY == 0:
                store.commit()
            if result.fetched % PROGRESS_EVERY == 0:
                report()
            if result.fetched % LOG_EVERY == 0:
                log.info("    %s jobs so far (%s new)", f"{result.fetched:,}", f"{result.new:,}")
    except BudgetExceeded as exc:
        ctx.incomplete(f"stopped early: {exc}")
    except Exception as exc:  # keep going with the other sources
        log.debug("%s failed", source.name, exc_info=True)
        error = exc
    store.commit()

    if ctx.incremental:
        # "recent" crawls (--since-days) are not a baseline for later incremental crawls
        result.mode = "recent" if since_days is not None else "incremental"
    else:
        result.mode = "full"
    result.requests = getattr(http, "requests_made", 0)
    result.notes = list(ctx.notes)
    if error is not None:
        result.status = "error"
        result.notes.append(f"{type(error).__name__}: {error}")
        log.warning("%s: failed: %s", source.name, error)
    elif not ctx.complete:
        result.status = "partial"
    elif result.mode == "full":
        # A complete listing: anything not in it has been taken down.
        result.closed += store.close_unseen(source.name, country, started, started)
    store.commit()
    store.finish_crawl(
        crawl_id,
        mode=result.mode,
        status=result.status,
        finished_at=iso(utc_now()),
        fetched=result.fetched,
        new=result.new,
        closed=result.closed,
        requests=result.requests,
        note="; ".join(result.notes) or None,
    )
    report(done=True)
    log.info(
        "%s: %s — %s jobs, %s new, %s closed, %s requests",
        source.name,
        result.status,
        f"{result.fetched:,}",
        f"{result.new:,}",
        f"{result.closed:,}",
        result.requests,
    )
    return result
