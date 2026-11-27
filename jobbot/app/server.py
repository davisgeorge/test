"""The web app: a JSON API plus the single-page interface in ``static/``.

Start it with ``jobbot serve``. It also starts the always-on bot.
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import secrets
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__, notify
from ..countries import COUNTRY_NAMES, country_name, resolve_country
from ..export import FIELDS
from ..runner import availability
from ..sources import ALL_SOURCES, SOURCES_BY_NAME
from ..store import Store
from . import settings as app_settings
from .bot import Bot
from .chat import Chat, needs_text

STATIC = Path(__file__).parent / "static"


class WatchIn(BaseModel):
    country: str
    every_hours: float | None = Field(default=None, gt=0, le=24 * 30)
    sources: list[str] | None = None


class WatchPatch(BaseModel):
    enabled: bool | None = None
    every_hours: float | None = Field(default=None, gt=0, le=24 * 30)


class ChatIn(BaseModel):
    message: str = Field(max_length=500)


def create_app(
    db_path: str | Path = "jobbot.db",
    *,
    toml_path: str | Path | None = None,
    password: str | None = None,
    bot: Bot | None = None,
    start_bot: bool = True,
) -> FastAPI:
    db_path = str(db_path)
    bot = bot or Bot(db_path, toml_path=toml_path)
    chat = Chat(bot, db_path, toml_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with Store(db_path) as store:  # create or upgrade the database
            app_settings.load(store, toml_path)
        if start_bot:
            bot.start()
        yield
        if start_bot:
            bot.shutdown()

    app = FastAPI(title="jobbot", version=__version__, lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.state.bot = bot

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        """For Docker and uptime monitors; needs no password."""
        return {"ok": True, "bot": bot.status()["state"]}

    if password:
        @app.middleware("http")
        async def require_password(request: Request, call_next):
            if request.url.path != "/healthz" and not _password_ok(request.headers.get("authorization"), password):
                return Response("Password required", status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="jobbot", charset="UTF-8"'})
            return await call_next(request)

    def store() -> Store:
        return Store(db_path)

    def code_or_404(value: str) -> str:
        code = resolve_country(value)
        if code is None:
            raise HTTPException(404, f"unknown country: {value}")
        return code

    # ---------------------------------------------------------------- status

    @app.get("/api/status")
    def get_status() -> dict[str, Any]:
        return {**bot.status(), "version": __version__}

    @app.post("/api/bot/{action}")
    def control(action: str) -> dict[str, Any]:
        if action == "pause":
            bot.pause()
        elif action == "resume":
            bot.resume()
        elif action == "stop":
            bot.stop_current()
        else:
            raise HTTPException(404, f"unknown action: {action}")
        return bot.status()

    # ------------------------------------------------------------- countries

    @app.get("/api/countries")
    def countries() -> dict[str, Any]:
        with store() as s:
            watches = {w["country"]: w for w in s.list_watches()}
            stats = s.country_stats()
            settings = app_settings.load(s, toml_path)
        config, env = settings.runtime()
        everything = []
        for code, name in sorted(COUNTRY_NAMES.items(), key=lambda kv: kv[1]):
            national = [src.name for src in ALL_SOURCES if src.countries and code in src.countries and not src.env_vars]
            keyed = [src.name for src in ALL_SOURCES if src.countries and code in src.countries and src.env_vars]
            everything.append({"code": code, "name": name, "national": national, "keyed": keyed})
        watched = []
        for code, watch in watches.items():
            items = availability(code, config, env)
            watched.append({
                **watch,
                "name": country_name(code),
                "open": stats.get(code, {}).get("open", 0),
                "new_this_week": stats.get(code, {}).get("new_this_week", 0),
                "ready": [{"source": a.source.name, "label": a.source.label, "coverage": a.source.coverage}
                          for a in items if a.ready],
                "blocked": [{"source": a.source.name, "label": a.source.label, "needs": a.needs,
                             "reason": needs_text(a)} for a in items if not a.ready],
            })
        others = [
            {"code": code, "name": country_name(code), **item}
            for code, item in stats.items() if code not in watches
        ]
        return {"watched": watched, "with_data": others, "all": everything}

    @app.get("/api/countries/{country}/sources")
    def country_sources(country: str) -> list[dict[str, Any]]:
        code = code_or_404(country)
        with store() as s:
            config, env = app_settings.load(s, toml_path).runtime()
        return [
            {"source": a.source.name, "label": a.source.label, "coverage": a.source.coverage,
             "ready": a.ready, "needs": a.needs, "reason": needs_text(a) if not a.ready else ""}
            for a in availability(code, config, env)
        ]

    @app.post("/api/watches", status_code=201)
    def add_watch(body: WatchIn) -> dict[str, Any]:
        code = code_or_404(body.country)
        if body.sources:
            unknown = [s for s in body.sources if s not in SOURCES_BY_NAME]
            if unknown:
                raise HTTPException(400, f"unknown sources: {', '.join(unknown)}")
        every = max(30, round(body.every_hours * 60)) if body.every_hours else None
        with store() as s:
            watch = s.save_watch(code, every_minutes=every, sources=body.sources)
            if body.sources is not None:
                s.update_watch(code, sources=body.sources or None)
            s.add_event("watch", f"Started watching {country_name(code)}.", code)
        bot.run_now(code)
        return watch

    @app.patch("/api/watches/{country}")
    def change_watch(country: str, body: WatchPatch) -> dict[str, Any]:
        code = code_or_404(country)
        with store() as s:
            watch = s.get_watch(code)
            if watch is None:
                raise HTTPException(404, f"not watching {country_name(code)}")
            changes: dict[str, Any] = {}
            if body.enabled is not None:
                changes["enabled"] = body.enabled
            if body.every_hours is not None:
                changes["every_minutes"] = max(30, round(body.every_hours * 60))
            if changes:
                s.update_watch(code, **changes)
            return s.get_watch(code)

    @app.delete("/api/watches/{country}")
    def remove_watch(country: str) -> dict[str, Any]:
        code = code_or_404(country)
        with store() as s:
            if not s.delete_watch(code):
                raise HTTPException(404, f"not watching {country_name(code)}")
            s.add_event("watch", f"Stopped watching {country_name(code)}.", code)
        return {"ok": True}

    @app.post("/api/watches/{country}/run")
    def run_watch(country: str) -> dict[str, Any]:
        code = code_or_404(country)
        return {"state": bot.run_now(code)}

    # ------------------------------------------------------------------ jobs

    @app.get("/api/jobs")
    def jobs(
        country: str | None = None,
        q: str = "",
        source: str | None = None,
        mode: str | None = Query(default=None, pattern="^(remote|hybrid|onsite)$"),
        new: bool = False,
        days: int | None = Query(default=None, ge=1, le=365),
        sort: str = Query(default="posted", pattern="^(posted|found)$"),
        page: int = Query(default=1, ge=1),
        size: int = Query(default=30, ge=1, le=100),
    ) -> dict[str, Any]:
        code = code_or_404(country) if country else None
        with store() as s:
            found, total = s.search_jobs(
                country=code, words=q.split(), source=source, work_mode=mode, only_new=new,
                posted_within_days=days, sort=sort, limit=size, offset=(page - 1) * size,
            )
        return {"jobs": found, "total": total, "page": page, "size": size}

    @app.get("/api/jobs/{uid:path}")
    def job(uid: str) -> dict[str, Any]:
        with store() as s:
            found = s.get_job(uid)
        if found is None:
            raise HTTPException(404, "job not found")
        return found

    @app.get("/api/export")
    def export(country: str, format: str = Query(default="csv", pattern="^(csv|json)$")) -> StreamingResponse:
        code = code_or_404(country)
        filename = f"jobs-{code.lower()}.{format}"

        def rows() -> Iterator[str]:
            with store() as s:
                if format == "csv":
                    buffer = io.StringIO()
                    writer = csv.DictWriter(buffer, fieldnames=FIELDS, extrasaction="ignore")
                    writer.writeheader()
                    yield "﻿" + buffer.getvalue()
                    for row in s.iter_jobs(code):
                        buffer.seek(0)
                        buffer.truncate()
                        writer.writerow(row)
                        yield buffer.getvalue()
                else:
                    yield "["
                    for n, row in enumerate(s.iter_jobs(code)):
                        yield ("," if n else "") + "\n " + json.dumps({f: row.get(f) for f in FIELDS}, ensure_ascii=False)
                    yield "\n]\n"

        media = "text/csv; charset=utf-8" if format == "csv" else "application/json"
        return StreamingResponse(rows(), media_type=media,
                                 headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    # -------------------------------------------------------------- activity

    @app.get("/api/activity")
    def activity(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
        with store() as s:
            crawls = [dict(r) for r in s.recent_crawls(limit=limit)]
            events = s.recent_events(limit)
        return {"events": events, "crawls": crawls}

    # -------------------------------------------------------------- settings

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        with store() as s:
            current = app_settings.load(s, toml_path)
        data = app_settings.public(current)
        data["sources"] = [
            {"name": src.name, "label": src.label, "coverage": src.coverage,
             "countries": sorted(src.countries) if src.countries else None,
             "env_vars": list(src.env_vars), "signup": src.signup}
            for src in ALL_SOURCES
        ]
        return data

    @app.put("/api/settings")
    def put_settings(payload: dict[str, Any]) -> dict[str, Any]:
        with store() as s:
            current = app_settings.load(s, toml_path)
            try:
                app_settings.update(current, payload)
            except (ValueError, TypeError, AttributeError) as exc:
                raise HTTPException(400, str(exc)) from None
            app_settings.save(s, current)
            s.add_event("settings", "Settings changed.")
        return get_settings()

    @app.post("/api/settings/test-webhook")
    def test_webhook() -> dict[str, Any]:
        with store() as s:
            url = app_settings.load(s, toml_path).webhook_url
        if not url:
            raise HTTPException(400, "no webhook URL saved")
        sample = [{"title": "Test message from jobbot", "company": "jobbot", "location": "", "url": ""}]
        try:
            notify.send(url, "XX", "your webhook", sample, 1, post=bot.post)
        except Exception as exc:
            raise HTTPException(502, f"the webhook did not accept the message: {exc}") from None
        return {"ok": True}

    # ------------------------------------------------------------------ chat

    @app.post("/api/chat")
    def talk(body: ChatIn) -> dict[str, Any]:
        return chat.reply(body.message).to_dict()

    @app.exception_handler(ValueError)
    async def bad_value(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app


def _password_ok(header: str | None, password: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    _, _, given = decoded.partition(":")
    return secrets.compare_digest(given.encode(), password.encode())
