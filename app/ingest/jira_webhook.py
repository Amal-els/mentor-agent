"""Adapts Jira Cloud webhook event payloads (jira:issue_created/updated)
into the raw-dict shape app/ingest/normalize.py's normalize_work_item
expects. Jira's webhook payload is NOT the same shape as mcp-jira-cloud's
pre-flattened MCP tool output (app/ingest/live_source.py's
_adapt_jira_issue) — it's the raw REST API issue object, fields nested
under "fields" (summary, status.name, assignee.emailAddress, duedate,
updated), so this is its own adapter rather than a reuse, unlike Linear's
webhook (app/ingest/linear_webhook.py), whose payload does match its own
pull connector's shape exactly.

blocks_others is always False here — the "outward Blocks link" signal
(app/ingest/live_source.py's _blocks_others) needs a separate
jira_get_issue_links call the webhook payload doesn't carry; a future
enhancement could call that MCP tool from the webhook handler, not done
here to keep this receiver's only dependency the webhook payload itself.

STATUS: built from Atlassian's public webhook payload documentation, not
yet live-proven against a real delivery — see
app/ingest/github_webhook.py's module docstring for why that matters."""


def adapt_issue_event(payload: dict, base_url: str) -> dict | None:
    issue = payload.get("issue")
    if issue is None:
        return None
    key = issue.get("key", "")
    if not key:
        return None
    fields = issue.get("fields", {})

    assignee = fields.get("assignee")
    assignee_email = assignee.get("emailAddress") if assignee else None

    return {
        "source": "jira",
        "external_id": key,
        "actor_reference_key": f"jira:{assignee_email or key}",
        "title": fields.get("summary", ""),
        "status": fields.get("status", {}).get("name", ""),
        "url": f"{base_url.rstrip('/')}/browse/{key}" if base_url else None,
        "due_at": fields.get("duedate"),
        "updated_at": fields.get("updated"),
        "blocks_others": False,
    }
