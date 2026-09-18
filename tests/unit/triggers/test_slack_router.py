import hashlib
import hmac
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.triggers.slack.slack_router import router

SECRET = "test-signing-secret"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _signed_headers(
    body: str, secret: str = SECRET, timestamp: str | None = None
) -> dict:
    timestamp = timestamp or str(int(time.time()))
    basestring = f"v0:{timestamp}:{body}".encode()
    digest = hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": f"v0={digest}",
        "Content-Type": "application/x-www-form-urlencoded",
    }


def test_rejects_unsigned_requests_when_no_signing_secret_configured(monkeypatch):
    monkeypatch.setattr("app.triggers.slack.slack_router.get_settings", lambda: _Settings(""))

    body = "command=%2Fmentor&text=pulse&user_id=U1&channel_id=C1"
    response = _client().post(
        "/slack/commands", content=body, headers=_signed_headers(body)
    )

    assert response.status_code == 401


class _Settings:
    def __init__(self, signing_secret: str, database_url: str = ""):
        self.slack_signing_secret = signing_secret
        self.database_url = database_url


def test_rejects_requests_with_a_bad_signature(monkeypatch):
    monkeypatch.setattr(
        "app.triggers.slack.slack_router.get_settings", lambda: _Settings(SECRET)
    )

    body = "command=%2Fmentor&text=pulse&user_id=U1&channel_id=C1"
    response = _client().post(
        "/slack/commands", content=body, headers=_signed_headers(body, secret="wrong")
    )

    assert response.status_code == 401


def test_unlinked_slack_user_gets_a_helpful_message(monkeypatch, pg_session):
    monkeypatch.setattr(
        "app.triggers.slack.slack_router.get_settings",
        lambda: _Settings(SECRET, database_url=str(pg_session.bind.url)),
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_router.get_engine", lambda url: pg_session.bind
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_router.get_session_factory",
        lambda engine: lambda: pg_session,
    )

    body = "command=%2Fmentor&text=pulse&user_id=U0UNLINKED&channel_id=C1"
    response = _client().post(
        "/slack/commands", content=body, headers=_signed_headers(body)
    )

    assert response.status_code == 200
    assert "isn't linked" in response.text


def test_unsupported_subcommand_gets_a_plain_reply(monkeypatch):
    monkeypatch.setattr(
        "app.triggers.slack.slack_router.get_settings", lambda: _Settings(SECRET)
    )

    body = "command=%2Fmentor&text=quiet&user_id=U1&channel_id=C1"
    response = _client().post(
        "/slack/commands", content=body, headers=_signed_headers(body)
    )

    assert response.status_code == 200
    assert (
        "not supported" in response.text.lower() or "isn't supported" in response.text
    )


def test_linked_user_gets_acked_and_a_background_task_is_scheduled(
    monkeypatch, pg_session, ensure_test_user
):
    ensure_test_user("usr_router1", slack_user_id="U0ROUTER1")

    monkeypatch.setattr(
        "app.triggers.slack.slack_router.get_settings",
        lambda: _Settings(SECRET, database_url=str(pg_session.bind.url)),
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_router.get_engine", lambda url: pg_session.bind
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_router.get_session_factory",
        lambda engine: lambda: pg_session,
    )

    calls = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_router._run_pulse_command_with_own_session",
        lambda owner_user_id, channel_id: calls.append((owner_user_id, channel_id)),
    )

    body = "command=%2Fmentor&text=pulse&user_id=U0ROUTER1&channel_id=C0ROUTER1"
    response = _client().post(
        "/slack/commands", content=body, headers=_signed_headers(body)
    )

    assert response.status_code == 200
    assert "on it" in response.text.lower()
    # TestClient runs background tasks synchronously before returning
    assert calls == [("usr_router1", "C0ROUTER1")]


def test_linked_user_prep_gets_acked_and_a_background_task_is_scheduled(
    monkeypatch, pg_session, ensure_test_user
):
    ensure_test_user("usr_router_prep1", slack_user_id="U0ROUTERPREP1")

    monkeypatch.setattr(
        "app.triggers.slack.slack_router.get_settings",
        lambda: _Settings(SECRET, database_url=str(pg_session.bind.url)),
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_router.get_engine", lambda url: pg_session.bind
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_router.get_session_factory",
        lambda engine: lambda: pg_session,
    )

    calls = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_router._run_prep_command_with_own_session",
        lambda owner_user_id, channel_id: calls.append((owner_user_id, channel_id)),
    )

    body = "command=%2Fmentor&text=prep&user_id=U0ROUTERPREP1&channel_id=C0ROUTERPREP1"
    response = _client().post(
        "/slack/commands", content=body, headers=_signed_headers(body)
    )

    assert response.status_code == 200
    assert "on it" in response.text.lower()
    assert "dossier" in response.text.lower()
    # TestClient runs background tasks synchronously before returning
    assert calls == [("usr_router_prep1", "C0ROUTERPREP1")]


def test_linked_user_review_gets_acked_and_a_background_task_is_scheduled(
    monkeypatch, pg_session, ensure_test_user
):
    ensure_test_user("usr_router_review1", slack_user_id="U0ROUTERREVIEW1")

    monkeypatch.setattr(
        "app.triggers.slack.slack_router.get_settings",
        lambda: _Settings(SECRET, database_url=str(pg_session.bind.url)),
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_router.get_engine", lambda url: pg_session.bind
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_router.get_session_factory",
        lambda engine: lambda: pg_session,
    )

    calls = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_router._run_review_command_with_own_session",
        lambda owner_user_id, channel_id: calls.append((owner_user_id, channel_id)),
    )

    body = "command=%2Fmentor&text=review&user_id=U0ROUTERREVIEW1&channel_id=C0ROUTERREVIEW1"
    response = _client().post(
        "/slack/commands", content=body, headers=_signed_headers(body)
    )

    assert response.status_code == 200
    assert "on it" in response.text.lower()
    assert "reflection" in response.text.lower()
    # TestClient runs background tasks synchronously before returning
    assert calls == [("usr_router_review1", "C0ROUTERREVIEW1")]
