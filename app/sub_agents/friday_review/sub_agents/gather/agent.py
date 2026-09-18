"""Layer 6's gather step for F4: pure aggregation over tables F1/F2/F3
already populate — no connector, no LLM call (AGENT.md §2). Almost every
query here is a direct OwnerScope-scoped SQL read; the one exception is
AgendaItem, which goes through PairScope (agenda's own scoping rule) via
resolve_pair_scope(session, owner, owner) — the owner viewing their own
agenda, the same self-access case app.sub_agents.dossier.sub_agents.
gather.agent already uses for recurring-1:1 carryover."""

import datetime
from collections import Counter
from dataclasses import dataclass, field

from app.agenda.models import Accomplishment, AgendaItem
from app.agenda.scope import resolve_pair_scope
from app.agenda.store import get_agenda
from app.core.clock import Clock
from app.core.models import Commitment, Goal, PulseDelivery, WorkItem
from app.core.scope import OwnerScope
from app.identity.friday_batch import FridayBatchItem, build_batch

EVIDENCE_WINDOW_DAYS = 7
THREE_WEEK_PATTERN_DAYS = 21
SKILL_DISTRIBUTION_WINDOW_DAYS = 56  # trailing 8 weeks
STUCK_SURFACED_COUNT_THRESHOLD = 3
DAILY_PULSE_PATTERN_MIN_DISTINCT_DAYS = 3

# Case-insensitive containment check against real Jira/Linear/GitHub status
# text (app/ingest/live_source.py passes each source's raw status string
# straight through — WorkItem.status is never normalized to a fixed case
# or vocabulary). A pragmatic closed set, not an exhaustive one; documented
# rather than hidden, same posture as this codebase's other scoped
# simplifications.
_CLOSED_WORK_ITEM_STATUSES = {"done", "closed", "merged", "resolved", "complete", "completed"}


def _is_closed_status(status: str | None) -> bool:
    return status is not None and status.strip().lower() in _CLOSED_WORK_ITEM_STATUSES


def week_start_monday(now: datetime.datetime) -> datetime.date:
    return now.date() - datetime.timedelta(days=now.weekday())


@dataclass(frozen=True)
class WinEvidence:
    description: str
    source_reference_key: str
    source_link: str | None
    kind: str  # "accomplishment" | "commitment" | "work_item"
    # False means this evidence has no Accomplishment row yet — a
    # candidate for the "Confirm & log" proposal (design spec §2's
    # correction: proposed here, never auto-written).
    already_logged: bool
    existing_skill_category: str | None = None
    # The real Accomplishment.id — set only for kind="accomplishment"
    # (already_logged=True), None otherwise. Exists so a freshly-LLM-
    # classified skill_category can be written back to the EXACT row it
    # came from (app.sub_agents.friday_review.agent's persistence step) —
    # NOT via source_reference_key, which is deliberately NOT unique per
    # row (e.g. every meeting-synthesized accomplishment for a report
    # shares "agenda:{report_user_id}" — see app/agenda/store.py) and
    # would risk stamping the wrong row's classification onto an
    # unrelated one that happens to share the same key.
    id: str | None = None


@dataclass(frozen=True)
class SlippedItem:
    description: str
    due_at: datetime.datetime | None
    source_reference_key: str
    weeks_running: bool


@dataclass(frozen=True)
class AgendaSummary:
    resolved_this_week: list = field(default_factory=list)
    carried: list = field(default_factory=list)
    stuck: list = field(default_factory=list)


@dataclass(frozen=True)
class OkrProgress:
    title: str
    goal_type: str
    progress: float | None
    current_value: float | None
    target_value: float | None


@dataclass(frozen=True)
class SkillDistribution:
    counts: dict


@dataclass(frozen=True)
class FridayReviewContext:
    week_start: datetime.date
    wins: list
    slipped: list
    agenda: AgendaSummary
    okr_progress: list
    career_goal: OkrProgress | None
    daily_pulse_patterns: list
    skill_distribution: SkillDistribution
    identity_batch: list


def _gather_wins(owner_scope: OwnerScope, now: datetime.datetime) -> list:
    window_start = now - datetime.timedelta(days=EVIDENCE_WINDOW_DAYS)
    wins: list[WinEvidence] = []

    accomplishments = (
        owner_scope.session.execute(
            owner_scope.query(Accomplishment).where(
                Accomplishment.occurred_at >= window_start,
                Accomplishment.occurred_at <= now,
            )
        )
        .scalars()
        .all()
    )
    for row in accomplishments:
        wins.append(
            WinEvidence(
                description=row.description,
                source_reference_key=row.source_reference_key,
                source_link=None,
                kind="accomplishment",
                already_logged=True,
                existing_skill_category=row.skill_category,
                id=row.id,
            )
        )

    commitments = (
        owner_scope.session.execute(
            owner_scope.query(Commitment).where(
                Commitment.status == "delivered",
                Commitment.delivered_at.is_not(None),
                Commitment.delivered_at >= window_start,
                Commitment.delivered_at <= now,
            )
        )
        .scalars()
        .all()
    )
    for row in commitments:
        wins.append(
            WinEvidence(
                description=row.description,
                source_reference_key=row.id,
                source_link=None,
                kind="commitment",
                already_logged=False,
            )
        )

    work_items = (
        owner_scope.session.execute(
            owner_scope.query(WorkItem).where(
                WorkItem.source.in_(["jira", "github", "linear"]),
                WorkItem.updated_at.is_not(None),
                WorkItem.updated_at >= window_start,
                WorkItem.updated_at <= now,
            )
        )
        .scalars()
        .all()
    )
    for row in work_items:
        if not _is_closed_status(row.status):
            continue
        wins.append(
            WinEvidence(
                description=row.title or row.external_id or row.id,
                source_reference_key=row.id,
                source_link=row.url,
                kind="work_item",
                already_logged=False,
            )
        )

    return wins


def _gather_slipped(owner_scope: OwnerScope, now: datetime.datetime) -> list:
    pattern_cutoff = now - datetime.timedelta(days=THREE_WEEK_PATTERN_DAYS)
    rows = (
        owner_scope.session.execute(
            owner_scope.query(Commitment).where(
                Commitment.status == "open",
                Commitment.due_at.is_not(None),
                Commitment.due_at < now,
            )
        )
        .scalars()
        .all()
    )
    slipped = [
        SlippedItem(
            description=row.description,
            due_at=row.due_at,
            source_reference_key=row.id,
            weeks_running=row.promised_at <= pattern_cutoff,
        )
        for row in rows
    ]

    # A promised_at <= 21d-ago open commitment can lack a due_at (open-
    # ended promise) and so never show up in the overdue query above —
    # still worth naming as a running pattern (design spec §3's own
    # `promised_at <= now-21d` filter is independent of the due_at query).
    already_included_ids = {s.source_reference_key for s in slipped}
    pattern_rows = (
        owner_scope.session.execute(
            owner_scope.query(Commitment).where(
                Commitment.status == "open",
                Commitment.promised_at <= pattern_cutoff,
            )
        )
        .scalars()
        .all()
    )
    for row in pattern_rows:
        if row.id in already_included_ids:
            continue
        slipped.append(
            SlippedItem(
                description=row.description,
                due_at=row.due_at,
                source_reference_key=row.id,
                weeks_running=True,
            )
        )
    return slipped


def _gather_agenda(owner_scope: OwnerScope, now: datetime.datetime) -> AgendaSummary:
    pair_scope = resolve_pair_scope(
        owner_scope.session, owner_scope.owner_user_id, owner_scope.owner_user_id
    )
    if pair_scope is None:
        return AgendaSummary()

    window_start = now - datetime.timedelta(days=EVIDENCE_WINDOW_DAYS)
    items = get_agenda(pair_scope)
    resolved_this_week = [
        item
        for item in items
        if item.status == "resolved"
        and item.resolved_at is not None
        and window_start <= item.resolved_at <= now
    ]
    carried = [item for item in items if item.status == "open"]
    stuck = [
        item for item in carried if item.surfaced_count >= STUCK_SURFACED_COUNT_THRESHOLD
    ]
    return AgendaSummary(resolved_this_week=resolved_this_week, carried=carried, stuck=stuck)


def _gather_okr_and_career_goal(owner_scope: OwnerScope):
    rows = (
        owner_scope.session.execute(
            owner_scope.query(Goal).where(Goal.status == "active")
        )
        .scalars()
        .all()
    )
    okr_progress = [
        OkrProgress(
            title=row.title, goal_type=row.goal_type, progress=row.progress,
            current_value=row.current_value, target_value=row.target_value,
        )
        for row in rows
        if row.goal_type in ("objective", "key_result")
    ]
    career_rows = [row for row in rows if row.goal_type == "career_goal"]
    career_goal = (
        OkrProgress(
            title=career_rows[0].title, goal_type="career_goal",
            progress=career_rows[0].progress, current_value=career_rows[0].current_value,
            target_value=career_rows[0].target_value,
        )
        if career_rows
        else None
    )
    return okr_progress, career_goal


def _gather_skill_distribution(
    owner_scope: OwnerScope, now: datetime.datetime
) -> SkillDistribution:
    window_start = now - datetime.timedelta(days=SKILL_DISTRIBUTION_WINDOW_DAYS)
    rows = (
        owner_scope.session.execute(
            owner_scope.query(Accomplishment).where(
                Accomplishment.occurred_at >= window_start,
                Accomplishment.occurred_at <= now,
                Accomplishment.skill_category.is_not(None),
            )
        )
        .scalars()
        .all()
    )
    return SkillDistribution(counts=dict(Counter(row.skill_category for row in rows)))


def _gather_daily_pulse_patterns(owner_scope: OwnerScope, now: datetime.datetime) -> list:
    window_start = (now - datetime.timedelta(days=EVIDENCE_WINDOW_DAYS)).date()
    rows = (
        owner_scope.session.execute(
            owner_scope.query(PulseDelivery).where(
                PulseDelivery.ritual == "pulse",
                PulseDelivery.local_date >= window_start,
                PulseDelivery.local_date <= now.date(),
            )
        )
        .scalars()
        .all()
    )
    day_sets: dict = {}
    for row in rows:
        for item_id in row.item_ids:
            day_sets.setdefault(item_id, set()).add(row.local_date)
    return sorted(
        item_id
        for item_id, days in day_sets.items()
        if len(days) >= DAILY_PULSE_PATTERN_MIN_DISTINCT_DAYS
    )


def gather_friday_review_context(owner_scope: OwnerScope, clock: Clock) -> FridayReviewContext:
    now = clock.now()
    okr_progress, career_goal = _gather_okr_and_career_goal(owner_scope)
    return FridayReviewContext(
        week_start=week_start_monday(now),
        wins=_gather_wins(owner_scope, now),
        slipped=_gather_slipped(owner_scope, now),
        agenda=_gather_agenda(owner_scope, now),
        okr_progress=okr_progress,
        career_goal=career_goal,
        daily_pulse_patterns=_gather_daily_pulse_patterns(owner_scope, now),
        skill_distribution=_gather_skill_distribution(owner_scope, now),
        identity_batch=build_batch(owner_scope),
    )
