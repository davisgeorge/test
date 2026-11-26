from datetime import timedelta

import pytest

from jobbot import runner
from jobbot.config import Config
from jobbot.models import Job, Removed, iso, utc_now
from jobbot.sources import Source
from jobbot.store import Store


def job(source, source_id, title="Barista", company="Cafe", city="Auckland", country="NZ"):
    return Job(source=source, source_id=source_id, country=country, title=title, company=company, city=city)


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_upsert_tracks_new_and_reopens(store):
    assert store.upsert(job("a", "1"), "2026-11-01T00:00:00Z") is True
    assert store.upsert(job("a", "1", title="Head Barista"), "2026-11-02T00:00:00Z") is False
    assert store.close_job("a:1", "2026-11-03T00:00:00Z")
    assert store.count_jobs("NZ") == 0
    store.upsert(job("a", "1"), "2026-11-04T00:00:00Z")  # listed again
    rows = list(store.iter_jobs("NZ"))
    assert len(rows) == 1
    assert rows[0]["first_seen"] == "2026-11-01T00:00:00Z"
    assert rows[0]["last_seen"] == "2026-11-04T00:00:00Z"
    assert rows[0]["closed_at"] is None


def test_dedupe_across_sources_only(store):
    store.upsert(job("a", "1"), "2026-11-01T00:00:00Z")
    store.upsert(job("b", "9"), "2026-11-02T00:00:00Z")  # same opening on another source
    store.upsert(job("a", "2"), "2026-11-02T00:00:00Z")  # look-alike posting on the same source
    store.upsert(job("b", "10", title="Chef"), "2026-11-02T00:00:00Z")
    rows = list(store.iter_jobs("NZ"))
    assert sorted(r["uid"] for r in rows) == ["a:1", "a:2", "b:10"]
    assert {r["uid"]: r["sources"] for r in rows}["a:1"] == "a,b"
    assert store.count_jobs("NZ", dedupe=True) == 3
    assert len(list(store.iter_jobs("NZ", dedupe=False))) == 4


# ------------------------------------------------------------------- runner


class ScriptedSource(Source):
    """A source whose output each test scripts."""

    name = "scripted"
    label = "Scripted"
    coverage = "test data"
    countries = frozenset({"NZ"})
    incremental = True
    full_refresh_days = 7

    def __init__(self):
        self.batches = []
        self.seen_since = []

    def fetch(self, country, ctx):
        self.seen_since.append(ctx.since)
        if ctx.since is not None:
            ctx.incremental = True
        items, complete = self.batches.pop(0)
        for item in items:
            if isinstance(item, Exception):
                raise item
            yield item
        if not complete:
            ctx.incomplete("ran out of budget")


@pytest.fixture
def scripted(monkeypatch):
    source = ScriptedSource()
    monkeypatch.setattr(runner, "ALL_SOURCES", (source,))
    return source


def run(store, **kwargs):
    return runner.run("NZ", store, Config(), env={}, http_factory=lambda s, b: object(), **kwargs)


def test_full_then_incremental_then_closing(store, scripted):
    scripted.batches = [
        ([job("scripted", "1"), job("scripted", "2", title="Chef")], True),  # first run: full
        ([job("scripted", "3", title="Waiter")], True),  # second run: only changes
        ([job("scripted", "1"), job("scripted", "3", title="Waiter")], True),  # forced full
    ]
    first = run(store)
    assert first.crawls[0].mode == "full" and first.crawls[0].new == 2 and first.crawls[0].initial

    second = run(store)
    assert scripted.seen_since[1] is not None
    assert second.crawls[0].mode == "incremental"
    assert second.crawls[0].closed == 0  # an incremental crawl never closes jobs
    assert second.returning_sources == ["scripted"]
    assert store.count_jobs("NZ") == 3

    third = run(store, full=True)
    assert scripted.seen_since[2] is None
    assert third.crawls[0].closed == 1  # job 2 is gone from the complete listing
    assert sorted(r["source_id"] for r in store.iter_jobs("NZ")) == ["1", "3"]


def test_partial_crawls_do_not_close_jobs(store, scripted):
    scripted.batches = [
        ([job("scripted", "1"), job("scripted", "2", title="Chef")], True),
        ([job("scripted", "1")], False),  # budget ran out before job 2
    ]
    run(store)
    result = run(store, full=True)
    assert result.crawls[0].status == "partial"
    assert result.crawls[0].closed == 0
    assert store.count_jobs("NZ") == 2


def test_errors_are_recorded_and_keep_fetched_jobs(store, scripted):
    scripted.batches = [([job("scripted", "1"), RuntimeError("API down")], True)]
    result = run(store)
    crawl = result.crawls[0]
    assert crawl.status == "error" and crawl.fetched == 1
    assert "API down" in crawl.notes[-1]
    assert store.count_jobs("NZ") == 1
    # a failed crawl is not a baseline: the next run is a full crawl again
    scripted.batches = [([job("scripted", "1")], True)]
    run(store)
    assert scripted.seen_since[-1] is None


def test_removals_close_jobs(store, scripted):
    scripted.batches = [([job("scripted", "1"), job("scripted", "2", title="Chef")], True),
                        ([Removed("2")], True)]
    run(store)
    result = run(store)
    assert result.crawls[0].closed == 1
    assert [r["source_id"] for r in store.iter_jobs("NZ")] == ["1"]


def test_since_days_runs_are_not_baselines(store, scripted):
    scripted.batches = [([job("scripted", "1")], True), ([job("scripted", "1")], True)]
    result = run(store, since_days=2)
    assert result.crawls[0].mode == "recent"
    assert scripted.seen_since[0] is not None
    run(store)
    assert scripted.seen_since[1] is None  # still needs its first full crawl


def test_full_refresh_after_a_week(store, scripted):
    scripted.batches = [([job("scripted", "1")], True), ([job("scripted", "1")], True)]
    run(store)
    old = iso(utc_now() - timedelta(days=8))
    store.db.execute("UPDATE crawls SET started_at = ?", (old,))
    run(store)
    assert scripted.seen_since[1] is None


def test_skipped_sources_explain_why(store, monkeypatch):
    from jobbot.sources import SOURCES_BY_NAME

    monkeypatch.setattr(runner, "ALL_SOURCES", (SOURCES_BY_NAME["adzuna"], SOURCES_BY_NAME["greenhouse"]))
    result = run(store)
    assert [c.status for c in result.crawls] == ["skipped", "skipped"]
    assert "ADZUNA_APP_ID" in result.crawls[0].notes[0]
    assert "[companies] greenhouse" in result.crawls[1].notes[0]
