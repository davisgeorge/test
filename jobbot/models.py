"""The normalised job record every source produces, plus small parsing helpers."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone, tzinfo
from typing import Any


@dataclass
class Job:
    """One job opening, normalised across sources.

    ``source_id`` only needs to be unique within its source. ``posted_at`` and
    ``expires_at`` are ISO 8601 strings in UTC, or plain ``YYYY-MM-DD`` dates
    when the source only gives a date.
    """

    source: str
    source_id: str
    country: str  # ISO 3166-1 alpha-2, upper case
    title: str
    company: str | None = None
    location: str | None = None
    city: str | None = None
    region: str | None = None
    work_mode: str | None = None  # "onsite", "hybrid" or "remote"
    employment_type: str | None = None
    category: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None  # "hour", "day", "week", "month" or "year"
    salary_text: str | None = None
    description: str | None = None
    url: str | None = None
    posted_at: str | None = None
    expires_at: str | None = None

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.source_id}"

    def fingerprint(self) -> str:
        """Key used to spot the same opening listed by several sources."""
        return fingerprint(self.title, self.company, self.city or self.location)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Removed:
    """A source reported that one of its postings was taken down."""

    source_id: str


# --------------------------------------------------------------------------- text

_BLOCK_TAG = re.compile(r"(?i)<\s*/?\s*(?:br|p|div|li|ul|ol|tr|h[1-6])\b[^>]*>")
_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t\r\f\v ​]+")


def clean_text(value: Any) -> str | None:
    """Turn HTML (possibly entity-escaped) into readable plain text."""
    if value is None:
        return None
    text = html.unescape(str(value))
    if "<" in text:
        text = _BLOCK_TAG.sub("\n", text)
        text = _TAG.sub("", text)
        text = html.unescape(text)
    lines = (_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    text = "\n".join(line for line in lines if line)
    return text or None


def clean_line(value: Any) -> str | None:
    """Like :func:`clean_text` but always a single line."""
    text = clean_text(value)
    if not text:
        return None
    text = text.replace("\\*", "*").replace("\\_", "_")  # Markdown escapes some boards leave in
    return " ".join(text.split())


def first(*values: Any) -> Any:
    """Return the first value that is not None or empty."""
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(str(value).replace(",", ""))
    except ValueError:
        return None
    return number if number > 0 else None


# --------------------------------------------------------------------------- dates

_FRACTION = re.compile(r"(\.\d{6})\d+")


def to_iso(value: Any, assume_tz: tzinfo = timezone.utc) -> str | None:
    """Normalise a timestamp to ``YYYY-MM-DDTHH:MM:SSZ``.

    Accepts ISO 8601 strings, ``datetime``/``date`` objects and Unix epochs in
    seconds or milliseconds. Strings without a UTC offset are read in
    ``assume_tz``. Plain dates are returned as ``YYYY-MM-DD``.
    """
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    elif isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        return value.isoformat()
    else:
        text = str(value).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return text
        text = _FRACTION.sub(r"\1", text.replace("Z", "+00:00"))
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=assume_tz)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    """Bookkeeping timestamps (first seen, crawl start...) keep microseconds so
    two crawls in the same second still compare correctly."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# --------------------------------------------------------------------------- dedupe

# "(m/w/d)", "(f/m/x)", "(H/F)", "(all genders)" and friends add noise to titles.
_GENDER_TAG = re.compile(
    r"\(\s*(?:[mwfdxhi*]\s*[/|,]\s*)+[mwfdxhi*]\s*\)|\(\s*(?:all genders?|gn\*?|w\*m\*d)\s*\)",
    re.IGNORECASE,
)
_LEGAL_FORMS = {
    "ab", "ag", "as", "asa", "bv", "co", "company", "corp", "corporation", "gmbh", "group",
    "holding", "holdings", "inc", "incorporated", "kg", "llc", "llp", "lp", "ltd", "limited",
    "nv", "oy", "plc", "pty", "sa", "sas", "se", "spa", "srl", "the",
}


def _normalise(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = _GENDER_TAG.sub(" ", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def fingerprint(title: str | None, company: str | None, place: str | None) -> str:
    company_words = [w for w in _normalise(company).split() if w not in _LEGAL_FORMS]
    place_words = _normalise((place or "").split(",")[0]).split()
    key = "|".join([_normalise(title), " ".join(company_words), " ".join(place_words)])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
