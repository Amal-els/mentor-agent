"""Live Slack SourceClient (app/ingest/base.py) backed by
@modelcontextprotocol/server-slack. Split out of the former app/ingest/
live_source.py — see app/ingest/live_source/__init__.py's own docstring
for the split's full reasoning; every class/adapter here is re-exported
from there so `from app.ingest.live_source import LiveSlackClient` still
works unchanged."""

import datetime
import os
import re

from slack_sdk import WebClient

from app.ingest.base import Healthy, Unauthorized, Window
from app.tools.mcp_config import McpSession, slack_mcp_spec


def _mentions_viewer(raw: dict, viewer_slack_user_id: str) -> bool:
    """Real Slack message text (confirmed live) is a plain string like
    "<@U0BN9MPRTK2> can you review this?" — checked transiently here only
    to decide relevance, never stored (AGENT.md privacy rule; raw["text"]
    never appears in the adapted row _adapt_slack_message returns).
    Excludes the bot's own messages — a live run caught this for real: the
    bot's own reaction-swap/probe messages mention the linked user too
    (e.g. delivery acks), and without this they'd be picked up as if a
    human had tagged the user."""
    if raw.get("bot_id") is not None:
        return False
    return f"<@{viewer_slack_user_id}>" in raw.get("text", "")


_SLACK_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")


def _extract_mentioned_slack_user_ids(text: str) -> list[str]:
    """Structural metadata, not content — a list of Slack user ids the raw
    text @-mentions, computed transiently from raw["text"] the same way
    _mentions_viewer already does (see its own docstring), but generalized
    to every id mentioned rather than checking one specific id. Safe to
    carry into the adapted row despite the "never the message text"
    privacy rule (AGENT.md): this is IDs, not prose — the same category
    of derived signal is_dm/action_requested already are, not the message
    body itself. Used by app.sub_agents.dossier.sub_agents.gather.agent
    to filter Slack signal down to a specific meeting's attendees, which
    needs "who was tagged," not just "who sent it" (actor_reference_key
    alone only ever answers the latter)."""
    return _SLACK_MENTION_RE.findall(text or "")


def resolve_slack_display_name(bot_token: str, slack_user_id: str, cache: dict) -> str | None:
    """Turns a bare Slack user id into a real display name via one
    users.info call — the same "bypass MCP, call slack_sdk directly"
    exception LiveSlackClient._permalink already takes. Free function
    (not a LiveSlackClient method) so app.triggers.slack.slack_socket_listener's
    push path can resolve mentions in real time too, without needing a
    whole LiveSlackClient instance. cache is owned by the caller, not this
    function — scoped to one fetch/one message, not this process — so
    repeated mentions of the same person cost one real API call, not one
    per occurrence. LiveSlackClient._resolve_sender_name below delegates
    here rather than duplicating this call."""
    if slack_user_id in cache:
        return cache[slack_user_id]
    if not bot_token:
        # No token means no real client to call — never attempt the API
        # call wholly on faith just to catch the resulting auth failure
        # (same "creds_available() gate before trying" discipline as
        # app.core.llm; also matters for tests that disable delivery via
        # a bare fake with no real token, which would otherwise pay for a
        # real network round-trip to Slack before failing).
        cache[slack_user_id] = None
        return None
    try:
        result = WebClient(bot_token).users_info(user=slack_user_id)
        profile = result.get("user", {}).get("profile", {}) if isinstance(result, dict) else {}
        name = (
            profile.get("display_name")
            or profile.get("real_name")
            or (result.get("user", {}) or {}).get("real_name")
        )
    except Exception:
        name = None
    cache[slack_user_id] = name
    return name


def resolve_slack_mentions_for_display(text: str, bot_token: str) -> str:
    """Replaces every raw <@U12345> mention tag in text with a readable
    @DisplayName. REAL BUG FOUND AND FIXED (reported live: "the slack
    mentions from the agent aren't eliminated" — raw <@U12345> tags were
    showing up in the UI). Root cause: app.triggers.slack.slack_socket_listener.
    _handle_events_api's agenda-item branch stores a Slack @-mention's raw
    event text verbatim into AgendaItem.text — the one place in this
    codebase that stores real Slack message text at all (Message rows
    never do; see _adapt_slack_message's own "never the message text"
    rule above). A mention like "<@U0BN9MPRTK2> can you review this?" was
    landing in the agenda UI exactly as Slack encoded it — an opaque id no
    user could read, not eliminated/resolved at all.

    Falls back to "@someone" per-id on a resolution failure (no token,
    API error, deactivated user) rather than leaving the raw tag in
    place — never worse than the old behavior, never a crash."""
    if not text or "<@" not in text:
        return text
    cache: dict[str, str | None] = {}

    def _replace(match: re.Match) -> str:
        name = resolve_slack_display_name(bot_token, match.group(1), cache)
        return f"@{name}" if name else "@someone"

    return _SLACK_MENTION_RE.sub(_replace, text)


def _adapt_slack_message(
    raw: dict,
    channel_id: str,
    url: str | None = None,
    channel_name: str | None = None,
    sender_display_name: str | None = None,
) -> dict:
    ts = raw.get("ts", "")
    user = raw.get("user", "")
    return {
        "external_id": ts,
        "source": "slack",
        "channel": channel_id,
        # REAL CHANGE (requested: "resolve user ids and channel ids in
        # slack to the username and channel name") — structural identity
        # metadata, not the message body itself; see Message.channel_
        # name/sender_display_name's own docstring for why this is safe
        # under the same privacy rule mentioned_slack_user_ids/is_dm
        # already are.
        "channel_name": channel_name,
        "sender_display_name": sender_display_name,
        "sent_at": (
            datetime.datetime.fromtimestamp(float(ts), tz=datetime.UTC).isoformat()
            if ts
            else None
        ),
        "url": url,
        "is_dm": channel_id.startswith("D"),
        # never the message text (AGENT.md privacy rule) — a pointer only
        "body_ref": f"slack://{channel_id}/{ts}",
        "actor_reference_key": f"slack:{user}",
        "mentioned_slack_user_ids": _extract_mentioned_slack_user_ids(
            raw.get("text", "")
        ),
    }


class LiveSlackClient:
    """Backed by @modelcontextprotocol/server-slack's `slack_list_channels`
    + `slack_get_channel_history` tools (real schemas confirmed via live
    session.list_tools() introspection). That npm package is marked
    deprecated upstream ("no longer supported") as of this writing — still
    functional, but worth re-checking for a maintained replacement before
    relying on it. Requires SLACK_BOT_TOKEN (and SLACK_TEAM_ID).

    viewer_slack_user_id (the linked owner's own Slack user id — a
    different thing from bot_token, which authenticates the bot, not any
    particular human) is required to actually filter for "mentions of
    you," matching the diagram this connector was built from
    (docs/whiteboards/Data_Flow_Diagram_For_Command_Center_Brief) — a live
    run caught this being missing entirely: without it, fetch() pulled
    *every* message in *every* channel the bot could see, drowning out
    everything else (one real pulse had 6 near-identical "Slack mention"
    entries competing with a single Google Docs comment). Left optional,
    unfiltered, only so existing callers that haven't been updated yet
    don't hard-fail — every real call site should pass it.

    Permalinks: no MCP tool here exposes one (confirmed via live
    list_tools() — no chat_getPermalink equivalent), so this calls the raw
    Slack Web API directly via slack_sdk for that one piece, bypassing the
    "MCP servers as the client" convention just for this — a deliberate,
    narrow exception, not a precedent to generalize from."""

    source = "slack"

    def __init__(
        self,
        bot_token: str | None = None,
        team_id: str | None = None,
        viewer_slack_user_id: str | None = None,
    ):
        self.bot_token = bot_token or os.environ.get("SLACK_BOT_TOKEN")
        self.team_id = team_id or os.environ.get("SLACK_TEAM_ID")
        self.viewer_slack_user_id = viewer_slack_user_id

    def _spec(self):
        return slack_mcp_spec(self.bot_token, self.team_id)

    def health(self):
        if not self.bot_token:
            return Unauthorized()
        return Healthy()

    def _permalink(self, channel_id: str, ts: str) -> str | None:
        try:
            result = WebClient(self.bot_token).chat_getPermalink(
                channel=channel_id, message_ts=ts
            )
            return result.get("permalink")
        except Exception:
            return None

    def _resolve_sender_name(self, slack_user_id: str, cache: dict) -> str | None:
        """A raw message from slack_get_channel_history only ever carries
        the sender's bare Slack user id (Slack's own conversations.history
        API doesn't inline profile info) — this is the one extra Web API
        call (same "bypass MCP, call slack_sdk directly" exception
        _permalink already takes) needed to turn that into a real name.
        cache is one plain dict, owned and passed in by fetch() itself —
        scoped to a single fetch() call, not this instance/process — so a
        channel with the same handful of people posting repeatedly costs
        one real users.info call per person per poll, not one per
        message."""
        return resolve_slack_display_name(self.bot_token, slack_user_id, cache)

    def fetch(self, window: Window, owner_user_id: str) -> list[dict]:
        try:
            spec = self._spec()
            with McpSession(spec) as session:
                channels_result = session.call("slack_list_channels", {})
                channels = (
                    channels_result.get("channels", channels_result)
                    if isinstance(channels_result, dict)
                    else channels_result
                )

                messages: list[dict] = []
                sender_name_cache: dict = {}
                for channel in channels:
                    channel_id = channel.get("id")
                    if not channel_id:
                        continue
                    # slack_list_channels' own channel objects already
                    # carry the real name (a plain Slack conversations.list
                    # field) — free, no extra API call, just previously
                    # discarded.
                    channel_name = channel.get("name")
                    history = session.call(
                        "slack_get_channel_history", {"channel_id": channel_id}
                    )
                    if isinstance(history, dict) and history.get("ok") is False:
                        # bot not a member of this channel (or similar Slack
                        # API error) — skip rather than crash; not every
                        # channel the bot can list is one it has been
                        # invited to read.
                        continue
                    raw_messages = (
                        history.get("messages", history)
                        if isinstance(history, dict)
                        else history
                    )
                    for m in raw_messages:
                        # REAL CHANGE (requested: "remove the slack DMs and
                        # message from the bot itself ... they don't count
                        # as real messages"). Unconditional — not gated on
                        # viewer_slack_user_id being set, unlike the mention
                        # filter below. Every caller of this client
                        # (including the two that deliberately construct it
                        # WITHOUT a viewer_slack_user_id for dossier prep —
                        # see that param's own docstring) needs this: the
                        # Mentor Agent's own delivered cards/DMs live in the
                        # same Slack channels this scans, and a bot message
                        # is never a real signal from a colleague, no matter
                        # who it happens to @-mention.
                        if m.get("bot_id") is not None:
                            continue
                        if (
                            self.viewer_slack_user_id is not None
                            and not _mentions_viewer(m, self.viewer_slack_user_id)
                        ):
                            continue
                        ts = m.get("ts", "")
                        url = self._permalink(channel_id, ts) if ts else None
                        sender_id = m.get("user")
                        sender_display_name = (
                            self._resolve_sender_name(sender_id, sender_name_cache)
                            if sender_id
                            else None
                        )
                        messages.append(
                            _adapt_slack_message(
                                m,
                                channel_id,
                                url,
                                channel_name=channel_name,
                                sender_display_name=sender_display_name,
                            )
                        )
        except Exception as exc:
            raise RuntimeError(f"slack fetch failed: {exc}") from exc

        return messages
