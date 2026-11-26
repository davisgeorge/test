import pytest
import requests

from jobbot.http import BudgetExceeded, Http, HttpError
from jobbot.models import Job, clean_text, fingerprint, to_iso


def test_clean_text_handles_escaped_html():
    html = "&lt;p&gt;Build &lt;strong&gt;APIs&lt;/strong&gt;.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&amp;amp;Go&lt;/li&gt;&lt;/ul&gt;"
    assert clean_text(html) == "Build APIs.\nPython&Go"
    assert clean_text("  plain\n\n text ") == "plain\ntext"
    assert clean_text("") is None
    assert clean_text(None) is None


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-11-23T17:02:31-05:00", "2026-11-23T22:02:31Z"),
        ("2026-10-28T15:14:43.941Z", "2026-10-28T15:14:43Z"),
        ("2026-11-20T00:00:00.0000", "2026-11-20T00:00:00Z"),  # USAJOBS style
        ("2026-12-20T23:59:59.9970", "2026-12-20T23:59:59Z"),
        ("2026-11-26", "2026-11-26"),
        (1788199939703, "2026-08-31T18:12:19Z"),  # epoch milliseconds
        ("not a date", None),
        (None, None),
    ],
)
def test_to_iso(value, expected):
    assert to_iso(value) == expected


def test_fingerprint_ignores_noise():
    a = fingerprint("Koch (m/w/d)", "Gasthaus Krone GmbH", "Berlin, Berlin")
    b = fingerprint("Koch", "gasthaus krone", "Berlin")
    c = fingerprint("Koch", "Gasthaus Krone", "Hamburg")
    assert a == b
    assert a != c


def test_job_uid():
    job = Job(source="adzuna", source_id="42", country="NZ", title="Barista")
    assert job.uid == "adzuna:42"


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.url = "https://api.example.com/jobs"
        self.text = "error body" if status >= 400 else ""

    def json(self):
        return self._payload

    def close(self):
        pass


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_http(responses, **kwargs):
    sleeps = []
    session = FakeSession(responses)
    http = Http(session=session, sleep=sleeps.append, clock=lambda: 0.0, **kwargs)
    return http, session, sleeps


def test_retries_on_rate_limit_and_honours_retry_after():
    http, session, sleeps = make_http([FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200, {"ok": 1})])
    assert http.get_json("https://api.example.com/jobs") == {"ok": 1}
    assert session.calls == 2
    assert sleeps == [7.0]


def test_retries_connection_errors_with_backoff():
    http, session, sleeps = make_http(
        [requests.ConnectionError("boom"), FakeResponse(503), FakeResponse(200, [1])], backoff=1.0
    )
    assert http.get_json("https://api.example.com/jobs") == [1]
    assert sleeps == [1.0, 2.0]


def test_client_errors_are_not_retried():
    http, session, _ = make_http([FakeResponse(404)])
    with pytest.raises(HttpError) as excinfo:
        http.get_json("https://api.example.com/jobs")
    assert excinfo.value.status == 404
    assert session.calls == 1


def test_request_budget():
    http, _, _ = make_http([FakeResponse(200, {}), FakeResponse(200, {})], max_requests=1)
    http.get_json("https://api.example.com/jobs")
    with pytest.raises(BudgetExceeded):
        http.get_json("https://api.example.com/jobs")
    assert http.requests_made == 1
