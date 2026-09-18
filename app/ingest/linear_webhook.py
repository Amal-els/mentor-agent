"""Adapts Linear webhook event payloads into the raw-dict shape
app/ingest/normalize.py's normalize_work_item expects — reuses
app/ingest/live_source.py's _adapt_linear_issue directly, since Linear's
webhook payload wraps the exact same Issue shape its GraphQL search API
already returns (data: {identifier, title, state, dueDate, updatedAt,
url, assignee}), just inside an envelope: {action, type, data, ...}.

STATUS: built from Linear's public webhook payload documentation, not yet
live-proven against a real delivery — see app/ingest/github_webhook.py's
module docstring for why that matters, same discipline applies here."""

from app.ingest.live_source import _adapt_linear_issue


def adapt_issue_event(payload: dict) -> dict | None:
    if payload.get("type") != "Issue":
        return None
    data = payload.get("data")
    if data is None:
        return None
    return _adapt_linear_issue(data)
