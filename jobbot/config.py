"""Optional settings file (``jobbot.toml``). API keys come from environment
variables, never from this file."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .countries import resolve_country

DEFAULT_PATH = Path("jobbot.toml")


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    # [sources.<name>] tables, e.g. {"adzuna": {"max_requests": 200}}
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    # [companies]: company job boards to read, per board type
    companies: dict[str, list[str]] = field(default_factory=dict)
    # [locations]: extra words meaning "in this country", keyed by ISO code
    locations: dict[str, list[str]] = field(default_factory=dict)
    path: Path | None = None

    def source_options(self, name: str) -> dict[str, Any]:
        return dict(self.sources.get(name, {}))


def load_config(path: str | Path | None = None) -> Config:
    """Read ``path``, or ``./jobbot.toml`` if it exists, or return defaults."""
    if path is None:
        if not DEFAULT_PATH.exists():
            return Config()
        path = DEFAULT_PATH
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None

    sources = data.get("sources", {})
    if not isinstance(sources, dict) or not all(isinstance(v, dict) for v in sources.values()):
        raise ConfigError(f"{path}: [sources.<name>] entries must be tables")

    companies: dict[str, list[str]] = {}
    for board, names in data.get("companies", {}).items():
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ConfigError(f"{path}: companies.{board} must be a list of strings")
        companies[board] = [n.strip() for n in names if n.strip()]

    locations: dict[str, list[str]] = {}
    for key, words in data.get("locations", {}).items():
        code = resolve_country(key)
        if code is None:
            raise ConfigError(f"{path}: unknown country in [locations]: {key!r}")
        if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
            raise ConfigError(f"{path}: locations.{key} must be a list of strings")
        locations.setdefault(code, []).extend(words)

    return Config(sources=sources, companies=companies, locations=locations, path=path)
