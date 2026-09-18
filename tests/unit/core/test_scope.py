import pytest
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm import Session as SASession

from app.core.db import Base, get_engine, get_session_factory
from app.core.scope import OwnerScope


class _Widget(Base):
    """Test-only owned model — not part of the app schema."""

    __tablename__ = "test_widgets_for_scope"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String)


@pytest.fixture
def session() -> SASession:
    engine = get_engine("sqlite:///:memory:")
    # Scope create_all to this test's own table — Base.metadata is process-wide and,
    # once app.identity.models is imported anywhere in the test session, also holds
    # Postgres-only JSONB columns that SQLite's DDL compiler cannot render.
    Base.metadata.create_all(engine, tables=[_Widget.__table__])
    return get_session_factory(engine)()


def test_query_filters_to_the_scoped_owner(session):
    session.add_all(
        [
            _Widget(id="1", owner_user_id="user-a", name="a-widget"),
            _Widget(id="2", owner_user_id="user-b", name="b-widget"),
        ]
    )
    session.commit()

    scope = OwnerScope(owner_user_id="user-a", session=session)
    rows = session.execute(scope.query(_Widget)).scalars().all()

    assert [r.name for r in rows] == ["a-widget"]


def test_add_stamps_owner_user_id(session):
    scope = OwnerScope(owner_user_id="user-a", session=session)
    widget = _Widget(id="3", name="stamped")

    scope.add(widget)
    scope.commit()

    fetched = session.get(_Widget, "3")
    assert fetched.owner_user_id == "user-a"


def test_add_rejects_mismatched_owner_already_set(session):
    scope = OwnerScope(owner_user_id="user-a", session=session)
    widget = _Widget(id="4", owner_user_id="user-b", name="mismatch")

    with pytest.raises(ValueError):
        scope.add(widget)
