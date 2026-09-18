import datetime
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.agenda.models import Pair


def test_only_one_active_pair_per_report(db_session, make_user):
    report = make_user()
    manager_a = make_user()
    manager_b = make_user()
    now = datetime.datetime.now(datetime.UTC)
    db_session.add(
        Pair(
            id=str(uuid.uuid4()),
            report_user_id=report,
            manager_user_id=manager_a,
            started_at=now,
            ended_at=None,
        )
    )
    db_session.commit()

    db_session.add(
        Pair(
            id=str(uuid.uuid4()),
            report_user_id=report,
            manager_user_id=manager_b,
            started_at=now,
            ended_at=None,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_ended_pairs_do_not_collide(db_session, make_user):
    report = make_user()
    manager_a = make_user()
    manager_b = make_user()
    now = datetime.datetime.now(datetime.UTC)
    db_session.add(
        Pair(
            id=str(uuid.uuid4()),
            report_user_id=report,
            manager_user_id=manager_a,
            started_at=now,
            ended_at=now,
        )
    )
    db_session.commit()
    db_session.add(
        Pair(
            id=str(uuid.uuid4()),
            report_user_id=report,
            manager_user_id=manager_b,
            started_at=now,
            ended_at=None,
        )
    )
    db_session.commit()  # no error — ended_at IS NOT NULL, partial index doesn't apply

    count = db_session.query(Pair).filter_by(report_user_id=report).count()
    assert count == 2
