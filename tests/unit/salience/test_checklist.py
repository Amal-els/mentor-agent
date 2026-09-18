import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import ChecklistCompletion, DossierDelivery, Event, Message, WorkItem
from app.salience.pulse.checklist import (
    RESOLVED_LOOKBACK_HOURS,
    build_checklist,
    find_checklist_item,
    record_completion,
)

NOW = datetime.datetime(2026, 8, 26, 9, 0, tzinfo=datetime.UTC)
CLOCK = FrozenClock(at=NOW)


def _work_item(owner_user_id: str, **overrides) -> WorkItem:
    defaults = {
        "id": str(uuid.uuid4()),
        "owner_user_id": owner_user_id,
        "actor_reference_key": "linear:sarah@acme.com",
        "resolved_person_id": None,
        "source": "linear",
        "external_id": f"MENT-{uuid.uuid4().hex[:6]}",
        "title": "Fix flaky roster test",
        "status": "in_progress",
        "url": "https://linear.app/MENT-1",
        "due_at": None,
        "updated_at": NOW - datetime.timedelta(hours=1),
        "blocks_others": False,
    }
    defaults.update(overrides)
    return WorkItem(**defaults)


def _message(owner_user_id: str, **overrides) -> Message:
    defaults = {
        "id": str(uuid.uuid4()),
        "owner_user_id": owner_user_id,
        "actor_reference_key": "slack:U123",
        "resolved_person_id": None,
        "source": "slack",
        "external_id": uuid.uuid4().hex,
        "channel": "C0123",
        "sent_at": NOW - datetime.timedelta(hours=1),
        "is_dm": False,
        "body_ref": "slack://C0123/1",
    }
    defaults.update(overrides)
    return Message(**defaults)


def test_open_work_item_shows_up_pending(make_scope, pg_session):
    scope = make_scope()
    wi = _work_item(scope.owner_user_id)
    pg_session.add(wi)
    pg_session.commit()

    view = build_checklist(scope, CLOCK)

    assert [i.item_id for i in view.pending] == [wi.id]
    assert view.resolved == []
    assert view.done_count == 0
    assert view.total_count == 1


def test_manually_completed_item_moves_to_resolved(make_scope, pg_session):
    scope = make_scope()
    wi = _work_item(scope.owner_user_id)
    pg_session.add(wi)
    pg_session.commit()

    record_completion(scope, CLOCK, "work_item", (wi.source, wi.external_id), "Nice work!")
    view = build_checklist(scope, CLOCK)

    assert view.pending == []
    assert len(view.resolved) == 1
    assert view.resolved[0].item_id == wi.id
    assert view.resolved[0].status == "manual"
    assert view.resolved[0].ai_response == "Nice work!"
    assert view.done_count == 1
    assert view.total_count == 1


def test_completion_survives_row_being_deleted_and_reinserted_with_a_new_id(
    make_scope, pg_session
):
    # The exact bug this stable-key design fixes: seed_live's Google-Docs
    # reset-then-refetch cycle (app/salience/checklist.py's own module
    # docstring) deletes a Message row and re-inserts a logically
    # identical one with a brand-new primary key. A completion keyed on
    # (source, external_id) must still recognize the new row as resolved.
    scope = make_scope()
    original = _message(scope.owner_user_id, source="google_docs", external_id="gdoc-comment-1")
    pg_session.add(original)
    pg_session.commit()
    record_completion(scope, CLOCK, "message", ("google_docs", "gdoc-comment-1"), "Handled.")

    # simulate seed_live's reset-then-refetch: delete the old row, insert a
    # new one with a fresh id but the same (source, external_id)
    pg_session.delete(original)
    pg_session.commit()
    reinserted = _message(
        scope.owner_user_id, source="google_docs", external_id="gdoc-comment-1"
    )
    pg_session.add(reinserted)
    pg_session.commit()

    view = build_checklist(scope, CLOCK)

    assert view.pending == []
    assert len(view.resolved) == 1
    assert view.resolved[0].item_id == reinserted.id
    assert view.resolved[0].status == "manual"
    assert view.resolved[0].ai_response == "Handled."


def test_auto_resolved_work_item_shows_aside_not_pending(make_scope, pg_session):
    scope = make_scope()
    merged = _work_item(
        scope.owner_user_id, status="merged", updated_at=NOW - datetime.timedelta(hours=2)
    )
    open_item = _work_item(scope.owner_user_id, status="open")
    pg_session.add_all([merged, open_item])
    pg_session.commit()

    view = build_checklist(scope, CLOCK)

    assert [i.item_id for i in view.pending] == [open_item.id]
    assert [i.item_id for i in view.resolved] == [merged.id]
    assert view.resolved[0].status == "auto"
    assert view.done_count == 1
    assert view.total_count == 2


def test_auto_resolved_work_item_outside_lookback_window_is_dropped(make_scope, pg_session):
    scope = make_scope()
    stale_merge = _work_item(
        scope.owner_user_id,
        status="merged",
        updated_at=NOW - datetime.timedelta(hours=RESOLVED_LOOKBACK_HOURS + 1),
    )
    pg_session.add(stale_merge)
    pg_session.commit()

    view = build_checklist(scope, CLOCK)

    assert view.pending == []
    assert view.resolved == []
    assert view.total_count == 0


def test_message_pending_and_completable(make_scope, pg_session):
    scope = make_scope()
    msg = _message(scope.owner_user_id)
    pg_session.add(msg)
    pg_session.commit()

    view = build_checklist(scope, CLOCK)
    assert [i.item_id for i in view.pending] == [msg.id]

    record_completion(scope, CLOCK, "message", (msg.source, msg.external_id), "Handled.")
    view2 = build_checklist(scope, CLOCK)
    assert view2.pending == []
    assert [i.item_id for i in view2.resolved] == [msg.id]


def test_record_completion_is_idempotent_upsert(make_scope, pg_session):
    scope = make_scope()
    wi = _work_item(scope.owner_user_id)
    pg_session.add(wi)
    pg_session.commit()
    key = (wi.source, wi.external_id)

    record_completion(scope, CLOCK, "work_item", key, "first")
    later = FrozenClock(at=NOW + datetime.timedelta(minutes=5))
    record_completion(scope, later, "work_item", key, "second")

    rows = pg_session.query(ChecklistCompletion).filter(
        ChecklistCompletion.owner_user_id == scope.owner_user_id
    ).all()
    assert len(rows) == 1
    assert rows[0].ai_response == "second"
    assert rows[0].completed_at == later.now()


def test_build_checklist_day_carries_meeting_url_and_dossier_id(make_scope, pg_session):
    scope = make_scope()
    event = Event(
        id=str(uuid.uuid4()),
        owner_user_id=scope.owner_user_id,
        source="calendar",
        external_id="evt-checklist-1",
        title="1:1 with Sarah",
        starts_at=NOW + datetime.timedelta(hours=1),
        ends_at=NOW + datetime.timedelta(hours=2),
        actor_reference_key="cal#1",
        url="https://calendar.google.com/event?eid=abc",
    )
    dossier_id = str(uuid.uuid4())
    pg_session.add(event)
    pg_session.add(
        DossierDelivery(
            id=dossier_id, owner_user_id=scope.owner_user_id,
            event_external_id="evt-checklist-1", sent_at=NOW,
            prompt_version="dossier_synthesize@v1", talking_points_source="fresh",
            card_ref=None, feedback="none", feedback_at=None, created_at=NOW,
            who_summary="", why_now="",
        )
    )
    pg_session.commit()

    view = build_checklist(scope, CLOCK)

    assert len(view.day) == 1
    assert view.day[0].title == "1:1 with Sarah"
    assert view.day[0].url == "https://calendar.google.com/event?eid=abc"
    assert view.day[0].dossier_id == dossier_id
    assert view.day[0].has_dossier is True
    # events never compete for pending/resolved (app/pipeline/pulse.py's
    # own ranking_pool exclusion) — the meeting only ever shows up here.
    assert view.pending == []
    assert view.resolved == []


def test_find_checklist_item_returns_none_for_unknown_item_type(make_scope):
    scope = make_scope()
    assert find_checklist_item(scope, "event", "anything") is None


def test_find_checklist_item_is_ownership_scoped(make_scope, pg_session):
    owner_scope = make_scope()
    other_scope = make_scope()
    wi = _work_item(owner_scope.owner_user_id)
    pg_session.add(wi)
    pg_session.commit()

    assert find_checklist_item(owner_scope, "work_item", wi.id) is not None
    assert find_checklist_item(other_scope, "work_item", wi.id) is None


def test_find_checklist_item_returns_message_title_and_source_key(make_scope, pg_session):
    scope = make_scope()
    msg = _message(scope.owner_user_id, source="slack", is_dm=True)
    pg_session.add(msg)
    pg_session.commit()

    item = find_checklist_item(scope, "message", msg.id)
    assert item is not None
    assert item.title == "Slack DM"
    assert item.source_key == (msg.source, msg.external_id)
