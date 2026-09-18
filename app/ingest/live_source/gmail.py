"""Live Gmail SourceClient (app/ingest/base.py) backed by
@a-bonus/google-docs-mcp's "gmail" tool group. Split out of the former
app/ingest/live_source.py — see app/ingest/live_source/__init__.py's own
docstring for the split's full reasoning; every class/adapter here is
re-exported from there so `from app.ingest.live_source import
LiveGmailClient` still works unchanged."""

import email.utils
import re

from app.ingest.base import Healthy, Unauthorized, Window
from app.tools.mcp_config import McpSession, google_docs_mcp_client_env, google_docs_mcp_spec


def _extract_email_address(from_header: str) -> str:
    match = re.search(r"<([^>]+)>", from_header)
    return match.group(1) if match else from_header


# Gmail's own bulk-mail categorization — triageInbox only ever scopes to
# is:unread, so without this filter every newsletter/promo/social/bulk
# notification sitting unread in the inbox becomes a scored message
# candidate. Found live: a real inbox's unread mail was dominated by
# newsletters (Morning Brew, Shortform), promotions (an Atlassian Jira
# marketing email), and social digests (Facebook Groups) — none of it
# belongs anywhere near a morning pulse, and it inflated the shortlist
# count / "See all N items" button with noise no one asked to triage.
_NOISE_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES"}


def _is_gmail_noise(raw: dict) -> bool:
    if raw.get("isNewsletter", False):
        return True
    return bool(_NOISE_LABELS & set(raw.get("labels", [])))


def _adapt_gmail_message(raw: dict) -> dict:
    from app.ingest.email_echo import extract_source_echo

    message_id = raw.get("id", "")
    thread_id = raw.get("threadId", message_id)
    date_header = raw.get("date")
    sent_at = None
    if date_header:
        try:
            sent_at = email.utils.parsedate_to_datetime(date_header).isoformat()
        except (TypeError, ValueError):
            sent_at = None

    echo_ref = extract_source_echo(raw)

    return {
        "external_id": message_id,
        "source": "gmail",
        "channel": "inbox",
        "sent_at": sent_at,
        "url": f"https://mail.google.com/mail/u/0/#inbox/{thread_id}",
        "is_dm": False,
        # never subject/snippet/bodyExcerpt (AGENT.md privacy rule) — a
        # pointer only, discarded here the same way LiveGoogleDocsClient
        # discards listComments' own `content` field: the text crosses MCP
        # (fetch() requests a real bodyExcerptLength — see its own
        # docstring for why 0 doesn't work) but is never carried past this
        # adapter into the normalized row. Only the derived structured reference
        # (source_echo_of) is retained.
        "body_ref": f"gmail://{message_id}",
        "actor_reference_key": f"gmail:{_extract_email_address(raw.get('from', ''))}",
        "action_requested": bool(raw.get("actionRequested", False)),
        "source_echo_of": echo_ref.to_dict() if echo_ref is not None else None,
    }


class LiveGmailClient:
    """Backed by @a-bonus/google-docs-mcp's `triageInbox` tool — the same
    MCP server LiveGoogleDocsClient already spawns (google_docs_mcp_spec),
    just a different tool group ("gmail", confirmed live via the server's
    own "Registered tool groups" startup log). Real schema and response
    shape confirmed via a live round-trip against a real inbox: `messages`
    is a flat list of {id, threadId, from, domain, to, subject, date,
    snippet, bodyExcerpt, labels, isNewsletter, containsMeetingReference,
    containsQuestion, actionRequested}.

    triageInbox always scopes to `is:unread` server-side (not configurable)
    and additionally accepts an additionalQuery clause — window is turned
    into an "after:<unix-seconds>" query rather than filtered client-side,
    since Gmail's own query language already expresses it more cheaply
    than fetching everything unread and discarding old messages
    afterward.

    _is_gmail_noise then drops newsletters/promotions/social/bulk-update
    mail (isNewsletter, or a CATEGORY_PROMOTIONS/CATEGORY_SOCIAL/
    CATEGORY_UPDATES label) before anything reaches the scorer — found
    live: is:unread alone pulls in every newsletter and marketing email
    sitting unread in the inbox, which both cluttered the shortlist count
    and had no business competing with real correspondence for pulse
    attention.

    bodyExcerptLength is passed as _BODY_EXCERPT_LENGTH (the tool's own
    default, 400), NOT 0 — found live: the installed package's
    triageInbox.js only extracts message text at all when
    bodyExcerptLength > 0 (`const text = args.bodyExcerptLength > 0 ?
    extractTextBody(msg.payload) : ''`), and its containsMeetingReference/
    containsQuestion/actionRequested regexes run against `subject + text`,
    not the raw Gmail payload — passing 0 to "avoid sending body content"
    silently starved every one of those signals down to subject-line-only
    matching (confirmed live: the same real message scored
    actionRequested=true with the tool's default length and
    actionRequested=false with length=0). The text still crosses MCP
    either way; _adapt_gmail_message is what actually enforces the privacy
    rule by discarding subject/snippet/bodyExcerpt before the row is ever
    normalized, the same pattern LiveGoogleDocsClient already uses for
    listComments' full comment text.

    action_requested comes straight from triageInbox's own heuristic
    classification (a keyword/phrase regex over subject + body text, see
    the installed package's triageInbox.js) — requires_reply is left unset
    (None) since triageInbox has no equivalent per-message signal for it;
    normalize_message() treats requires_reply=None as "unknown, preserve
    existing scoring behavior" (see its own docstring / app/salience/
    score.py's score_message()).

    Requires GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and the same one-time
    `npx -y @a-bonus/google-docs-mcp auth` consent flow as
    LiveGoogleDocsClient — found live: a refresh token minted before
    gmail.modify was added to the OAuth consent screen's scope list kept
    failing every gmail.* tool call with a 403 ("Permission denied. Confirm
    the gmail.modify scope was granted.") even after the consent screen was
    updated, since Google doesn't retroactively upgrade an existing refresh
    token's granted scopes — fixed by deleting the cached token.json and
    re-running the consent flow from scratch. Also found live: granting the
    scope alone wasn't enough either — the Gmail API itself has to be
    separately enabled for the GCP project in Cloud Console (APIs &
    Services > Library), a distinct toggle from the OAuth consent screen's
    scope list."""

    source = "gmail"
    _MAX_RESULTS = 20
    # The tool's own default (see triageInbox.js) — large enough for its
    # classification regexes to see real content, small enough to bound
    # per-message payload size. Never persisted (see _adapt_gmail_message).
    _BODY_EXCERPT_LENGTH = 400

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
        # after:<unix-seconds> rather than newer_than:{days}d — Gmail's
        # newer_than: operator only has day-level granularity, which
        # can't express "since 90 seconds ago" at all (it'd round up to
        # newer_than:1d regardless). after: takes a real timestamp, which
        # is what makes app.ingest.seed.seed_live's narrowed incremental
        # window (owner.last_live_seed_at, not a fixed full-day start)
        # actually incremental instead of silently widening back out to a
        # full day on every call.
        try:
            with McpSession(self._spec()) as session:
                result = session.call(
                    "triageInbox",
                    {
                        "maxResults": self._MAX_RESULTS,
                        "additionalQuery": f"after:{int(window.start.timestamp())}",
                        "bodyExcerptLength": self._BODY_EXCERPT_LENGTH,
                    },
                )
        except Exception as exc:
            raise RuntimeError(f"gmail fetch failed: {exc}") from exc

        messages = result.get("messages", []) if isinstance(result, dict) else result
        return [_adapt_gmail_message(m) for m in messages if not _is_gmail_noise(m)]
