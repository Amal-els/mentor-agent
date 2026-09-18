"""L2 ingestion contract (AGENT.md §2 — raw payloads only, no interpretation,
no LLM calls). Every connector, fixture-backed or live, implements this."""

import datetime
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Window:
    start: datetime.datetime
    end: datetime.datetime


@dataclass(frozen=True)
class Healthy:
    pass


@dataclass(frozen=True)
class Unauthorized:
    pass


@dataclass(frozen=True)
class Error:
    stale_as_of: datetime.datetime


Health = Healthy | Unauthorized | Error


class SourceClient(Protocol):
    source: str

    def fetch(self, window: Window, owner_user_id: str) -> list[dict]: ...

    def health(self) -> Health: ...
