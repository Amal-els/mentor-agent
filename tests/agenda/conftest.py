import datetime
import uuid

import pytest
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import Base, get_engine, get_session_factory
from app.core.models import User


@pytest.fixture
def pg_session() -> Session:
    engine = get_engine(get_settings().database_url)
    Base.metadata.create_all(engine)
    session: Session = get_session_factory(engine)()
    yield session
    session.close()


@pytest.fixture
def db_session(pg_session) -> Session:
    return pg_session


@pytest.fixture
def make_user(pg_session):
    def _make(user_id: str | None = None, **overrides) -> str:
        uid = user_id or str(uuid.uuid4())
        pg_session.add(
            User(id=uid, created_at=datetime.datetime.now(datetime.UTC), **overrides)
        )
        pg_session.commit()
        return uid

    return _make


@pytest.fixture
def make_pair(pg_session, make_user):
    def _make(
        report_user_id: str | None = None,
        manager_user_id: str | None = None,
        started_at: datetime.datetime | None = None,
        ended_at: datetime.datetime | None = None,
    ):
        from app.agenda.models import Pair

        report = report_user_id or make_user()
        manager = manager_user_id or make_user()
        pair = Pair(
            id=str(uuid.uuid4()),
            report_user_id=report,
            manager_user_id=manager,
            started_at=started_at or datetime.datetime.now(datetime.UTC),
            ended_at=ended_at,
        )
        pg_session.add(pair)
        pg_session.commit()
        return pair

    return _make
