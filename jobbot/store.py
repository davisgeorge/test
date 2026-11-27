"""SQLite storage: every job the bot has seen, and a log of every crawl.

A job stays "active" (``closed_at IS NULL``) until a source reports it removed,
or a complete full crawl of that source no longer lists it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import fields
from datetime import timedelta
from pathlib import Path
from typing import Any

from .models import Job, iso, parse_iso, utc_now

JOB_FIELDS = [f.name for f in fields(Job)]
_UPDATE_FIELDS = [name for name in JOB_FIELDS if name not in ("source", "source_id")]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS jobs (
    uid TEXT PRIMARY KEY,
    {", ".join(f"{name} {'REAL' if name.startswith('salary_m') else 'TEXT'}" for name in JOB_FIELDS)},
    fingerprint TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    closed_at TEXT,
    search_text TEXT,  -- title, company, place and category, lower-cased without accents
    in_initial_load INTEGER NOT NULL DEFAULT 0  -- stored by a source's very first crawl
);

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

CREATE TABLE IF NOT EXISTS watches (
    country TEXT PRIMARY KEY,
    every_minutes INTEGER NOT NULL DEFAULT 360,
    sources TEXT,  -- JSON list of source names, NULL for every ready source
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    last_status TEXT,
    next_run_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    country TEXT,
    message TEXT NOT NULL
);
"""

# Created after migrations, since they use columns old databases lack.
INDEXES = """
CREATE INDEX IF NOT EXISTS jobs_by_country ON jobs (country, closed_at);
CREATE INDEX IF NOT EXISTS jobs_by_source ON jobs (source, country, last_seen);
CREATE INDEX IF NOT EXISTS jobs_by_fingerprint ON jobs (fingerprint);
CREATE INDEX IF NOT EXISTS jobs_by_date ON jobs (country, COALESCE(posted_at, first_seen));
CREATE INDEX IF NOT EXISTS jobs_by_found ON jobs (country, first_seen);
CREATE INDEX IF NOT EXISTS jobs_new ON jobs (in_initial_load, first_seen);
"""

_initialised: set[str] = set()
_init_lock = threading.Lock()


def fold(text: str | None) -> str:
    """Lower-case and strip accents, so "Malmo" finds "Malmö" and "strasse" finds "Straße"."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()


def _search_text(*parts: str | None) -> str:
    return fold(" ".join(p for p in parts if p))

_ORDER = "ORDER BY COALESCE(posted_at, first_seen) DESC, uid"


class Store:
    """One connection to the database. Open one per thread."""

    def __init__(self, path: str | Path = "jobbot.db") -> None:
        self.path = str(path)
        self.db = sqlite3.connect(self.path, timeout=60)
        self.db.row_factory = sqlite3.Row
        self.db.create_function("jobbot_fold", 1, fold, deterministic=True)
        self.db.execute("PRAGMA synchronous=NORMAL")
        with _init_lock:
            if self.path == ":memory:" or self.path not in _initialised:
                self._initialise()
                _initialised.add(self.path)

    def _initialise(self) -> None:
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(jobs)")}
        if "search_text" not in columns:  # database from an older jobbot
            self.db.execute("ALTER TABLE jobs ADD COLUMN search_text TEXT")
        if "in_initial_load" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN in_initial_load INTEGER NOT NULL DEFAULT 1")
        self.db.execute(
            "UPDATE jobs SET search_text = jobbot_fold(title || ' ' || COALESCE(company, '') || ' ' "
            "|| COALESCE(location, '') || ' ' || COALESCE(category, '')) WHERE search_text IS NULL"
        )
        self.db.executescript(INDEXES)
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def commit(self) -> None:
        self.db.commit()

    # ------------------------------------------------------------------ jobs

    def upsert(self, job: Job, now: str, *, initial: bool = False) -> bool:
        """Insert or refresh a job. Returns True if it was not stored before.

        ``initial`` marks jobs stored by a source's first crawl for a country,
        so the app does not call a whole backlog "new".
        """
        row = job.to_dict()
        row.update(
            uid=job.uid,
            fingerprint=job.fingerprint(),
            now=now,
            search_text=_search_text(job.title, job.company, job.location, job.category),
            initial=1 if initial else 0,
        )
        assignments = ", ".join(f"{name} = :{name}" for name in _UPDATE_FIELDS)
        updated = self.db.execute(
            f"UPDATE jobs SET {assignments}, fingerprint = :fingerprint, search_text = :search_text, "
            "last_seen = :now, closed_at = NULL WHERE uid = :uid",
            row,
        ).rowcount
        if updated:
            return False
        names = ["uid", *JOB_FIELDS, "fingerprint", "search_text"]
        self.db.execute(
            f"INSERT INTO jobs ({', '.join(names)}, first_seen, last_seen, in_initial_load) "
            f"VALUES ({', '.join(':' + name for name in names)}, :now, :now, :initial)",
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

    # --------------------------------------------------------- app: searching

    def search_jobs(
        self,
        *,
        country: str | None = None,
        words: Sequence[str] = (),
        source: str | None = None,
        work_mode: str | None = None,
        only_new: bool = False,
        posted_within_days: int | None = None,
        sort: str = "posted",
        limit: int = 50,
        offset: int = 0,
        dedupe: bool = True,
    ) -> tuple[list[dict[str, Any]], int]:
        """Open jobs matching every word (in title, company, place, category or
        description). Returns one page of results and the total count."""
        where, args = ["closed_at IS NULL"], []
        if country:
            where.append("country = ?")
            args.append(country)
        if source:
            where.append("source = ?")
            args.append(source)
        if work_mode:
            where.append("work_mode = ?")
            args.append(work_mode)
        if only_new:
            where.append("in_initial_load = 0")
        if posted_within_days:
            since = iso(utc_now() - timedelta(days=posted_within_days))[:10]
            where.append("COALESCE(posted_at, first_seen) >= ?")
            args.append(since)
        for word in words:
            folded = fold(word).replace("%", "").replace("_", "")
            if folded:
                where.append("(search_text LIKE ? OR description LIKE ?)")
                args += [f"%{folded}%", f"%{word}%"]
        condition = " AND ".join(where)
        order = "first_seen DESC, uid" if sort == "found" else "COALESCE(posted_at, first_seen) DESC, uid"

        # Merging duplicates only matters when several sources cover the country.
        if dedupe and country and len(self.sources_with_data(country)) > 1:
            base = f"""
                SELECT * FROM (
                    SELECT jobs.*,
                           FIRST_VALUE(source) OVER (PARTITION BY fingerprint ORDER BY first_seen, uid) AS keep_source,
                           GROUP_CONCAT(source, ',') OVER (PARTITION BY fingerprint) AS all_sources
                    FROM jobs WHERE {condition}
                ) WHERE source = keep_source"""
        else:
            base = f"SELECT jobs.*, source AS all_sources FROM jobs WHERE {condition}"
        total = self.db.execute(f"SELECT COUNT(*) FROM ({base})", args).fetchone()[0]
        rows = self.db.execute(
            f"SELECT * FROM ({base}) ORDER BY {order} LIMIT ? OFFSET ?", [*args, int(limit), int(offset)]
        ).fetchall()
        return [self._job_row(row) for row in rows], total

    def get_job(self, uid: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT jobs.*, source AS all_sources FROM jobs WHERE uid = ?", (uid,)).fetchone()
        if row is None:
            return None
        job = self._job_row(row, keep_description=True)
        others = self.db.execute(
            "SELECT source, url FROM jobs WHERE fingerprint = ? AND uid != ? AND closed_at IS NULL",
            (row["fingerprint"], uid),
        ).fetchall()
        job["also_on"] = [dict(r) for r in others]
        return job

    @staticmethod
    def _job_row(row: sqlite3.Row, keep_description: bool = False) -> dict[str, Any]:
        item = dict(row)
        item["sources"] = ",".join(dict.fromkeys((item.pop("all_sources", None) or item["source"]).split(",")))
        item["new"] = not item.pop("in_initial_load", 0)
        for key in ("keep_source", "search_text", "fingerprint"):
            item.pop(key, None)
        description = item.pop("description", None) or ""
        if keep_description:
            item["description"] = description
        else:
            item["snippet"] = " ".join(description.split())[:240]
        return item

    def sources_with_data(self, country: str) -> list[str]:
        rows = self.db.execute(
            "SELECT DISTINCT source FROM crawls WHERE country = ? AND status IN ('ok', 'partial')", (country,)
        ).fetchall()
        return [r["source"] for r in rows]

    def country_stats(self) -> dict[str, dict[str, Any]]:
        """Open and new job counts per country."""
        week_ago = iso(utc_now() - timedelta(days=7))
        stats: dict[str, dict[str, Any]] = {}
        for row in self.db.execute("SELECT country, COUNT(*) AS open FROM jobs WHERE closed_at IS NULL GROUP BY country"):
            stats[row["country"]] = {"open": row["open"], "new_this_week": 0}
        for row in self.db.execute(
            "SELECT country, COUNT(*) AS new FROM jobs WHERE in_initial_load = 0 AND first_seen >= ? "
            "AND closed_at IS NULL GROUP BY country",
            (week_ago,),
        ):
            if row["country"] in stats:
                stats[row["country"]]["new_this_week"] = row["new"]
        return stats

    # ----------------------------------------------------------- app: watches

    def list_watches(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM watches ORDER BY created_at").fetchall()
        return [self._watch_row(r) for r in rows]

    def get_watch(self, country: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM watches WHERE country = ?", (country,)).fetchone()
        return self._watch_row(row) if row else None

    def save_watch(
        self, country: str, *, every_minutes: int | None = None, sources: Sequence[str] | None = None
    ) -> dict[str, Any]:
        """Start (or resume) watching a country. Its first check is due now."""
        now = iso(utc_now())
        existing = self.get_watch(country)
        if existing is None:
            self.db.execute(
                "INSERT INTO watches (country, every_minutes, sources, enabled, created_at, next_run_at) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (country, every_minutes or 360, json.dumps(list(sources)) if sources else None, now, now),
            )
        else:
            self.update_watch(
                country,
                enabled=True,
                every_minutes=every_minutes or existing["every_minutes"],
                next_run_at=existing["next_run_at"] or now,
            )
        self.db.commit()
        return self.get_watch(country)

    def update_watch(self, country: str, **values: Any) -> None:
        allowed = {"every_minutes", "enabled", "last_run_at", "last_status", "next_run_at", "sources"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"cannot update {', '.join(sorted(unknown))}")
        if "sources" in values and values["sources"] is not None:
            values["sources"] = json.dumps(list(values["sources"]))
        if "enabled" in values:
            values["enabled"] = 1 if values["enabled"] else 0
        names = sorted(values)
        self.db.execute(
            f"UPDATE watches SET {', '.join(f'{n} = ?' for n in names)} WHERE country = ?",
            [values[n] for n in names] + [country],
        )
        self.db.commit()

    def delete_watch(self, country: str) -> bool:
        deleted = self.db.execute("DELETE FROM watches WHERE country = ?", (country,)).rowcount
        self.db.commit()
        return deleted > 0

    def next_due_watch(self, now: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM watches WHERE enabled = 1 AND (next_run_at IS NULL OR next_run_at <= ?) "
            "ORDER BY next_run_at LIMIT 1",
            (now,),
        ).fetchone()
        return self._watch_row(row) if row else None

    def next_scheduled_watch(self) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM watches WHERE enabled = 1 ORDER BY next_run_at IS NOT NULL, next_run_at LIMIT 1"
        ).fetchone()
        return self._watch_row(row) if row else None

    @staticmethod
    def _watch_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["sources"] = json.loads(item["sources"]) if item["sources"] else None
        return item

    # ---------------------------------------------------- app: settings, log

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        self.db.commit()

    def add_event(self, kind: str, message: str, country: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO events (at, kind, country, message) VALUES (?, ?, ?, ?)",
            (iso(utc_now()), kind, country, message),
        )
        self.db.commit()

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
