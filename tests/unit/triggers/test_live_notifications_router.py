import datetime
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.models import DossierDelivery, FridayReviewDelivery, PulseDelivery, User
from app.triggers.live_notifications_router import (
    _managed_report_ids_sync,
    _poll_once_sync,
    router,
)

NOW = datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC)
TOKEN = "test-live-notifications-webhook-token"


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


def _make_user(pg_session) -> tuple[str, str]:
    user_id = str(uuid.uuid4())
    secret = f"user-secret-{user_id}"
    pg_session.add(User(id=user_id, created_at=NOW, agenda_client_secret=secret))
    pg_session.commit()
    return user_id, secret


def test_notifications_stream_requires_token(monkeypatch, pg_session):
    monkeypatch.setattr(
        "app.triggers.live_notifications_router.get_settings", lambda: _Settings()
    )
    response = _client().get("/webhooks/notifications", params={"owner_user_id": "whoever"})
    assert response.status_code == 401


def test_notifications_stream_requires_valid_secret(monkeypatch, pg_session):
    monkeypatch.setattr(
        "app.triggers.live_notifications_router.get_settings", lambda: _Settings()
    )
    user_id, _secret = _make_user(pg_session)
    response = _client().get(
        "/webhooks/notifications",
        params={"owner_user_id": user_id, "token": TOKEN, "secret": "wrong"},
    )
    assert response.status_code == 401


def test_dossier_audio_requires_token(monkeypatch, pg_session):
    monkeypatch.setattr(
        "app.triggers.live_notifications_router.get_settings", lambda: _Settings()
    )
    response = _client().get(
        "/webhooks/dossier-audio",
        params={"owner_user_id": "whoever", "delivery_id": "whatever"},
    )
    assert response.status_code == 401


def test_dossier_audio_returns_404_for_missing_delivery(monkeypatch, pg_session):
    monkeypatch.setattr(
        "app.triggers.live_notifications_router.get_settings", lambda: _Settings()
    )
    user_id, secret = _make_user(pg_session)
    response = _client().get(
        "/webhooks/dossier-audio",
        params={
            "owner_user_id": user_id, "delivery_id": "nope",
            "token": TOKEN, "secret": secret,
        },
    )
    assert response.status_code == 404


def test_dossier_audio_returns_mp3_for_existing_delivery(monkeypatch, pg_session):
    monkeypatch.setattr(
        "app.triggers.live_notifications_router.get_settings", lambda: _Settings()
    )
    monkeypatch.setattr(
        "app.triggers.live_notifications_router.synthesize_speech",
        lambda text: b"fake-mp3-bytes",
    )
    user_id, secret = _make_user(pg_session)
    delivery_id = str(uuid.uuid4())
    pg_session.add(
        DossierDelivery(
            id=delivery_id, owner_user_id=user_id, event_external_id="evt-1",
            sent_at=NOW, prompt_version="dossier_synthesize@v1",
            talking_points_source="fresh", card_ref=None, feedback="none",
            feedback_at=None, created_at=NOW,
            who_summary="Sam External", why_now="Renewal next week.",
        )
    )
    pg_session.commit()

    response = _client().get(
        "/webhooks/dossier-audio",
        params={
            "owner_user_id": user_id, "delivery_id": delivery_id,
            "token": TOKEN, "secret": secret,
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == b"fake-mp3-bytes"


def test_friday_review_audio_returns_mp3_for_existing_delivery(monkeypatch, pg_session):
    monkeypatch.setattr(
        "app.triggers.live_notifications_router.get_settings", lambda: _Settings()
    )
    monkeypatch.setattr(
        "app.triggers.live_notifications_router.synthesize_speech",
        lambda text: b"fake-mp3-bytes",
    )
    user_id, secret = _make_user(pg_session)
    delivery_id = str(uuid.uuid4())
    pg_session.add(
        FridayReviewDelivery(
            id=delivery_id, owner_user_id=user_id,
            week_start_date=datetime.date(2026, 8, 24), trigger="pull",
            sent_at=NOW, skipped_reason=None,
            prompt_version="friday_review_synthesize@v1", card_ref=None,
            proposed_ledger_items=[{"description": "Shipped X", "source_reference_key": "wi-1"}],
            ledger_confirmed_at=None, created_at=NOW,
        )
    )
    pg_session.commit()

    response = _client().get(
        "/webhooks/friday-review-audio",
        params={
            "owner_user_id": user_id, "delivery_id": delivery_id,
            "token": TOKEN, "secret": secret,
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == b"fake-mp3-bytes"


def test_pulse_audio_returns_mp3_for_existing_delivery(monkeypatch, pg_session):
    monkeypatch.setattr(
        "app.triggers.live_notifications_router.get_settings", lambda: _Settings()
    )
    monkeypatch.setattr(
        "app.triggers.live_notifications_router.synthesize_speech",
        lambda text: b"fake-mp3-bytes",
    )
    user_id, secret = _make_user(pg_session)
    delivery_id = str(uuid.uuid4())
    pg_session.add(
        PulseDelivery(
            id=delivery_id, owner_user_id=user_id, ritual="pulse",
            local_date=NOW.date(), trigger="cron", delivered_at=NOW,
            item_ids=["item-1", "item-2"], context_hash="h", prompt_version="p",
        )
    )
    pg_session.commit()

    response = _client().get(
        "/webhooks/pulse-audio",
        params={
            "owner_user_id": user_id, "delivery_id": delivery_id,
            "token": TOKEN, "secret": secret,
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == b"fake-mp3-bytes"


def test_poll_once_sync_pulse_ready_carries_card_json(pg_session):
    user_id = str(uuid.uuid4())
    pg_session.add(User(id=user_id, created_at=NOW))
    pg_session.commit()
    delivery_id = str(uuid.uuid4())
    card = {"focus": [{"item_id": "wi-1", "title": "Fix roster test"}], "day": []}
    pg_session.add(
        PulseDelivery(
            id=delivery_id, owner_user_id=user_id, ritual="pulse",
            local_date=NOW.date(), trigger="cron", delivered_at=NOW,
            item_ids=["wi-1"], context_hash="h", prompt_version="p",
            card_json=card,
        )
    )
    pg_session.commit()

    events = _poll_once_sync(user_id, NOW - datetime.timedelta(minutes=1))

    pulse_events = [(name, data) for name, data in events if name == "pulse_ready"]
    assert len(pulse_events) == 1
    _name, data = pulse_events[0]
    assert data["id"] == delivery_id
    assert data["item_count"] == 1
    assert data["card"] == card


def test_poll_once_sync_pulse_ready_defaults_card_to_empty_dict(pg_session):
    # A delivery written before card_json existed (or before this owner's
    # very first pulse under the new column's default) still produces a
    # valid, JSON-safe event — the browser falls back to the count-only
    # bubble on an empty dict, never a missing key or None.
    user_id = str(uuid.uuid4())
    pg_session.add(User(id=user_id, created_at=NOW))
    pg_session.commit()
    delivery_id = str(uuid.uuid4())
    pg_session.add(
        PulseDelivery(
            id=delivery_id, owner_user_id=user_id, ritual="pulse",
            local_date=NOW.date(), trigger="cron", delivered_at=NOW,
            item_ids=[], context_hash="h", prompt_version="p",
        )
    )
    pg_session.commit()

    events = _poll_once_sync(user_id, NOW - datetime.timedelta(minutes=1))

    _name, data = next(e for e in events if e[0] == "pulse_ready")
    assert data["card"] == {}


def test_pulse_audio_requires_token(monkeypatch, pg_session):
    monkeypatch.setattr(
        "app.triggers.live_notifications_router.get_settings", lambda: _Settings()
    )
    response = _client().get(
        "/webhooks/pulse-audio",
        params={"owner_user_id": "whoever", "delivery_id": "whatever"},
    )
    assert response.status_code == 401


def test_friday_review_audio_requires_token(monkeypatch, pg_session):
    monkeypatch.setattr(
        "app.triggers.live_notifications_router.get_settings", lambda: _Settings()
    )
    response = _client().get(
        "/webhooks/friday-review-audio",
        params={"owner_user_id": "whoever", "delivery_id": "whatever"},
    )
    assert response.status_code == 401


# ---- managed_reports_changed (REAL CHANGE: fix the log-out-and-back-in
# gap — a newly-synced Pair used to only ever show up in the report
# picker on the manager's next login) ----


def test_managed_report_ids_sync_returns_only_this_managers_reports(
    pg_session, make_pair
):
    pair = make_pair()
    other_pair = make_pair()  # a different manager entirely — must not leak in

    ids = _managed_report_ids_sync(pair.manager_user_id)

    assert ids == frozenset({pair.report_user_id})
    assert other_pair.report_user_id not in ids


def test_managed_report_ids_sync_empty_for_a_report_with_no_reports_of_their_own(
    pg_session, make_pair
):
    pair = make_pair()

    assert _managed_report_ids_sync(pair.report_user_id) == frozenset()
