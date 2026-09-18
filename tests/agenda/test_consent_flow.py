import datetime

from app.agenda.scope import resolve_pair_scope
from app.agenda.store import append_agenda_item, mark_resolved, update_agenda_item
from app.core.clock import FrozenClock

NOW = datetime.datetime(2026, 8, 14, tzinfo=datetime.UTC)


def _scope(db_session, pair):
    return resolve_pair_scope(db_session, pair.report_user_id, pair.report_user_id)


def _seeded_item(scope, clock):
    return append_agenda_item(
        scope,
        {
            "text": "revisit the roadmap",
            "source": "manual",
            "source_link": None,
            "visibility": "shared",
            "created_by_user_id": scope.report_user_id,
            "created_by_role": "report",
        },
        clock,
    )


def test_crossing_threshold_flips_status_to_pending_consent(db_session, make_pair):
    from app.agenda import config
    from app.agenda.models import AgendaItem

    pair = make_pair()
    scope = _scope(db_session, pair)
    item = _seeded_item(scope, FrozenClock(at=NOW))
    row = db_session.get(AgendaItem, item.id)
    row.surfaced_count = config.PENDING_CONSENT_THRESHOLD - 1
    db_session.commit()

    updated = update_agenda_item(
        scope,
        item.id,
        {"surfaced_count": config.PENDING_CONSENT_THRESHOLD},
        scope.report_user_id,
        FrozenClock(at=NOW),
    )

    assert updated.status == "pending_consent"


def test_keep_resets_surfaced_count_and_reopens(db_session, make_pair):
    from app.agenda import config
    from app.agenda.models import AgendaItem

    pair = make_pair()
    scope = _scope(db_session, pair)
    item = _seeded_item(scope, FrozenClock(at=NOW))
    row = db_session.get(AgendaItem, item.id)
    row.status = "pending_consent"
    row.surfaced_count = config.PENDING_CONSENT_THRESHOLD
    db_session.commit()

    updated = update_agenda_item(
        scope,
        item.id,
        {"status": "open", "surfaced_count": 0},
        scope.report_user_id,
        FrozenClock(at=NOW),
    )

    assert updated.status == "open"
    assert updated.surfaced_count == 0


def test_drop_resolves_with_a_consent_marker(db_session, make_pair):
    pair = make_pair()
    scope = _scope(db_session, pair)
    item = _seeded_item(scope, FrozenClock(at=NOW))

    resolved = mark_resolved(scope, item.id, scope.report_user_id, FrozenClock(at=NOW))

    assert resolved.status == "resolved"
    assert resolved.resolved_at == NOW


def test_every_mutation_appends_history(db_session, make_pair):
    from app.agenda.models import AgendaItemHistory

    pair = make_pair()
    scope = _scope(db_session, pair)
    item = _seeded_item(scope, FrozenClock(at=NOW))

    update_agenda_item(
        scope,
        item.id,
        {"text": "revisit the roadmap (v2)"},
        scope.report_user_id,
        FrozenClock(at=NOW),
    )
    mark_resolved(scope, item.id, scope.report_user_id, FrozenClock(at=NOW))

    history = (
        db_session.query(AgendaItemHistory).filter_by(agenda_item_id=item.id).all()
    )
    # 1 for creation (Task 8) + 1 for the update + 1 for mark_resolved
    assert len(history) == 3
