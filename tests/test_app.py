import base64
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from jobbot import runner
from jobbot.app import settings as app_settings
from jobbot.app.bot import Bot
from jobbot.app.chat import Chat
from jobbot.app.server import create_app
from jobbot.models import Job, Removed
from jobbot.sources import Source
from jobbot.store import Store


class FakeSource(Source):
    """Lists whatever the test puts in ``jobs`` for New Zealand."""

    name = "fake"
    label = "Fake board"
    coverage = "test data"
    countries = frozenset({"NZ"})
    incremental = False
    full_refresh_days = None

    def __init__(self):
        self.jobs = []
        self.calls = 0

    def fetch(self, country, ctx):
        self.calls += 1
        yield from self.jobs if hasattr(self.jobs, "__next__") else list(self.jobs)


def job(source_id, title="Barista", company="Flat White Co", location="Wellington", description=None, mode=None):
    return Job(source="fake", source_id=source_id, country="NZ", title=title, company=company,
               location=location, city=location, description=description, work_mode=mode)


@pytest.fixture
def fake(monkeypatch):
    source = FakeSource()
    monkeypatch.setattr(runner, "ALL_SOURCES", (source,))
    return source


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "app.db")


def http_factory(source, budget):
    return object()


# ------------------------------------------------------------------- the bot


def test_bot_checks_a_watched_country_and_reschedules(db, fake):
    fake.jobs = [job("1"), job("2", title="Chef")]
    bot = Bot(db, http_factory=http_factory)
    with Store(db) as store:
        store.save_watch("NZ", every_minutes=120)
    bot._check("NZ")
    with Store(db) as store:
        watch = store.get_watch("NZ")
        assert watch["last_status"] == "ok"
        assert watch["next_run_at"] > watch["last_run_at"]
        assert store.country_stats()["NZ"]["open"] == 2
        assert store.country_stats()["NZ"]["new_this_week"] == 0  # the first load is not "new"
        assert "First check of New Zealand" in store.recent_events()[0]["message"]

    fake.jobs = [job("1"), job("3", title="Waiter")]
    bot._check("NZ")
    with Store(db) as store:
        assert store.country_stats()["NZ"] == {"open": 2, "new_this_week": 1}
        assert "1 new job" in store.recent_events()[0]["message"]
        found, total = store.search_jobs(country="NZ", only_new=True)
        assert [j["title"] for j in found] == ["Waiter"] and found[0]["new"]


def test_bot_thread_runs_due_watches(db, fake):
    fake.jobs = [job("1")]
    with Store(db) as store:
        store.save_watch("NZ")
    bot = Bot(db, http_factory=http_factory, tick=0.05)
    bot.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            with Store(db) as store:
                if store.get_watch("NZ")["last_run_at"]:
                    break
            time.sleep(0.05)
        assert fake.calls == 1
        assert bot.status()["state"] in ("idle", "running")
        assert bot.status()["next"]["country"] == "NZ"
    finally:
        bot.shutdown()
    assert bot.status()["state"] == "stopped"


def test_run_now_queue_and_pause(db, fake):
    bot = Bot(db, http_factory=http_factory)
    assert bot.run_now("NZ") == "queued"
    assert bot.run_now("NZ") == "queued"
    assert bot.status()["queue"] == ["NZ"]
    bot.pause()
    assert bot.status()["paused"]
    with Store(db) as store:
        store.save_watch("SE")
    assert bot._next_country() == "NZ"  # asked for explicitly: runs even when paused
    assert bot._next_country() is None  # scheduled checks wait while paused
    bot.resume()
    assert bot._next_country() == "SE"


def test_stopping_a_check_keeps_what_was_found(db, fake):
    bot = Bot(db, http_factory=http_factory)

    def slow():
        yield job("1")
        bot.stop_current()
        yield job("2")

    fake.jobs = slow()
    with Store(db) as store:
        store.save_watch("NZ")
    bot._check("NZ")
    with Store(db) as store:
        assert store.get_watch("NZ")["last_status"] == "partial"
        assert store.country_stats()["NZ"]["open"] == 1


# ------------------------------------------------------------------ the chat


@pytest.fixture
def chat(db, fake):
    bot = Bot(db, http_factory=http_factory)
    return Chat(bot, db), bot


def say(chat, text):
    return chat[0].reply(text)


def test_chat_watch_and_unwatch(chat, db):
    reply = say(chat, "watch New Zealand every 12 hours")
    assert "Watching New Zealand every 12 hours using Fake board" in reply.text
    assert chat[1].status()["queue"] == ["NZ"]
    assert "already watching New Zealand" in say(chat, "watch nz").text
    assert "Now checking New Zealand every day" in say(chat, "track nz daily").text
    assert "Stopped watching New Zealand" in say(chat, "stop watching nz").text
    assert "wasn't watching" in say(chat, "unwatch NZ").text
    with Store(db) as store:
        assert store.list_watches() == []


def test_chat_explains_countries_it_cannot_fetch(chat):
    reply = say(chat, "watch Japan")
    assert "no source can fetch it yet" in reply.text


def test_chat_commands(chat, db):
    assert "Things you can say" in say(chat, "help").text
    assert say(chat, "check nz now").text == "Checking New Zealand now."
    assert "not watching any country" in say(chat, "refresh everything").text
    assert "Paused" in say(chat, "pause").text and chat[1].status()["paused"]
    assert "Resumed" in say(chat, "resume").text and not chat[1].status()["paused"]
    assert "Nothing is running" in say(chat, "cancel the current check").text
    assert "Sources for New Zealand" in say(chat, "sources for new zealand").text
    assert "Queued: New Zealand" in say(chat, "status").text


def test_chat_search(chat, db, fake):
    fake.jobs = [
        job("1", title="Senior Python Developer", location="Auckland", mode="remote"),
        job("2", title="Barista", location="Wellington"),
        job("3", title="Café Manager", location="Wellington", description="Run a busy café"),
    ]
    with Store(db) as store:
        store.save_watch("NZ")
    chat[1]._check("NZ")

    reply = say(chat, "python jobs in New Zealand")
    assert reply.total == 1 and reply.jobs[0]["title"] == "Senior Python Developer"
    assert reply.search == {"country": "NZ", "q": "python", "mode": None, "new": False, "days": None}
    # the only watched country is assumed, accents don't matter, descriptions count
    assert say(chat, "cafe").total == 1
    assert say(chat, "busy").total == 1
    assert say(chat, "remote jobs").total == 1
    assert say(chat, "jobs in Wellington").total == 2  # a city implies its country
    assert "No jobs matching “plumber” in New Zealand" in say(chat, "plumber").text
    assert say(chat, "jobs posted today in New Zealand").text.startswith("I found 3 jobs posted today in New Zealand")
    assert say(chat, "remote python jobs").text.startswith("I found 1 remote job matching “python”")
    assert "I don't have jobs for Sweden yet" in say(chat, "jobs in Sweden").text
    assert "New Zealand: 3 open jobs" in say(chat, "how many jobs in nz?").text


def test_chat_does_not_read_small_words_as_countries(chat):
    assert chat[0]._country("show us remote jobs", "show us remote jobs")[0] is None
    assert chat[0]._country("jobs in it", "jobs in it")[0] is None
    assert chat[0]._country("watch se", "watch se")[0] == "SE"
    assert chat[0]._country("nurse jobs in DE", "nurse jobs in de")[0] == "DE"


# ------------------------------------------------------------------- the API


@pytest.fixture
def client(db, fake):
    fake.jobs = [job("1", title="Barista"), job("2", title="Chef", description="Cook things.")]
    app = create_app(db, start_bot=False, bot=Bot(db, http_factory=http_factory))
    with TestClient(app) as test_client:
        yield test_client, app.state.bot


def test_api_watch_flow(client):
    c, bot = client
    assert c.post("/api/watches", json={"country": "new zealand", "every_hours": 3}).status_code == 201
    assert bot.status()["queue"] == ["NZ"]
    bot._check("NZ")
    countries = c.get("/api/countries").json()
    nz = countries["watched"][0]
    assert (nz["country"], nz["open"], nz["every_minutes"]) == ("NZ", 2, 180)
    assert nz["ready"][0]["label"] == "Fake board"
    assert any(c_["code"] == "SE" and c_["national"] == ["jobtech"] for c_ in countries["all"])

    assert c.patch("/api/watches/nz", json={"enabled": False}).json()["enabled"] is False
    assert c.post("/api/watches/NZ/run").json() == {"state": "queued"}
    assert c.delete("/api/watches/NZ").json() == {"ok": True}
    assert c.delete("/api/watches/NZ").status_code == 404
    assert c.post("/api/watches", json={"country": "Narnia"}).status_code == 404


def test_api_jobs_and_export(client):
    c, bot = client
    c.post("/api/watches", json={"country": "NZ"})
    bot._check("NZ")
    data = c.get("/api/jobs", params={"country": "NZ", "q": "cook"}).json()
    assert data["total"] == 1 and data["jobs"][0]["snippet"] == "Cook things."
    detail = c.get(f"/api/jobs/{data['jobs'][0]['uid']}").json()
    assert detail["description"] == "Cook things."
    assert c.get("/api/jobs/fake:nope").status_code == 404
    assert c.get("/api/jobs", params={"mode": "sideways"}).status_code == 422
    csv_text = c.get("/api/export", params={"country": "NZ"}).text
    assert csv_text.startswith("﻿country,title") and "Barista" in csv_text
    assert len(c.get("/api/export", params={"country": "NZ", "format": "json"}).json()) == 2
    activity = c.get("/api/activity").json()
    assert activity["crawls"][0]["source"] == "fake" and activity["events"]


def test_api_settings(client, monkeypatch):
    c, _ = client
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    data = c.put("/api/settings", json={
        "keys": {"ADZUNA_APP_ID": "abcd1234efgh"},
        "companies": {"greenhouse": ["stripe", " ", "cloudflare"]},
        "locations": {"New Zealand": ["Hamilton"]},
        "webhook_url": "https://hooks.slack.com/services/x",
        "adzuna_max_requests": 100,
    }).json()
    assert data["keys"]["ADZUNA_APP_ID"] == {"set": True, "where": "app", "hint": "…efgh"}
    assert data["companies"]["greenhouse"] == ["stripe", "cloudflare"]
    assert data["locations"] == {"NZ": ["Hamilton"]}
    assert data["adzuna_max_requests"] == 100
    assert "abcd1234efgh" not in c.get("/api/settings").text  # keys never go back to the browser
    assert c.put("/api/settings", json={"companies": {"workday": ["x"]}}).status_code == 400
    assert c.put("/api/settings", json={"webhook_url": "ftp://x"}).status_code == 400
    removed = c.put("/api/settings", json={"keys": {"ADZUNA_APP_ID": None}}).json()
    assert removed["keys"]["ADZUNA_APP_ID"]["set"] is False


def test_api_chat_and_status(client):
    c, _ = client
    assert c.post("/api/chat", json={"message": "watch nz"}).json()["text"].startswith("Watching New Zealand")
    status = c.get("/api/status").json()
    assert status["queue"] == ["NZ"] and status["version"]
    assert c.post("/api/bot/pause").json()["paused"] is True
    assert c.post("/api/bot/explode").status_code == 404
    assert c.get("/").status_code == 200 and "jobbot" in c.get("/").text


def test_password(db, fake):
    app = create_app(db, start_bot=False, password="s3cret")
    with TestClient(app) as c:
        assert c.get("/api/status").status_code == 401
        assert c.get("/").status_code == 401
        good = base64.b64encode(b"me:s3cret").decode()
        bad = base64.b64encode(b"me:nope").decode()
        assert c.get("/api/status", headers={"Authorization": f"Basic {bad}"}).status_code == 401
        assert c.get("/api/status", headers={"Authorization": f"Basic {good}"}).status_code == 200


# ----------------------------------------------------------------- storage


def test_old_databases_are_upgraded(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as old:
        old.execute("""CREATE TABLE jobs (uid TEXT PRIMARY KEY, source TEXT, source_id TEXT, country TEXT,
            title TEXT, company TEXT, location TEXT, city TEXT, region TEXT, work_mode TEXT, employment_type TEXT,
            category TEXT, salary_min REAL, salary_max REAL, salary_currency TEXT, salary_period TEXT,
            salary_text TEXT, description TEXT, url TEXT, posted_at TEXT, expires_at TEXT,
            fingerprint TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, closed_at TEXT)""")
        old.execute("""INSERT INTO jobs (uid, source, source_id, country, title, company, fingerprint, first_seen, last_seen)
            VALUES ('a:1', 'a', '1', 'SE', 'Säljare', 'Örebro AB', 'f', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""")
    with Store(path) as store:
        found, total = store.search_jobs(country="SE", words=["orebro"])
        assert total == 1 and not found[0]["new"]


def test_settings_seeded_from_toml(tmp_path, monkeypatch):
    toml = tmp_path / "jobbot.toml"
    toml.write_text('[companies]\nashby = ["openai=OpenAI"]\n')
    with Store(tmp_path / "s.db") as store:
        loaded = app_settings.load(store, toml)
        assert loaded.companies == {"ashby": ["openai=OpenAI"]}
        loaded.keys["ADZUNA_APP_KEY"] = "from-app"
        monkeypatch.setenv("ADZUNA_APP_KEY", "from-env")
        config, env = loaded.runtime()
        assert env["ADZUNA_APP_KEY"] == "from-app" and config.companies["ashby"] == ["openai=OpenAI"]


def test_removed_jobs_leave_search(db, fake):
    with Store(db) as store:
        store.upsert(job("1"), "2026-11-01T00:00:00.000000Z")
        store.commit()
        assert store.search_jobs(country="NZ")[1] == 1
        store.close_job("fake:1", "2026-11-02T00:00:00.000000Z")
        assert store.search_jobs(country="NZ")[1] == 0
    assert Removed("1") == Removed("1")
