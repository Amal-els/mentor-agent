import datetime
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.models import DossierDelivery, Event, User, WorkItem
from app.triggers.checklist_router import router

NOW = datetime.datetime(2026, 8, 26, 9, 0, 0, tzinfo=datetime.UTC)
TOKEN = "test-checklist-webhook-token"


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


def _make_work_item(pg_session, owner_user_id: str, **overrides) -> WorkItem:
    defaults = {
        "id": str(uuid.uuid4()),
        "owner_user_id": owner_user_id,
        "actor_reference_key": "linear:sarah@acme.com",
        "resolved_person_id": None,
        "source": "linear",
        "external_id": f"MENT-{uuid.uuid4().hex[:6]}",
        "title": "Fix flaky roster test",
        "status": "in_progress",
        "url": "https://linear.app/MENT-1",
        "due_at": None,
        "updated_at": NOW - datetime.timedelta(hours=1),
        "blocks_others": False,
    }
    defaults.update(overrides)
    wi = WorkItem(**defaults)
    pg_session.add(wi)
    pg_session.commit()
    return wi


def test_checklist_get_requires_token(monkeypatch, pg_session):
    monkeypatch.setattr(
        "app.triggers.checklist_router.get_settings", lambda: _Settings()
    )
    response = _client().get("/webhooks/checklist", params={"owner_user_id": "whoever"})
    assert response.status_code == 401


def test_checklist_get_requires_valid_secret(monkeypatch, pg_session):
    monkeypatch.setattr(
        "app.triggers.checklist_router.get_settings", lambda: _Settings()
    )
    user_id, _secret = _make_user(pg_session)
    response = _client().get(
        "/webhooks/checklist",
        params={"owner_user_id": user_id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": "wrong"},
    )
    assert response.status_code == 401


def test_checklist_get_returns_pending_work_item(monkeypatch, pg_session):
    monkeypatch.setattr(
        "app.triggers.checklist_router.get_settings", lambda: _Settings()
    )
    user_id, secret = _make_user(pg_session)
    wi = _make_work_item(pg_session, user_id)

    response = _client().get(
        "/webhooks/checklist",
        params={"owner_user_id": user_id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    body = response.json()
    assert [i["item_id"] for i in body["pending"]] == [wi.id]
    assert body["resolved"] == []
    assert body["stats"] == {"done": 0, "total": 1}


def test_checklist_get_includes_todays_meetings_with_dossier_link(monkeypatch, pg_session):
    monkeypatch.setattr(
        "app.triggers.checklist_router.get_settings", lambda: _Settings()
    )
    # The router builds its own SystemClock() (real wall-clock time) —
    # pinned here so "today" matches this test's fixed NOW regardless of
    # when the suite actually runs (select_window's own "unelapsed event
    # today wins" logic needs the event to still be in the future relative
    # to whatever clock the router used).
    monkeypatch.setattr(
        "app.triggers.checklist_router.SystemClock", lambda: FrozenClock(at=NOW)
    )
    user_id, secret = _make_user(pg_session)
    dossier_id = str(uuid.uuid4())
    pg_session.add(
        Event(
            id=str(uuid.uuid4()), owner_user_id=user_id, source="calendar",
            external_id="evt-router-1", title="1:1 with Sarah",
            starts_at=NOW + datetime.timedelta(hours=1),
            ends_at=NOW + datetime.timedelta(hours=2),
            actor_reference_key="cal#1",
            url="https://calendar.google.com/event?eid=abc",
        )
    )
    pg_session.add(
        DossierDelivery(
            id=dossier_id, owner_user_id=user_id, event_external_id="evt-router-1",
            sent_at=NOW, prompt_version="dossier_synthesize@v1",
            talking_points_source="fresh", card_ref=None, feedback="none",
            feedback_at=None, created_at=NOW, who_summary="", why_now="",
        )
    )
    pg_session.commit()

    response = _client().get(
        "/webhooks/checklist",
        params={"owner_user_id": user_id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["day"]) == 1
    assert body["day"][0]["title"] == "1:1 with Sarah"
    assert body["day"][0]["url"] == "https://calendar.google.com/event?eid=abc"
    assert body["day"][0]["dossier_id"] == dossier_id


def test_checklist_complete_requires_valid_secret(monkeypatch, pg_session):
    monkeypatch.setattr(
        "app.triggers.checklist_router.get_settings", lambda: _Settings()
    )
    user_id, _secret = _make_user(pg_session)
    wi = _make_work_item(pg_session, user_id)

    response = _client().post(
        "/webhooks/checklist/complete",
        json={"owner_user_id": user_id, "item_type": "work_item", "item_id": wi.id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": "wrong"},
    )
    assert response.status_code == 401


def test_checklist_complete_returns_404_for_unowned_item(monkeypatch, pg_session):
    monkeypatch.setattr(
        "app.triggers.checklist_router.get_settings", lambda: _Settings()
    )
    monkeypatch.setattr(
        "app.triggers.checklist_router.generate_supportive_response",
        lambda title, done, total: "unused",
    )
    owner_id, owner_secret = _make_user(pg_session)
    other_owner_id, _other_secret = _make_user(pg_session)
    wi = _make_work_item(pg_session, other_owner_id)

    response = _client().post(
        "/webhooks/checklist/complete",
        json={"owner_user_id": owner_id, "item_type": "work_item", "item_id": wi.id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": owner_secret},
    )
    assert response.status_code == 404


def test_checklist_complete_records_completion_and_returns_ai_response(
    monkeypatch, pg_session
):
    monkeypatch.setattr(
        "app.triggers.checklist_router.get_settings", lambda: _Settings()
    )
    monkeypatch.setattr(
        "app.triggers.checklist_router.generate_supportive_response",
        lambda title, done, total: f"Nice — {title} ({done}/{total})",
    )
    user_id, secret = _make_user(pg_session)
    wi = _make_work_item(pg_session, user_id)

    response = _client().post(
        "/webhooks/checklist/complete",
        json={"owner_user_id": user_id, "item_type": "work_item", "item_id": wi.id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["ai_response"] == "Nice — Fix flaky roster test (1/1)"
    assert body["stats"] == {"done": 1, "total": 1}

    follow_up = _client().get(
        "/webhooks/checklist",
        params={"owner_user_id": user_id},
        headers={"X-Agenda-Token": TOKEN, "X-Acting-User-Secret": secret},
    )
    follow_up_body = follow_up.json()
    assert follow_up_body["pending"] == []
    assert [i["item_id"] for i in follow_up_body["resolved"]] == [wi.id]
    assert follow_up_body["resolved"][0]["source"] == "manual"
