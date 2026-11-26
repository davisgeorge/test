"""SQLite storage: every job the bot has seen, and a log of every crawl.

A job stays "active" (``closed_at IS NULL``) until a source reports it removed,
or a complete full crawl of that source no longer lists it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import fields
from pathlib import Path
from typing import Any

from .models import Job

JOB_FIELDS = [f.name for f in fields(Job)]
_UPDATE_FIELDS = [name for name in JOB_FIELDS if name not in ("source", "source_id")]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS jobs (
    uid TEXT PRIMARY KEY,
    {", ".join(f"{name} {'REAL' if name.startswith('salary_m') else 'TEXT'}" for name in JOB_FIELDS)},
    fingerprint TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_by_country ON jobs (country, closed_at);
CREATE INDEX IF NOT EXISTS jobs_by_source ON jobs (source, country, last_seen);
CREATE INDEX IF NOT EXISTS jobs_by_fingerprint ON jobs (fingerprint);

CREATE TABLE IF NOT EXISTS crawls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    country TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    fetched INTEGER NOT NULL DEFAULT 0,
    new INTEGER NOT NULL DEFAULT 0,
    closed INTEGER NOT NULL DEFAULT 0,
    requests INTEGER NOT NULL DEFAULT 0,
    note TEXT
);
CREATE INDEX IF NOT EXISTS crawls_by_source ON crawls (source, country, started_at);
"""

_ORDER = "ORDER BY COALESCE(posted_at, first_seen) DESC, uid"


class Store:
    def __init__(self, path: str | Path = "jobbot.db") -> None:
        self.path = str(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def commit(self) -> None:
        self.db.commit()

    # ------------------------------------------------------------------ jobs

    def upsert(self, job: Job, now: str) -> bool:
        """Insert or refresh a job. Returns True if it was not stored before."""
        row = job.to_dict()
        row.update(uid=job.uid, fingerprint=job.fingerprint(), now=now)
        assignments = ", ".join(f"{name} = :{name}" for name in _UPDATE_FIELDS)
        updated = self.db.execute(
            f"UPDATE jobs SET {assignments}, fingerprint = :fingerprint, last_seen = :now, "
            "closed_at = NULL WHERE uid = :uid",
            row,
        ).rowcount
        if updated:
            return False
        names = ["uid", *JOB_FIELDS, "fingerprint"]
        self.db.execute(
            f"INSERT INTO jobs ({', '.join(names)}, first_seen, last_seen) "
            f"VALUES ({', '.join(':' + name for name in names)}, :now, :now)",
            row,
        )
        return True

    def close_job(self, uid: str, now: str) -> bool:
        cursor = self.db.execute(
            "UPDATE jobs SET closed_at = ? WHERE uid = ? AND closed_at IS NULL", (now, uid)
        )
        return cursor.rowcount > 0

    def close_unseen(self, source: str, country: str, seen_since: str, now: str) -> int:
        """Close this source's active jobs that were not seen since ``seen_since``."""
        cursor = self.db.execute(
            "UPDATE jobs SET closed_at = ? WHERE source = ? AND country = ? "
            "AND closed_at IS NULL AND last_seen < ?",
            (now, source, country, seen_since),
        )
        return cursor.rowcount

    def iter_jobs(
        self,
        country: str,
        *,
        include_closed: bool = False,
        dedupe: bool = True,
        sources: Sequence[str] | None = None,
        first_seen_since: str | None = None,
        limit: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stored jobs for a country, newest first.

        With ``dedupe`` each opening listed by several sources appears once, and
        its ``sources`` column names every source that listed it.
        ``first_seen_since`` keeps only openings first seen at or after that time.
        """
        sql, args = self._select(country, include_closed, dedupe, sources, first_seen_since)
        sql += f" {_ORDER}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        for row in self.db.execute(sql, args):
            item = dict(row)
            item.pop("keep_source", None)
            item["sources"] = ",".join(dict.fromkeys(item.pop("all_sources").split(",")))
            yield item

    def count_jobs(
        self,
        country: str,
        *,
        include_closed: bool = False,
        dedupe: bool = False,
        sources: Sequence[str] | None = None,
        first_seen_since: str | None = None,
    ) -> int:
        sql, args = self._select(country, include_closed, dedupe, sources, first_seen_since)
        return self.db.execute(f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]

    @staticmethod
    def _select(country, include_closed, dedupe, sources, first_seen_since) -> tuple[str, list[Any]]:
        where, args = ["country = ?"], [country]
        if not include_closed:
            where.append("closed_at IS NULL")
        if sources is not None:
            where.append(f"source IN ({', '.join('?' for _ in sources) or 'NULL'})")
            args.extend(sources)
        condition = " AND ".join(where)
        if dedupe:
            # Same title, company and place on several sources: keep the listing
            # from the source that had it first. Postings that look alike on the
            # same source are separate openings, so they are all kept.
            sql = f"""
                SELECT * FROM (
                    SELECT jobs.*,
                           FIRST_VALUE(source) OVER (PARTITION BY fingerprint ORDER BY first_seen, uid) AS keep_source,
                           GROUP_CONCAT(source, ',') OVER (PARTITION BY fingerprint) AS all_sources
                    FROM jobs WHERE {condition}
                ) WHERE source = keep_source"""
        else:
            sql = f"SELECT jobs.*, source AS all_sources FROM jobs WHERE {condition}"
        if first_seen_since:
            sql = f"SELECT * FROM ({sql}) WHERE first_seen >= ?"
            args.append(first_seen_since)
        return sql, args

    def summary(self, country: str | None = None) -> list[sqlite3.Row]:
        where, args = ("WHERE country = ?", [country]) if country else ("", [])
        return self.db.execute(
            f"""SELECT country, source, COUNT(*) AS total,
                       SUM(closed_at IS NULL) AS active, MAX(last_seen) AS last_seen
                FROM jobs {where} GROUP BY country, source ORDER BY country, source""",
            args,
        ).fetchall()

    # ---------------------------------------------------------------- crawls

    def start_crawl(self, source: str, country: str, mode: str, started_at: str) -> int:
        cursor = self.db.execute(
            "INSERT INTO crawls (source, country, mode, started_at) VALUES (?, ?, ?, ?)",
            (source, country, mode, started_at),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def finish_crawl(self, crawl_id: int, **values: Any) -> None:
        names = sorted(values)
        self.db.execute(
            f"UPDATE crawls SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?",
            [values[n] for n in names] + [crawl_id],
        )
        self.db.commit()

    def last_crawl(
        self, source: str, country: str, *, status: str = "ok", modes: Sequence[str] | None = None
    ) -> sqlite3.Row | None:
        sql = "SELECT * FROM crawls WHERE source = ? AND country = ? AND status = ?"
        args: list[Any] = [source, country, status]
        if modes:
            sql += f" AND mode IN ({', '.join('?' for _ in modes)})"
            args.extend(modes)
        return self.db.execute(sql + " ORDER BY started_at DESC, id DESC LIMIT 1", args).fetchone()

    def has_data(self, source: str, country: str) -> bool:
        """True once a crawl of this source has stored anything for the country."""
        row = self.db.execute(
            "SELECT 1 FROM crawls WHERE source = ? AND country = ? AND status IN ('ok', 'partial') LIMIT 1",
            (source, country),
        ).fetchone()
        return row is not None

    def recent_crawls(self, country: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
        where, args = ("WHERE country = ?", [country]) if country else ("", [])
        return self.db.execute(
            f"SELECT * FROM crawls {where} ORDER BY started_at DESC, id DESC LIMIT ?", [*args, limit]
        ).fetchall()
