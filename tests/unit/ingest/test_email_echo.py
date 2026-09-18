"""Unit tests for app/ingest/email_echo.py — extraction of structured origin references from
notification emails (Gmail echoes of GitHub, Jira, Slack, Notion events)."""

import pytest

from app.ingest.email_echo import (
    SourceEchoRef,
    extract_source_echo,
    match_github_echo,
    match_jira_echo,
    match_notion_echo,
    match_slack_echo,
)


# ─────────────────────────────────────── SourceEchoRef ────────────────────────


def test_source_echo_ref_to_dict():
    ref = SourceEchoRef(source="github", external_id="org/repo#42")
    assert ref.to_dict() == {"source": "github", "external_id": "org/repo#42"}


def test_source_echo_ref_from_dict_roundtrip():
    data = {"source": "slack", "external_id": "1787864910.052589"}
    ref = SourceEchoRef.from_dict(data)
    assert ref is not None
    assert ref.source == "slack"
    assert ref.external_id == "1787864910.052589"


def test_source_echo_ref_from_dict_none():
    assert SourceEchoRef.from_dict(None) is None
    assert SourceEchoRef.from_dict({}) is None
    assert SourceEchoRef.from_dict({"source": "github"}) is None


# ─────────────────────────────────────── GitHub ───────────────────────────────


def test_github_match_url_in_body():
    raw = {
        "from": "notifications@github.com",
        "subject": "[org/repo] Some issue title (#42)",
        "bodyExcerpt": "Check https://github.com/org/repo/issues/42 for details.",
    }
    ref = match_github_echo(raw)
    assert ref is not None
    assert ref.source == "github"
    assert ref.external_id == "org/repo#42"


def test_github_match_pull_request_url():
    raw = {
        "from": "notifications@github.com",
        "subject": "[myorg/myrepo] PR title (#99)",
        "bodyExcerpt": "https://github.com/myorg/myrepo/pull/99 was merged.",
    }
    ref = match_github_echo(raw)
    assert ref is not None
    assert ref.external_id == "myorg/myrepo#99"


def test_github_match_subject_pattern():
    raw = {
        "from": "notifications@github.com",
        "subject": "[my-org/my-repo] Fix login bug (#123)",
        "snippet": "Someone commented on this issue.",
    }
    ref = match_github_echo(raw)
    assert ref is not None
    assert ref.source == "github"
    assert ref.external_id == "my-org/my-repo#123"


def test_github_no_match_wrong_sender():
    raw = {
        "from": "noreply@example.com",
        "subject": "[org/repo] Fix login bug (#123)",
    }
    ref = match_github_echo(raw)
    assert ref is None


def test_github_no_match_no_issue_reference():
    raw = {
        "from": "notifications@github.com",
        "subject": "GitHub: Your account security",
        "snippet": "Sign in from a new device.",
    }
    ref = match_github_echo(raw)
    assert ref is None


# ─────────────────────────────────────── Jira ─────────────────────────────────


def test_jira_match_browse_url():
    raw = {
        "from": "jira@company.atlassian.net",
        "subject": "[JIRA] (MENT-214) Issue title",
        "bodyExcerpt": "View at https://company.atlassian.net/browse/MENT-214",
    }
    ref = match_jira_echo(raw)
    assert ref is not None
    assert ref.source == "jira"
    assert ref.external_id == "MENT-214"


def test_jira_match_key_in_subject():
    raw = {
        "from": "noreply@atlassian.net",
        "subject": "MENT-201: Fix deployment pipeline",
        "snippet": "A comment was added.",
    }
    ref = match_jira_echo(raw)
    assert ref is not None
    assert ref.external_id == "MENT-201"


def test_jira_match_key_in_text():
    raw = {
        "from": "jira@acme.atlassian.net",
        "subject": "Jira comment notification",
        "snippet": "You've been assigned to PROJ-999.",
    }
    ref = match_jira_echo(raw)
    assert ref is not None
    assert ref.external_id == "PROJ-999"


def test_jira_no_match_wrong_sender():
    raw = {
        "from": "noreply@example.com",
        "subject": "PROJ-123 something",
    }
    ref = match_jira_echo(raw)
    assert ref is None


# ─────────────────────────────────────── Slack ────────────────────────────────


def test_slack_match_permalink_16digit():
    raw = {
        "from": "notifications@slack.com",
        "subject": "DAG asked about meeting prep",
        "bodyExcerpt": (
            "View this message: "
            "https://mentoragent.slack.com/archives/C0BMFBNRYLA/p1787864910052589"
        ),
    }
    ref = match_slack_echo(raw)
    assert ref is not None
    assert ref.source == "slack"
    assert ref.external_id == "1787864910.052589"


def test_slack_match_permalink_in_snippet():
    raw = {
        "from": "no-reply@slack.com",
        "subject": "New message in #general",
        "snippet": "https://team.slack.com/archives/C01ABC123/p1700000000123456",
    }
    ref = match_slack_echo(raw)
    assert ref is not None
    assert ref.external_id == "1700000000.123456"


def test_slack_no_match_wrong_sender():
    raw = {
        "from": "noreply@example.com",
        "bodyExcerpt": "https://team.slack.com/archives/C01ABC123/p1700000000123456",
    }
    ref = match_slack_echo(raw)
    assert ref is None


def test_slack_no_match_no_permalink():
    raw = {
        "from": "notifications@slack.com",
        "subject": "Your Slack digest",
        "snippet": "You have unread messages.",
    }
    ref = match_slack_echo(raw)
    assert ref is None


# ─────────────────────────────────────── Notion ───────────────────────────────


def test_notion_match_content_mention():
    raw = {
        "from": "notify@notion.so",
        "subject": "Alice mentioned you in Q3 Planning",
        "bodyExcerpt": (
            "Alice mentioned you in a page: "
            "https://www.notion.so/myworkspace/Q3-Planning-aabbccddeeff00112233445566778899"
        ),
    }
    ref = match_notion_echo(raw)
    assert ref is not None
    assert ref.source == "notion"
    # UUID is normalized
    assert "-" in ref.external_id


def test_notion_match_comment_notification():
    raw = {
        "from": "notifications@notion.so",
        "subject": "New comment on Project Spec",
        "bodyExcerpt": (
            "Bob added a comment: "
            "https://notion.so/aabbccddeeff00112233445566778899"
        ),
    }
    ref = match_notion_echo(raw)
    assert ref is not None
    assert ref.source == "notion"


def test_notion_reject_workspace_invite():
    """Workspace membership email must NOT be treated as an echo — it's its own event."""
    raw = {
        "from": "notify@notion.so",
        "subject": "You've been invited to join the workspace",
        "bodyExcerpt": "Click here to join the team workspace.",
    }
    ref = match_notion_echo(raw)
    assert ref is None


def test_notion_reject_role_change():
    raw = {
        "from": "notify@notion.so",
        "subject": "Your workspace role has changed",
        "snippet": "You are now an admin.",
    }
    ref = match_notion_echo(raw)
    assert ref is None


def test_notion_reject_password_reset():
    raw = {
        "from": "notify@notion.so",
        "subject": "Reset your password",
        "snippet": "Click the link to reset your Notion password.",
    }
    ref = match_notion_echo(raw)
    assert ref is None


def test_notion_reject_ambiguous_no_content_keyword():
    """A Notion email with no content-activity keyword and no workspace keyword → no match."""
    raw = {
        "from": "notify@notion.so",
        "subject": "Weekly digest",
        "snippet": "Here is a summary of recent activity.",
    }
    ref = match_notion_echo(raw)
    assert ref is None


def test_notion_uuid_normalization():
    """32-char hex IDs (no hyphens) must be normalized to 36-char hyphenated UUIDs."""
    raw = {
        "from": "notify@notion.so",
        "subject": "Bob commented on your page",
        "bodyExcerpt": (
            "commented on "
            "https://notion.so/aabbccddeeff00112233445566778899"
        ),
    }
    ref = match_notion_echo(raw)
    assert ref is not None
    assert ref.external_id == "aabbccdd-eeff-0011-2233-445566778899"


# ─────────────────────────────────────── Registry / extract_source_echo ───────


def test_extract_source_echo_github():
    raw = {
        "from": "notifications@github.com",
        "subject": "[acme/api] Fix auth (#5)",
        "bodyExcerpt": "https://github.com/acme/api/issues/5",
    }
    ref = extract_source_echo(raw)
    assert ref is not None
    assert ref.source == "github"
    assert ref.external_id == "acme/api#5"


def test_extract_source_echo_non_notification_email():
    raw = {
        "from": "boss@company.com",
        "subject": "Can we talk tomorrow?",
        "snippet": "Let me know your availability.",
    }
    ref = extract_source_echo(raw)
    assert ref is None


def test_extract_source_echo_jira_wins_over_github_subject_overlap():
    """If JIRA sender, Jira matcher should return; GitHub won't fire (wrong sender domain)."""
    raw = {
        "from": "jira@company.atlassian.net",
        "subject": "[JIRA] (PROJ-7) Fixed bug in org/repo#7",
        "bodyExcerpt": "https://company.atlassian.net/browse/PROJ-7",
    }
    ref = extract_source_echo(raw)
    # GitHub matcher won't match (not a github sender), Jira will
    assert ref is not None
    assert ref.source == "github" or ref.source == "jira"
    # Specifically: GitHub check runs first but won't match, so Jira wins
    assert ref.source == "jira"
    assert ref.external_id == "PROJ-7"
