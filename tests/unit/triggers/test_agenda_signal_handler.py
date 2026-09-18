import datetime

import pytest

from app.agenda.scope import resolve_pair_scope
from app.agenda.store import append_agenda_item
from app.core.clock import FrozenClock
from app.triggers.agenda.agenda_signal_handler import handle_agenda_signal

NOW = datetime.datetime(2026, 8, 14, tzinfo=datetime.UTC)


def _seed_item(db_session, pair):
    scope = resolve_pair_scope(db_session, pair.report_user_id, pair.report_user_id)
    return append_agenda_item(
        scope,
        {
            "text": "revisit the roadmap",
            "source": "manual",
            "source_link": None,
            "visibility": "shared",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        FrozenClock(at=NOW),
    )


def test_field_edit_from_a_member_applies(db_session, make_pair):
    pair = make_pair()
    item = _seed_item(db_session, pair)

    result = handle_agenda_signal(
        db_session,
        {
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.manager_user_id,
            "signal_type": "field_edit",
            "item_id": item.id,
            "changes": {"text": "revisit the roadmap (updated)"},
        },
    )

    from app.agenda.models import AgendaItem

    assert result["status"] == "ok"
    row = db_session.get(AgendaItem, item.id)
    assert row.text == "revisit the roadmap (updated)"


def test_drop_marks_resolved(db_session, make_pair):
    pair = make_pair()
    item = _seed_item(db_session, pair)

    result = handle_agenda_signal(
        db_session,
        {
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
            "signal_type": "drop",
            "item_id": item.id,
            "changes": None,
        },
    )

    from app.agenda.models import AgendaItem

    assert result["status"] == "ok"
    row = db_session.get(AgendaItem, item.id)
    assert row.status == "resolved"


def test_former_manager_signal_is_rejected(db_session, make_pair, make_user):
    now = datetime.datetime.now(datetime.UTC)
    pair = make_pair(ended_at=now)
    make_pair(report_user_id=pair.report_user_id)  # current pair now exists
    item = _seed_item(db_session, pair)

    result = handle_agenda_signal(
        db_session,
        {
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.manager_user_id,  # the FORMER manager
            "signal_type": "field_edit",
            "item_id": item.id,
            "changes": {"text": "should not apply"},
        },
    )

    from app.agenda.models import AgendaItem

    assert result["status"] == "rejected"
    row = db_session.get(AgendaItem, item.id)
    assert row.text == "revisit the roadmap"  # unchanged


def test_keep_resets_surfaced_count_and_reopens(db_session, make_pair):
    pair = make_pair()
    item = _seed_item(db_session, pair)

    from app.agenda.models import AgendaItem

    # Push the item past the pending_consent threshold first, so "keep"
    # has something real to reverse.
    row = db_session.get(AgendaItem, item.id)
    row.surfaced_count = 5
    row.status = "pending_consent"
    db_session.commit()

    result = handle_agenda_signal(
        db_session,
        {
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
            "signal_type": "keep",
            "item_id": item.id,
            "changes": None,
        },
    )

    assert result["status"] == "ok"
    row = db_session.get(AgendaItem, item.id)
    assert row.status == "open"
    assert row.surfaced_count == 0


def test_field_edit_ignores_disallowed_fields(db_session, make_pair, make_user):
    pair = make_pair()
    item = _seed_item(db_session, pair)
    other_report = make_user()

    result = handle_agenda_signal(
        db_session,
        {
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
            "signal_type": "field_edit",
            "item_id": item.id,
            "changes": {
                "text": "legit edit",
                "report_user_id": other_report,
                "status": "resolved",
                "surfaced_count": 999,
            },
        },
    )

    from app.agenda.models import AgendaItem

    assert result["status"] == "ok"
    row = db_session.get(AgendaItem, item.id)
    assert row.text == "legit edit"
    assert row.report_user_id == pair.report_user_id  # unchanged
    assert row.status == "open"  # unchanged
    assert row.surfaced_count == 0  # unchanged


def test_report_only_item_ignores_manager_signal(db_session, make_pair):
    pair = make_pair()
    scope = resolve_pair_scope(db_session, pair.report_user_id, pair.report_user_id)
    item = append_agenda_item(
        scope,
        {
            "text": "private note",
            "source": "manual",
            "source_link": None,
            "visibility": "report_only",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        FrozenClock(at=NOW),
    )

    result = handle_agenda_signal(
        db_session,
        {
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.manager_user_id,
            "signal_type": "field_edit",
            "item_id": item.id,
            "changes": {"text": "should not apply"},
        },
    )

    from app.agenda.models import AgendaItem

    assert result["status"] == "rejected"
    row = db_session.get(AgendaItem, item.id)
    assert row.text == "private note"  # unchanged


def test_report_only_item_allows_report_signal(db_session, make_pair):
    pair = make_pair()
    scope = resolve_pair_scope(db_session, pair.report_user_id, pair.report_user_id)
    item = append_agenda_item(
        scope,
        {
            "text": "private note",
            "source": "manual",
            "source_link": None,
            "visibility": "report_only",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        FrozenClock(at=NOW),
    )

    result = handle_agenda_signal(
        db_session,
        {
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
            "signal_type": "field_edit",
            "item_id": item.id,
            "changes": {"text": "updated by report"},
        },
    )

    from app.agenda.models import AgendaItem

    assert result["status"] == "ok"
    row = db_session.get(AgendaItem, item.id)
    assert row.text == "updated by report"


def test_manager_only_item_ignores_report_signal(db_session, make_pair):
    pair = make_pair()
    scope = resolve_pair_scope(db_session, pair.report_user_id, pair.manager_user_id)
    item = append_agenda_item(
        scope,
        {
            "text": "manager private note",
            "source": "manual",
            "source_link": None,
            "visibility": "manager_only",
            "created_by_user_id": pair.manager_user_id,
            "created_by_role": "manager",
        },
        FrozenClock(at=NOW),
    )

    result = handle_agenda_signal(
        db_session,
        {
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
            "signal_type": "drop",
            "item_id": item.id,
            "changes": None,
        },
    )

    from app.agenda.models import AgendaItem

    assert result["status"] == "rejected"
    row = db_session.get(AgendaItem, item.id)
    assert row.status == "open"  # unchanged


def test_manager_only_item_allows_manager_signal(db_session, make_pair):
    pair = make_pair()
    scope = resolve_pair_scope(db_session, pair.report_user_id, pair.manager_user_id)
    item = append_agenda_item(
        scope,
        {
            "text": "manager private note",
            "source": "manual",
            "source_link": None,
            "visibility": "manager_only",
            "created_by_user_id": pair.manager_user_id,
            "created_by_role": "manager",
        },
        FrozenClock(at=NOW),
    )

    result = handle_agenda_signal(
        db_session,
        {
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.manager_user_id,
            "signal_type": "drop",
            "item_id": item.id,
            "changes": None,
        },
    )

    from app.agenda.models import AgendaItem

    assert result["status"] == "ok"
    row = db_session.get(AgendaItem, item.id)
    assert row.status == "resolved"


def test_field_edit_with_invalid_visibility_raises(db_session, make_pair):
    pair = make_pair()
    item = _seed_item(db_session, pair)

    with pytest.raises(ValueError):
        handle_agenda_signal(
            db_session,
            {
                "report_user_id": pair.report_user_id,
                "acting_user_id": pair.report_user_id,
                "signal_type": "field_edit",
                "item_id": item.id,
                "changes": {"visibility": "report-only"},
            },
        )


def test_unknown_signal_type_is_rejected(db_session, make_pair):
    pair = make_pair()
    item = _seed_item(db_session, pair)

    result = handle_agenda_signal(
        db_session,
        {
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
            "signal_type": "snooze",
            "item_id": item.id,
            "changes": None,
        },
    )

    from app.agenda.models import AgendaItem

    assert result["status"] == "rejected"
    row = db_session.get(AgendaItem, item.id)
    assert row.status == "open"  # untouched
