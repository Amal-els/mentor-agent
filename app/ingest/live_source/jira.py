"""Live Jira SourceClient (app/ingest/base.py) backed by mcp-jira-cloud.
Split out of the former app/ingest/live_source.py — see app/ingest/
live_source/__init__.py's own docstring for the split's full reasoning;
every class/adapter here is re-exported from there so
`from app.ingest.live_source import LiveJiraClient` still works
unchanged."""

import os

from app.ingest.base import Healthy, Unauthorized, Window
from app.tools.mcp_config import McpSession, jira_mcp_spec


def _adapt_jira_issue(raw: dict, base_url: str, blocks_others: bool = False) -> dict:
    key = raw.get("key", "")
    assignee = raw.get("assignee")
    return {
        "external_id": key,
        "source": "jira",
        "title": raw.get("summary", ""),
        "status": raw.get("status", ""),
        "due_at": raw.get("duedate"),
        "updated_at": raw.get("updated"),
        "url": f"{base_url.rstrip('/')}/browse/{key}" if key else None,
        "actor_reference_key": f"jira:{assignee or key}",
        "blocks_others": blocks_others,
    }


def _blocks_others(links_response: dict | list) -> bool:
    """ "Blocking others" (command-center-brief priority table): this issue
    has a downstream dependent, i.e. an outward "Blocks" link — confirmed
    live via jira_get_issue_links's real response shape (created two
    throwaway linked issues in a real Jira Cloud project, SCRUM-2 Blocks
    SCRUM-3): {"issueKey", "links": [{"type", "direction": "outward"|
    "inward", "linkedIssue": {...}}]}. Inward links ("is blocked by") are
    the opposite relationship and don't count."""
    if not isinstance(links_response, dict):
        return False
    links = links_response.get("links", [])
    return any(
        link.get("type") == "Blocks" and link.get("direction") == "outward"
        for link in links
    )


class LiveJiraClient:
    """Backed by mcp-jira-cloud's `jira_search_issues` tool (real schema
    and response shape confirmed via a live round-trip: created a
    throwaway issue in a real Jira Cloud project, searched for it, and
    deleted it — see docs/plans/morning-pulse.md).

    Two things found only by that live round-trip, not from the tool's
    own docs:

    1. `jira_get_my_open_issues` (the obvious "issues assigned to me"
       tool) returns a near-empty shape — just {summary, description,
       key}, no status/updated/duedate/assignee. Useless for this app's
       fields. `jira_search_issues` with JQL `assignee = currentUser()
       AND resolution = Unresolved` returns the full field set instead,
       and is what this client actually uses.

    2. This server pre-flattens Jira's REST API response: `status`,
       `assignee`, `reporter`, `issuetype`, `project` all come back as
       plain strings (e.g. "To Do", "Amal Bahri"), not the nested
       {name/id/emailAddress} objects the raw Jira REST API returns.
       There is no email address or accountId in the search response,
       and no `self`/url field either — actor_reference_key is built
       from the assignee display name (identity resolution's job to
       reconcile with other sources, not this connector's), and the
       browse URL is built manually from JIRA_BASE_URL + the issue key.

    Also: unbounded JQL (e.g. bare "order by created desc") is rejected
    by this Jira Cloud instance with a 400 ("Unbounded JQL queries are
    not allowed here") — the JQL used here is bounded by assignee and
    resolution, not by date, so this doesn't apply, but any other query
    added later needs a restriction clause.

    Requires JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN."""

    source = "jira"

    def __init__(
        self,
        base_url: str | None = None,
        email: str | None = None,
        api_token: str | None = None,
    ):
        self.base_url = base_url or os.environ.get("JIRA_BASE_URL")
        self.email = email or os.environ.get("JIRA_EMAIL")
        self.api_token = api_token or os.environ.get("JIRA_API_TOKEN")

    def _spec(self):
        return jira_mcp_spec(self.base_url, self.email, self.api_token)

    def health(self):
        if not (self.base_url and self.email and self.api_token):
            return Unauthorized()
        return Healthy()

    def fetch(self, window: Window, owner_user_id: str) -> list[dict]:
        try:
            with McpSession(self._spec()) as session:
                raw = session.call(
                    "jira_search_issues",
                    {
                        "jql": "assignee = currentUser() AND resolution = Unresolved "
                        "order by updated desc",
                        "maxResults": 50,
                    },
                )

                issues = raw.get("issues", raw) if isinstance(raw, dict) else raw

                def blocks_others(key: str) -> bool:
                    if not key:
                        return False
                    try:
                        links = session.call(
                            "jira_get_issue_links", {"issueIdOrKey": key}
                        )
                    except Exception:
                        # a links lookup failing for one issue must not fail
                        # the whole fetch — this issue just scores as
                        # blocks_others=False
                        return False
                    return _blocks_others(links)

                return [
                    _adapt_jira_issue(
                        i, self.base_url or "", blocks_others(i.get("key", ""))
                    )
                    for i in issues
                ]
        except Exception as exc:
            raise RuntimeError(f"jira fetch failed: {exc}") from exc
