"""All job sources the bot knows about."""

from __future__ import annotations

from .adzuna import Adzuna
from .arbeitsagentur import Arbeitsagentur
from .base import CompanyBoardSource, FetchContext, Source
from .boards import Ashby, Greenhouse, Lever, SmartRecruiters
from .jobtech import JobTech
from .usajobs import USAJobs

ALL_SOURCES: tuple[Source, ...] = (
    JobTech(),
    Arbeitsagentur(),
    Adzuna(),
    USAJobs(),
    Greenhouse(),
    Lever(),
    Ashby(),
    SmartRecruiters(),
)
SOURCES_BY_NAME = {source.name: source for source in ALL_SOURCES}

__all__ = ["ALL_SOURCES", "SOURCES_BY_NAME", "CompanyBoardSource", "FetchContext", "Source"]
