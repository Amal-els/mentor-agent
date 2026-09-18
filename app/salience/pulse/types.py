"""L5 output types. Leaf module — score.py, gate.py, assemble.py all import
from here without importing each other's internals."""

import datetime
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScoredItem:
    item_id: str
    item_type: str  # "event" | "work_item" | "message"
    score: float
    score_terms: dict
    candidate_focus: bool
    series_id: str | None = None


@dataclass(frozen=True)
class OwedItem:
    item_id: str
    description: str
    promised_to: str | None
    promised_at: datetime.datetime
    due_at: datetime.datetime | None
    overdue: bool


@dataclass(frozen=True)
class Removal:
    item_id: str
    reason: str


@dataclass(frozen=True)
class DayEventSummary:
    """The "Day" card section: every event on window_date, chronological —
    not just the ones that made the competitive shortlist. has_dossier is
    populated from DossierDelivery rows — set to True if a dossier was
    delivered for this event, enabling the morning pulse UI to flag events
    with pre-meeting dossiers. dossier_id carries the same fact as a real
    id (DossierDelivery.id) rather than just a bool, so a UI consumer
    (app/salience/checklist.py's build_checklist, folded into /webhooks/
    checklist's own "day" section) can link straight to that dossier
    without a second lookup/match step of its own. url is the calendar
    event's own link (Event.url) — None for a source that never carried
    one."""

    item_id: str
    title: str
    starts_at: datetime.datetime
    has_dossier: bool = False
    dossier_id: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class PreGateContext:
    owner_user_id: str
    owner_tz: str
    window_date: datetime.date
    window_reason: str  # "today" | "late_cutoff" | "today_exhausted"
    shortlist: list[ScoredItem]
    day_events: list[DayEventSummary]
    owed: list[OwedItem]
    degraded_sources: list[str]
    # {"event": float, "work_item": float, "message": float} — the same
    # get_effective_weight() values assemble_and_score already computed to
    # score this run's candidates, carried forward so app/delivery/cards.py
    # can surface a personalization note without a second DB read.
    weights: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class PulseContext:
    owner_user_id: str
    owner_tz: str
    window_date: datetime.date
    window_reason: str  # "today" | "late_cutoff" | "today_exhausted"
    trigger: str
    requested_at: datetime.datetime
    shortlist: list[ScoredItem]
    day_events: list[DayEventSummary]
    owed: list[OwedItem]
    degraded_sources: list[str]
    removals: list[Removal] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=dict)
