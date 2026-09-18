import datetime
import logging
from typing import Any
from zoneinfo import ZoneInfo
from app.core.clock import Clock
from app.core.models import (
    ChecklistCompletion,
    Commitment,
    DossierDelivery,
    Event,
    Goal,
    Message,
    OneOnOneNote,
    PulseDelivery,
    User,
    WorkItem,
)
from app.core.scope import OwnerScope
from app.identity.models import Person
from app.ingest.base import Healthy
from app.salience.pulse.score import (
    is_resolved_work_item,
    score_event,
    score_message,
    score_work_item,
)
from app.salience.pulse.types import DayEventSummary, OwedItem, PreGateContext
from app.salience.pulse.weights import get_effective_weight

logger = logging.getLogger(__name__)

def find_origin_item(
    scope: OwnerScope, echo_of: dict, current_message_id: str | None = None
) -> Any | None:
    source = echo_of.get("source")
    external_id = echo_of.get("external_id")
    if not source or not external_id:
        return None
    if source in ("github", "jira", "linear"):
        return scope.session.execute(
            scope.query(WorkItem).where(
                WorkItem.source == source,
                WorkItem.external_id == external_id,
            )
        ).scalars().first()
    if source == "slack":
        q = scope.query(Message).where(
            Message.source == "slack",
            Message.external_id == external_id,
        )
        if current_message_id:
            q = q.where(Message.id != current_message_id)
        return scope.session.execute(q).scalars().first()
    if source == "notion":
        goal = scope.session.execute(
            scope.query(Goal).where(
                Goal.source == "notion",
                Goal.external_id == external_id,
            )
        ).scalars().first()
        if goal is not None:
            return goal
        note = scope.session.execute(
            scope.query(OneOnOneNote).where(
                OneOnOneNote.source == "notion",
                OneOnOneNote.external_id == external_id,
            )
        ).scalars().first()
        if note is not None:
            return note
        return scope.session.execute(
            scope.query(WorkItem).where(
                WorkItem.source == "notion",
                WorkItem.external_id == external_id,
            )
        ).scalars().first()
    return scope.session.execute(
        scope.query(WorkItem).where(
            WorkItem.source == source,
            WorkItem.external_id == external_id,
        )
    ).scalars().first()

def select_window(
    user: User | None, clock: Clock, events_today: list[Event]
) -> tuple[datetime.date, str]:
    now = clock.now()
    tz = ZoneInfo(user.tz) if user and user.tz else datetime.UTC
    now_local = now.astimezone(tz)
    unelapsed = any(
        event.ends_at is not None and event.ends_at > now for event in events_today
    )
    if unelapsed:
        return now_local.date(), "today"
    late_cutoff_local = user.late_cutoff_local if user else datetime.time(21, 0)
    tomorrow = now_local.date() + datetime.timedelta(days=1)
    if now_local.time() >= late_cutoff_local:
        return tomorrow, "late_cutoff"
    return tomorrow, "today_exhausted"

def assemble_and_score(
    scope: OwnerScope,
    clock: Clock,
    source_clients: list,
    limit: int | None = None,
) -> PreGateContext:
    now = clock.now()
    user = scope.session.get(User, scope.owner_user_id)
    tz = ZoneInfo(user.tz) if user and user.tz else datetime.UTC
    today_local = now.astimezone(tz).date()
    degraded_sources = sorted(
        client.source
        for client in source_clients
        if not isinstance(client.health(), Healthy)
    )
    person_cache: dict[str, str | None] = {}
    def person_name(person_id: str | None) -> str | None:
        if person_id is None:
            return None
        if person_id not in person_cache:
            person = scope.session.get(Person, person_id)
            person_cache[person_id] = person.canonical_name if person else None
        return person_cache[person_id]
    event_weight = get_effective_weight(scope, "type:event", clock)
    work_item_weight = get_effective_weight(scope, "type:work_item", clock)
    message_weight = get_effective_weight(scope, "type:message", clock)
    weights = {
        "event": event_weight,
        "work_item": work_item_weight,
        "message": message_weight,
    }
    events = scope.session.execute(scope.query(Event)).scalars().all()
    events_today = [
        e
        for e in events
        if e.starts_at is not None and e.starts_at.astimezone(tz).date() == today_local
    ]
    window_date, window_reason = select_window(user, clock, events_today)
    events_in_window = (
        events_today
        if window_date == today_local
        else [
            e
            for e in events
            if e.starts_at is not None
            and e.starts_at.astimezone(tz).date() == window_date
        ]
    )
    dossier_id_by_event_external_id = {
        row.event_external_id: row.id
        for row in scope.session.query(
            DossierDelivery.event_external_id, DossierDelivery.id
        )
        .filter(DossierDelivery.owner_user_id == scope.owner_user_id)
        .filter(DossierDelivery.event_external_id.in_([e.external_id for e in events_in_window]))
        .all()
    }
    day_events = sorted(
        (
            DayEventSummary(
                item_id=e.id,
                title=e.title or "",
                starts_at=e.starts_at,
                has_dossier=e.external_id in dossier_id_by_event_external_id,
                dossier_id=dossier_id_by_event_external_id.get(e.external_id),
                url=e.url,
            )
            for e in events_in_window
        ),
        key=lambda d: d.starts_at,
    )
    completed_item_keys = {
        (row.item_type, row.source, row.external_id)
        for row in scope.session.execute(scope.query(ChecklistCompletion)).scalars().all()
    }
    work_items = [
        row
        for row in scope.session.execute(scope.query(WorkItem)).scalars().all()
        if not is_resolved_work_item(row)
        and ("work_item", row.source, row.external_id) not in completed_item_keys
    ]
    raw_messages = scope.session.execute(scope.query(Message)).scalars().all()
    messages = []
    for m in raw_messages:
        if ("message", m.source, m.external_id) in completed_item_keys:
            continue
        if (
            m.source == "slack"
            and user is not None
            and user.slack_user_id is not None
            and m.actor_reference_key == f"slack:{user.slack_user_id}"
        ):
            continue
        if m.source_echo_of:
            origin_item = find_origin_item(scope, m.source_echo_of, current_message_id=m.id)
            if origin_item is not None:
                logger.info(
                    "suppressed Gmail echo: owner=%s message_id=%s echo_of=%s origin_item_id=%s",
                    scope.owner_user_id,
                    m.id,
                    m.source_echo_of,
                    getattr(origin_item, "id", str(origin_item)),
                )
                continue
        messages.append(m)
    today_meeting_person_ids = {
        e.resolved_person_id for e in events_today if e.resolved_person_id is not None
    }
    last_delivery = scope.session.execute(
        scope.query(PulseDelivery).order_by(PulseDelivery.delivered_at.desc())
    ).scalars().first()
    last_delivered_at = last_delivery.delivered_at if last_delivery else None

    def meeting_today(person_id: str | None) -> bool:
        return person_id is not None and person_id in today_meeting_person_ids

    def is_new_since_last_pulse(activity_at) -> bool:
        if last_delivered_at is None or activity_at is None:
            return True
        aware_activity = (
            activity_at
            if activity_at.tzinfo is not None
            else activity_at.replace(tzinfo=datetime.UTC)
        )
        aware_delivered = (
            last_delivered_at
            if last_delivered_at.tzinfo is not None
            else last_delivered_at.replace(tzinfo=datetime.UTC)
        )
        return aware_activity > aware_delivered
    scored = (
        [
            score_event(e, person_name(e.resolved_person_id), event_weight, clock)
            for e in events_in_window
        ]
        + [
            score_work_item(
                w,
                person_name(w.resolved_person_id),
                work_item_weight,
                clock,
                meeting_today=meeting_today(w.resolved_person_id),
                is_new_since_last_pulse=is_new_since_last_pulse(w.updated_at),
            )
            for w in work_items
        ]
        + [
            score_message(
                m,
                person_name(m.resolved_person_id),
                message_weight,
                clock,
                meeting_today=meeting_today(m.resolved_person_id),
                is_new_since_last_pulse=is_new_since_last_pulse(m.sent_at),
            )
            for m in messages
        ]
    )
    scored.sort(key=lambda item: item.score, reverse=True)
    shortlist = scored if limit is None else scored[:limit]
    commitments = (
        scope.session.execute(
            scope.query(Commitment).where(Commitment.status == "open")
        )
        .scalars()
        .all()
    )
    commitments.sort(key=lambda c: c.promised_at)
    owed = [
        OwedItem(
            item_id=c.id,
            description=c.description,
            promised_to=person_name(c.promised_to_person_id),
            promised_at=c.promised_at,
            due_at=c.due_at,
            overdue=c.due_at is not None and c.due_at < now,
        )
        for c in commitments
    ]
    return PreGateContext(
        owner_user_id=scope.owner_user_id,
        owner_tz=user.tz if user and user.tz else "UTC",
        window_date=window_date,
        window_reason=window_reason,
        shortlist=shortlist,
        day_events=day_events,
        owed=owed,
        degraded_sources=degraded_sources,
        weights=weights,
    )