"""Resolution result type (spec §4.1) and the matcher candidate type (spec §6).
This is a leaf module: matchers.py, resolve.py, and any L5/L6 consumer import
from here without importing resolve.py (the writer module)."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RawReference:
    source: str
    external_id: str | None
    handle: str | None
    email: str | None
    display_name: str | None


@dataclass(frozen=True)
class MatchCandidate:
    person_id: str
    tier: int
    score: float


@dataclass(frozen=True)
class Resolved:
    person_id: str
    tier: int
    confidence: Literal["verified", "inferred"]


@dataclass(frozen=True)
class Unconfirmed:
    reference_key: str
    top: MatchCandidate
    margin: float


@dataclass(frozen=True)
class Unattributed:
    reference_key: str
    raw_handle: str | None


Resolution = Resolved | Unconfirmed | Unattributed
