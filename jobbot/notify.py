"""Post new job openings to a Slack, Discord or generic JSON webhook."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

import requests

from .countries import country_name

log = logging.getLogger("jobbot")

DISCORD_LIMIT = 2000


def format_job(job: dict[str, Any]) -> str:
    where = job.get("location") or job.get("city")
    line = f"• {job.get('title')}"
    if job.get("company"):
        line += f" — {job['company']}"
    if where:
        line += f" ({where})"
    if job.get("url"):
        line += f" {job['url']}"
    return line


def build_message(country_name: str, jobs: Sequence[dict[str, Any]], total: int, limit: int = 4000) -> str:
    noun = "opening" if total == 1 else "openings"
    lines = [f"jobbot: {total:,} new job {noun} in {country_name}"]
    shown = 0
    for job in jobs:
        line = format_job(job)
        if sum(len(x) + 1 for x in lines) + len(line) + 40 > limit:
            break
        lines.append(line)
        shown += 1
    if total > shown:
        lines.append(f"…and {total - shown:,} more")
    return "\n".join(lines)


def send(
    url: str,
    country: str,
    country_name: str,
    jobs: Sequence[dict[str, Any]],
    total: int,
    *,
    post=requests.post,
) -> None:
    host = (urlparse(url).hostname or "").lower()
    if host in ("discord.com", "discordapp.com") or host.endswith((".discord.com", ".discordapp.com")):
        payload: dict[str, Any] = {"content": build_message(country_name, jobs, total, DISCORD_LIMIT)}
    elif host == "hooks.slack.com":
        payload = {"text": build_message(country_name, jobs, total)}
    else:
        payload = {
            "text": build_message(country_name, jobs, total),
            "country": country,
            "new_jobs": total,
            "jobs": [
                {k: job.get(k) for k in ("title", "company", "location", "url", "posted_at", "source")}
                for job in jobs
            ],
        }
    response = post(url, json=payload, timeout=30)
    response.raise_for_status()


def new_jobs_of_run(store, result, limit: int = 50) -> tuple[list[dict[str, Any]], int]:
    """Openings first seen during ``result``'s run, skipping sources whose
    first-ever crawl this was (that is a backlog, not news)."""
    returning = result.returning_sources
    if not returning:
        return [], 0
    country = result.country
    count = store.count_jobs(country, dedupe=True, sources=returning, first_seen_since=result.started_at)
    if not count:
        return [], 0
    jobs = list(store.iter_jobs(country, sources=returning, first_seen_since=result.started_at, limit=limit))
    return jobs, count


def notify_run(store, result, url: str, *, post=requests.post) -> int:
    """Post a run's new openings to a webhook. Returns how many were new."""
    jobs, count = new_jobs_of_run(store, result)
    if not count:
        return 0
    try:
        send(url, result.country, country_name(result.country), jobs, count, post=post)
        log.info("posted %s new jobs to the webhook", count)
    except Exception as exc:
        log.warning("could not post to the webhook: %s", exc)
    return count
