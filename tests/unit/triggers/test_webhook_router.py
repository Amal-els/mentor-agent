import hashlib
import hmac
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.triggers.webhooks.webhook_router import router

SECRET = "test-github-webhook-secret"

_PR_PAYLOAD = {
    "action": "opened",
    "repository": {"full_name": "acme/mentor-agent"},
    "pull_request": {
        "number": 42,
        "title": "Fix flaky roster test",
        "html_url": "https://github.com/acme/mentor-agent/pull/42",
        "state": "open",
        "draft": False,
        "merged": False,
        "updated_at": "2026-08-13T09:00:00Z",
        "user": {"login": "amal"},
        "assignee": {"login": "sarah-b"},
        "requested_reviewers": [],
    },
}


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _sign(body: str, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _linear_sign(body: str, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


class _Settings:
    def __init__(
        self,
        github_webhook_secret: str = SECRET,
        linear_webhook_secret: str = SECRET,
        jira_webhook_token: str = "test-jira-token",
        webhook_owner_user_id: str = "usr_webhook1",
        database_url: str = "",
    ):
        self.github_webhook_secret = github_webhook_secret
        self.linear_webhook_secret = linear_webhook_secret
        self.jira_webhook_token = jira_webhook_token
        self.webhook_owner_user_id = webhook_owner_user_id
        self.database_url = database_url


def test_rejects_requests_with_no_secret_configured(monkeypatch):
    monkeypatch.setattr(
        "app.triggers.webhooks.webhook_router.get_settings",
        lambda: _Settings(github_webhook_secret=""),
    )

    body = json.dumps(_PR_PAYLOAD)
    response = _client().post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Event": "pull_request",
        },
    )

    assert response.status_code == 401


def test_rejects_requests_with_a_bad_signature(monkeypatch):
    monkeypatch.setattr(
        "app.triggers.webhooks.webhook_router.get_settings", lambda: _Settings()
    )

    body = json.dumps(_PR_PAYLOAD)
    response = _client().post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": _sign(body, secret="wrong"),
            "X-GitHub-Event": "pull_request",
        },
    )

    assert response.status_code == 401


def test_valid_pull_request_event_upserts_a_work_item(monkeypatch):
    monkeypatch.setattr(
        "app.triggers.webhooks.webhook_router.get_settings", lambda: _Settings()
    )

    calls = []
    monkeypatch.setattr(
        "app.triggers.webhooks.webhook_router._upsert_work_item",
        lambda owner_user_id, raw: calls.append((owner_user_id, raw)),
    )

    body = json.dumps(_PR_PAYLOAD)
    response = _client().post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Event": "pull_request",
        },
    )

    assert response.status_code == 200
    assert len(calls) == 1
    owner_user_id, raw = calls[0]
    assert owner_user_id == "usr_webhook1"
    assert raw["external_id"] == "acme/mentor-agent#42"


def test_unrelated_event_type_is_acked_and_ignored(monkeypatch):
    monkeypatch.setattr(
        "app.triggers.webhooks.webhook_router.get_settings", lambda: _Settings()
    )

    calls = []
    monkeypatch.setattr(
        "app.triggers.webhooks.webhook_router._upsert_work_item",
        lambda owner_user_id, raw: calls.append((owner_user_id, raw)),
    )

    body = json.dumps({"action": "created"})
    response = _client().post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Event": "star",
        },
    )

    assert response.status_code == 200
    assert calls == []


def test_missing_owner_config_is_acked_without_upserting(monkeypatch):
    monkeypatch.setattr(
        "app.triggers.webhooks.webhook_router.get_settings",
        lambda: _Settings(webhook_owner_user_id=""),
    )

    calls = []
    monkeypatch.setattr(
        "app.triggers.webhooks.webhook_router._upsert_work_item",
        lambda owner_user_id, raw: calls.append((owner_user_id, raw)),
    )

    body = json.dumps(_PR_PAYLOAD)
    response = _client().post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Event": "pull_request",
        },
    )

    assert response.status_code == 200
    assert calls == []


_LINEAR_ISSUE_PAYLOAD = {
    "action": "update",
    "type": "Issue",
    "data": {
        "identifier": "MENT-214",
        "title": "Fix flaky roster test",
        "state": {"name": "In Progress"},
        "updatedAt": "2026-08-13T09:00:00Z",
        "url": "https://linear.app/acme/issue/MENT-214",
        "assignee": {"email": "sarah@acme.com"},
    },
}


def test_rejects_linear_requests_with_a_bad_signature(monkeypatch):
    monkeypatch.setattr(
        "app.triggers.webhooks.webhook_router.get_settings", lambda: _Settings()
    )

    body = json.dumps(_LINEAR_ISSUE_PAYLOAD)
    response = _client().post(
        "/webhooks/linear",
        content=body,
        headers={"Linear-Signature": _linear_sign(body, secret="wrong")},
    )

    assert response.status_code == 401


def test_valid_linear_issue_event_upserts_a_work_item(monkeypatch):
    monkeypatch.setattr(
        "app.triggers.webhooks.webhook_router.get_settings", lambda: _Settings()
    )

    calls = []
    monkeypatch.setattr(
        "app.triggers.webhooks.webhook_router._upsert_work_item",
        lambda owner_user_id, raw: calls.append((owner_user_id, raw)),
    )

    body = json.dumps(_LINEAR_ISSUE_PAYLOAD)
    response = _client().post(
        "/webhooks/linear",
        content=body,
        headers={"Linear-Signature": _linear_sign(body)},
    )

    assert response.status_code == 200
    assert len(calls) == 1
    owner_user_id, raw = calls[0]
    assert owner_user_id == "usr_webhook1"
    assert raw["external_id"] == "MENT-214"


_JIRA_ISSUE_PAYLOAD = {
    "webhookEvent": "jira:issue_updated",
    "issue": {
        "key": "SCRUM-2",
        "fields": {
            "summary": "Fix flaky roster test",
            "status": {"name": "To Do"},
            "assignee": {"emailAddress": "sarah@acme.com"},
            "duedate": "2026-08-20",
            "updated": "2026-08-13T09:00:00.000+0000",
        },
    },
}


def test_rejects_jira_requests_with_no_token_configured(monkeypatch):
    monkeypatch.setattr(
        "app.triggers.webhooks.webhook_router.get_settings",
        lambda: _Settings(jira_webhook_token=""),
    )

    response = _client().post(
        "/webhooks/jira?token=anything", json=_JIRA_ISSUE_PAYLOAD
    )

    assert response.status_code == 401


def test_rejects_jira_requests_with_a_wrong_token(monkeypatch):
    monkeypatch.setattr(
        "app.triggers.webhooks.webhook_router.get_settings", lambda: _Settings()
    )

    response = _client().post(
        "/webhooks/jira?token=wrong-token", json=_JIRA_ISSUE_PAYLOAD
    )

    assert response.status_code == 401


def test_valid_jira_issue_event_upserts_a_work_item(monkeypatch):
    monkeypatch.setattr(
        "app.triggers.webhooks.webhook_router.get_settings", lambda: _Settings()
    )
    monkeypatch.setenv("JIRA_BASE_URL", "https://acme.atlassian.net")

    calls = []
    monkeypatch.setattr(
        "app.triggers.webhooks.webhook_router._upsert_work_item",
        lambda owner_user_id, raw: calls.append((owner_user_id, raw)),
    )

    response = _client().post(
        "/webhooks/jira?token=test-jira-token", json=_JIRA_ISSUE_PAYLOAD
    )

    assert response.status_code == 200
    assert len(calls) == 1
    owner_user_id, raw = calls[0]
    assert owner_user_id == "usr_webhook1"
    assert raw["external_id"] == "SCRUM-2"
    assert raw["url"] == "https://acme.atlassian.net/browse/SCRUM-2"
