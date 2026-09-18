import pytest

from app.core.db import _make_retrying_creator, get_engine


def test_get_engine_enables_pool_pre_ping_for_postgres():
    engine = get_engine(
        "postgresql+psycopg://mentor:mentor_dev_only@localhost:5432/mentor"
    )
    assert engine.pool._pre_ping is True


def test_get_engine_sqlite_still_works_without_pool_tuning():
    engine = get_engine("sqlite:///:memory:")
    # SQLite's SingletonThreadPool/StaticPool has no _pre_ping concept the same way;
    # this just confirms get_engine doesn't raise for the single-user-local-install path
    assert engine is not None


# ---- _make_retrying_creator (REAL CHANGE: a transient DNS failure
# reaching Supabase — "failed to resolve host ...: [Errno 11001]
# getaddrinfo failed" — was surfacing as a hard 500 on whichever route
# happened to need a fresh DB connection at that moment, 62 times across
# one real session) ----


class _FakeDialect:
    def __init__(self, dbapi):
        self._dbapi = dbapi

    def import_dbapi(self):
        return self._dbapi

    def create_connect_args(self, url):
        return ((), {})


def _fake_make_url(dbapi):
    """Stands in for sqlalchemy.engine.make_url — returns an object whose
    .get_dialect() gives back a class that, once instantiated, hands out
    the given fake dbapi. Real dialect resolution needs a real DB driver
    installed and reachable; this isolates _make_retrying_creator's own
    retry logic from that entirely."""
    class _FakeParsedURL:
        def get_dialect(self):
            return lambda: _FakeDialect(dbapi)

    return lambda url: _FakeParsedURL()


def test_retrying_creator_retries_transient_failures_then_succeeds(monkeypatch):
    attempts = {"n": 0}

    class _FakeDBAPI:
        def connect(self, *args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("simulated transient failure")
            return "fake-connection"

    monkeypatch.setattr(
        "app.core.db.make_url", _fake_make_url(_FakeDBAPI())
    )
    monkeypatch.setattr("app.core.db.time.sleep", lambda seconds: None)

    creator = _make_retrying_creator("postgresql+psycopg://fake")
    result = creator()

    assert result == "fake-connection"
    assert attempts["n"] == 3


def test_retrying_creator_raises_the_real_error_after_exhausting_attempts(
    monkeypatch,
):
    class _FakeDBAPI:
        def connect(self, *args, **kwargs):
            raise RuntimeError("always fails")

    monkeypatch.setattr(
        "app.core.db.make_url", _fake_make_url(_FakeDBAPI())
    )
    monkeypatch.setattr("app.core.db.time.sleep", lambda seconds: None)

    creator = _make_retrying_creator("postgresql+psycopg://fake")

    with pytest.raises(RuntimeError, match="always fails"):
        creator()


def test_retrying_creator_succeeds_immediately_without_any_retry(monkeypatch):
    attempts = {"n": 0}

    class _FakeDBAPI:
        def connect(self, *args, **kwargs):
            attempts["n"] += 1
            return "fake-connection"

    monkeypatch.setattr(
        "app.core.db.make_url", _fake_make_url(_FakeDBAPI())
    )

    creator = _make_retrying_creator("postgresql+psycopg://fake")
    result = creator()

    assert result == "fake-connection"
    assert attempts["n"] == 1  # never had to sleep/retry at all
