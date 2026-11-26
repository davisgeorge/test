"""The German board only lets one query page through 10,000 results; check
that the crawl splits big searches into slices and still gets every job."""

import itertools

import pytest

from jobbot.http import HttpError
from jobbot.sources import arbeitsagentur as ba
from jobbot.sources import FetchContext

FIELDS = ["Verkauf", "Lager", "Pflege"]
STATES = ["Bayern", "Berlin", "Hessen"]


def make_jobs():
    jobs = []
    combos = itertools.product(FIELDS, ["false", "true"], ["1", "2", "3"], ["false", "true"], STATES)
    for n, (field, temp, contract, pav, state) in enumerate(combos):
        copies = 6 if field == "Verkauf" else 1  # one field is far bigger than the rest
        for k in range(copies):
            jobs.append({"id": f"J{n}-{k}", "berufsfeld": field, "zeitarbeit": temp,
                         "befristung": contract, "pav": pav, "wo": state})
    return jobs


class FakeBoard:
    def __init__(self, jobs, window):
        self.jobs = jobs
        self.window = window
        self.requests_made = 0

    def get_json(self, url, *, params=None, headers=None):
        self.requests_made += 1
        page, size = int(params["page"]), int(params["size"])
        if page * size > self.window + size - 1 and (page - 1) * size >= self.window:
            raise HttpError(400, url, "Ergebnismenge überschritten")
        filters = {k: str(v) for k, v in params.items() if k in ("berufsfeld", "zeitarbeit", "befristung", "pav", "wo")}
        if filters.get("wo") == "Deutschland":
            del filters["wo"]  # every fake job is in Germany
        hits = [j for j in self.jobs if all(j[k] == v for k, v in filters.items())]
        facets = {}
        for name in ("berufsfeld", "zeitarbeit", "befristung", "pav"):
            others = {k: v for k, v in filters.items() if k != name}  # facets ignore their own filter
            pool = [j for j in self.jobs if all(j[k] == v for k, v in others.items())]
            counts = {}
            for j in pool:
                counts[j[name]] = counts.get(j[name], 0) + 1
            facets[name] = {"counts": counts, "maxCount": len(pool)}
        start = (page - 1) * size
        items = [
            {"referenznummer": j["id"], "stellenangebotsTitel": f"Job {j['id']}", "firma": "Firma",
             "stellenlokationen": [{"adresse": {"ort": "Ort", "region": j["wo"].upper(), "land": "DEUTSCHLAND"}}]}
            for j in hits[start:start + size]
        ]
        return {"ergebnisliste": items, "maxErgebnisse": len(hits), "facetten": facets}


@pytest.fixture
def small_window(monkeypatch):
    monkeypatch.setattr(ba, "WINDOW", 20)
    monkeypatch.setattr(ba, "PAGE_SIZE", 5)
    monkeypatch.setattr(ba, "SPLITS", ba.SPLITS[:-1] + (("wo", tuple(STATES)),))
    monkeypatch.setattr(ba, "CIRCLE_KM", 0)


def test_split_reaches_every_job(small_window):
    jobs = make_jobs()
    board = FakeBoard(jobs, window=20)
    ctx = FetchContext(http=board)
    found = [job.source_id for job in ba.Arbeitsagentur().fetch("DE", ctx)]
    assert sorted(found) == sorted(j["id"] for j in jobs)
    assert len(found) == len(set(found))
    assert ctx.complete, ctx.notes


def test_slices_too_big_to_split_are_reported(small_window, monkeypatch):
    jobs = [{"id": f"X{i}", "berufsfeld": "Verkauf", "zeitarbeit": "false", "befristung": "2",
             "pav": "false", "wo": "Berlin"} for i in range(30)]
    board = FakeBoard(jobs, window=20)
    ctx = FetchContext(http=board)
    found = list(ba.Arbeitsagentur().fetch("DE", ctx))
    assert len(found) == 20
    assert not ctx.complete
    assert any("10 job listings sat in slices too big" in note for note in ctx.notes)
