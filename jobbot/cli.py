"""Command line: ``jobbot fetch --country se`` and friends."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path

from . import __version__, notify
from .config import ConfigError, load_config
from .countries import country_name, resolve_country, suggest_countries
from .export import FORMATS, write_jobs
from .runner import RunResult, availability, run
from .sources import ALL_SOURCES, SOURCES_BY_NAME
from .store import Store

log = logging.getLogger("jobbot")


class UsageError(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    if not args.verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
    try:
        return args.handler(args)
    except (UsageError, ConfigError) as exc:
        print(f"jobbot: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\njobbot: stopped", file=sys.stderr)
        return 130


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=os.environ.get("JOBBOT_DB", "jobbot.db"),
                        help="SQLite database file (default: jobbot.db, or $JOBBOT_DB)")
    common.add_argument("--config", help="settings file (default: ./jobbot.toml if present)")
    common.add_argument("-v", "--verbose", action="store_true", help="show debug output")
    common.add_argument("-q", "--quiet", action="store_true", help="only show warnings and the summary")

    parser = argparse.ArgumentParser(
        prog="jobbot",
        description="Collect every job opening in a country from public job APIs.",
    )
    parser.add_argument("--version", action="version", version=f"jobbot {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("sources", parents=[common], help="list job sources and what they cover")
    p.add_argument("--country", "-c", help="show which sources can run for this country")
    p.set_defaults(handler=cmd_sources)

    fetch_options = argparse.ArgumentParser(add_help=False)
    fetch_options.add_argument("--country", "-c", required=True, help='country code or name, e.g. "se" or "Sweden"')
    fetch_options.add_argument("--source", "-s", action="append", choices=sorted(SOURCES_BY_NAME),
                               help="only use this source (repeatable)")
    fetch_options.add_argument("--full", action="store_true",
                               help="re-download everything instead of only recent changes")
    fetch_options.add_argument("--since-days", type=float, metavar="N",
                               help="only fetch jobs posted or changed in the last N days")
    fetch_options.add_argument("--max-requests", type=int, help="request budget per source for this run")
    fetch_options.add_argument("--out", "-o", help="also export the country's open jobs to this file")
    fetch_options.add_argument("--webhook", default=os.environ.get("JOBBOT_WEBHOOK_URL"),
                               help="Slack/Discord/JSON webhook to post new openings to ($JOBBOT_WEBHOOK_URL)")

    p = sub.add_parser("fetch", parents=[common, fetch_options], help="fetch job openings for a country")
    p.set_defaults(handler=cmd_fetch)

    p = sub.add_parser("watch", parents=[common, fetch_options],
                       help="keep fetching on a schedule and report new openings")
    p.add_argument("--every", default="6h", help="time between runs, e.g. 30m, 6h, 1d (default 6h)")
    p.set_defaults(handler=cmd_watch)

    p = sub.add_parser("export", parents=[common], help="write stored jobs to CSV, JSON or JSON Lines")
    p.add_argument("--country", "-c", required=True)
    p.add_argument("--out", "-o", required=True, help="output file; format from extension unless --format")
    p.add_argument("--format", "-f", choices=FORMATS)
    p.add_argument("--source", "-s", action="append", choices=sorted(SOURCES_BY_NAME))
    p.add_argument("--include-closed", action="store_true", help="also export jobs no longer listed")
    p.add_argument("--no-dedupe", action="store_true", help="keep the same opening once per source")
    p.add_argument("--no-description", action="store_true", help="leave out the long description text")
    p.set_defaults(handler=cmd_export)

    p = sub.add_parser("stats", parents=[common], help="show what is stored and recent crawls")
    p.add_argument("--country", "-c")
    p.set_defaults(handler=cmd_stats)
    return parser


# ----------------------------------------------------------------------- commands


def cmd_sources(args) -> int:
    config = load_config(args.config)
    if args.country:
        country = parse_country(args.country)
        rows = availability(country, config, os.environ)
        print(f"Sources for {country_name(country)} ({country}):\n")
        if not rows:
            print("  none")
        for item in rows:
            state = "ready" if item.ready else item.reason
            print(f"  {item.source.name:<16} {item.source.coverage}")
            print(f"  {'':<16} -> {state}\n")
        return 0
    for source in ALL_SOURCES:
        where = "any country (companies you list)" if source.countries is None else " ".join(sorted(source.countries))
        print(f"{source.name:<16} {source.coverage}")
        print(f"{'':<16} countries: {where}")
        if source.env_vars:
            print(f"{'':<16} needs: {', '.join(source.env_vars)} — {source.signup}")
        print()
    return 0


def cmd_fetch(args) -> int:
    config = load_config(args.config)
    country = parse_country(args.country)
    usable = [a for a in availability(country, config, os.environ) if a.ready]
    if args.source:
        usable = [a for a in usable if a.source.name in args.source]
    if not usable:
        raise UsageError(no_sources_message(country, args.source, config))

    began = time.monotonic()
    store = Store(args.db)
    try:
        result = run(
            country,
            store,
            config,
            only=args.source,
            full=args.full,
            since_days=args.since_days,
            max_requests=args.max_requests,
        )
        print_summary(store, result, time.monotonic() - began)
        if args.out:
            count = write_jobs(store.iter_jobs(country), args.out)
            print(f"\nWrote {count:,} open jobs to {args.out}")
        if args.webhook:
            post_new_jobs(store, result, args.webhook)
    finally:
        store.close()
    return 1 if all(c.status == "error" for c in result.ran) else 0


def cmd_watch(args) -> int:
    interval = parse_interval(args.every)
    while True:
        try:
            cmd_fetch(args)
        except UsageError:
            raise
        except Exception as exc:  # a bad run should not stop the bot
            log.exception("run failed: %s", exc)
        log.info("next run in %s (Ctrl+C to stop)", args.every)
        time.sleep(interval)


def cmd_export(args) -> int:
    country = parse_country(args.country)
    if not Path(args.db).exists():
        raise UsageError(f"no database at {args.db} yet; run `jobbot fetch --country {args.country}` first")
    store = Store(args.db)
    try:
        rows = store.iter_jobs(
            country,
            include_closed=args.include_closed,
            dedupe=not args.no_dedupe,
            sources=args.source,
        )
        count = write_jobs(rows, args.out, args.format, description=not args.no_description)
    finally:
        store.close()
    print(f"Wrote {count:,} jobs to {args.out}")
    return 0


def cmd_stats(args) -> int:
    country = parse_country(args.country) if args.country else None
    if not Path(args.db).exists():
        raise UsageError(f"no database at {args.db} yet; run `jobbot fetch --country <code>` first")
    store = Store(args.db)
    try:
        rows = store.summary(country)
        if not rows:
            print("Nothing stored yet.")
            return 0
        print(f"{'country':<8} {'source':<16} {'open':>10} {'stored':>10}  last seen")
        for row in rows:
            print(f"{row['country']:<8} {row['source']:<16} {row['active']:>10,} {row['total']:>10,}  "
                  f"{_short(row['last_seen'])}")
        countries = sorted({row["country"] for row in rows})
        print()
        for code in countries:
            unique = store.count_jobs(code, dedupe=True)
            print(f"{country_name(code)}: {unique:,} unique open jobs")
        print("\nRecent crawls:")
        for crawl in store.recent_crawls(country, limit=10):
            note = f"  ({crawl['note']})" if crawl["note"] else ""
            print(f"  {_short(crawl['started_at'])}  {crawl['country']} {crawl['source']:<16} {crawl['mode']:<12} "
                  f"{crawl['status']:<8} {crawl['fetched']:>9,} found {crawl['new']:>9,} new{note}")
    finally:
        store.close()
    return 0


# ----------------------------------------------------------------------- helpers


def _short(timestamp: str | None) -> str:
    """"2026-11-26T21:49:35.123456Z" -> "2026-11-26 21:49:35"."""
    return (timestamp or "")[:19].replace("T", " ")


def parse_country(value: str) -> str:
    code = resolve_country(value)
    if code is None:
        hints = suggest_countries(value)
        raise UsageError(f"unknown country {value!r}" + (f"; did you mean {', '.join(hints)}?" if hints else ""))
    return code


def parse_interval(text: str) -> float:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", text.lower())
    if not match:
        raise UsageError(f"bad interval {text!r}; use e.g. 30m, 6h or 1d")
    value, unit = float(match.group(1)), match.group(2) or "s"
    seconds = value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if seconds < 60:
        raise UsageError("interval must be at least a minute")
    return seconds


def no_sources_message(country: str, only: list[str] | None, config) -> str:
    name = country_name(country)
    rows = [a for a in availability(country, config, os.environ) if not only or a.source.name in only]
    lines = [f"no job source is ready for {name} ({country})."]
    if rows:
        lines.append("To enable one:")
        lines += [f"  {a.source.name}: {a.reason}" for a in rows]
    else:
        lines.append("None of the selected sources cover it; run `jobbot sources` to see what does.")
    return "\n".join(lines)


def print_summary(store: Store, result: RunResult, seconds: float) -> None:
    country = result.country
    minutes, secs = divmod(int(seconds), 60)
    print(f"\n{country_name(country)} ({country}) — finished in {minutes}m {secs:02d}s\n")
    print(f"  {'source':<16} {'mode':<12} {'status':<8} {'found':>9} {'new':>9} {'closed':>8} {'requests':>9}")
    for crawl in result.ran:
        print(f"  {crawl.source:<16} {crawl.mode:<12} {crawl.status:<8} {crawl.fetched:>9,} "
              f"{crawl.new:>9,} {crawl.closed:>8,} {crawl.requests:>9,}")
        if crawl.status != "ok":
            for note in crawl.notes[-3:]:
                print(f"  {'':<16} {note}")
    skipped = [c for c in result.crawls if c.status == "skipped"]
    if skipped:
        needs_key = [c.source for c in skipped if "key" in c.notes[0]]
        needs_list = [c.source for c in skipped if c.source not in needs_key]
        parts = []
        if needs_key:
            parts.append(f"{', '.join(needs_key)} (need an API key)")
        if needs_list:
            parts.append(f"{', '.join(needs_list)} (no companies listed in jobbot.toml)")
        print(f"\n  Not used: {'; '.join(parts)}.")
        print(f"  See `jobbot sources --country {country.lower()}` to enable them.")
    total = store.count_jobs(country)
    unique = store.count_jobs(country, dedupe=True)
    print(f"\nOpen jobs stored for {country_name(country)}: {total:,} ({unique:,} unique across sources)")

    returning = result.returning_sources
    if not returning:
        return
    new_count = store.count_jobs(country, dedupe=True, sources=returning, first_seen_since=result.started_at)
    if new_count:
        print(f"\nNew since the last run: {new_count:,}")
        for job in store.iter_jobs(country, sources=returning, first_seen_since=result.started_at, limit=10):
            print(f"  {notify.format_job(job)}")
        if new_count > 10:
            print(f"  …and {new_count - 10:,} more (see `jobbot export`)")


def post_new_jobs(store: Store, result: RunResult, url: str) -> None:
    returning = result.returning_sources
    if not returning:
        log.info("first crawl for these sources: not posting the initial load to the webhook")
        return
    country = result.country
    count = store.count_jobs(country, dedupe=True, sources=returning, first_seen_since=result.started_at)
    if not count:
        return
    jobs = list(store.iter_jobs(country, sources=returning, first_seen_since=result.started_at, limit=50))
    try:
        notify.send(url, country, country_name(country), jobs, count)
        log.info("posted %s new jobs to the webhook", count)
    except Exception as exc:
        log.warning("could not post to the webhook: %s", exc)
