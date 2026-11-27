"""Turns short chat messages into actions and searches.

It understands requests such as "watch Sweden every 12 hours", "nurse jobs
in Berlin", "new remote jobs in Germany", "check Sweden now", "status" or
"stop watching Germany". No AI service is involved: it looks for command
words, country names and city names.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..countries import COUNTRY_NAMES, countries_in, country_name, find_country_names, normalize
from ..models import parse_iso, utc_now
from ..runner import availability
from ..store import Store
from . import settings as app_settings

DEFAULT_EVERY = 360  # minutes
MIN_EVERY = 30
RESULTS = 8

_COMMAND_VERBS = (
    "watch|watching|track|tracking|follow|following|monitor|add|check|refresh|update|fetch|run|scan|"
    "crawl|reload|unwatch|untrack|unfollow|remove|forget|delete|drop|sources|setup|in|for"
)
_STOPWORDS = set(
    """a an the and or of for in at on to from near around with by about find search show list give get
    me my us i im am want need looking look like any some all jobs job openings opening positions position
    roles role vacancies vacancy work career careers hiring hire employment please can you could would
    there is are what which new newest latest recent fresh today week this past last month posted open
    available now currently""".split()
)
_MODES = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "office": "onsite"}
# Two-letter words that are also country codes; never read them as countries.
_SMALL_WORDS = {"am", "as", "at", "be", "by", "do", "in", "is", "it", "me", "my", "no", "so", "to", "us"}


@dataclass
class Reply:
    text: str
    jobs: list[dict[str, Any]] = field(default_factory=list)
    total: int | None = None
    search: dict[str, Any] | None = None  # filters to open in the Jobs tab
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Chat:
    def __init__(self, bot, db_path: str, toml_path=None) -> None:
        self.bot = bot
        self.db_path = db_path
        self.toml_path = toml_path

    def reply(self, message: str) -> Reply:
        text = normalize(message or "")
        if not text:
            return self._help("Tell me what to do.")
        country, rest = self._country(message, text)
        has = lambda pattern: re.search(pattern, text) is not None  # noqa: E731

        if re.fullmatch(r"help|commands|h|what can you do|how does (it|this) work|what do you do", text):
            return self._help()
        if has(r"^(hi|hello|hey|hej|hallo|hola|good (morning|afternoon|evening))\b") and len(text.split()) <= 3:
            return self._help("Hi! I'm jobbot. I collect job openings and keep them up to date.")
        if has(r"\b(stop|cancel|abort) (the |this )?(current )?(check|run|crawl|download)\b"):
            return self._stop_current()
        if re.fullmatch(r"(pause|stop|sleep|halt)( checking| the bot| everything)?", text):
            self.bot.pause()
            return Reply("Paused. I won't run scheduled checks until you say “resume”.", suggestions=["Resume"])
        if re.fullmatch(r"(resume|start|continue|unpause|wake up)( checking| the bot)?", text):
            self.bot.resume()
            return Reply("Resumed. Scheduled checks are back on.", suggestions=["Status"])
        if country and has(r"\b(stop watching|unwatch|stop tracking|untrack|stop following|unfollow|remove|forget|delete|drop)\b"):
            return self._unwatch(country)
        if country and has(r"\b(watch|track|follow|monitor|subscribe|add|keep an eye on)\b"):
            return self._watch(country, text)
        if has(r"\b(check|refresh|update|fetch|run|scan|crawl|reload)\b") and not self._command_leftovers(rest) and "jobs" not in text.split():
            if country:
                return self._run_now([country])
            if has(r"\b(all|everything|now|everywhere)\b") or text in ("check", "refresh", "update", "fetch", "run"):
                return self._run_all()
        if has(r"\b(what|which) countr(y|ies)\b|\bwatch ?list\b|^list( countries)?$|\bwhat are you (watching|tracking)\b"):
            return self._watching()
        if has(r"\b(status|what are you doing|are you (running|working|busy|there|alive)|progress|how is it going|hows it going|whats happening)\b"):
            return self._status()
        if has(r"\b(how many|stats|statistics|numbers|summary|overview|count)\b"):
            return self._stats(country)
        if country and has(r"\b(sources?|what do you need|set ?up|coverage|which (sites|websites|boards)|api key)\b"):
            return self._sources(country)
        return self._search(country, rest, text)

    # ------------------------------------------------------------- parsing

    def _country(self, message: str, text: str) -> tuple[str | None, str]:
        """The country a message is about, and the message without its name."""
        named = find_country_names(message)
        if named:
            code, phrase = named[0]
            return code, re.sub(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", " ", text)
        # A bare code right after a command word or "in": "watch se", "jobs in DE".
        for match in re.finditer(rf"\b(?:{_COMMAND_VERBS})\s+([a-z]{{2}})\b", text):
            code = match.group(1)
            last = match.end(1) == len(text)
            if code.upper() in COUNTRY_NAMES and (code not in _SMALL_WORDS or (code == "us" and last)):
                return code.upper(), text[: match.start(1)] + text[match.end(1):]
        # A city or region: "nurse jobs in Berlin" -> Germany (the city stays a search word).
        implied = countries_in(message)
        if len(implied) == 1:
            return next(iter(implied)), text
        return None, text

    @staticmethod
    def _keywords(text: str) -> list[str]:
        words = [w for w in text.split() if w not in _STOPWORDS and w not in _MODES and len(w) > 1]
        return [w for w in words if not re.fullmatch(r"\d+|every|hours?|days?|minutes?|again", w)]

    def _command_leftovers(self, text: str) -> list[str]:
        """Words left after removing command verbs: "check se now" -> []."""
        filler = f"{_COMMAND_VERBS}|everything|everywhere|again|please|right|away"
        return [w for w in self._keywords(text) if not re.fullmatch(filler, w)]

    @staticmethod
    def _interval(text: str) -> int | None:
        if re.search(r"\b(hourly|every hour)\b", text):
            return 60
        if re.search(r"\b(daily|every day|once a day)\b", text):
            return 1440
        if re.search(r"\btwice a day\b", text):
            return 720
        if re.search(r"\b(weekly|every week)\b", text):
            return 10080
        match = re.search(r"\bevery (\d+(?:\.\d+)?) ?(minutes?|mins?|m|hours?|hrs?|h|days?|d)\b", text)
        if not match:
            return None
        amount, unit = float(match.group(1)), match.group(2)[0]
        return int(amount * {"m": 1, "h": 60, "d": 1440}[unit])

    # ------------------------------------------------------------- actions

    def _watch(self, country: str, text: str) -> Reply:
        every = self._interval(text)
        if every is not None:
            every = max(every, MIN_EVERY)
        name = country_name(country)
        with Store(self.db_path) as store:
            existing = store.get_watch(country)
            if existing and existing["enabled"] and every in (None, existing["every_minutes"]):
                when = f"last checked {_when(existing['last_run_at'])}" if existing["last_run_at"] else "not checked yet"
                return Reply(
                    f"I'm already watching {name} {_every(existing['every_minutes'])} ({when}, next check "
                    f"{_soon(existing['next_run_at'])}). Say “check {name} now” to check right away.",
                    suggestions=[f"Check {name} now", f"New jobs in {name}"],
                )
            watch = store.save_watch(country, every_minutes=every or (existing or {}).get("every_minutes") or DEFAULT_EVERY)
            ready, blocked = self._availability(store, country)
            had_data = bool(store.sources_with_data(country))
        how_often = _every(watch["every_minutes"])
        if existing:  # resumed, or a new schedule
            verb = "Resumed watching" if not existing["enabled"] else "Now checking"
            return Reply(f"{verb} {name} {how_often}.", suggestions=["Status"])
        if not ready:
            return Reply(
                f"I'll watch {name} {how_often}, but no source can fetch it yet. {setup_hint(blocked)}",
                suggestions=[f"Sources for {name}"],
            )
        self.bot.run_now(country)
        sources = ", ".join(a.source.label for a in ready)
        first = "" if had_data else (
            " The first check downloads everything, which can take a while for big countries; "
            "you can follow the progress at the top."
        )
        return Reply(
            f"Watching {name} {how_often} using {sources}. Checking it now.{first}",
            suggestions=["Status", f"New jobs in {name}"],
        )

    def _unwatch(self, country: str) -> Reply:
        name = country_name(country)
        with Store(self.db_path) as store:
            removed = store.delete_watch(country)
            kept = store.country_stats().get(country, {}).get("open", 0)
        if not removed:
            return Reply(f"I wasn't watching {name}.", suggestions=["What countries are you watching?"])
        tail = f" Its {kept:,} stored jobs stay searchable." if kept else ""
        return Reply(f"Stopped watching {name}.{tail}")

    def _run_now(self, countries: list[str]) -> Reply:
        names = []
        for country in countries:
            state = self.bot.run_now(country)
            names.append(f"{country_name(country)}" + (" (already running)" if state == "running" else ""))
        return Reply(f"Checking {', '.join(names)} now.", suggestions=["Status"])

    def _run_all(self) -> Reply:
        with Store(self.db_path) as store:
            countries = [w["country"] for w in store.list_watches() if w["enabled"]]
        if not countries:
            return Reply("I'm not watching any country yet. Try “watch Sweden”.", suggestions=["Watch Sweden"])
        return self._run_now(countries)

    def _stop_current(self) -> Reply:
        if self.bot.stop_current():
            return Reply("Stopping the current check. Jobs found so far are kept.")
        return Reply("Nothing is running right now.")

    def _status(self) -> Reply:
        status = self.bot.status()
        with Store(self.db_path) as store:
            watches = store.list_watches()
        current = status["current"]
        if current:
            name = country_name(current["country"])
            source = f" ({current['source']})" if current.get("source") else ""
            lines = [f"Checking {name}{source} right now: {current['fetched']:,} jobs read, {current['new']:,} new so far."]
        elif status["state"] == "paused":
            lines = ["Paused. Say “resume” to restart scheduled checks."]
        elif status["state"] == "stopped":
            lines = ["The bot thread has stopped. Restart the app."]
        elif status["next"]:
            lines = [f"Idle. Next check: {country_name(status['next']['country'])} {_soon(status['next']['at'])}."]
        else:
            lines = ["Idle. I'm not watching any country yet. Try “watch Sweden”."]
        if status["queue"]:
            lines.append("Queued: " + ", ".join(country_name(c) for c in status["queue"]) + ".")
        if watches:
            lines.append("Watching: " + ", ".join(
                f"{country_name(w['country'])} ({_every(w['every_minutes'])}{', paused' if not w['enabled'] else ''})"
                for w in watches) + ".")
        return Reply(" ".join(lines), suggestions=["How many jobs?"] if watches else ["Watch Sweden", "Help"])

    def _watching(self) -> Reply:
        with Store(self.db_path) as store:
            watches = store.list_watches()
        if not watches:
            return Reply("I'm not watching any country yet. Try “watch Sweden” or “watch Germany”.",
                         suggestions=["Watch Sweden", "Watch Germany"])
        lines = [f"{country_name(w['country'])}: {_every(w['every_minutes'])}"
                 + ("" if w["enabled"] else " (paused)")
                 + (f", last checked {_when(w['last_run_at'])}" if w["last_run_at"] else ", not checked yet")
                 for w in watches]
        return Reply("I'm watching " + "; ".join(lines) + ".")

    def _stats(self, country: str | None) -> Reply:
        with Store(self.db_path) as store:
            stats = store.country_stats()
        if country:
            item = stats.get(country)
            name = country_name(country)
            if not item:
                return Reply(f"I have no jobs for {name} yet.", suggestions=[f"Watch {name}"])
            return Reply(f"{name}: {item['open']:,} open jobs, {item['new_this_week']:,} new this week.",
                         suggestions=[f"New jobs in {name}"])
        if not stats:
            return Reply("I haven't collected any jobs yet. Try “watch Sweden”.", suggestions=["Watch Sweden"])
        lines = [f"{country_name(c)}: {s['open']:,} open, {s['new_this_week']:,} new this week" for c, s in
                 sorted(stats.items(), key=lambda kv: -kv[1]["open"])]
        return Reply("; ".join(lines) + ".")

    def _sources(self, country: str) -> Reply:
        with Store(self.db_path) as store:
            ready, blocked = self._availability(store, country)
        name = country_name(country)
        if not ready and not blocked:
            return Reply(f"No source covers {name}.")
        lines = [f"Sources for {name}:"]
        lines += [f"• {a.source.label}: ready ({a.source.coverage})" for a in ready]
        lines += [f"• {a.source.label}: {needs_text(a)}" for a in blocked if a.needs == "key"]
        boards = [a.source.label.replace(" boards", "") for a in blocked if a.needs == "companies"]
        if boards:
            lines.append(f"• Company career sites ({', '.join(boards)}): list companies in Settings")
        return Reply("\n".join(lines))

    def _search(self, country: str | None, rest: str, text: str) -> Reply:
        # Look for flags in the text without the country's name ("New Zealand" is not "new").
        words = self._keywords(rest)
        mode = next((_MODES[w] for w in rest.split() if w in _MODES), None)
        only_new = bool(re.search(r"\b(new|newest|latest|recent|fresh)\b", rest))
        days = 1 if re.search(r"\btoday\b", rest) else 7 if re.search(r"\b(this|past|last) week\b", rest) else (
            30 if re.search(r"\b(this|past|last) month\b", rest) else None)
        with Store(self.db_path) as store:
            if country is None:
                watched = [w["country"] for w in store.list_watches()]
                if len(watched) == 1:
                    country = watched[0]
            jobs, total = store.search_jobs(
                country=country, words=words, work_mode=mode, only_new=only_new, posted_within_days=days,
                sort="found" if only_new else "posted", limit=RESULTS,
            )
            has_data = bool(store.sources_with_data(country)) if country else bool(store.country_stats())
            watched_here = bool(country and store.get_watch(country))
        where = f" in {country_name(country)}" if country else ""
        kind = " ".join(filter(None, ["new" if only_new else "", mode or ""]))
        matching = f" matching “{' '.join(words)}”" if words else ""
        matching += {1: " posted today", 7: " posted this week", 30: " posted this month"}.get(days, "")
        search = {"country": country, "q": " ".join(words), "mode": mode, "new": only_new, "days": days}
        if total:
            noun = f"{kind + ' ' if kind else ''}job{'s' if total != 1 else ''}"
            text = f"I found {total:,} {noun}{matching}{where}. Newest first:"
            return Reply(text, jobs=jobs, total=total, search=search)
        if not has_data:
            name = country_name(country) if country else "any country"
            if watched_here:
                return Reply(f"I don't have jobs for {name} yet — the first check hasn't finished.",
                             suggestions=["Status"])
            hint = f" Say “watch {country_name(country)}” and I'll start collecting them." if country else (
                " Tell me a country to watch, like “watch Sweden”.")
            return Reply(f"I don't have jobs for {name} yet.{hint}",
                         suggestions=[f"Watch {country_name(country)}"] if country else ["Watch Sweden", "Help"])
        return Reply(f"No {kind + ' ' if kind else ''}jobs{matching}{where} right now. Try fewer or different words.",
                     total=0, search=search)

    def _availability(self, store: Store, country: str):
        settings = app_settings.load(store, self.toml_path)
        config, env = settings.runtime()
        items = availability(country, config, env)
        return [a for a in items if a.ready], [a for a in items if not a.ready]

    def _help(self, intro: str = "") -> Reply:
        lines = [
            "Things you can say:",
            "• “watch Sweden” or “watch Germany every 12 hours” — keep a country's jobs up to date",
            "• “nurse jobs in Berlin”, “remote python jobs”, “new jobs in Sweden” — search",
            "• “check Sweden now” — run a check right away",
            "• “status”, “how many jobs?”, “what countries are you watching?”",
            "• “stop watching Germany”, “pause”, “resume”",
            "• “sources for New Zealand” — what a country needs (API keys, company boards)",
        ]
        return Reply((intro + "\n" if intro else "") + "\n".join(lines),
                     suggestions=["Watch Sweden", "Status", "How many jobs?"])


def needs_text(item) -> str:
    if item.needs == "key":
        return f"needs a free API key ({' and '.join(item.missing)}): sign up at {item.source.signup}, then add it in Settings"
    if item.needs == "companies":
        return "list companies in Settings"
    return item.reason


def setup_hint(blocked) -> str:
    keyed = [a for a in blocked if a.needs == "key"]
    boards = [a for a in blocked if a.needs == "companies"]
    tips = [f"{a.source.label} {needs_text(a)}" for a in keyed]
    if boards:
        tips.append("or list company career sites in Settings")
    return ("To fix that: " + "; ".join(tips) + ".") if tips else ""


def _every(minutes: int) -> str:
    if minutes % 1440 == 0:
        days = minutes // 1440
        return "every day" if days == 1 else f"every {days} days"
    if minutes % 60 == 0:
        hours = minutes // 60
        return "every hour" if hours == 1 else f"every {hours} hours"
    return f"every {minutes} minutes"


def _soon(timestamp: str | None) -> str:
    """Like _when, for times that should be in the future."""
    text = _when(timestamp)
    return "soon" if text.endswith("ago") or text in ("now", "just now") else text


def _when(timestamp: str | None) -> str:
    if not timestamp:
        return "soon"
    minutes = (parse_iso(timestamp) - utc_now()).total_seconds() / 60
    future = minutes >= 0
    minutes = abs(minutes)
    if minutes < 1:
        return "now" if future else "just now"
    if minutes < 60:
        span = f"{int(minutes)} min"
    elif minutes < 1440:
        span = f"{int(minutes // 60)} h {int(minutes % 60)} min"
    else:
        span = f"{int(minutes // 1440)} d {int(minutes % 1440 // 60)} h"
    return f"in {span}" if future else f"{span} ago"
