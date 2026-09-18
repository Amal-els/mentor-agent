from app.ingest.github_webhook import adapt_issue_event, adapt_pull_request_event

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
        "requested_reviewers": [{"login": "marc"}],
    },
}

_ISSUE_PAYLOAD = {
    "action": "opened",
    "repository": {"full_name": "acme/mentor-agent"},
    "issue": {
        "number": 7,
        "title": "Flaky test in CI",
        "html_url": "https://github.com/acme/mentor-agent/issues/7",
        "state": "open",
        "updated_at": "2026-08-13T08:00:00Z",
        "user": {"login": "amal"},
        "assignee": None,
    },
}


def test_adapt_pull_request_event_maps_standard_fields():
    row = adapt_pull_request_event(_PR_PAYLOAD)

    assert row["source"] == "github"
    assert row["external_id"] == "acme/mentor-agent#42"
    assert row["title"] == "Fix flaky roster test"
    assert row["status"] == "open"
    assert row["url"] == "https://github.com/acme/mentor-agent/pull/42"
    assert row["updated_at"] == "2026-08-13T09:00:00Z"
    assert row["actor_reference_key"] == "github:sarah-b"


def test_adapt_pull_request_event_prefers_assignee_over_author():
    row = adapt_pull_request_event(_PR_PAYLOAD)

    assert row["actor_reference_key"] == "github:sarah-b"


def test_adapt_pull_request_event_falls_back_to_author_with_no_assignee():
    payload = {
        **_PR_PAYLOAD,
        "pull_request": {**_PR_PAYLOAD["pull_request"], "assignee": None},
    }

    row = adapt_pull_request_event(payload)

    assert row["actor_reference_key"] == "github:amal"


def test_adapt_pull_request_event_flags_blocks_others_when_reviewers_requested():
    row = adapt_pull_request_event(_PR_PAYLOAD)

    assert row["blocks_others"] is True


def test_adapt_pull_request_event_no_reviewers_is_not_blocking():
    payload = {
        **_PR_PAYLOAD,
        "pull_request": {**_PR_PAYLOAD["pull_request"], "requested_reviewers": []},
    }

    row = adapt_pull_request_event(payload)

    assert row["blocks_others"] is False


def test_adapt_pull_request_event_status_is_merged_when_merged():
    payload = {
        **_PR_PAYLOAD,
        "pull_request": {**_PR_PAYLOAD["pull_request"], "merged": True},
    }

    row = adapt_pull_request_event(payload)

    assert row["status"] == "merged"


def test_adapt_pull_request_event_status_is_draft_when_draft():
    payload = {
        **_PR_PAYLOAD,
        "pull_request": {**_PR_PAYLOAD["pull_request"], "draft": True},
    }

    row = adapt_pull_request_event(payload)

    assert row["status"] == "draft"


def test_adapt_pull_request_event_returns_none_without_a_pull_request_key():
    assert adapt_pull_request_event({"action": "opened"}) is None


def test_adapt_issue_event_maps_standard_fields():
    row = adapt_issue_event(_ISSUE_PAYLOAD)

    assert row["source"] == "github"
    assert row["external_id"] == "acme/mentor-agent#7"
    assert row["title"] == "Flaky test in CI"
    assert row["status"] == "open"
    assert row["actor_reference_key"] == "github:amal"
    assert row["blocks_others"] is False


def test_adapt_issue_event_skips_a_pull_request_disguised_as_an_issue():
    # GitHub fires "issues" events for PRs too — the issue object carries
    # a "pull_request" key in that case, which adapt_pull_request_event
    # already handles; skip here to avoid double-counting.
    payload = {
        **_ISSUE_PAYLOAD,
        "issue": {**_ISSUE_PAYLOAD["issue"], "pull_request": {"url": "..."}},
    }

    assert adapt_issue_event(payload) is None


def test_adapt_issue_event_returns_none_without_an_issue_key():
    assert adapt_issue_event({"action": "opened"}) is None
