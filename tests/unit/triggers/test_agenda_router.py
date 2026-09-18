import datetime
import uuid
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agenda.scope import resolve_pair_scope
from app.agenda.store import append_agenda_item
from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.triggers.agenda.agenda_router import router
from app.triggers.agenda.auth_router import router as auth_router
from app.triggers.agenda.goals_router import router as goals_router
from app.triggers.agenda.ledgers_router import router as ledgers_router
from app.triggers.agenda.preferences_router import router as preferences_router
from app.triggers.agenda.setup_router import router as setup_router

NOW = datetime.datetime(2026, 8, 14, tzinfo=datetime.UTC)
TOKEN = "test-agenda-webhook-token"


def _client() -> TestClient:
    # agenda_router.py used to be one ~1300-line file covering setup,
    # login, agenda orchestration, preferences, goals, and ledgers; it was
    # split into one router per feature area (see each router module's own
    # docstring). This client still needs all of them mounted together —
    # these tests exercise routes that now live across all six files.
    app = FastAPI()
    app.include_router(router)
    app.include_router(setup_router)
    app.include_router(auth_router)
    app.include_router(preferences_router)
    app.include_router(goals_router)
    app.include_router(ledgers_router)
    return TestClient(app)


class _Settings:
    def __init__(
        self, agenda_webhook_token: str = TOKEN, database_url: str | None = None
    ):
        self.agenda_webhook_token = agenda_webhook_token
        self.database_url = database_url or get_settings().database_url


def _seed_item(db_session, pair):
    scope = resolve_pair_scope(db_session, pair.report_user_id, pair.report_user_id)
    return append_agenda_item(
        scope,
        {
            "text": "revisit the roadmap",
            "source": "manual",
            "source_link": None,
            "visibility": "shared",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        FrozenClock(at=NOW),
    )


def _set_secret(db_session, user_id: str, secret: str) -> str:
    """Sets agenda_client_secret on user_id and returns the value actually
    stored. Suffixed with user_id (unique per make_user call) since
    agenda_client_secret is globally unique and these tests share a live
    Postgres across runs with no per-test rollback — a literal constant
    like "manager-secret" would collide across tests/runs the same way
    tests/agenda/test_store.py's dedup tests avoid reusing ids."""
    from app.core.models import User

    value = f"{secret}-{user_id}"
    row = db_session.get(User, user_id)
    row.agenda_client_secret = value
    db_session.commit()
    return value


# ---- POST /webhooks/request-setup-link + /webhooks/complete-setup ----


def test_request_setup_link_sets_a_token_and_sends_email(monkeypatch, db_session):
    from app.core.models import User

    monkeypatch.setattr("app.triggers.agenda.setup_router.get_settings", lambda: _Settings())
    email = f"setup-{uuid.uuid4()}@example.com"
    uid = str(uuid.uuid4())
    db_session.add(
        User(
            id=uid,
            created_at=datetime.datetime.now(datetime.UTC),
            notion_owner_email=email,
        )
    )
    db_session.commit()

    sent = {}

    class _FakeEmailDeliverer:
        def send(self, to, subject, body):
            sent["to"] = to
            sent["body"] = body
            return {"sent": True}

    monkeypatch.setattr(
        "app.triggers.agenda.setup_router.EmailDeliverer", _FakeEmailDeliverer
    )

    response = _client().post(
        "/webhooks/request-setup-link",
        json={"email": email},
        headers={"X-Agenda-Token": TOKEN},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert sent["to"] == email
    assert "/ui?setup_token=" in sent["body"]

    db_session.refresh(db_session.get(User, uid))
    row = db_session.get(User, uid)
    assert row.setup_token is not None
    assert row.setup_token_expires_at is not None


def test_request_setup_link_logs_a_warning_when_the_email_send_fails(
    monkeypatch, db_session, caplog
):
    # EmailDeliverer.send already catches every failure into a
    # {"sent": False, "reason": ...} return instead of raising -- before
    # this fix, issue_setup_link discarded that return value entirely, so
    # a broken send (expired OAuth grant, MCP not configured) was
    # completely invisible: the route always replies "ok" regardless.
    from app.core.models import User

    monkeypatch.setattr("app.triggers.agenda.setup_router.get_settings", lambda: _Settings())
    email = f"setup-{uuid.uuid4()}@example.com"
    uid = str(uuid.uuid4())
    db_session.add(
        User(
            id=uid,
            created_at=datetime.datetime.now(datetime.UTC),
            notion_owner_email=email,
        )
    )
    db_session.commit()

    class _FailingEmailDeliverer:
        def send(self, to, subject, body):
            return {"sent": False, "reason": "Gmail rejected the message: invalid_grant"}

    monkeypatch.setattr(
        "app.triggers.agenda.setup_router.EmailDeliverer", _FailingEmailDeliverer
    )

    with caplog.at_level("WARNING", logger="app.triggers.agenda.agenda_router"):
        response = _client().post(
            "/webhooks/request-setup-link",
            json={"email": email},
            headers={"X-Agenda-Token": TOKEN},
        )

    # Still the generic "ok" -- the route's own docstring requires this
    # regardless of send outcome, to avoid using it to probe registered
    # emails. The warning is the only signal a failure happened.
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert any(
        "issue_setup_link" in r.message and "invalid_grant" in r.message
        for r in caplog.records
    )


def test_request_setup_link_matches_email_case_insensitively(monkeypatch, db_session):
    from app.core.models import User

    monkeypatch.setattr("app.triggers.agenda.setup_router.get_settings", lambda: _Settings())
    email = f"setup-{uuid.uuid4()}@example.com"
    uid = str(uuid.uuid4())
    db_session.add(
        User(
            id=uid,
            created_at=datetime.datetime.now(datetime.UTC),
            notion_owner_email=email,
        )
    )
    db_session.commit()

    sent = {}

    class _FakeEmailDeliverer:
        def send(self, to, subject, body):
            sent["to"] = to
            return {"sent": True}

    monkeypatch.setattr(
        "app.triggers.agenda.setup_router.EmailDeliverer", _FakeEmailDeliverer
    )

    response = _client().post(
        "/webhooks/request-setup-link",
        json={"email": email.upper()},
        headers={"X-Agenda-Token": TOKEN},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert sent["to"] == email

    row = db_session.get(User, uid)
    assert row.setup_token is not None


def test_request_setup_link_is_generic_for_an_unregistered_email(
    monkeypatch, db_session
):
    monkeypatch.setattr("app.triggers.agenda.setup_router.get_settings", lambda: _Settings())

    response = _client().post(
        "/webhooks/request-setup-link",
        json={"email": "nobody@example.com"},
        headers={"X-Agenda-Token": TOKEN},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_complete_setup_redeems_a_valid_token(monkeypatch, db_session):
    from app.core.models import User

    monkeypatch.setattr("app.triggers.agenda.setup_router.get_settings", lambda: _Settings())
    uid = str(uuid.uuid4())
    db_session.add(
        User(
            id=uid,
            created_at=datetime.datetime.now(datetime.UTC),
            notion_owner_email=f"setup-{uid}@example.com",
            notion_display_name="Setup Person",
            setup_token=f"tok-{uid}",
            setup_token_expires_at=datetime.datetime.now(datetime.UTC)
            + datetime.timedelta(hours=1),
        )
    )
    db_session.commit()

    response = _client().post(
        "/webhooks/complete-setup",
        json={"token": f"tok-{uid}"},
        headers={"X-Agenda-Token": TOKEN},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["display_name"] == "Setup Person"
    assert body["secret"]

    row = db_session.get(User, uid)
    assert row.setup_token is None
    assert row.agenda_client_secret == body["secret"]


def test_complete_setup_rejects_an_expired_token(monkeypatch, db_session):
    from app.core.models import User

    monkeypatch.setattr("app.triggers.agenda.setup_router.get_settings", lambda: _Settings())
    uid = str(uuid.uuid4())
    db_session.add(
        User(
            id=uid,
            created_at=datetime.datetime.now(datetime.UTC),
            setup_token=f"tok-{uid}",
            setup_token_expires_at=datetime.datetime.now(datetime.UTC)
            - datetime.timedelta(hours=1),
        )
    )
    db_session.commit()

    response = _client().post(
        "/webhooks/complete-setup",
        json={"token": f"tok-{uid}"},
        headers={"X-Agenda-Token": TOKEN},
    )

    assert response.status_code == 400


def test_complete_setup_rejects_an_unknown_token(monkeypatch, db_session):
    monkeypatch.setattr("app.triggers.agenda.setup_router.get_settings", lambda: _Settings())

    response = _client().post(
        "/webhooks/complete-setup",
        json={"token": "does-not-exist"},
        headers={"X-Agenda-Token": TOKEN},
    )

    assert response.status_code == 400


def test_complete_setup_token_is_single_use(monkeypatch, db_session):
    from app.core.models import User

    monkeypatch.setattr("app.triggers.agenda.setup_router.get_settings", lambda: _Settings())
    uid = str(uuid.uuid4())
    db_session.add(
        User(
            id=uid,
            created_at=datetime.datetime.now(datetime.UTC),
            setup_token=f"tok-{uid}",
            setup_token_expires_at=datetime.datetime.now(datetime.UTC)
            + datetime.timedelta(hours=1),
        )
    )
    db_session.commit()

    first = _client().post(
        "/webhooks/complete-setup",
        json={"token": f"tok-{uid}"},
        headers={"X-Agenda-Token": TOKEN},
    )
    second = _client().post(
        "/webhooks/complete-setup",
        json={"token": f"tok-{uid}"},
        headers={"X-Agenda-Token": TOKEN},
    )

    assert first.status_code == 200
    assert second.status_code == 400


# ---- POST /webhooks/login ----


def test_login_returns_empty_managed_reports_for_a_pure_report(
    monkeypatch, db_session, make_pair
):
    """A user who is only ever the report side of a pair (manages nobody)
    still gets a normal login — every user has exactly one dashboard
    (their own), regardless of whether anyone manages them.
    managed_reports is only ever non-empty for someone who manages at
    least one report themselves."""
    from app.core.models import User

    monkeypatch.setattr("app.triggers.agenda.auth_router.get_settings", lambda: _Settings())
    pair = make_pair()
    secret = _set_secret(db_session, pair.report_user_id, "report-secret")
    report_row = db_session.get(User, pair.report_user_id)
    report_row.notion_owner_email = f"report-{pair.id}@example.com"
    db_session.commit()

    response = _client().post(
        "/webhooks/login",
        json={"email": report_row.notion_owner_email, "secret": secret},
        headers={"X-Agenda-Token": TOKEN},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["user_id"] == pair.report_user_id
    assert body["managed_reports"] == []


def test_login_matches_email_case_insensitively(monkeypatch, db_session, make_pair):
    """Regression test for a real bug found live: login previously did a
    plain case-sensitive == match on notion_owner_email. Neither `mentor
    link-notion` nor auto-provisioning normalizes case on write, and
    there's no reason the case a user types at login matches whatever
    case Notion happened to store — a mismatch failed with the same
    generic "invalid email or secret" a wrong secret produces, which is
    exactly what happened live ("TheDagHub@gmail.com" vs the stored
    "thedaghub@gmail.com")."""
    from app.core.models import User

    monkeypatch.setattr("app.triggers.agenda.auth_router.get_settings", lambda: _Settings())
    pair = make_pair()
    secret = _set_secret(db_session, pair.report_user_id, "case-secret")
    report_row = db_session.get(User, pair.report_user_id)
    report_row.notion_owner_email = f"report-{pair.id}@example.com"
    db_session.commit()

    response = _client().post(
        "/webhooks/login",
        json={
            "email": report_row.notion_owner_email.upper(),
            "secret": secret,
        },
        headers={"X-Agenda-Token": TOKEN},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["user_id"] == pair.report_user_id


def test_login_returns_every_report_for_the_manager_side(
    monkeypatch, db_session, make_pair, make_user
):
    from app.core.models import User

    monkeypatch.setattr("app.triggers.agenda.auth_router.get_settings", lambda: _Settings())
    manager_id = make_user()
    pair_one = make_pair(manager_user_id=manager_id)
    pair_two = make_pair(manager_user_id=manager_id)
    secret = _set_secret(db_session, manager_id, "manager-secret")
    manager_row = db_session.get(User, manager_id)
    manager_row.notion_owner_email = f"manager-{manager_id}@example.com"
    db_session.commit()

    response = _client().post(
        "/webhooks/login",
        json={"email": manager_row.notion_owner_email, "secret": secret},
        headers={"X-Agenda-Token": TOKEN},
    )

    assert response.status_code == 200
    body = response.json()
    report_ids = {p["report_user_id"] for p in body["managed_reports"]}
    assert report_ids == {pair_one.report_user_id, pair_two.report_user_id}


def test_login_rejects_wrong_secret(monkeypatch, db_session, make_pair):
    from app.core.models import User

    monkeypatch.setattr("app.triggers.agenda.auth_router.get_settings", lambda: _Settings())
    pair = make_pair()
    _set_secret(db_session, pair.report_user_id, "report-secret")
    report_row = db_session.get(User, pair.report_user_id)
    report_row.notion_owner_email = f"report-{pair.id}@example.com"
    db_session.commit()

    response = _client().post(
        "/webhooks/login",
        json={"email": report_row.notion_owner_email, "secret": "wrong-secret"},
        headers={"X-Agenda-Token": TOKEN},
    )

    assert response.status_code == 401


# ---- GET /webhooks/managed-reports ----


def test_managed_reports_webhook_returns_the_same_shape_as_login(
    monkeypatch, db_session, make_pair, make_user
):
    """REAL CHANGE (requested: fix the log-out-and-back-in gap for a
    newly-synced Pair). Same underlying data login_webhook's own
    managed_reports returns — this is just the on-demand refetch of it."""
    monkeypatch.setattr("app.triggers.agenda.auth_router.get_settings", lambda: _Settings())
    manager_id = make_user()
    pair = make_pair(manager_user_id=manager_id)
    secret = _set_secret(db_session, manager_id, "managed-reports-secret")

    response = _client().get(
        "/webhooks/managed-reports",
        params={"owner_user_id": manager_id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    report_ids = {p["report_user_id"] for p in body["managed_reports"]}
    assert report_ids == {pair.report_user_id}


def test_managed_reports_webhook_reflects_a_pair_that_synced_after_login(
    monkeypatch, db_session, make_pair, make_user
):
    """The exact gap being fixed: calling this again after a NEW Pair
    lands (simulating what the SSE managed_reports_changed listener
    triggers) must show it, without needing a fresh login."""
    monkeypatch.setattr("app.triggers.agenda.auth_router.get_settings", lambda: _Settings())
    manager_id = make_user()
    secret = _set_secret(db_session, manager_id, "managed-reports-secret-2")

    before = _client().get(
        "/webhooks/managed-reports",
        params={"owner_user_id": manager_id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )
    assert before.json()["managed_reports"] == []

    pair = make_pair(manager_user_id=manager_id)

    after = _client().get(
        "/webhooks/managed-reports",
        params={"owner_user_id": manager_id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )
    report_ids = {p["report_user_id"] for p in after.json()["managed_reports"]}
    assert report_ids == {pair.report_user_id}


def test_managed_reports_webhook_rejects_without_a_valid_secret(
    monkeypatch, db_session, make_pair
):
    monkeypatch.setattr("app.triggers.agenda.auth_router.get_settings", lambda: _Settings())
    pair = make_pair()

    response = _client().get(
        "/webhooks/managed-reports",
        params={"owner_user_id": pair.manager_user_id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": "wrong"},
    )

    assert response.status_code == 401


def test_managed_reports_webhook_requires_a_valid_token(monkeypatch, db_session):
    monkeypatch.setattr("app.triggers.agenda.auth_router.get_settings", lambda: _Settings())

    response = _client().get(
        "/webhooks/managed-reports", params={"owner_user_id": "whoever"}
    )

    assert response.status_code == 401


def test_login_rejects_unknown_email(monkeypatch, db_session):
    monkeypatch.setattr("app.triggers.agenda.auth_router.get_settings", lambda: _Settings())

    response = _client().post(
        "/webhooks/login",
        json={"email": "nobody@example.com", "secret": "anything"},
        headers={"X-Agenda-Token": TOKEN},
    )

    assert response.status_code == 401


def test_login_rejects_without_a_valid_token(monkeypatch, db_session):
    monkeypatch.setattr("app.triggers.agenda.auth_router.get_settings", lambda: _Settings())

    response = _client().post(
        "/webhooks/login",
        json={"email": "nobody@example.com", "secret": "anything"},
        headers={"X-Agenda-Token": "wrong-token"},
    )

    assert response.status_code == 401


# ---- /webhooks/agenda-signal ----


def test_agenda_signal_valid_token_applies_and_returns_real_result(
    monkeypatch, db_session, make_pair
):
    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()
    item = _seed_item(db_session, pair)
    secret = _set_secret(db_session, pair.manager_user_id, "manager-secret")

    response = _client().post(
        "/webhooks/agenda-signal",
        json={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.manager_user_id,
            "signal_type": "field_edit",
            "item_id": item.id,
            "changes": {"text": "revisit the roadmap (updated)"},
        },
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    from app.agenda.models import AgendaItem

    db_session.expire_all()
    row = db_session.get(AgendaItem, item.id)
    assert row.text == "revisit the roadmap (updated)"


def test_agenda_signal_missing_token_is_401(monkeypatch, db_session, make_pair):
    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()
    item = _seed_item(db_session, pair)

    response = _client().post(
        "/webhooks/agenda-signal",
        json={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
            "signal_type": "drop",
            "item_id": item.id,
            "changes": None,
        },
    )

    assert response.status_code == 401


def test_agenda_signal_wrong_token_is_401(monkeypatch, db_session, make_pair):
    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()
    item = _seed_item(db_session, pair)

    response = _client().post(
        "/webhooks/agenda-signal",
        json={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
            "signal_type": "drop",
            "item_id": item.id,
            "changes": None,
        },
        headers={"X-Agenda-Token": "wrong-token"},
    )

    assert response.status_code == 401


def test_agenda_signal_no_token_configured_is_401(monkeypatch, db_session, make_pair):
    monkeypatch.setattr(
        "app.triggers.agenda.agenda_router.get_settings",
        lambda: _Settings(agenda_webhook_token=""),
    )
    pair = make_pair()
    item = _seed_item(db_session, pair)

    response = _client().post(
        "/webhooks/agenda-signal",
        json={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
            "signal_type": "drop",
            "item_id": item.id,
            "changes": None,
        },
        headers={"X-Agenda-Token": TOKEN},
    )

    assert response.status_code == 401


# ---- /webhooks/agenda-trigger ----


def test_agenda_trigger_manual_note_creates_a_real_agenda_item(
    monkeypatch, db_session, make_pair
):
    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()
    secret = _set_secret(db_session, pair.report_user_id, "report-secret")

    response = _client().post(
        "/webhooks/agenda-trigger",
        json={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
            "trigger_type": "manual_note",
            "text": "ask about the roadmap",
            "visibility": "shared",
        },
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    from app.agenda.models import AgendaItem

    rows = (
        db_session.query(AgendaItem).filter_by(report_user_id=pair.report_user_id).all()
    )
    assert len(rows) == 1
    assert rows[0].text == "ask about the roadmap"


def test_agenda_trigger_missing_token_is_401(monkeypatch, db_session, make_pair):
    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()

    response = _client().post(
        "/webhooks/agenda-trigger",
        json={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
            "trigger_type": "manual_note",
            "text": "ask about the roadmap",
            "visibility": "shared",
        },
    )

    assert response.status_code == 401


def test_agenda_trigger_former_manager_is_cleanly_rejected_not_500(
    monkeypatch, db_session, make_pair
):
    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    now = datetime.datetime.now(datetime.UTC)
    pair = make_pair(ended_at=now)
    make_pair(report_user_id=pair.report_user_id)  # current pair now exists
    # The former manager still needs a valid secret here — this test is
    # specifically about the pair-scope cutoff rejecting them, not about
    # the acting-user-secret check (which would also reject them, but for
    # the wrong reason if left unset).
    secret = _set_secret(db_session, pair.manager_user_id, "former-manager-secret")

    response = _client().post(
        "/webhooks/agenda-trigger",
        json={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.manager_user_id,  # the FORMER manager
            "trigger_type": "manual_note",
            "text": "should not be written",
            "visibility": "shared",
        },
        headers={
            "X-Agenda-Token": TOKEN,
            "X-Acting-User-Secret": secret,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "rejected",
        "reason": "no active relationship",
    }

    from app.agenda.models import AgendaItem

    rows = (
        db_session.query(AgendaItem).filter_by(report_user_id=pair.report_user_id).all()
    )
    assert rows == []


# ---- Finding 1: caller-declared identity must be backed by a per-user secret ----


def test_agenda_trigger_report_cannot_impersonate_manager_with_own_secret(
    monkeypatch, db_session, make_pair
):
    """The shared token alone used to be enough to claim any acting_user_id
    — a report holding the one shared token could declare
    acting_user_id=<their manager's id> and read/act on manager_only
    items. Now the caller must ALSO know that specific user's own
    agenda_client_secret, so knowing only the report's own secret must
    not let a request claiming to be the manager succeed."""
    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()
    report_secret = _set_secret(db_session, pair.report_user_id, "report-secret")
    _set_secret(db_session, pair.manager_user_id, "manager-secret")

    response = _client().post(
        "/webhooks/agenda-trigger",
        json={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.manager_user_id,  # claims to be the manager
            "trigger_type": "manual_note",
            "text": "should not be written",
            "visibility": "manager_only",
        },
        headers={
            "X-Agenda-Token": TOKEN,
            # ... but only knows the REPORT's own secret.
            "X-Acting-User-Secret": report_secret,
        },
    )

    assert response.status_code == 401
    assert response.json()["status"] == "rejected"

    from app.agenda.models import AgendaItem

    rows = (
        db_session.query(AgendaItem).filter_by(report_user_id=pair.report_user_id).all()
    )
    assert rows == []  # no mutation happened


def test_agenda_trigger_succeeds_with_the_correct_acting_user_secret(
    monkeypatch, db_session, make_pair
):
    """Positive case: a caller who knows the CORRECT secret for the party
    they're claiming to be (here, the manager's own secret) succeeds."""
    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()
    _set_secret(db_session, pair.report_user_id, "report-secret")
    manager_secret = _set_secret(db_session, pair.manager_user_id, "manager-secret")

    response = _client().post(
        "/webhooks/agenda-trigger",
        json={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.manager_user_id,
            "trigger_type": "manual_note",
            "text": "a legitimate manager note",
            "visibility": "manager_only",
        },
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": manager_secret},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    from app.agenda.models import AgendaItem

    rows = (
        db_session.query(AgendaItem).filter_by(report_user_id=pair.report_user_id).all()
    )
    assert len(rows) == 1
    assert rows[0].text == "a legitimate manager note"


def test_agenda_trigger_missing_acting_user_secret_header_is_401(
    monkeypatch, db_session, make_pair
):
    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()
    _set_secret(db_session, pair.report_user_id, "report-secret")

    response = _client().post(
        "/webhooks/agenda-trigger",
        json={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
            "trigger_type": "manual_note",
            "text": "should not be written",
            "visibility": "shared",
        },
        headers={"X-Agenda-Token": TOKEN},  # no X-Acting-User-Secret at all
    )

    assert response.status_code == 401

    from app.agenda.models import AgendaItem

    rows = (
        db_session.query(AgendaItem).filter_by(report_user_id=pair.report_user_id).all()
    )
    assert rows == []


def test_agenda_trigger_no_secret_set_for_acting_user_is_401(
    monkeypatch, db_session, make_pair
):
    """A user who has never run `link-agenda-client` (agenda_client_secret
    is None) cannot be claimed as the acting party over HTTP at all —
    None must never match a provided header via a permissive comparison."""
    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()  # no secrets set on either party

    response = _client().post(
        "/webhooks/agenda-trigger",
        json={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
            "trigger_type": "manual_note",
            "text": "should not be written",
            "visibility": "shared",
        },
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": "anything"},
    )

    assert response.status_code == 401


def test_agenda_signal_wrong_acting_user_secret_is_401_no_mutation(
    monkeypatch, db_session, make_pair
):
    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()
    item = _seed_item(db_session, pair)
    report_secret = _set_secret(db_session, pair.report_user_id, "report-secret")
    _set_secret(db_session, pair.manager_user_id, "manager-secret")

    response = _client().post(
        "/webhooks/agenda-signal",
        json={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.manager_user_id,  # claims to be the manager
            "signal_type": "field_edit",
            "item_id": item.id,
            "changes": {"text": "should not apply"},
        },
        headers={
            "X-Agenda-Token": TOKEN,
            "X-Acting-User-Secret": report_secret,  # wrong party's secret
        },
    )

    assert response.status_code == 401

    from app.agenda.models import AgendaItem

    db_session.expire_all()
    row = db_session.get(AgendaItem, item.id)
    assert row.text == "revisit the roadmap"  # unchanged


# ---- Finding 1: GET /webhooks/agenda-payload (pull-based delivery) ----


def test_agenda_payload_returns_current_state_respecting_visibility(
    monkeypatch, db_session, make_pair
):
    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()
    _seed_item(db_session, pair)  # shared, visible to both
    manager_secret = _set_secret(db_session, pair.manager_user_id, "manager-secret")
    report_secret = _set_secret(db_session, pair.report_user_id, "report-secret")

    from app.agenda.store import add_manual_note
    from app.core.clock import FrozenClock

    scope = resolve_pair_scope(db_session, pair.report_user_id, pair.report_user_id)
    restricted = add_manual_note(
        scope,
        pair.report_user_id,
        "private report note",
        "report_only",
        FrozenClock(at=NOW),
    )

    manager_response = _client().get(
        "/webhooks/agenda-payload",
        params={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.manager_user_id,
        },
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": manager_secret},
    )
    assert manager_response.status_code == 200
    manager_item_ids = {
        c["item_id"] for c in manager_response.json()["components"] if "item_id" in c
    }
    assert restricted.id not in manager_item_ids

    report_response = _client().get(
        "/webhooks/agenda-payload",
        params={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
        },
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": report_secret},
    )
    assert report_response.status_code == 200
    report_item_ids = {
        c["item_id"] for c in report_response.json()["components"] if "item_id" in c
    }
    assert restricted.id in report_item_ids


def test_agenda_payload_never_shows_a_resolved_item(monkeypatch, db_session, make_pair):
    """A resolved item must actually disappear from the payload — the
    original filter (status != "pending_consent") wrongly let resolved
    items through too, so a resolved item kept rendering as an
    editable, still-active component forever."""
    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()
    item = _seed_item(db_session, pair)
    report_secret = _set_secret(db_session, pair.report_user_id, "resolve-secret")

    from app.agenda.store import mark_resolved

    scope = resolve_pair_scope(db_session, pair.report_user_id, pair.report_user_id)
    mark_resolved(scope, item.id, pair.report_user_id, FrozenClock(at=NOW))

    response = _client().get(
        "/webhooks/agenda-payload",
        params={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
        },
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": report_secret},
    )
    assert response.status_code == 200
    item_ids = {c["item_id"] for c in response.json()["components"] if "item_id" in c}
    assert item.id not in item_ids


def test_agenda_payload_missing_token_is_401(monkeypatch, db_session, make_pair):
    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()

    response = _client().get(
        "/webhooks/agenda-payload",
        params={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
        },
    )

    assert response.status_code == 401


# ---- Finding 2: meeting_end is backgrounded, not awaited inline ----


def test_agenda_trigger_meeting_end_returns_202_via_background_path(
    monkeypatch, db_session, make_pair
):
    """FastAPI's TestClient runs BackgroundTasks to completion within the
    same call (there is no live server to observe true async deferral
    from), so this asserts the actual contract instead of wall-clock
    timing: meeting_end takes the backgrounded branch, evidenced by (a) a
    202 "accepted" response with no inline result, contrasted with
    ledger_event/manual_note's 200+result shape, and (b) the background
    target function itself being invoked with the right arguments — proof
    the route scheduled it via background_tasks.add_task rather than
    awaiting the orchestrator directly in the request path."""
    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()
    secret = _set_secret(db_session, pair.report_user_id, "report-secret")

    with patch(
        "app.triggers.agenda.agenda_router._run_orchestrator_in_background"
    ) as mock_background:
        response = _client().post(
            "/webhooks/agenda-trigger",
            json={
                "report_user_id": pair.report_user_id,
                "acting_user_id": pair.report_user_id,
                "trigger_type": "meeting_end",
                "meeting_id": "meeting-1",
            },
            headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
        )

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    mock_background.assert_called_once_with(
        pair.report_user_id,
        pair.report_user_id,
        {
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
            "trigger_type": "meeting_end",
            "meeting_id": "meeting-1",
        },
    )


def test_agenda_trigger_ledger_event_still_synchronous(
    monkeypatch, db_session, make_pair
):
    """Regression guard: only meeting_end is backgrounded — ledger_event
    must still run inline and return the real result in the response."""
    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()
    secret = _set_secret(db_session, pair.report_user_id, "report-secret")

    response = _client().post(
        "/webhooks/agenda-trigger",
        json={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
            "trigger_type": "ledger_event",
            "kind": "jira_blocker",
            "text": "unblock the CI flake",
            "source": "jira",
            "source_link": "JIRA-77",
            "visibility": "shared",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    from app.agenda.models import AgendaItem

    rows = (
        db_session.query(AgendaItem).filter_by(report_user_id=pair.report_user_id).all()
    )
    assert len(rows) == 1
    assert rows[0].source == "jira"


# ---- GET /webhooks/agenda-trigger-status (real background-failure surfacing) ----


def test_trigger_status_is_pending_for_unknown_meeting_id(monkeypatch, db_session, make_pair):
    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()
    secret = _set_secret(db_session, pair.report_user_id, "report-secret")

    response = _client().get(
        "/webhooks/agenda-trigger-status",
        params={"meeting_id": "never-triggered", "acting_user_id": pair.report_user_id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "pending"}


def test_trigger_status_requires_a_valid_token(monkeypatch, db_session, make_pair):
    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()

    response = _client().get(
        "/webhooks/agenda-trigger-status",
        params={"meeting_id": "x", "acting_user_id": pair.report_user_id},
        headers={"X-Agenda-Token": "wrong-token"},
    )

    assert response.status_code == 401


def test_trigger_status_requires_a_valid_secret(monkeypatch, db_session, make_pair):
    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()

    response = _client().get(
        "/webhooks/agenda-trigger-status",
        params={"meeting_id": "x", "acting_user_id": pair.report_user_id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": "wrong"},
    )

    assert response.status_code == 401


async def _run_background_and_record(
    report_user_id, acting_user_id, body, monkeypatch, orchestrator_result=None, orchestrator_raises=None
):
    from app.triggers.agenda import agenda_router

    async def fake_run_orchestrator(report_user_id, acting_user_id, body):
        if orchestrator_raises is not None:
            raise orchestrator_raises
        return orchestrator_result

    monkeypatch.setattr(agenda_router, "_run_orchestrator", fake_run_orchestrator)
    await agenda_router._run_orchestrator_in_background(report_user_id, acting_user_id, body)


def test_trigger_status_reports_a_real_error_after_background_failure(
    monkeypatch, db_session, make_pair
):
    """The exact live bug this whole endpoint exists to fix: a background
    meeting_end run that raises (confirmed live: no active Pair as
    report) must be visible to the polling frontend as a real "error"
    with the actual message — not indistinguishable from "still
    running" forever."""
    import asyncio

    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()
    secret = _set_secret(db_session, pair.report_user_id, "report-secret")
    meeting_id = f"meeting-fail-{uuid.uuid4().hex[:8]}"

    asyncio.run(
        _run_background_and_record(
            pair.report_user_id,
            pair.report_user_id,
            {"meeting_id": meeting_id},
            monkeypatch,
            orchestrator_raises=ValueError("no current Pair for report_user_id=... "),
        )
    )

    response = _client().get(
        "/webhooks/agenda-trigger-status",
        params={"meeting_id": meeting_id, "acting_user_id": pair.report_user_id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert "no current Pair" in body["detail"]


def test_trigger_status_reports_ok_after_background_success(monkeypatch, db_session, make_pair):
    import asyncio

    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()
    secret = _set_secret(db_session, pair.report_user_id, "report-secret")
    meeting_id = f"meeting-ok-{uuid.uuid4().hex[:8]}"

    asyncio.run(
        _run_background_and_record(
            pair.report_user_id,
            pair.report_user_id,
            {"meeting_id": meeting_id},
            monkeypatch,
            orchestrator_result={"status": "ok", "agenda_payload": {"components": []}},
        )
    )

    response = _client().get(
        "/webhooks/agenda-trigger-status",
        params={"meeting_id": meeting_id, "acting_user_id": pair.report_user_id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "detail": None}


def test_trigger_status_does_not_leak_another_users_error_detail(
    monkeypatch, db_session, make_pair, make_user
):
    """A different acting_user_id polling the same (guessed or observed)
    meeting_id must see "pending", never the real user's error text —
    that text can contain internal exception detail."""
    import asyncio

    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()
    triggering_secret = _set_secret(db_session, pair.report_user_id, "report-secret")
    other_user_id = make_user()
    other_secret = _set_secret(db_session, other_user_id, "other-secret")
    meeting_id = f"meeting-leak-{uuid.uuid4().hex[:8]}"

    asyncio.run(
        _run_background_and_record(
            pair.report_user_id,
            pair.report_user_id,
            {"meeting_id": meeting_id},
            monkeypatch,
            orchestrator_raises=ValueError("sensitive internal detail"),
        )
    )

    # sanity: the actual triggering user CAN see it
    own_response = _client().get(
        "/webhooks/agenda-trigger-status",
        params={"meeting_id": meeting_id, "acting_user_id": pair.report_user_id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": triggering_secret},
    )
    assert own_response.json()["status"] == "error"

    other_response = _client().get(
        "/webhooks/agenda-trigger-status",
        params={"meeting_id": meeting_id, "acting_user_id": other_user_id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": other_secret},
    )
    assert other_response.status_code == 200
    assert other_response.json() == {"status": "pending"}


# ---- Finding 3: malformed input is a clean rejection, not a 500 ----


def test_agenda_trigger_malformed_body_is_400_not_500(
    monkeypatch, db_session, make_pair
):
    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()
    secret = _set_secret(db_session, pair.report_user_id, "report-secret")

    response = _client().post(
        "/webhooks/agenda-trigger",
        json={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
            "trigger_type": "manual_note",
            # "text" deliberately omitted — required by the manual_note path
        },
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 400
    assert response.json()["status"] == "rejected"


def test_agenda_trigger_unknown_trigger_type_is_clean_rejection(
    monkeypatch, db_session, make_pair
):
    monkeypatch.setattr("app.triggers.agenda.agenda_router.get_settings", lambda: _Settings())
    pair = make_pair()
    secret = _set_secret(db_session, pair.report_user_id, "report-secret")

    response = _client().post(
        "/webhooks/agenda-trigger",
        json={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
            "trigger_type": "snooze",
        },
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code in (400, 200)
    assert response.json()["status"] == "rejected"


# ---- GET /webhooks/goals ----


def _seed_goal(db_session, report_user_id: str, **overrides) -> None:
    import datetime as dt
    import uuid

    from app.core.models import Goal

    defaults = dict(
        id=str(uuid.uuid4()),
        owner_user_id=report_user_id,
        title="Objective",
        status="active",
        created_at=dt.datetime.now(dt.UTC),
        source="notion",
        goal_type="objective",
        external_id="obj-1",
    )
    defaults.update(overrides)
    db_session.add(Goal(**defaults))
    db_session.commit()


def test_goals_webhook_returns_the_reports_goals(monkeypatch, db_session, make_pair):
    monkeypatch.setattr("app.triggers.agenda.goals_router.get_settings", lambda: _Settings())
    pair = make_pair()
    secret = _set_secret(db_session, pair.report_user_id, "report-secret")
    _seed_goal(db_session, pair.report_user_id, external_id="obj-1")
    _seed_goal(
        db_session,
        pair.report_user_id,
        external_id="kr-1",
        goal_type="key_result",
        parent_external_id="obj-1",
    )

    response = _client().get(
        "/webhooks/goals",
        params={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
        },
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    external_ids = {g["external_id"] for g in body["goals"]}
    assert external_ids == {"obj-1", "kr-1"}


def test_goals_webhook_rejects_a_manager_viewing_a_reports_goals(
    monkeypatch, db_session, make_pair
):
    """REAL BUG FOUND AND FIXED (requested: "a manager can't see okrs
    and goals of their reports and vice versa"). Goals/OKRs are private,
    unlike the shared agenda — a manager passing their report's
    report_user_id here (with their OWN, real acting_user_id/secret)
    must be rejected, not silently allowed through resolve_pair_scope
    the way the agenda/notes routes correctly allow."""
    monkeypatch.setattr("app.triggers.agenda.goals_router.get_settings", lambda: _Settings())
    pair = make_pair()
    manager_secret = _set_secret(db_session, pair.manager_user_id, "manager-secret")
    _seed_goal(db_session, pair.report_user_id, external_id="obj-1")

    response = _client().get(
        "/webhooks/goals",
        params={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.manager_user_id,
        },
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": manager_secret},
    )

    assert response.status_code == 403
    assert response.json()["status"] == "rejected"


def test_goals_webhook_rejects_a_report_viewing_their_managers_goals(
    monkeypatch, db_session, make_pair
):
    monkeypatch.setattr("app.triggers.agenda.goals_router.get_settings", lambda: _Settings())
    pair = make_pair()
    report_secret = _set_secret(db_session, pair.report_user_id, "report-secret-2")
    _seed_goal(db_session, pair.manager_user_id, external_id="mgr-obj-1")

    response = _client().get(
        "/webhooks/goals",
        params={
            "report_user_id": pair.manager_user_id,
            "acting_user_id": pair.report_user_id,
        },
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": report_secret},
    )

    assert response.status_code == 403
    assert response.json()["status"] == "rejected"


def test_goals_webhook_rejects_without_a_valid_token(monkeypatch, db_session, make_pair):
    monkeypatch.setattr("app.triggers.agenda.goals_router.get_settings", lambda: _Settings())
    pair = make_pair()

    response = _client().get(
        "/webhooks/goals",
        params={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
        },
        headers={"X-Agenda-Token": "wrong-token"},
    )

    assert response.status_code == 401


# ---- GET /webhooks/one-on-one-notes ----


def test_one_on_one_notes_webhook_returns_notes_newest_first(
    monkeypatch, db_session, make_pair
):
    import datetime as dt
    import uuid

    from app.core.models import OneOnOneNote

    monkeypatch.setattr("app.triggers.agenda.ledgers_router.get_settings", lambda: _Settings())
    pair = make_pair()
    secret = _set_secret(db_session, pair.report_user_id, "report-secret")
    db_session.add(
        OneOnOneNote(
            id=str(uuid.uuid4()),
            owner_user_id=pair.report_user_id,
            source="notion",
            external_id="note-older",
            title="Older note",
            created_at=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
        )
    )
    db_session.add(
        OneOnOneNote(
            id=str(uuid.uuid4()),
            owner_user_id=pair.report_user_id,
            source="notion",
            external_id="note-newer",
            title="Newer note",
            created_at=dt.datetime(2026, 8, 10, tzinfo=dt.UTC),
        )
    )
    db_session.commit()

    response = _client().get(
        "/webhooks/one-on-one-notes",
        params={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
        },
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    titles = [n["title"] for n in response.json()["notes"]]
    assert titles[:2] == ["Newer note", "Older note"]


# ---- GET /webhooks/ledgers ----


def test_ledgers_webhook_returns_both_ledgers(monkeypatch, db_session, make_pair):
    import datetime as dt
    import uuid

    from app.agenda.models import Accomplishment
    from app.core.models import Commitment

    monkeypatch.setattr("app.triggers.agenda.ledgers_router.get_settings", lambda: _Settings())
    pair = make_pair()
    secret = _set_secret(db_session, pair.report_user_id, "report-secret")
    db_session.add(
        Commitment(
            id=str(uuid.uuid4()),
            owner_user_id=pair.report_user_id,
            description="Ship the migration",
            source_reference_key="ref-1",
            promised_at=dt.datetime.now(dt.UTC),
            status="open",
        )
    )
    db_session.add(
        Accomplishment(
            id=str(uuid.uuid4()),
            owner_user_id=pair.report_user_id,
            description="Shipped the L2 sync component",
            source_reference_key="ref-2",
            occurred_at=dt.datetime.now(dt.UTC),
        )
    )
    db_session.commit()

    response = _client().get(
        "/webhooks/ledgers",
        params={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
        },
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["commitments"][0]["description"] == "Ship the migration"
    assert body["accomplishments"][0]["description"] == "Shipped the L2 sync component"


def test_ledgers_webhook_rejects_without_a_valid_token(
    monkeypatch, db_session, make_pair
):
    monkeypatch.setattr("app.triggers.agenda.ledgers_router.get_settings", lambda: _Settings())
    pair = make_pair()

    response = _client().get(
        "/webhooks/ledgers",
        params={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
        },
        headers={"X-Agenda-Token": "wrong-token"},
    )

    assert response.status_code == 401


# ---- POST /webhooks/ledgers/commitment ----


def test_add_commitment_webhook_creates_a_real_row(monkeypatch, db_session, make_user):
    from app.core.models import Commitment

    monkeypatch.setattr("app.triggers.agenda.ledgers_router.get_settings", lambda: _Settings())
    owner_id = make_user()
    secret = _set_secret(db_session, owner_id, "owner-secret")

    response = _client().post(
        "/webhooks/ledgers/commitment",
        json={"owner_user_id": owner_id, "description": "Send the design doc"},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["commitment"]["description"] == "Send the design doc"
    assert body["commitment"]["status"] == "open"

    row = db_session.get(Commitment, body["commitment"]["id"])
    assert row is not None
    assert row.owner_user_id == owner_id
    assert row.source_reference_key == f"manual:{owner_id}"


def test_add_commitment_webhook_accepts_a_due_at(monkeypatch, db_session, make_user):
    monkeypatch.setattr("app.triggers.agenda.ledgers_router.get_settings", lambda: _Settings())
    owner_id = make_user()
    secret = _set_secret(db_session, owner_id, "owner-secret")

    response = _client().post(
        "/webhooks/ledgers/commitment",
        json={
            "owner_user_id": owner_id,
            "description": "Send the design doc",
            "due_at": "2026-09-01T00:00:00+00:00",
        },
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    assert response.json()["commitment"]["due_at"] == "2026-09-01T00:00:00+00:00"


def test_add_commitment_webhook_rejects_empty_description(monkeypatch, db_session, make_user):
    monkeypatch.setattr("app.triggers.agenda.ledgers_router.get_settings", lambda: _Settings())
    owner_id = make_user()
    secret = _set_secret(db_session, owner_id, "owner-secret")

    response = _client().post(
        "/webhooks/ledgers/commitment",
        json={"owner_user_id": owner_id, "description": "   "},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 400


def test_add_commitment_webhook_rejects_wrong_secret(monkeypatch, db_session, make_user):
    monkeypatch.setattr("app.triggers.agenda.ledgers_router.get_settings", lambda: _Settings())
    owner_id = make_user()
    _set_secret(db_session, owner_id, "owner-secret")

    response = _client().post(
        "/webhooks/ledgers/commitment",
        json={"owner_user_id": owner_id, "description": "Send the design doc"},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": "wrong"},
    )

    assert response.status_code == 401


# ---- POST /webhooks/ledgers/accomplishment ----


def test_add_accomplishment_webhook_creates_a_real_row(monkeypatch, db_session, make_user):
    from app.agenda.models import Accomplishment

    monkeypatch.setattr("app.triggers.agenda.ledgers_router.get_settings", lambda: _Settings())
    owner_id = make_user()
    secret = _set_secret(db_session, owner_id, "owner-secret")

    response = _client().post(
        "/webhooks/ledgers/accomplishment",
        json={"owner_user_id": owner_id, "description": "Shipped the L2 sync component"},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accomplishment"]["description"] == "Shipped the L2 sync component"
    assert body["accomplishment"]["goal_id"] is None

    row = db_session.get(Accomplishment, body["accomplishment"]["id"])
    assert row is not None
    assert row.owner_user_id == owner_id


def test_add_accomplishment_webhook_links_a_real_goal(monkeypatch, db_session, make_user):
    import datetime as dt

    from app.core.models import Goal

    monkeypatch.setattr("app.triggers.agenda.ledgers_router.get_settings", lambda: _Settings())
    owner_id = make_user()
    secret = _set_secret(db_session, owner_id, "owner-secret")
    goal_id = str(uuid.uuid4())
    db_session.add(
        Goal(
            id=goal_id,
            owner_user_id=owner_id,
            title="Ship L2 sync",
            status="active",
            created_at=dt.datetime.now(dt.UTC),
            goal_type="key_result",
        )
    )
    db_session.commit()

    response = _client().post(
        "/webhooks/ledgers/accomplishment",
        json={
            "owner_user_id": owner_id,
            "description": "Shipped the L2 sync component",
            "goal_id": goal_id,
        },
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    assert response.json()["accomplishment"]["goal_id"] == goal_id

    ledgers = _client().get(
        "/webhooks/ledgers",
        params={"report_user_id": owner_id, "acting_user_id": owner_id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )
    accomplishment = ledgers.json()["accomplishments"][0]
    assert accomplishment["goal_id"] == goal_id
    assert accomplishment["goal_title"] == "Ship L2 sync"


def test_add_accomplishment_webhook_rejects_a_goal_from_another_owner(
    monkeypatch, db_session, make_user
):
    import datetime as dt

    from app.core.models import Goal

    monkeypatch.setattr("app.triggers.agenda.ledgers_router.get_settings", lambda: _Settings())
    owner_id = make_user()
    other_owner_id = make_user()
    secret = _set_secret(db_session, owner_id, "owner-secret")
    goal_id = str(uuid.uuid4())
    db_session.add(
        Goal(
            id=goal_id,
            owner_user_id=other_owner_id,
            title="Someone else's goal",
            status="active",
            created_at=dt.datetime.now(dt.UTC),
            goal_type="key_result",
        )
    )
    db_session.commit()

    response = _client().post(
        "/webhooks/ledgers/accomplishment",
        json={
            "owner_user_id": owner_id,
            "description": "Shipped something",
            "goal_id": goal_id,
        },
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 400


# ---- GET /webhooks/fathom-transcript ----


def test_fathom_transcript_webhook_reports_not_connected(
    monkeypatch, db_session, make_pair
):
    from app.ingest.base import Unauthorized

    monkeypatch.setattr("app.triggers.agenda.ledgers_router.get_settings", lambda: _Settings())
    pair = make_pair()
    secret = _set_secret(db_session, pair.report_user_id, "report-secret")

    class _FakeFathomClient:
        def health(self):
            return Unauthorized()

    monkeypatch.setattr(
        "app.triggers.agenda.ledgers_router.LiveFathomClient", _FakeFathomClient
    )

    response = _client().get(
        "/webhooks/fathom-transcript",
        params={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
        },
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "not_connected", "transcript_text": None}


def test_fathom_transcript_webhook_returns_a_real_transcript(
    monkeypatch, db_session, make_pair
):
    from app.ingest.base import Healthy

    monkeypatch.setattr("app.triggers.agenda.ledgers_router.get_settings", lambda: _Settings())
    pair = make_pair()
    secret = _set_secret(db_session, pair.report_user_id, "report-secret")

    class _FakeFathomClient:
        def health(self):
            return Healthy()

        def find_transcript(self, window, organizer_email=None):
            return "Amal: real transcript."

    monkeypatch.setattr(
        "app.triggers.agenda.ledgers_router.LiveFathomClient", _FakeFathomClient
    )

    response = _client().get(
        "/webhooks/fathom-transcript",
        params={
            "report_user_id": pair.report_user_id,
            "acting_user_id": pair.report_user_id,
        },
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "transcript_text": "Amal: real transcript.",
    }


# --- POST /webhooks/update-preferences --------------------------------------


def test_update_preferences_updates_only_the_given_fields(monkeypatch, db_session, make_user):
    monkeypatch.setattr("app.triggers.agenda.preferences_router.get_settings", lambda: _Settings())
    uid = make_user(tz="UTC")
    secret = _set_secret(db_session, uid, "pref-secret")

    response = _client().post(
        "/webhooks/update-preferences",
        json={"acting_user_id": uid, "tz": "America/New_York"},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tz"] == "America/New_York"

    from app.core.models import User

    row = db_session.get(User, uid)
    assert row.tz == "America/New_York"
    # untouched fields keep their prior values
    assert row.pulse_fire_time_local == datetime.time(8, 30)


def test_update_preferences_updates_pulse_and_cutoff_times(
    monkeypatch, db_session, make_user
):
    monkeypatch.setattr("app.triggers.agenda.preferences_router.get_settings", lambda: _Settings())
    uid = make_user()
    secret = _set_secret(db_session, uid, "pref-time-secret")

    response = _client().post(
        "/webhooks/update-preferences",
        json={
            "acting_user_id": uid,
            "pulse_fire_time_local": "09:15",
            "late_cutoff_local": "22:30",
        },
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["pulse_fire_time_local"] == "09:15"
    assert body["late_cutoff_local"] == "22:30"

    from app.core.models import User

    row = db_session.get(User, uid)
    assert row.pulse_fire_time_local == datetime.time(9, 15)
    assert row.late_cutoff_local == datetime.time(22, 30)


def test_update_preferences_rejects_an_unrecognized_timezone(
    monkeypatch, db_session, make_user
):
    monkeypatch.setattr("app.triggers.agenda.preferences_router.get_settings", lambda: _Settings())
    uid = make_user()
    secret = _set_secret(db_session, uid, "pref-badtz-secret")

    response = _client().post(
        "/webhooks/update-preferences",
        json={"acting_user_id": uid, "tz": "Not/A_Real_Zone"},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 400
    assert response.json()["status"] == "rejected"


def test_update_preferences_rejects_a_malformed_time(monkeypatch, db_session, make_user):
    monkeypatch.setattr("app.triggers.agenda.preferences_router.get_settings", lambda: _Settings())
    uid = make_user()
    secret = _set_secret(db_session, uid, "pref-badtime-secret")

    response = _client().post(
        "/webhooks/update-preferences",
        json={"acting_user_id": uid, "pulse_fire_time_local": "not-a-time"},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 400
    assert response.json()["status"] == "rejected"


def test_update_preferences_rejects_without_a_valid_acting_user_secret(
    monkeypatch, db_session, make_user
):
    monkeypatch.setattr("app.triggers.agenda.preferences_router.get_settings", lambda: _Settings())
    uid = make_user()
    _set_secret(db_session, uid, "pref-secret-real")

    response = _client().post(
        "/webhooks/update-preferences",
        json={"acting_user_id": uid, "tz": "America/New_York"},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": "wrong"},
    )

    assert response.status_code == 401


def test_update_preferences_cannot_edit_someone_elses_row(
    monkeypatch, db_session, make_user
):
    """acting_user_id IS the edit target — there is no separate field to
    mismatch, but confirm a caller who only knows THEIR OWN secret can't
    smuggle in someone else's user id and have it silently apply to the
    wrong row."""
    monkeypatch.setattr("app.triggers.agenda.preferences_router.get_settings", lambda: _Settings())
    victim_uid = make_user(tz="UTC")
    attacker_uid = make_user(tz="UTC")
    _set_secret(db_session, victim_uid, "victim-secret")
    attacker_secret = _set_secret(db_session, attacker_uid, "attacker-secret")

    response = _client().post(
        "/webhooks/update-preferences",
        json={"acting_user_id": victim_uid, "tz": "America/New_York"},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": attacker_secret},
    )

    assert response.status_code == 401

    from app.core.models import User

    victim_row = db_session.get(User, victim_uid)
    assert victim_row.tz == "UTC"
