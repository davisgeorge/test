from datetime import datetime, timedelta, timezone

import pytest

from jobbot.http import HttpError
from jobbot.models import Removed
from jobbot.sources import SOURCES_BY_NAME, FetchContext
from jobbot.sources.adzuna import API_URL as ADZUNA_URL
from jobbot.sources.arbeitsagentur import API_URL as BA_URL
from jobbot.sources.jobtech import SNAPSHOT_URL, STREAM_URL
from jobbot.sources.usajobs import API_URL as USAJOBS_URL

from .conftest import load

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)


def ctx(http, **kwargs):
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("env", {})
    return FetchContext(http=http, **kwargs)


def fetch(name, country, context):
    return list(SOURCES_BY_NAME[name].fetch(country, context))


# --------------------------------------------------------------------- Sweden


def test_jobtech_snapshot(http):
    http.add(SNAPSHOT_URL, load("jobtech_snapshot"))
    context = ctx(http)
    jobs = fetch("jobtech", "SE", context)

    assert [j.source_id for j in jobs] == ["30000001", "30000003"]  # the Oslo job is skipped
    first = jobs[0]
    assert first.title == "Undersköterska till hemtjänsten"
    assert first.company == "Exempelkommunen"
    assert first.location == "Uppsala, Uppsala län"
    assert first.work_mode == "onsite"
    assert first.employment_type.endswith("Heltid")
    assert first.salary_text == "Månadslön enligt avtal"
    assert first.posted_at == "2026-11-20T07:15:00Z"  # Swedish local time -> UTC
    assert first.url.endswith("/30000001")
    second = jobs[1]
    assert second.title == "Systemutvecklare & arkitekt"
    assert second.company == "Kodbolaget"
    assert second.city == "Solna"
    assert second.work_mode == "remote"
    assert second.posted_at == "2026-07-01T10:00:00Z"  # summer time
    assert http.calls[0]["headers"]["Accept"] == "application/jsonl"
    assert not context.incremental
    assert "outside Sweden" in context.notes[0]


def test_jobtech_stream_reports_removals(http):
    http.add(STREAM_URL, load("jobtech_stream"), params={"updated-after": "2026-11-25T12:00:00"})
    context = ctx(http, since=NOW - timedelta(days=1))
    items = fetch("jobtech", "SE", context)
    assert items[0].title.endswith("(uppdaterad)")
    assert items[1] == Removed("30000003")
    assert context.incremental


# -------------------------------------------------------------------- Germany


def test_arbeitsagentur_maps_jobs_and_skips_abroad(http):
    http.add(BA_URL, load("arbeitsagentur"), params={"angebotsart": 1, "page": 1})
    context = ctx(http)
    jobs = fetch("arbeitsagentur", "DE", context)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.source_id == "10000-1111111111-S"
    assert job.title == "Fachkraft Lagerlogistik (m/w/d)"
    assert job.company == "Beispiel Logistik GmbH"
    assert job.location == "Bremen, Bremen"
    assert job.employment_type == "job, permanent"
    assert (job.salary_min, job.salary_max, job.salary_currency, job.salary_period) == (15.5, 18.0, "EUR", "hour")
    assert job.posted_at == "2026-11-24"
    assert job.url == "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-1111111111-S"
    assert http.calls[0]["headers"] == {"X-API-Key": "jobboerse-jobsuche"}
    assert context.complete
    assert "1 located abroad" in context.notes[0]


def test_arbeitsagentur_incremental_uses_days_since(http):
    http.add(BA_URL, load("arbeitsagentur"), params={"veroeffentlichtseit": 2})
    context = ctx(http, since=NOW - timedelta(days=1, hours=2))
    fetch("arbeitsagentur", "DE", context)
    assert context.incremental


# --------------------------------------------------------------------- Adzuna


def test_adzuna_pages_until_count(http):
    env = {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}
    http.add(ADZUNA_URL.format(country="nz", page=1), load("adzuna_page1"))
    http.add(ADZUNA_URL.format(country="nz", page=2), load("adzuna_page2"))
    context = ctx(http, env=env)
    jobs = fetch("adzuna", "NZ", context)

    assert [j.source_id for j in jobs] == ["4400000001", "4400000002", "4400000003"]
    dev, barista, farm = jobs
    assert dev.title == "Software Developer"
    assert dev.company == "Kiwi Software Ltd"
    assert (dev.city, dev.region) == ("Auckland CBD", "Auckland")
    assert (dev.salary_min, dev.salary_max, dev.salary_currency, dev.salary_period) == (90000, 110000, "NZD", "year")
    assert dev.employment_type == "full-time, permanent"
    assert barista.salary_min is None  # Adzuna's estimate, not an advertised salary
    assert farm.city == "Hamilton"
    params = http.calls[0]["params"]
    assert params["app_id"] == "id" and params["results_per_page"] == 50 and params["sort_by"] == "date"
    assert "max_days_old" not in params


def test_adzuna_stops_cleanly_when_deep_pages_are_refused(http):
    env = {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}
    page1 = load("adzuna_page1")
    page1["count"] = 500
    http.add(ADZUNA_URL.format(country="nz", page=1), page1, params={"max_days_old": 1})
    http.add(ADZUNA_URL.format(country="nz", page=2), HttpError(400, "https://api.adzuna.com"))
    context = ctx(http, env=env, since=NOW - timedelta(hours=6))
    jobs = fetch("adzuna", "NZ", context)
    assert len(jobs) == 2
    assert not context.complete


# -------------------------------------------------------------------- USAJOBS


def test_usajobs(http):
    env = {"USAJOBS_API_KEY": "secret", "USAJOBS_EMAIL": "me@example.com"}
    http.add(USAJOBS_URL, load("usajobs"), params={"Page": 1, "ResultsPerPage": 500})
    jobs = fetch("usajobs", "US", ctx(http, env=env))

    assert len(jobs) == 1  # the overseas-only announcement is skipped
    job = jobs[0]
    assert job.title == "Registered Nurse"
    assert job.company == "Veterans Health Administration — Department of Veterans Affairs"
    assert (job.salary_min, job.salary_max, job.salary_period) == (78000, 120000, "year")
    assert job.description == "Provide patient care."
    assert job.posted_at == "2026-11-20T00:00:00Z"
    assert http.calls[0]["headers"] == {"Authorization-Key": "secret", "User-Agent": "me@example.com"}


# ------------------------------------------------------------- company boards


def test_greenhouse_filters_by_country_and_uses_offices(http):
    http.add("https://boards-api.greenhouse.io/v1/boards/exampleco/jobs", load("greenhouse"))
    jobs = fetch("greenhouse", "US", ctx(http, companies=["exampleco"]))

    assert [j.title for j in jobs] == ["Backend Engineer", "Account Executive"]
    remote, hybrid = jobs
    assert remote.work_mode == "remote"
    assert remote.description == "Build APIs.\nPython"
    assert remote.source_id == "exampleco:1001"
    assert hybrid.location == "Austin, TX"  # "Hybrid" alone says nothing; the office does
    assert hybrid.city == "Austin"
    assert hybrid.work_mode == "hybrid"


def test_lever_uses_country_field_and_locations(http):
    http.add("https://api.lever.co/v0/postings/exampleco", load("lever"))
    us = fetch("lever", "US", ctx(http, companies=["exampleco=Example Co"]))
    assert [j.title for j in us] == ["Customer Success Manager", "Solutions Architect"]
    assert us[0].company == "Example Co"
    assert (us[0].salary_min, us[0].salary_currency, us[0].salary_period) == (85000, "USD", "year")
    assert us[0].work_mode == "hybrid"

    ca = fetch("lever", "CA", ctx(http, companies=["exampleco"]))
    assert [j.title for j in ca] == ["Field Engineer", "Solutions Architect"]
    assert ca[0].company == "Exampleco"


def test_lever_eu_host(http):
    http.add("https://api.eu.lever.co/v0/postings/euco", [])
    assert fetch("lever", "DE", ctx(http, companies=["eu:euco"])) == []


def test_ashby_matches_secondary_locations(http):
    http.add("https://api.ashbyhq.com/posting-api/job-board/exampleco", load("ashby"))
    de = fetch("ashby", "DE", ctx(http, companies=["exampleco"]))
    assert [j.title for j in de] == ["Engineering Manager - EU"]  # unlisted role skipped
    job = de[0]
    assert (job.salary_min, job.salary_max, job.salary_currency, job.salary_period) == (150000, 250000, "EUR", "year")
    assert job.employment_type == "full-time"
    assert job.work_mode == "remote"

    us = fetch("ashby", "US", ctx(http, companies=["exampleco"]))
    assert [(j.title, j.city, j.region, j.work_mode) for j in us] == [
        ("Designer", "San Francisco", "California", "onsite")
    ]


def test_smartrecruiters_pages_with_country_filter(http):
    url = "https://api.smartrecruiters.com/v1/companies/ExampleCo/postings"
    http.add(url, load("smartrecruiters_page1"), params={"country": "de", "offset": 0})
    http.add(url, load("smartrecruiters_page2"), params={"country": "de", "offset": 2})
    jobs = fetch("smartrecruiters", "DE", ctx(http, companies=["ExampleCo"]))

    assert [j.title for j in jobs] == ["Senior / Lead Engineering Manager", "Werkstudent (m/w/d) Einkauf", "Plant Manager"]
    assert jobs[0].url == "https://jobs.smartrecruiters.com/ExampleCo/744000000000001-senior-lead-engineering-manager"
    assert [j.work_mode for j in jobs] == ["hybrid", "remote", "onsite"]
    assert jobs[0].company == "ExampleCo GmbH"


def test_board_errors_skip_that_company_only(http):
    http.add("https://boards-api.greenhouse.io/v1/boards/gone/jobs", HttpError(404, "https://boards-api.greenhouse.io"))
    http.add("https://boards-api.greenhouse.io/v1/boards/exampleco/jobs", load("greenhouse"))
    context = ctx(http, companies=["gone", "exampleco"])
    jobs = fetch("greenhouse", "US", context)
    assert len(jobs) == 2
    assert not context.complete
    assert "gone" in context.notes[0]


@pytest.mark.parametrize("name", sorted(SOURCES_BY_NAME))
def test_every_source_describes_itself(name):
    source = SOURCES_BY_NAME[name]
    assert source.name == name and source.label and source.coverage
    assert source.countries is None or all(len(c) == 2 and c.isupper() for c in source.countries)
