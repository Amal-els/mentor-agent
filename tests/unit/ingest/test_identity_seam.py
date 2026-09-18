import datetime
import uuid
from unittest.mock import MagicMock, patch

from app.core.clock import FrozenClock
from app.identity.models import Identity, Person
from app.ingest.identity_seam import resolve_actor, resolve_slack_actor


def _make_person(scope, **overrides):
    person = Person(
        id=str(uuid.uuid4()),
        canonical_name=overrides.get("canonical_name", "Sarah Ben Youssef"),
        primary_email=overrides.get("primary_email"),
        is_self=overrides.get("is_self", False),
        is_active=overrides.get("is_active", True),
        roster_source=overrides.get("roster_source"),
        created_at=datetime.datetime.now(datetime.UTC),
    )
    scope.add(person)
    scope.commit()
    return person


def test_resolves_by_primary_email_match(make_scope):
    scope = make_scope()
    person = _make_person(scope, primary_email="sarah@acme.com")

    person_id = resolve_actor(scope, "calendar:sarah@acme.com")

    assert person_id == person.id


def test_unmatched_reference_key_returns_none(make_scope):
    scope = make_scope()
    _make_person(scope, primary_email="sarah@acme.com")

    person_id = resolve_actor(scope, "calendar:nobody@acme.com")

    assert person_id is None


def test_opaque_external_id_with_no_email_shape_returns_none(make_scope):
    scope = make_scope()

    # "usr_amal" is not shaped like an email — no primary-email match is
    # attempted, and there's no cached Identity, so it must stay unresolved
    # (raw handle) rather than guess.
    person_id = resolve_actor(scope, "linear:usr_amal")

    assert person_id is None


def test_cached_identity_hits_on_exact_reference_key(pg_session, make_scope):
    scope = make_scope()
    person = _make_person(scope, primary_email="sarah@acme.com")
    pg_session.add(
        Identity(
            id=str(uuid.uuid4()),
            owner_user_id=scope.owner_user_id,
            person_id=person.id,
            source="linear",
            external_id="U1",
            reference_key="linear:U1",
            key_version=1,
            tier=0,
            confidence="verified",
            verified_by="auto",
            handle=None,
            email=None,
            display_name=None,
            provenance={},
            first_seen=datetime.datetime.now(datetime.UTC),
            last_seen=datetime.datetime.now(datetime.UTC),
        )
    )
    pg_session.commit()

    person_id = resolve_actor(scope, "linear:U1")

    assert person_id == person.id


def test_does_not_write_any_identity_row(make_scope, pg_session):
    scope = make_scope()
    _make_person(scope, primary_email="sarah@acme.com")

    resolve_actor(scope, "calendar:sarah@acme.com")

    rows = pg_session.execute(scope.query(Identity)).scalars().all()
    assert rows == []


# ── resolve_slack_actor tests ─────────────────────────────────────────────────

_CLOCK = FrozenClock(datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC))


def test_resolve_slack_actor_hits_identity_cache(pg_session, make_scope):
    """When an Identity row already exists for a slack user ID, no API call
    is made and the cached person_id is returned immediately."""
    scope = make_scope()
    person = _make_person(scope, primary_email="amal@acme.com")
    pg_session.add(
        Identity(
            id=str(uuid.uuid4()),
            owner_user_id=scope.owner_user_id,
            person_id=person.id,
            source="slack",
            external_id="U08AMALID",
            reference_key="slack:U08AMALID",
            key_version=1,
            tier=0,
            confidence="verified",
            verified_by="auto",
            handle=None,
            email="amal@acme.com",
            display_name="Amal Bahri",
            provenance={},
            first_seen=datetime.datetime.now(datetime.UTC),
            last_seen=datetime.datetime.now(datetime.UTC),
        )
    )
    pg_session.commit()

    with patch("app.ingest.identity_seam.os.environ.get", return_value="xoxb-fake"):
        with patch("app.ingest.identity_seam.WebClient") as mock_wc:
            result = resolve_slack_actor(scope, "U08AMALID", _CLOCK)

    assert result == person.id
    mock_wc.assert_not_called()  # cache hit — no API call


def test_resolve_slack_actor_api_email_match_writes_identity(make_scope, pg_session):
    """When no cached Identity exists, users.info is called, the email matches
    a roster Person, and a new Identity row is written and returned."""
    scope = make_scope()
    person = _make_person(scope, primary_email="amal@acme.com", canonical_name="Amal Bahri")

    fake_users_info = MagicMock(return_value={
        "user": {
            "real_name": "Amal Bahri",
            "profile": {
                "email": "amal@acme.com",
                "display_name": "Amal Bahri",
                "real_name": "Amal Bahri",
            },
        }
    })

    with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-fake"}):
        with patch("app.ingest.identity_seam.WebClient") as mock_wc:
            mock_wc.return_value.users_info = fake_users_info
            result = resolve_slack_actor(scope, "U08AMALID", _CLOCK)

    assert result == person.id

    # Identity row must be written so subsequent calls hit the cache
    identity = pg_session.execute(
        scope.query(Identity).where(Identity.external_id == "U08AMALID")
    ).scalar_one_or_none()
    assert identity is not None
    assert identity.person_id == person.id
    assert identity.source == "slack"


def test_resolve_slack_actor_api_failure_returns_none(make_scope):
    """When users.info raises (Slack API down, bad token, etc.), the function
    returns None gracefully without crashing."""
    scope = make_scope()

    with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-fake"}):
        with patch("app.ingest.identity_seam.WebClient") as mock_wc:
            mock_wc.return_value.users_info.side_effect = RuntimeError("API error")
            result = resolve_slack_actor(scope, "U08AMALID", _CLOCK)

    assert result is None


def test_resolve_slack_actor_no_email_in_profile_returns_none(make_scope):
    """When users.info succeeds but the profile has no email (e.g. a guest
    or a restricted account), the function returns None."""
    scope = make_scope()

    fake_users_info = MagicMock(return_value={
        "user": {"real_name": "Guest", "profile": {"display_name": "Guest"}}
    })

    with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-fake"}):
        with patch("app.ingest.identity_seam.WebClient") as mock_wc:
            mock_wc.return_value.users_info = fake_users_info
            result = resolve_slack_actor(scope, "U08GUEST", _CLOCK)

    assert result is None


def test_resolve_slack_actor_no_bot_token_returns_none(make_scope):
    """If SLACK_BOT_TOKEN is not set, the function returns None without making
    any network call."""
    scope = make_scope()

    with patch.dict("os.environ", {}, clear=True):
        with patch("app.ingest.identity_seam.WebClient") as mock_wc:
            result = resolve_slack_actor(scope, "U08AMALID", _CLOCK)

    assert result is None
    mock_wc.assert_not_called()
