"""L5 scoring: pure functions, no DB access, no LLM, no randomness. Person
names and weights are resolved by the caller (app/salience/assemble.py) so
these stay independently testable. Commitments are never scored here —
pulse.md keeps "Owed" a separate, unranked card section (see
docs/plans/morning-pulse.md M3), so they go straight into
PreGateContext.owed instead of competing for the shortlist."""

import datetime

from app.core.clock import Clock
from app.core.models import Event, Message, WorkItem
from app.salience.pulse.config import (
    ACTION_REQUESTED_BONUS,
    BASE_SCORE_EVENT,
    BASE_SCORE_MESSAGE,
    BASE_SCORE_WORK_ITEM,
    BLOCKING_OTHERS_BONUS,
    MEETING_PROXIMITY_BONUS,
    RELEVANCE_BONUS_PERSON_WAITING,
    STALENESS_BONUS_MAX,
    STALENESS_BONUS_PER_DAY,
    STALENESS_THRESHOLD_DAYS,
    URGENCY_BONUS_DUE_TODAY,
    URGENCY_BONUS_OVERDUE,
)
from app.salience.pulse.types import ScoredItem


def _as_aware(dt: datetime.datetime) -> datetime.datetime:
    """DB rows come back tz-aware (DateTime(timezone=True) columns); test
    clocks (FrozenClock) are sometimes constructed with a naive datetime.
    Naive - aware raises TypeError, so treat a naive value as UTC rather
    than let that mismatch reach the caller."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=datetime.UTC)


def _staleness(last_activity, now) -> tuple[int, bool, float]:
    """Shared by score_work_item/score_message: days since last activity,
    whether that crosses STALENESS_THRESHOLD_DAYS, and the resulting bonus
    (capped, scales with age — command-center-brief priority table)."""
    if last_activity is None:
        return 0, False, 0.0
    days_stale = (_as_aware(now) - _as_aware(last_activity)).days
    stale = days_stale >= STALENESS_THRESHOLD_DAYS
    bonus = min(days_stale * STALENESS_BONUS_PER_DAY, STALENESS_BONUS_MAX) if stale else 0.0
    return days_stale, stale, bonus


def score_event(
    row: Event, person_name: str | None, weight: float, clock: Clock
) -> ScoredItem:
    now = clock.now()
    due_today = row.starts_at is not None and row.starts_at.date() == now.date()
    has_deadline = due_today
    person_waiting = person_name
    urgency_bonus = URGENCY_BONUS_DUE_TODAY if due_today else 0.0
    relevance_bonus = RELEVANCE_BONUS_PERSON_WAITING if person_waiting else 0.0

    return ScoredItem(
        item_id=row.id,
        item_type="event",
        score=(BASE_SCORE_EVENT + urgency_bonus + relevance_bonus) * weight,
        score_terms={
            "has_deadline": has_deadline,
            "due_today": due_today,
            "person_waiting": person_waiting,
        },
        candidate_focus=has_deadline and person_waiting is not None,
        series_id=row.series_id,
    )


_RESOLVED_WORK_ITEM_STATUSES = {
    "merged",
    "closed",
    "done",
    "cancelled",
    "canceled",
    "resolved",
}


def is_resolved_work_item(row: WorkItem) -> bool:
    """A merged/closed PR, or a Linear/Jira issue sitting in a workspace-
    configured "done"-shaped status, has nothing left to act on — showing
    it in a future pulse/dossier would just be re-surfacing something
    already handled, forever, since nothing else ever removes a WorkItem
    row once webhook-ingested. GitHub's own status vocabulary is fixed
    and exact ("merged"/"closed"/"open"/"draft" — adapt_pull_request_
    event/adapt_issue_event, app/ingest/github_webhook.py), so that half
    is a reliable match. Linear/Jira statuses are workspace-configurable
    free text (a team could name their "done" column anything), so this
    is a best-effort match on the common shapes real teams actually use,
    not a guaranteed-exhaustive list — an unusual custom status name
    (e.g. "Shipped") won't be caught by this alone."""
    return (row.status or "").strip().lower() in _RESOLVED_WORK_ITEM_STATUSES


def score_work_item(
    row: WorkItem,
    person_name: str | None,
    weight: float,
    clock: Clock,
    meeting_today: bool = False,
    is_new_since_last_pulse: bool = True,
) -> ScoredItem:
    now = clock.now()
    overdue = row.due_at is not None and row.due_at < now
    due_today = row.due_at is not None and row.due_at.date() == now.date()
    has_deadline = overdue or due_today
    blocked = row.status == "blocked"
    person_waiting = person_name if (blocked and person_name) else None
    urgency_bonus = (
        URGENCY_BONUS_OVERDUE
        if overdue
        else (URGENCY_BONUS_DUE_TODAY if due_today else 0.0)
    )
    relevance_bonus = RELEVANCE_BONUS_PERSON_WAITING if person_waiting else 0.0
    blocks_others = bool(row.blocks_others)
    blocking_bonus = BLOCKING_OTHERS_BONUS if blocks_others else 0.0
    days_stale, stale, staleness_bonus = _staleness(row.updated_at, now)
    meeting_bonus = MEETING_PROXIMITY_BONUS if meeting_today else 0.0

    return ScoredItem(
        item_id=row.id,
        item_type="work_item",
        score=(
            BASE_SCORE_WORK_ITEM
            + urgency_bonus
            + relevance_bonus
            + blocking_bonus
            + staleness_bonus
            + meeting_bonus
        )
        * weight,
        score_terms={
            "has_deadline": has_deadline,
            "overdue": overdue,
            "due_today": due_today,
            "blocked": blocked,
            "person_waiting": person_waiting,
            "blocks_others": blocks_others,
            "days_stale": days_stale,
            "stale": stale,
            "meeting_today": meeting_today,
            "is_new_since_last_pulse": is_new_since_last_pulse,
        },
        candidate_focus=has_deadline and person_waiting is not None,
        series_id=None,
    )


def score_message(
    row: Message,
    person_name: str | None,
    weight: float,
    clock: Clock,
    meeting_today: bool = False,
    is_new_since_last_pulse: bool = True,
) -> ScoredItem:
    """Mirrors score_event's shape exactly: a message is "due" the day it
    arrived, not on some deadline it doesn't have — sent_at.date() ==
    today plays the same role starts_at.date() == today plays for events.
    person_name is the resolved sender/commenter (identity resolution's
    job, not this function's) — without one, a message can never become
    candidate_focus, same as an event with no resolved person.

    Staleness (days since sent_at) and meeting_today/is_new_since_last_pulse
    (both computed by the caller from cross-item context this function
    doesn't have — today's events, the last pulse delivery) are the same
    priority-ranking signals score_work_item adds; "blocking others" has no
    Message equivalent, so it's the one signal not added here.

    row.action_requested/row.requires_reply are only ever set by connectors
    that classify at fetch time (currently Gmail's triageInbox) — both
    default False/None for every other source. requires_reply=False (only
    Gmail can produce this) suppresses person_waiting/relevance_bonus even
    for a resolved sender, so a plain FYI email doesn't get the same bonus
    as one that's actually asking you something; None (Slack/Docs, which
    never touch this column) preserves today's behavior exactly — every
    resolved sender counts as person_waiting."""
    now = clock.now()
    due_today = row.sent_at is not None and row.sent_at.date() == now.date()
    has_deadline = due_today
    person_waiting = person_name if row.requires_reply is not False else None
    urgency_bonus = URGENCY_BONUS_DUE_TODAY if due_today else 0.0
    relevance_bonus = RELEVANCE_BONUS_PERSON_WAITING if person_waiting else 0.0
    days_stale, stale, staleness_bonus = _staleness(row.sent_at, now)
    meeting_bonus = MEETING_PROXIMITY_BONUS if meeting_today else 0.0
    action_requested = bool(row.action_requested)
    action_requested_bonus = ACTION_REQUESTED_BONUS if action_requested else 0.0

    return ScoredItem(
        item_id=row.id,
        item_type="message",
        score=(
            BASE_SCORE_MESSAGE
            + urgency_bonus
            + relevance_bonus
            + staleness_bonus
            + meeting_bonus
            + action_requested_bonus
        )
        * weight,
        score_terms={
            "has_deadline": has_deadline,
            "due_today": due_today,
            "person_waiting": person_waiting,
            "days_stale": days_stale,
            "stale": stale,
            "meeting_today": meeting_today,
            "is_new_since_last_pulse": is_new_since_last_pulse,
            "action_requested": action_requested,
        },
        candidate_focus=has_deadline and person_waiting is not None,
        series_id=None,
    )
