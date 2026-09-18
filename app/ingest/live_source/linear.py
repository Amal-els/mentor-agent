"""Live Linear SourceClient (app/ingest/base.py) backed by
mcp-server-linear. Split out of the former app/ingest/live_source.py —
see app/ingest/live_source/__init__.py's own docstring for the split's
full reasoning; every class/adapter here is re-exported from there so
`from app.ingest.live_source import LiveLinearClient` still works
unchanged."""

import os

from app.ingest.base import Healthy, Unauthorized, Window
from app.tools.mcp_config import call_tool, linear_mcp_spec


def _adapt_linear_issue(raw: dict) -> dict:
    external_id = raw.get("identifier") or raw.get("id", "")
    assignee_email = (
        raw.get("assignee", {}).get("email") if raw.get("assignee") else None
    )
    return {
        "external_id": external_id,
        "source": "linear",
        "title": raw.get("title", ""),
        "status": raw.get("state", {}).get("name", ""),
        "due_at": raw.get("dueDate"),
        "updated_at": raw.get("updatedAt"),
        "url": raw.get("url"),
        "actor_reference_key": f"linear:{assignee_email or external_id}",
    }


class LiveLinearClient:
    """Backed by mcp-server-linear's `linear_search_issues` tool (real
    schema confirmed via live session.list_tools() introspection).
    Requires LINEAR_API_KEY (our own env var name). The spawned server
    itself reads its token from LINEAR_ACCESS_TOKEN, per the package's
    README — not LINEAR_API_KEY, despite the name similarity; a live run
    caught this exact mismatch (server started with no token and every
    tool call failed with "Not authenticated. Call linear_auth first.")."""

    source = "linear"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("LINEAR_API_KEY")

    def _spec(self):
        return linear_mcp_spec(self.api_key)

    def health(self):
        if not self.api_key:
            return Unauthorized()
        return Healthy()

    def fetch(self, window: Window, owner_user_id: str) -> list[dict]:
        try:
            raw = call_tool(self._spec(), "linear_search_issues", {"first": 50})
        except Exception as exc:
            raise RuntimeError(f"linear fetch failed: {exc}") from exc

        # real shape (confirmed live): {"issues": {"pageInfo": {...},
        # "nodes": [...]}} — a GraphQL connection, one level deeper than
        # a flat {"issues": [...]}.
        if isinstance(raw, dict):
            issues_field = raw.get("issues", raw)
            issues = (
                issues_field.get("nodes", [])
                if isinstance(issues_field, dict)
                else issues_field
            )
        else:
            issues = raw
        return [_adapt_linear_issue(i) for i in issues]
