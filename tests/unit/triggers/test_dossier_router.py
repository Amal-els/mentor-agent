import datetime
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.models import DossierDelivery, User
from app.triggers.dossier.dossier_router import router

NOW = datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC)
TOKEN = "test-dossier-webhook-token"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class _Settings:
    def __init__(
        self, agenda_webhook_token: str = TOKEN, database_url: str | None = None
    ):
        self.agenda_webhook_token = agenda_webhook_token
        self.database_url = database_url or get_settings().database_url


def test_dossier_payload_requires_token(monkeypatch, pg_session):
    monkeypatch.setattr("app.triggers.dossier.dossier_router.get_settings", lambda: _Settings())
    response = _client().get(
        "/webhooks/dossier-payload", params={"owner_user_id": "whoever"}
    )
    assert response.status_code == 401


def test_dossier_payload_returns_deliveries_for_owner(monkeypatch, pg_session):
    monkeypatch.setattr("app.triggers.dossier.dossier_router.get_settings", lambda: _Settings())
    user_id = str(uuid.uuid4())
    secret = f"user-secret-{user_id}"
    user = User(id=user_id, created_at=NOW, agenda_client_secret=secret)
    pg_session.add(user)
    pg_session.flush()
    pg_session.add(
        DossierDelivery(
            id=str(uuid.uuid4()), owner_user_id=user.id, event_external_id="evt-1",
            sent_at=NOW, prompt_version="dossier_synthesize@v1",
            talking_points_source="fresh", card_ref=None, feedback="none",
            feedback_at=None, created_at=NOW,
            who_summary="Sam External", why_now="Renewal next week.",
        )
    )
    pg_session.commit()

    response = _client().get(
        "/webhooks/dossier-payload",
        params={"owner_user_id": user.id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )
    assert response.status_code == 200
    components = response.json()["components"]
    assert len(components) == 1
    assert components[0]["who"] == "Sam External"
    assert components[0]["why_now"] == "Renewal next week."
