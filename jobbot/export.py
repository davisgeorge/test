"""Write stored jobs to CSV, JSON or JSON Lines."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

FIELDS = [
    "country", "title", "company", "location", "city", "region", "work_mode",
    "employment_type", "category", "salary_min", "salary_max", "salary_currency",
    "salary_period", "salary_text", "posted_at", "expires_at", "url", "source",
    "source_id", "sources", "first_seen", "last_seen", "closed_at", "description",
]
FORMATS = ("csv", "json", "jsonl")


def guess_format(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix in ("jsonl", "ndjson"):
        return "jsonl"
    return suffix if suffix in FORMATS else "csv"


def write_jobs(
    rows: Iterable[dict[str, Any]],
    path: str | Path,
    fmt: str | None = None,
    *,
    description: bool = True,
) -> int:
    """Stream ``rows`` to ``path``. Returns the number of jobs written."""
    path = Path(path)
    fmt = fmt or guess_format(path)
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; use one of {', '.join(FORMATS)}")
    fields = FIELDS if description else [f for f in FIELDS if f != "description"]
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    # utf-8-sig so Excel shows accented characters correctly in CSV files
    with path.open("w", encoding="utf-8-sig" if fmt == "csv" else "utf-8", newline="") as out:
        if fmt == "csv":
            writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
                count += 1
        elif fmt == "jsonl":
            for row in rows:
                out.write(json.dumps({f: row.get(f) for f in fields}, ensure_ascii=False) + "\n")
                count += 1
        else:
            out.write("[")
            for row in rows:
                out.write(",\n " if count else "\n ")
                out.write(json.dumps({f: row.get(f) for f in fields}, ensure_ascii=False))
                count += 1
            out.write("\n]\n" if count else "]\n")
    return count
