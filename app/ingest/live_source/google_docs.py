"""Live Google Docs SourceClient (app/ingest/base.py) backed by
@a-bonus/google-docs-mcp. Split out of the former app/ingest/
live_source.py — see app/ingest/live_source/__init__.py's own docstring
for the split's full reasoning; every class/adapter here is re-exported
from there so `from app.ingest.live_source import LiveGoogleDocsClient`
still works unchanged."""

from app.ingest.base import Healthy, Unauthorized, Window
from app.tools.mcp_config import McpSession, google_docs_mcp_client_env, google_docs_mcp_spec


def _adapt_google_docs_comment(raw: dict, document_id: str) -> dict:
    comment_id = raw.get("id", "")
    author = raw.get("author")
    return {
        "external_id": f"{document_id}:{comment_id}",
        "source": "google_docs",
        "channel": document_id,
        "sent_at": raw.get("createdTime"),
        "url": f"https://docs.google.com/document/d/{document_id}/edit",
        "is_dm": False,
        # never the comment text (AGENT.md privacy rule) — a pointer only
        "body_ref": f"gdocs://{document_id}/{comment_id}",
        "actor_reference_key": f"google_docs:{author or comment_id}",
    }


class LiveGoogleDocsClient:
    """Backed by @a-bonus/google-docs-mcp's `listDriveFiles` (enumerate
    recently-modified docs) + `listComments` (per doc) tools — real schema
    and response shape confirmed via a live round-trip: added a throwaway
    comment and reply to a real doc, read both back via listComments/
    getComment, then deleted the comment.

    Two-step fetch, the same "list containers, then fetch per container"
    shape as LiveSlackClient's channel enumeration: there is no single
    "comments across all my docs" endpoint, so this lists the N most
    recently modified Google Docs the account can see, then calls
    listComments once per doc and keeps only unresolved threads. Comments
    are status-driven like work items, not time-windowed — window is
    accepted (protocol shape) but not used to filter.

    listComments' real shape (confirmed live) is pre-flattened same as
    Jira's server: `author` is a plain display-name string, no email or
    accountId — actor_reference_key is built from it directly, same
    caveat as LiveJiraClient. `sent_at` is the comment's own createdTime,
    not its latest reply's — listComments doesn't include replies inline
    (only a replyCount), and calling getComment per-comment just to get a
    fresher timestamp would multiply the number of live calls per doc;
    not done here. There is no per-comment permalink in the response, so
    `url` points at the document itself, not the specific comment anchor.

    Requires GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and a completed
    one-time `npx -y @a-bonus/google-docs-mcp auth` consent flow (stores
    its own refresh token, not passed via env) — health() only checks the
    env vars are set, not that the consent flow has completed, same
    caveat as LiveCalendarClient.

    Real latency found live: this connector's N+1 shape (one
    listDriveFiles + one listComments per doc) made a 20-doc fetch take
    ~2.5 minutes end to end under call_tool()'s one-spawn-per-call design,
    real latency for something in the morning-pulse critical path. Cut
    _MAX_DOCS down from 20 to 6 for that reason — recency, not
    exhaustiveness, is what matters here anyway. Even at 6 docs, the 7
    spawns (1 list + 6 comment lookups) still cost ~43s measured live —
    fixed by switching to McpSession (app/tools/mcp_config.py), which
    opens one process/session for the whole fetch() instead of one per
    tool call."""

    source = "google_docs"
    _MAX_DOCS = 6

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        profile: str | None = None,
    ):
        # Per-user token profile override (GOOGLE_MCP_PROFILE) — see
        # app.ingest.google_credential_store.materialize_docs_profile.
        # None means the legacy single-shared-account behavior every
        # caller had before per-user Google credentials existed.
        self.profile = profile
        env_id, env_secret = google_docs_mcp_client_env(profile)
        self.client_id = client_id or env_id
        self.client_secret = client_secret or env_secret

    def _spec(self):
        return google_docs_mcp_spec(self.client_id, self.client_secret, self.profile)

    def health(self):
        if not (self.client_id and self.client_secret):
            return Unauthorized()
        return Healthy()

    def fetch(self, window: Window, owner_user_id: str) -> list[dict]:
        try:
            with McpSession(self._spec()) as session:
                files_result = session.call(
                    "listDriveFiles",
                    {"mimeType": "document", "maxResults": self._MAX_DOCS},
                )
                files = (
                    files_result.get("files", files_result)
                    if isinstance(files_result, dict)
                    else files_result
                )

                comments: list[dict] = []
                for file in files:
                    document_id = file.get("id")
                    if not document_id:
                        continue
                    comments_result = session.call(
                        "listComments", {"documentId": document_id}
                    )
                    raw_comments = (
                        comments_result.get("comments", comments_result)
                        if isinstance(comments_result, dict)
                        else comments_result
                    )
                    comments.extend(
                        _adapt_google_docs_comment(c, document_id)
                        for c in raw_comments
                        if not c.get("resolved", False)
                    )
        except Exception as exc:
            raise RuntimeError(f"google_docs fetch failed: {exc}") from exc

        return comments
