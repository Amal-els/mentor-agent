import datetime

from app.agenda.scope import resolve_pair_scope
from app.agenda.store import append_ledger_item
from app.core.clock import FrozenClock
from app.core.models import Commitment
from app.core.scope import OwnerScope

NOW = datetime.datetime(2026, 8, 14, tzinfo=datetime.UTC)


def test_commitment_kind_writes_to_commitment_table_and_mirrors_a_pointer(
    db_session, make_pair
):
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)

    item = append_ledger_item(
        pair_scope,
        owner_scope,
        "commitment",
        {
            "description": "send the deck",
            "promised_to_person_id": None,
            "due_at": None,
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        FrozenClock(at=NOW),
    )

    commitments = (
        db_session.query(Commitment).filter_by(owner_user_id=pair.report_user_id).all()
    )
    assert len(commitments) == 1
    assert item.source == "commitment_ledger"
    assert item.source_link == commitments[0].id


def test_accomplishment_kind_writes_to_accomplishment_table_and_mirrors_a_pointer(
    db_session, make_pair
):
    from app.agenda.models import Accomplishment

    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)

    item = append_ledger_item(
        pair_scope,
        owner_scope,
        "accomplishment",
        {
            "description": "shipped the migration",
            "occurred_at": NOW.isoformat(),
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        FrozenClock(at=NOW),
    )

    rows = (
        db_session.query(Accomplishment)
        .filter_by(owner_user_id=pair.report_user_id)
        .all()
    )
    assert len(rows) == 1
    assert item.source == "accomplishment_ledger"
    assert item.source_link == rows[0].id


def test_unrecognized_kind_raises_value_error_and_writes_nothing(db_session, make_pair):
    # jira_blocker/slack_q go through append_agenda_item directly (Task 8),
    # never through append_ledger_item, which only understands
    # commitment/accomplishment. Confirms the rejection is clean: no
    # Commitment/Accomplishment/AgendaItem row is written as a side effect
    # before the raise.
    import pytest

    from app.agenda.models import Accomplishment, AgendaItem

    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)

    with pytest.raises(ValueError):
        append_ledger_item(
            pair_scope,
            owner_scope,
            "jira_blocker",
            {
                "description": "should never be written",
                "created_by_user_id": pair.report_user_id,
                "created_by_role": "report",
            },
            FrozenClock(at=NOW),
        )

    assert (
        db_session.query(Commitment)
        .filter_by(owner_user_id=pair.report_user_id)
        .count()
        == 0
    )
    assert (
        db_session.query(Accomplishment)
        .filter_by(owner_user_id=pair.report_user_id)
        .count()
        == 0
    )
    assert (
        db_session.query(AgendaItem)
        .filter_by(report_user_id=pair.report_user_id)
        .count()
        == 0
    )
