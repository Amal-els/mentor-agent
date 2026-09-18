import datetime
import uuid

import pytest
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import Base, get_engine, get_session_factory
from app.core.models import User
from app.core.scope import OwnerScope


@pytest.fixture
def pg_session() -> Session:
    engine = get_engine(get_settings().database_url)
    Base.metadata.create_all(engine)
    session: Session = get_session_factory(engine)()
    yield session
    session.close()


@pytest.fixture
def make_scope(pg_session):
    def _make(owner_user_id: str | None = None) -> OwnerScope:
        uid = owner_user_id or str(uuid.uuid4())
        pg_session.add(User(id=uid, created_at=datetime.datetime.now(datetime.UTC)))
        pg_session.commit()
        return OwnerScope(owner_user_id=uid, session=pg_session)

    return _make
