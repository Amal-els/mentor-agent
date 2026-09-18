import datetime
import json
import uuid

import pytest
from cryptography.fernet import Fernet

from app.core.clock import FrozenClock
from app.ingest import google_credential_store as store


@pytest.fixture(autouse=True)
def _fresh_key(monkeypatch):
    from app.core import crypto

    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


@pytest.fixture(autouse=True)
def _tmp_tokens_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MENTOR_GOOGLE_TOKENS_DIR", str(tmp_path / "mentor-google-tokens"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    yield


def test_not_connected_returns_none(make_scope):
    scope = make_scope()
    assert store.is_google_connected(scope.session, scope.owner_user_id) is False
    assert store.materialize_calendar_token_path(scope.session, scope.owner_user_id) is None
    assert store.materialize_docs_profile(scope.session, scope.owner_user_id) is None


def test_store_then_materialize_calendar_writes_refresh_token(make_scope):
    scope = make_scope()
    clock = FrozenClock(at=datetime.datetime(2026, 8, 26, tzinfo=datetime.UTC))
    store.store_google_credential(
        scope.session, scope.owner_user_id, "rt-123", "amal@example.com", clock
    )

    path = store.materialize_calendar_token_path(scope.session, scope.owner_user_id)
    assert path is not None
    payload = json.loads(open(path, encoding="utf-8").read())
    assert payload["refresh_token"] == "rt-123"
    assert payload["token_type"] == "Bearer"
    # A deliberate placeholder, not the app ever seeing Google's real
    # access token — @cocal/google-calendar-mcp's own loadAllAccounts
    # silently skips any account with no access_token at all (confirmed
    # live and by reading its source), so this must be present and
    # already-expired (expiry_date in the past) to force an immediate
    # real refresh via refresh_token rather than being mistaken for a
    # still-valid token.
    assert payload["access_token"]
    assert payload["expiry_date"] < 1_000_000_000_000


def test_materialize_docs_profile_is_sanitized_and_written(make_scope):
    # A fixed id would collide on users_pkey across repeated runs against
    # this shared, non-rolled-back dev Postgres (same reasoning every
    # other make_scope() caller already follows) — unique prefix, unsafe
    # suffix, so this still exercises sanitize_profile's character
    # substitution without needing a fresh DB every time.
    owner_user_id = f"{uuid.uuid4()}.def!123"
    scope = make_scope(owner_user_id=owner_user_id)
    clock = FrozenClock(at=datetime.datetime(2026, 8, 26, tzinfo=datetime.UTC))
    store.store_google_credential(
        scope.session, scope.owner_user_id, "rt-456", None, clock
    )

    profile = store.materialize_docs_profile(scope.session, scope.owner_user_id)
    assert profile == store.sanitize_profile(owner_user_id)
    assert profile == owner_user_id.replace(".", "_").replace("!", "_")

    import os
    from pathlib import Path

    token_path = (
        Path(os.environ["XDG_CONFIG_HOME"]) / "google-docs-mcp" / profile / "token.json"
    )
    payload = json.loads(token_path.read_text())
    assert payload["refresh_token"] == "rt-456"


def test_disconnect_removes_credential(make_scope):
    scope = make_scope()
    clock = FrozenClock(at=datetime.datetime(2026, 8, 26, tzinfo=datetime.UTC))
    store.store_google_credential(scope.session, scope.owner_user_id, "rt", None, clock)
    assert store.is_google_connected(scope.session, scope.owner_user_id) is True

    assert store.disconnect_google_credential(scope.session, scope.owner_user_id) is True
    assert store.is_google_connected(scope.session, scope.owner_user_id) is False
    assert store.disconnect_google_credential(scope.session, scope.owner_user_id) is False


def test_store_twice_re_encrypts_latest_token(make_scope):
    scope = make_scope()
    clock = FrozenClock(at=datetime.datetime(2026, 8, 26, tzinfo=datetime.UTC))
    store.store_google_credential(scope.session, scope.owner_user_id, "rt-old", None, clock)
    store.store_google_credential(scope.session, scope.owner_user_id, "rt-new", None, clock)

    path = store.materialize_calendar_token_path(scope.session, scope.owner_user_id)
    payload = json.loads(open(path, encoding="utf-8").read())
    assert payload["refresh_token"] == "rt-new"
