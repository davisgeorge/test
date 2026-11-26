"""A small HTTP client: retries with backoff, a minimum delay between calls,
and an optional cap on how many requests one source may make in a run."""

from __future__ import annotations

import email.utils
import logging
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import requests

from . import __version__

log = logging.getLogger("jobbot")

USER_AGENT = f"jobbot/{__version__} (job openings collector; python-requests)"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_WAIT = 300.0


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = "") -> None:
        detail = " ".join(body.split())[:300]
        super().__init__(f"HTTP {status} from {url}" + (f": {detail}" if detail else ""))
        self.status = status
        self.url = url
        self.body = body


class BudgetExceeded(RuntimeError):
    """The source used up the requests it was allowed for this run."""


class Http:
    def __init__(
        self,
        *,
        min_interval: float = 0.0,
        max_requests: int | None = None,
        timeout: float = 60.0,
        retries: int = 4,
        backoff: float = 2.0,
        user_agent: str = USER_AGENT,
        session: requests.Session | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
    ) -> None:
        self.min_interval = min_interval
        self.max_requests = max_requests
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.user_agent = user_agent
        self.session = session or requests.Session()
        self.requests_made = 0
        self._sleep = sleep
        self._clock = clock
        self._last_call: float | None = None

    # ------------------------------------------------------------------ public

    def get_json(self, url: str, *, params=None, headers=None) -> Any:
        return self._json(self.request("GET", url, params=params, headers=headers))

    def post_json(self, url: str, payload: Any, *, params=None, headers=None) -> Any:
        return self._json(self.request("POST", url, params=params, headers=headers, json_body=payload))

    def iter_lines(self, url: str, *, params=None, headers=None) -> Iterator[bytes]:
        """Stream a response line by line (for JSON Lines feeds)."""
        response = self.request("GET", url, params=params, headers=headers, stream=True)
        try:
            for line in response.iter_lines(chunk_size=1 << 16):
                if line and line.strip():
                    yield line
        finally:
            response.close()

    def request(
        self,
        method: str,
        url: str,
        *,
        params=None,
        headers=None,
        json_body=None,
        stream: bool = False,
    ) -> requests.Response:
        all_headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        all_headers.update(headers or {})
        attempt = 0
        while True:
            if self.max_requests is not None and self.requests_made >= self.max_requests:
                raise BudgetExceeded(f"used all {self.max_requests} requests allowed for this run")
            self._throttle()
            self.requests_made += 1
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    headers=all_headers,
                    json=json_body,
                    timeout=self.timeout,
                    stream=stream,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= self.retries:
                    raise
                wait = self.backoff * 2**attempt
                log.debug("%s %s failed (%s); retrying in %.0fs", method, url, exc, wait)
            else:
                if response.status_code < 400:
                    return response
                if response.status_code not in RETRY_STATUSES or attempt >= self.retries:
                    body = response.text if not stream else ""
                    response.close()
                    raise HttpError(response.status_code, response.url, body)
                wait = _retry_after(response) or self.backoff * 2**attempt
                log.debug("%s %s returned %s; retrying in %.0fs", method, url, response.status_code, wait)
                response.close()
            attempt += 1
            self._sleep(min(wait, MAX_WAIT))

    # ----------------------------------------------------------------- helpers

    def _throttle(self) -> None:
        if self.min_interval and self._last_call is not None:
            wait = self._last_call + self.min_interval - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

    @staticmethod
    def _json(response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise HttpError(response.status_code, response.url, "response was not JSON") from exc


def _retry_after(response: requests.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
