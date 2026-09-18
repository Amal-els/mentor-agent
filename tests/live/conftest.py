import pytest
from dotenv import load_dotenv
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import Base, get_engine, get_session_factory

load_dotenv()


@pytest.fixture(autouse=True)
def _no_real_llm_calls_by_default():
    """Overrides tests/conftest.py's blanket patch — this directory exists
    specifically to exercise the real LLM path (gated behind
    @pytest.mark.live), so the parent fixture's fallback-forcing must not
    apply here."""
    yield


@pytest.fixture(autouse=True)
def _no_real_connector_creds_by_default():
    """Overrides tests/conftest.py's env-var stripping — this directory
    exists specifically to exercise real connector credentials."""
    yield


@pytest.fixture
def pg_session() -> Session:
    engine = get_engine(get_settings().database_url)
    Base.metadata.create_all(engine)
    session: Session = get_session_factory(engine)()
    yield session
    session.close()
