import pytest
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import Base, get_engine, get_session_factory


@pytest.fixture
def pg_session() -> Session:
    engine = get_engine(get_settings().database_url)
    Base.metadata.create_all(engine)
    session: Session = get_session_factory(engine)()
    yield session
    session.close()
