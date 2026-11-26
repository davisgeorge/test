import csv
import json

import pytest

from jobbot import cli, notify, runner
from jobbot.export import write_jobs
from jobbot.models import Job
from jobbot.sources import Source


class TinySource(Source):
    name = "tiny"
    label = "Tiny"
    coverage = "two jobs"
    countries = frozenset({"NZ"})

    def fetch(self, country, ctx):
        yield Job(source="tiny", source_id="1", country="NZ", title="Barista", company="Cafe",
                  location="Wellington", url="https://example.com/1", description="Coffee.\nTea.")
        yield Job(source="tiny", source_id="2", country="NZ", title="Chef", company="Bistro", location="Auckland")


@pytest.fixture
def tiny(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "ALL_SOURCES", (TinySource(),))
    monkeypatch.chdir(tmp_path)  # no stray jobbot.toml


def test_fetch_then_export(tmp_path, tiny, capsys):
    db = tmp_path / "jobs.db"
    out = tmp_path / "nz.csv"
    assert cli.main(["fetch", "--country", "New Zealand", "--db", str(db), "--out", str(out), "-q"]) == 0
    printed = capsys.readouterr().out
    assert "New Zealand (NZ)" in printed
    assert "Open jobs stored for New Zealand: 2" in printed
    rows = list(csv.DictReader(out.open(encoding="utf-8-sig")))
    assert {r["title"] for r in rows} == {"Barista", "Chef"}

    jsonl = tmp_path / "nz.jsonl"
    assert cli.main(["export", "-c", "nz", "--db", str(db), "-o", str(jsonl), "--no-description"]) == 0
    records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2 and "description" not in records[0]

    assert cli.main(["stats", "--db", str(db)]) == 0
    assert "unique open jobs" in capsys.readouterr().out


def test_fetch_without_sources_explains_what_to_do(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("ADZUNA_APP_ID", "ADZUNA_APP_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["fetch", "--country", "nz", "--db", str(tmp_path / "x.db")]) == 2
    err = capsys.readouterr().err
    assert "no job source is ready for New Zealand" in err
    assert "ADZUNA_APP_ID" in err


def test_unknown_country(capsys):
    assert cli.main(["sources", "--country", "Swedn"]) == 2
    assert "did you mean Sweden (SE)" in capsys.readouterr().err


def test_sources_listing(capsys):
    assert cli.main(["sources"]) == 0
    out = capsys.readouterr().out
    for name in ("jobtech", "arbeitsagentur", "adzuna", "usajobs", "greenhouse", "lever", "ashby", "smartrecruiters"):
        assert name in out


def test_config_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "jobbot.toml").write_text('[companies]\ngreenhouse = ["acme"]\n[locations]\nNZ = ["Hamilton"]\n')
    assert cli.main(["sources", "--country", "nz"]) == 0
    out = capsys.readouterr().out
    assert "greenhouse" in out and "ready" in out


@pytest.mark.parametrize("text, seconds", [("30m", 1800), ("6h", 21600), ("1d", 86400), ("90", 90)])
def test_parse_interval(text, seconds):
    assert cli.parse_interval(text) == seconds


def test_parse_interval_rejects_nonsense():
    with pytest.raises(cli.UsageError):
        cli.parse_interval("soon")
    with pytest.raises(cli.UsageError):
        cli.parse_interval("5s")


def test_json_export_is_valid(tmp_path):
    rows = [{"title": "A", "country": "NZ"}, {"title": "B", "country": "NZ"}]
    path = tmp_path / "out.json"
    assert write_jobs(rows, path) == 2
    assert [r["title"] for r in json.loads(path.read_text())] == ["A", "B"]
    empty = tmp_path / "empty.json"
    write_jobs([], empty)
    assert json.loads(empty.read_text()) == []


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, url, json, timeout):
        self.calls.append((url, json))

        class Response:
            def raise_for_status(self):
                pass

        return Response()


JOBS = [{"title": f"Job {i}", "company": "Co", "location": "Auckland", "url": f"https://example.com/{i}"}
        for i in range(200)]


def test_webhook_payloads():
    post = Recorder()
    notify.send("https://hooks.slack.com/services/T/B/X", "NZ", "New Zealand", JOBS[:3], 3, post=post)
    notify.send("https://discord.com/api/webhooks/1/abc", "NZ", "New Zealand", JOBS, 200, post=post)
    notify.send("https://example.com/hook", "NZ", "New Zealand", JOBS[:2], 2, post=post)
    slack, discord, generic = (payload for _, payload in post.calls)
    assert slack == {"text": slack["text"]} and slack["text"].startswith("jobbot: 3 new job openings in New Zealand")
    assert len(discord["content"]) <= 2000 and discord["content"].endswith("more")
    assert generic["new_jobs"] == 2 and generic["jobs"][0]["title"] == "Job 0"


def test_watch_runs_until_stopped(tmp_path, tiny, monkeypatch, capsys):
    runs = []
    real_fetch = cli.cmd_fetch

    def counting_fetch(args):
        runs.append(1)
        return real_fetch(args)

    def stop(seconds):
        assert seconds == 1800
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_fetch", counting_fetch)
    monkeypatch.setattr(cli.time, "sleep", stop)
    assert cli.main(["watch", "-c", "nz", "--every", "30m", "--db", str(tmp_path / "w.db"), "-q"]) == 130
    assert runs == [1]
