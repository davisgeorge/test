"""Post new job openings to a Slack, Discord or generic JSON webhook."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

import requests

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
