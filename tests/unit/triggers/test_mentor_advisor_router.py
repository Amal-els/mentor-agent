import datetime
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.models import User
from app.sub_agents.mentor_advisor.agent import MentorAdviceOutput
from app.triggers.mentor_advisor_router import router

NOW = datetime.datetime(2026, 8, 27, 9, 0, 0, tzinfo=datetime.UTC)
TOKEN = "test-mentor-advisor-webhook-token"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class _Settings:
    def __init__(self, agenda_webhook_token: str = TOKEN, database_url: str | None = None):
        self.agenda_webhook_token = agenda_webhook_token
        self.database_url = database_url or get_settings().database_url


def _make_user(pg_session) -> tuple[str, str]:
    user_id = str(uuid.uuid4())
    secret = f"user-secret-{user_id}"
    pg_session.add(User(id=user_id, created_at=NOW, agenda_client_secret=secret))
    pg_session.commit()
    return user_id, secret


def test_mentor_advice_requires_token(monkeypatch, pg_session):
    monkeypatch.setattr(
        "app.triggers.mentor_advisor_router.get_settings", lambda: _Settings()
    )
    response = _client().get("/webhooks/mentor-advice", params={"owner_user_id": "whoever"})
    assert response.status_code == 401


def test_mentor_advice_requires_valid_secret(monkeypatch, pg_session):
    monkeypatch.setattr(
        "app.triggers.mentor_advisor_router.get_settings", lambda: _Settings()
    )
    user_id, _secret = _make_user(pg_session)
    response = _client().get(
        "/webhooks/mentor-advice",
        params={"owner_user_id": user_id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": "wrong"},
    )
    assert response.status_code == 401


def test_mentor_advice_returns_short_and_long_term_tasks(monkeypatch, pg_session):
    monkeypatch.setattr(
        "app.triggers.mentor_advisor_router.get_settings", lambda: _Settings()
    )
    user_id, secret = _make_user(pg_session)

    monkeypatch.setattr(
        "app.triggers.mentor_advisor_router.generate_mentor_advice",
        lambda context, agent: MentorAdviceOutput(
            short_term_tasks=["Push the migration OKR past 50%"],
            long_term_tasks=["Build more cross-team collaboration evidence"],
            rationale="The migration OKR is behind pace and skill distribution skews technical.",
        ),
    )

    response = _client().get(
        "/webhooks/mentor-advice",
        params={"owner_user_id": user_id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["short_term_tasks"] == ["Push the migration OKR past 50%"]
    assert body["long_term_tasks"] == ["Build more cross-team collaboration evidence"]
    assert "migration OKR" in body["rationale"]


def test_mentor_advice_failure_returns_a_clean_502_not_a_500(monkeypatch, pg_session):
    monkeypatch.setattr(
        "app.triggers.mentor_advisor_router.get_settings", lambda: _Settings()
    )
    user_id, secret = _make_user(pg_session)

    def _boom(context, agent):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(
        "app.triggers.mentor_advisor_router.generate_mentor_advice", _boom
    )

    response = _client().get(
        "/webhooks/mentor-advice",
        params={"owner_user_id": user_id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 502
    assert response.json()["status"] == "rejected"
