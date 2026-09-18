from app.ingest.jira_webhook import adapt_issue_event

_ISSUE_PAYLOAD = {
    "timestamp": 1755075600000,
    "webhookEvent": "jira:issue_updated",
    "issue": {
        "id": "10001",
        "key": "SCRUM-2",
        "fields": {
            "summary": "Fix flaky roster test",
            "status": {"name": "To Do"},
            "assignee": {
                "emailAddress": "sarah@acme.com",
                "displayName": "Sarah Ben Youssef",
            },
            "duedate": "2026-08-20",
            "updated": "2026-08-13T09:00:00.000+0000",
        },
    },
}

BASE_URL = "https://acme.atlassian.net"


def test_adapt_issue_event_maps_standard_fields():
    row = adapt_issue_event(_ISSUE_PAYLOAD, BASE_URL)

    assert row["source"] == "jira"
    assert row["external_id"] == "SCRUM-2"
    assert row["title"] == "Fix flaky roster test"
    assert row["status"] == "To Do"
    assert row["url"] == "https://acme.atlassian.net/browse/SCRUM-2"
    assert row["due_at"] == "2026-08-20"
    assert row["updated_at"] == "2026-08-13T09:00:00.000+0000"
    assert row["actor_reference_key"] == "jira:sarah@acme.com"
    assert row["blocks_others"] is False


def test_adapt_issue_event_falls_back_to_the_key_with_no_assignee():
    payload = {
        **_ISSUE_PAYLOAD,
        "issue": {
            **_ISSUE_PAYLOAD["issue"],
            "fields": {**_ISSUE_PAYLOAD["issue"]["fields"], "assignee": None},
        },
    }

    row = adapt_issue_event(payload, BASE_URL)

    assert row["actor_reference_key"] == "jira:SCRUM-2"


def test_adapt_issue_event_returns_none_without_an_issue_key():
    assert adapt_issue_event({"webhookEvent": "jira:issue_updated"}, BASE_URL) is None


def test_adapt_issue_event_url_is_none_without_a_base_url():
    row = adapt_issue_event(_ISSUE_PAYLOAD, "")

    assert row["url"] is None
