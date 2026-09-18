import datetime
import uuid
from dataclasses import dataclass
from app.core.clock import Clock
from app.core.models import ChecklistCompletion, Message, WorkItem
from app.core.scope import OwnerScope
from app.identity.models import Person
from app.pipeline.pulse import _message_title, _model_for
from app.salience.pulse.pulse_context import build_pulse_context
from app.salience.pulse.score import is_resolved_work_item
from app.salience.pulse.types import DayEventSummary, ScoredItem

RESOLVED_LOOKBACK_HOURS = 48
_COMPLETABLE_ITEM_TYPES = ("work_item", "message")

@dataclass(frozen=True)
class ChecklistItemView:
    item_id: str
    item_type: str
    title: str
    url: str | None
    detail: str | None
    source_key: tuple[str | None, str | None]  # (source, external_id) — the stable match key
    status: str  # "pending" | "manual" | "auto"
    completed_at: datetime.datetime | None
    ai_response: str | None


@dataclass(frozen=True)
class ChecklistView:
    pending: list[ChecklistItemView]
    resolved: list[ChecklistItemView]
    done_count: int
    total_count: int
    day: list[DayEventSummary]

def _as_aware(dt: datetime.datetime) -> datetime.datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=datetime.UTC)

@dataclass(frozen=True)
class _RowInfo:
    title: str
    url: str | None
    detail: str | None
    source: str | None
    external_id: str | None

def _row_info(
    scope: OwnerScope,
    item_type: str,
    row,
    person_cache: dict[str, str | None],
) -> _RowInfo:
    if item_type == "message":
        title = _message_title(row)
    else:
        title = row.title or ""
    person_name = None
    if row.resolved_person_id is not None:
        if row.resolved_person_id not in person_cache:
            person = scope.session.get(Person, row.resolved_person_id)
            person_cache[row.resolved_person_id] = (
                person.canonical_name if person is not None else None
            )
        person_name = person_cache[row.resolved_person_id]
    if item_type == "work_item":
        ticket = row.external_id
        detail = (
            f"{ticket} · {person_name}"
            if ticket and person_name
            else (ticket or person_name)
        )
    elif item_type == "message":
        detail = f"From {person_name}" if person_name else None
    else:
        detail = None
    return _RowInfo(
        title=title, url=row.url, detail=detail, source=row.source, external_id=row.external_id
    )

def build_checklist(scope: OwnerScope, clock: Clock) -> ChecklistView:
    now = clock.now()
    context = build_pulse_context(scope, clock, [], trigger="pull", requested_at=now)
    ranking_pool = [item for item in context.shortlist if item.item_type != "event"]
    completions = {
        (row.item_type, row.source, row.external_id): row
        for row in scope.session.execute(
            scope.query(ChecklistCompletion)
        ).scalars().all()
    }
    pending: list[ChecklistItemView] = []
    resolved: list[ChecklistItemView] = []
    person_cache: dict[str, str | None] = {}
    resolved_manual_keys: set[tuple[str, str | None, str | None]] = set()
    for scored_item in ranking_pool:
        row = scope.session.get(_model_for(scored_item.item_type), scored_item.item_id)
        if row is None:
            continue
        info = _row_info(scope, scored_item.item_type, row, person_cache)
        completion = completions.get((scored_item.item_type, info.source, info.external_id))
        view = ChecklistItemView(
            item_id=scored_item.item_id,
            item_type=scored_item.item_type,
            title=info.title,
            url=info.url,
            detail=info.detail,
            source_key=(info.source, info.external_id),
            status="manual" if completion else "pending",
            completed_at=completion.completed_at if completion else None,
            ai_response=completion.ai_response if completion else None,
        )
        if completion:
            resolved_manual_keys.add((scored_item.item_type, info.source, info.external_id))
            resolved.append(view)
        else:
            pending.append(view)
    for completion in completions.values():
        model = _model_for(completion.item_type)
        row = scope.session.execute(
            scope.query(model).where(
                model.source == completion.source,
                model.external_id == completion.external_id,
            )
        ).scalar_one_or_none()
        if row is None or (
            completion.item_type == "work_item" and is_resolved_work_item(row)
        ):
            continue
        key = (completion.item_type, completion.source, completion.external_id)
        if key in resolved_manual_keys:
            continue
        info = _row_info(scope, completion.item_type, row, person_cache)
        resolved_manual_keys.add(key)
        resolved.append(
            ChecklistItemView(
                item_id=row.id,
                item_type=completion.item_type,
                title=info.title,
                url=info.url,
                detail=info.detail,
                source_key=(info.source, info.external_id),
                status="manual",
                completed_at=completion.completed_at,
                ai_response=completion.ai_response,
            )
        )
    cutoff = now - datetime.timedelta(hours=RESOLVED_LOOKBACK_HOURS)
    recent_work_items = scope.session.execute(
        scope.query(WorkItem).where(WorkItem.updated_at >= cutoff)
    ).scalars().all()
    for row in recent_work_items:
        key = ("work_item", row.source, row.external_id)
        if key in resolved_manual_keys or not is_resolved_work_item(row):
            continue
        resolved.append(
            ChecklistItemView(
                item_id=row.id,
                item_type="work_item",
                title=row.title or "",
                url=row.url,
                detail=None,
                source_key=(row.source, row.external_id),
                status="auto",
                completed_at=row.updated_at,
                ai_response=None,
            )
        )
    resolved.sort(key=lambda v: _as_aware(v.completed_at) if v.completed_at else now, reverse=True)
    total_count = len(pending) + len(resolved)
    return ChecklistView(
        pending=pending,
        resolved=resolved,
        done_count=len(resolved),
        total_count=total_count,
        day=list(context.day_events),
    )

def find_checklist_item(
    scope: OwnerScope, item_type: str, item_id: str
) -> ChecklistItemView | None:
    if item_type not in _COMPLETABLE_ITEM_TYPES:
        return None
    model = WorkItem if item_type == "work_item" else Message
    row = scope.session.execute(
        scope.query(model).where(model.id == item_id)
    ).scalar_one_or_none()
    if row is None:
        return None
    info = _row_info(scope, item_type, row, {})
    return ChecklistItemView(
        item_id=item_id,
        item_type=item_type,
        title=info.title,
        url=info.url,
        detail=info.detail,
        source_key=(info.source, info.external_id),
        status="pending",
        completed_at=None,
        ai_response=None,
    )

def record_completion(
    scope: OwnerScope,
    clock: Clock,
    item_type: str,
    source_key: tuple[str | None, str | None],
    ai_response: str,
) -> None:
    source, external_id = source_key
    existing = scope.session.execute(
        scope.query(ChecklistCompletion).where(
            ChecklistCompletion.item_type == item_type,
            ChecklistCompletion.source == source,
            ChecklistCompletion.external_id == external_id,
        )
    ).scalar_one_or_none()
    now = clock.now()
    if existing is not None:
        existing.completed_at = now
        existing.ai_response = ai_response
    else:
        scope.add(
            ChecklistCompletion(
                id=str(uuid.uuid4()),
                item_type=item_type,
                source=source,
                external_id=external_id,
                completed_at=now,
                ai_response=ai_response,
            )
        )
    scope.commit()