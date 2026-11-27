"""Settings you change in the app. They live in the database; on first start
they are copied from jobbot.toml if one exists.

API keys can be saved in the app or set as environment variables. A key saved
in the app wins over the environment.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config, load_config
from ..countries import resolve_country
from ..sources import SOURCES_BY_NAME
from ..store import Store

KEY_NAMES = ("ADZUNA_APP_ID", "ADZUNA_APP_KEY", "USAJOBS_API_KEY", "USAJOBS_EMAIL")
BOARDS = ("greenhouse", "lever", "ashby", "smartrecruiters")
SETTINGS_KEY = "app_settings"


@dataclass
class AppSettings:
    companies: dict[str, list[str]] = field(default_factory=dict)
    locations: dict[str, list[str]] = field(default_factory=dict)
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    keys: dict[str, str] = field(default_factory=dict)
    webhook_url: str = ""

    def runtime(self, environ: dict[str, str] | None = None) -> tuple[Config, dict[str, str]]:
        """What the crawler needs: a Config and the environment with keys."""
        env = dict(os.environ if environ is None else environ)
        env.update({k: v for k, v in self.keys.items() if v})
        config = Config(sources=self.sources, companies=self.companies, locations=self.locations)
        return config, env


def load(store: Store, toml_path: str | Path | None = None) -> AppSettings:
    data = store.get_setting(SETTINGS_KEY)
    if data is None:  # first start: take what jobbot.toml has
        settings = AppSettings()
        try:
            config = load_config(toml_path)
        except Exception:
            config = Config()
        settings.companies = {k: list(v) for k, v in config.companies.items()}
        settings.locations = {k: list(v) for k, v in config.locations.items()}
        settings.sources = {k: dict(v) for k, v in config.sources.items()}
        save(store, settings)
        return settings
    known = AppSettings.__dataclass_fields__
    return AppSettings(**{k: v for k, v in data.items() if k in known})


def save(store: Store, settings: AppSettings) -> None:
    store.set_setting(SETTINGS_KEY, asdict(settings))


def public(settings: AppSettings, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Settings for the browser, with API keys masked."""
    environ = os.environ if environ is None else environ
    keys = {}
    for name in KEY_NAMES:
        saved, from_env = settings.keys.get(name, ""), environ.get(name, "")
        value = saved or from_env
        keys[name] = {
            "set": bool(value),
            "where": "app" if saved else "environment" if from_env else None,
            "hint": (f"…{value[-4:]}" if len(value) > 8 else "set") if value else "",
        }
    return {
        "companies": {board: settings.companies.get(board, []) for board in BOARDS},
        "locations": settings.locations,
        "keys": keys,
        "webhook_url": settings.webhook_url,
        "adzuna_max_requests": settings.sources.get("adzuna", {}).get(
            "max_requests", SOURCES_BY_NAME["adzuna"].max_requests
        ),
    }


def update(settings: AppSettings, payload: dict[str, Any]) -> AppSettings:
    """Apply changes sent by the browser. Raises ValueError on bad input."""
    if payload.get("companies") is not None:
        companies = {}
        for board, names in payload["companies"].items():
            if board not in BOARDS:
                raise ValueError(f"unknown job board type: {board}")
            companies[board] = [str(n).strip() for n in names if str(n).strip()]
        settings.companies = companies
    if payload.get("locations") is not None:
        locations: dict[str, list[str]] = {}
        for key, words in payload["locations"].items():
            code = resolve_country(key)
            if code is None:
                raise ValueError(f"unknown country: {key}")
            cleaned = [str(w).strip() for w in words if str(w).strip()]
            if cleaned:
                locations[code] = cleaned
        settings.locations = locations
    for name, value in (payload.get("keys") or {}).items():
        if name not in KEY_NAMES:
            raise ValueError(f"unknown key: {name}")
        if value is None or not str(value).strip():
            settings.keys.pop(name, None)
        else:
            settings.keys[name] = str(value).strip()
    if payload.get("webhook_url") is not None:
        url = str(payload["webhook_url"]).strip()
        if url and not url.startswith(("https://", "http://")):
            raise ValueError("the webhook must be an http(s) URL")
        settings.webhook_url = url
    if payload.get("adzuna_max_requests") is not None:
        budget = int(payload["adzuna_max_requests"])
        if budget < 1:
            raise ValueError("the Adzuna request budget must be at least 1")
        settings.sources.setdefault("adzuna", {})["max_requests"] = budget
    return settings
