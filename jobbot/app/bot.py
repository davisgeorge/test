"""The always-on bot: a background thread that checks every watched country
on its schedule, runs checks you ask for, and reports what it is doing."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import requests

from .. import notify
from ..countries import country_name
from ..models import iso, utc_now
from ..runner import RunResult, make_http, run
from ..store import Store
from . import settings as app_settings

log = logging.getLogger("jobbot")


class Bot:
    def __init__(
        self,
        db_path: str | Path,
        *,
        toml_path: str | Path | None = None,
        tick: float = 15.0,
        http_factory: Callable = make_http,
        post: Callable = requests.post,
    ) -> None:
        self.db_path = str(db_path)
        self.toml_path = toml_path
        self.tick = tick
        self.http_factory = http_factory
        self.post = post
        self.started_at = iso(utc_now())
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._halt = threading.Event()
        self._stop_run = threading.Event()
        self._queue: list[str] = []
        self._current: dict[str, Any] | None = None
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- control

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._halt.clear()
        self._thread = threading.Thread(target=self._loop, name="jobbot", daemon=True)
        self._thread.start()
        with Store(self.db_path) as store:
            store.add_event("bot", "Bot started.")

    def shutdown(self, timeout: float = 15.0) -> None:
        self._halt.set()
        self._stop_run.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def run_now(self, country: str) -> str:
        """Ask for a check of ``country`` as soon as possible."""
        with self._lock:
            if self._current and self._current["country"] == country:
                return "running"
            if country in self._queue:
                return "queued"
            self._queue.append(country)
        self._wake.set()
        return "queued"

    def stop_current(self) -> bool:
        with self._lock:
            running = self._current is not None
        if running:
            self._stop_run.set()
        return running

    def pause(self) -> None:
        with Store(self.db_path) as store:
            store.set_setting("paused", True)
            store.add_event("bot", "Paused: no scheduled checks until resumed.")

    def resume(self) -> None:
        with Store(self.db_path) as store:
            store.set_setting("paused", False)
            store.add_event("bot", "Resumed scheduled checks.")
        self._wake.set()

    # ----------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        with Store(self.db_path) as store:
            paused = bool(store.get_setting("paused", False))
            upcoming = store.next_scheduled_watch()
        with self._lock:
            current = dict(self._current) if self._current else None
            queue = list(self._queue)
        alive = bool(self._thread and self._thread.is_alive())
        state = "running" if current else "stopped" if not alive else "paused" if paused else "idle"
        return {
            "state": state,
            "paused": paused,
            "current": current,
            "queue": queue,
            "next": (
                {"country": upcoming["country"], "at": upcoming["next_run_at"]}
                if upcoming and not paused
                else None
            ),
            "started_at": self.started_at,
        }

    # ------------------------------------------------------------------- loop

    def _loop(self) -> None:
        while not self._halt.is_set():
            try:
                country = self._next_country()
                if country:
                    self._check(country)
                    continue
            except Exception:  # never let the bot die
                log.exception("bot loop error")
            self._wake.wait(self.tick)
            self._wake.clear()

    def _next_country(self) -> str | None:
        with self._lock:
            if self._queue:
                return self._queue.pop(0)
        with Store(self.db_path) as store:
            if store.get_setting("paused", False):
                return None
            due = store.next_due_watch(iso(utc_now()))
            return due["country"] if due else None

    def _check(self, country: str) -> RunResult | None:
        self._stop_run.clear()
        with Store(self.db_path) as store:
            settings = app_settings.load(store, self.toml_path)
            config, env = settings.runtime()
            watch = store.get_watch(country)
            with self._lock:
                self._current = {
                    "country": country,
                    "started_at": iso(utc_now()),
                    "source": None,
                    "fetched": 0,
                    "new": 0,
                    "done": [],
                }
            name = country_name(country)
            log.info("checking %s", name)
            try:
                result = run(
                    country,
                    store,
                    config,
                    only=watch["sources"] if watch else None,
                    env=env,
                    http_factory=self.http_factory,
                    progress=self._progress,
                    should_stop=lambda: self._stop_run.is_set(),
                )
            except Exception as exc:
                log.exception("check of %s failed", name)
                store.add_event("error", f"Checking {name} failed: {exc}", country)
                self._finish(store, watch, country, "error")
                return None

            ran = result.ran
            if not ran:
                store.add_event("warning", f"Nothing can check {name} yet: add an API key or company "
                                "job boards in Settings.", country)
                self._finish(store, watch, country, "needs setup")
                return result

            jobs, new_count = notify.new_jobs_of_run(store, result)
            store.add_event("run", _describe(name, result, new_count), country)
            if new_count and settings.webhook_url:
                notify.notify_run(store, result, settings.webhook_url, post=self.post)
            worst = "error" if any(c.status == "error" for c in ran) else (
                "partial" if any(c.status == "partial" for c in ran) else "ok"
            )
            self._finish(store, watch, country, worst)
            return result

    def _finish(self, store: Store, watch: dict | None, country: str, status: str) -> None:
        now = utc_now()
        if watch is not None and store.get_watch(country) is not None:
            store.update_watch(
                country,
                last_run_at=iso(now),
                last_status=status,
                next_run_at=iso(now + timedelta(minutes=watch["every_minutes"])),
            )
        with self._lock:
            self._current = None

    def _progress(self, update: dict[str, Any]) -> None:
        with self._lock:
            if self._current is None:
                return
            self._current.update(source=update["source"], fetched=update["fetched"], new=update["new"],
                                 initial=update.get("initial", False))
            if update.get("done"):
                self._current["done"] = self._current["done"] + [
                    {"source": update["source"], "fetched": update["fetched"], "new": update["new"]}
                ]


def _describe(name: str, result: RunResult, new_count: int) -> str:
    parts = []
    for crawl in result.ran:
        text = f"{crawl.source} {crawl.fetched:,} seen"
        if crawl.closed:
            text += f", {crawl.closed:,} closed"
        if crawl.status != "ok":
            text += f" ({crawl.status}: {crawl.notes[-1] if crawl.notes else crawl.status})"
        parts.append(text)
    if all(c.initial for c in result.ran):
        stored = sum(c.new for c in result.ran)
        return f"First check of {name}: stored {stored:,} open jobs ({'; '.join(parts)})."
    news = f"{new_count:,} new job{'s' if new_count != 1 else ''}" if new_count else "no new jobs"
    return f"Checked {name}: {news} ({'; '.join(parts)})."

