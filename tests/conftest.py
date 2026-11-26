import copy
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeHttp:
    """Stands in for jobbot.http.Http and serves canned responses.

    A route matches when the URL starts with its prefix and, if given, every
    expected query parameter has the given value. The response can be data or
    a function ``(url, params) -> data``.
    """

    def __init__(self):
        self.routes = []
        self.calls = []
        self.requests_made = 0

    def add(self, url_prefix, response, *, params=None, method="GET"):
        self.routes.append((method, url_prefix, params or {}, response))
        return self

    def _answer(self, method, url, params, headers):
        self.requests_made += 1
        self.calls.append({"method": method, "url": url, "params": dict(params), "headers": dict(headers)})
        for route_method, prefix, expected, response in self.routes:
            if route_method != method or not url.startswith(prefix):
                continue
            if all(str(params.get(k)) == str(v) for k, v in expected.items()):
                result = response(url, params) if callable(response) else response
                if isinstance(result, Exception):
                    raise result
                return copy.deepcopy(result)
        raise AssertionError(f"unexpected request: {method} {url} {params}")

    def get_json(self, url, *, params=None, headers=None):
        return self._answer("GET", url, params or {}, headers or {})

    def post_json(self, url, payload, *, params=None, headers=None):
        return self._answer("POST", url, params or {}, headers or {})

    def iter_lines(self, url, *, params=None, headers=None):
        for record in self._answer("GET", url, params or {}, headers or {}):
            yield json.dumps(record).encode("utf-8")


@pytest.fixture
def http():
    return FakeHttp()
