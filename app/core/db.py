import logging
import time
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


# REAL CHANGE (requested: intermittent 500s across every route — "why
# doesn't the graph load" turned out to have nothing to do with the
# graph; a real live server log showed
# "psycopg.OperationalError: failed to resolve host
# 'aws-1-eu-west-1.pooler.supabase.com': [Errno 11001] getaddrinfo
# failed" hitting whichever endpoint happened to need a fresh DB
# connection at that moment — 62 times across one session, spread across
# agenda-payload, goals, notes, dossier-payload, checklist, notifications,
# and graph). pool_pre_ping=True (already set below) only re-validates an
# EXISTING pooled connection before reuse — it does nothing for a brand-
# new connection attempt whose own DNS lookup fails, which is exactly
# this error. A few short, bounded retries around the actual DBAPI
# connect call turns a single transient DNS blip into a silent retry
# instead of a 500 surfaced all the way to the UI.
_CONNECT_RETRY_ATTEMPTS = 3
_CONNECT_RETRY_BACKOFF_SECONDS = 0.3


def _make_retrying_creator(url: str):
    """Builds a `creator` callable for create_engine's own `creator` param
    — SQLAlchemy calls this (via the pool) every time it needs a genuinely
    NEW DBAPI connection, not just once at startup, so this covers every
    such attempt for the engine's whole lifetime, not just the first.
    Uses the dialect's own create_connect_args (the same machinery
    create_engine itself uses internally) to build the real connect
    args/kwargs from the URL — so this stays correct for whatever's
    already encoded in it (sslmode, etc.) rather than hand-rebuilding a
    connection string."""
    parsed = make_url(url)
    dialect = parsed.get_dialect()()
    dbapi = dialect.import_dbapi()
    cargs, cparams = dialect.create_connect_args(parsed)

    def _creator():
        last_exc: Exception | None = None
        for attempt in range(_CONNECT_RETRY_ATTEMPTS):
            try:
                return dbapi.connect(*cargs, **cparams)
            except Exception as exc:  # noqa: BLE001 - DBAPI raises its own exception types
                last_exc = exc
                if attempt < _CONNECT_RETRY_ATTEMPTS - 1:
                    logger.warning(
                        "db connect attempt %s/%s failed (%s), retrying in %ss",
                        attempt + 1,
                        _CONNECT_RETRY_ATTEMPTS,
                        exc,
                        _CONNECT_RETRY_BACKOFF_SECONDS * (attempt + 1),
                    )
                    time.sleep(_CONNECT_RETRY_BACKOFF_SECONDS * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    return _creator


@lru_cache(maxsize=None)
def _get_real_engine(url: str) -> Engine:
    return create_engine(
        url,
        creator=_make_retrying_creator(url),
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=1,
    )


def get_engine(url: str) -> Engine:
    """Every caller across this codebase calls get_engine() fresh on each
    request/poll tick/CLI invocation rather than holding a reference, so
    without caching here each of those calls opened its own brand-new
    connection pool instead of sharing one — the actual cause of exhausting
    Supabase's session-mode pooler (EMAXCONNSESSION, pool_size: 15) with only
    a handful of local dev processes running. _get_real_engine's lru_cache
    makes repeated calls with the same url return the same Engine, so each
    process now holds exactly one pool for its whole lifetime.

    sqlite is deliberately excluded from that cache: tests/unit/core/
    test_scope.py and test_db.py call get_engine("sqlite:///:memory:")
    specifically because each create_engine() call gives them a fresh,
    isolated in-memory DB for test isolation — caching that would leak rows
    across tests sharing the same fixture."""
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    return _get_real_engine(url)


def get_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)
