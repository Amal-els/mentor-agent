import datetime
import uuid

from app.agenda.models import AgendaItem
from app.agenda.scope import resolve_pair_scope


def test_report_gets_a_scope(db_session, make_pair):
    pair = make_pair()

    scope = resolve_pair_scope(db_session, pair.report_user_id, pair.report_user_id)

    assert scope is not None
    assert scope.report_user_id == pair.report_user_id


def test_current_manager_gets_a_scope(db_session, make_pair):
    pair = make_pair()

    scope = resolve_pair_scope(db_session, pair.report_user_id, pair.manager_user_id)

    assert scope is not None


def test_former_manager_gets_no_scope(db_session, make_pair, make_user):
    now = datetime.datetime.now(datetime.UTC)
    former_manager = make_user()
    pair = make_pair(ended_at=now)  # this Pair is already ended
    # give the report a NEW current pair so the report/current-manager
    # branches don't accidentally also match the former manager's id
    make_pair(report_user_id=pair.report_user_id)

    scope = resolve_pair_scope(db_session, pair.report_user_id, former_manager)

    # former_manager is nobody's manager here at all — the real regression
    # case is the *actual* former manager of this pair:
    scope_for_actual_former_manager = resolve_pair_scope(
        db_session, pair.report_user_id, pair.manager_user_id
    )
    assert scope is None
    assert scope_for_actual_former_manager is None


def test_stranger_gets_no_scope(db_session, make_pair, make_user):
    pair = make_pair()
    stranger = make_user()

    scope = resolve_pair_scope(db_session, pair.report_user_id, stranger)

    assert scope is None


def test_query_filters_by_report_user_id(db_session, make_pair):
    pair_a = make_pair()
    pair_b = make_pair()
    scope_a = resolve_pair_scope(
        db_session, pair_a.report_user_id, pair_a.report_user_id
    )
    scope_b = resolve_pair_scope(
        db_session, pair_b.report_user_id, pair_b.report_user_id
    )

    scope_a.add(
        AgendaItem(
            id=str(uuid.uuid4()),
            pair_id=pair_a.id,
            text="only visible to pair A",
            source="manual",
            source_link=None,
            created_at=datetime.datetime.now(datetime.UTC),
            created_by_user_id=pair_a.report_user_id,
            created_by_role="report",
        )
    )
    scope_a.commit()

    items_b = db_session.execute(scope_b.query(AgendaItem)).scalars().all()

    assert items_b == []


def test_add_rejects_mismatched_report_user_id(db_session, make_pair):
    pair = make_pair()
    scope = resolve_pair_scope(db_session, pair.report_user_id, pair.report_user_id)
    other_report_id = str(uuid.uuid4())

    item = AgendaItem(
        id=str(uuid.uuid4()),
        report_user_id=other_report_id,  # deliberately wrong
        pair_id=pair.id,
        text="mismatched",
        source="manual",
        source_link=None,
        created_at=datetime.datetime.now(datetime.UTC),
        created_by_user_id=pair.report_user_id,
        created_by_role="report",
    )
    import pytest

    with pytest.raises(ValueError):
        scope.add(item)
