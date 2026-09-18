from app.ingest.linear_webhook import adapt_issue_event

_ISSUE_PAYLOAD = {
    "action": "update",
    "type": "Issue",
    "data": {
        "id": "uuid-1",
        "identifier": "MENT-214",
        "title": "Fix flaky roster test",
        "state": {"name": "In Progress"},
        "dueDate": "2026-08-15",
        "updatedAt": "2026-08-13T09:00:00Z",
        "url": "https://linear.app/acme/issue/MENT-214",
        "assignee": {"email": "sarah@acme.com"},
    },
}


def test_adapt_issue_event_maps_standard_fields():
    row = adapt_issue_event(_ISSUE_PAYLOAD)

    assert row["source"] == "linear"
    assert row["external_id"] == "MENT-214"
    assert row["title"] == "Fix flaky roster test"
    assert row["status"] == "In Progress"
    assert row["url"] == "https://linear.app/acme/issue/MENT-214"
    assert row["actor_reference_key"] == "linear:sarah@acme.com"


def test_adapt_issue_event_returns_none_for_non_issue_types():
    payload = {**_ISSUE_PAYLOAD, "type": "Comment"}

    assert adapt_issue_event(payload) is None


def test_adapt_issue_event_returns_none_without_a_data_key():
    assert adapt_issue_event({"action": "update", "type": "Issue"}) is None
