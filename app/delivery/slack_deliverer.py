"""Real Slack delivery, gated behind SLACK_BOT_TOKEN — off (a no-op) unless
set, the same pattern as app/core/llm.py's creds_available(). A pulse is
private (pulse.md): invoked from a channel, it never posts the pulse there
— only a DM, plus an ephemeral acknowledgement in the channel."""

import os
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


def slack_enabled() -> bool:
    return bool(os.environ.get("SLACK_BOT_TOKEN"))


class SlackDeliverer:
    def __init__(self, token: str | None = None):
        self.token = token if token is not None else os.environ.get("SLACK_BOT_TOKEN")

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def deliver(
        self,
        user_slack_id: str,
        blocks: list[dict],
        channel_id: str | None = None,
        thread_ts: str | None = None,
        dm_thread_ts: str | None = None,
    ) -> dict:
        """channel_id set means this was invoked from a channel: the pulse
        itself still only ever goes to DM, plus an ack where it was asked.
        Never posts a pulse publicly.

        thread_ts, if given, threads that channel-side ack as a reply under
        the message it belongs to (e.g. a progress/ack message posted
        before this ran) — but Slack doesn't reliably render an *ephemeral*
        message as the first reply in a thread ("ephemeral messages in
        threads are only shown if there is already an active thread"), so
        a threaded ack is posted as a normal message instead (visible to
        the channel, same as the progress message it replies to — still
        never the pulse content itself). Without thread_ts, the ack stays
        ephemeral (visible only to the requester), matching the original
        behavior for a bare `/mentor pulse` with no progress message.

        dm_thread_ts, if given, threads the pulse card itself as a reply
        under a prior ack message posted directly in the user's DM — the
        DM-invoked counterpart to thread_ts (which only ever threads the
        channel-side ack, never the card). Used when the request itself
        came from inside the DM (slash command or keyword trigger), so
        there's no separate channel to post an ack in — the ack and the
        card end up in the same thread, in the same DM."""
        if not self.enabled:
            return {"sent": False, "reason": "SLACK_BOT_TOKEN not configured"}

        client = WebClient(self.token)
        dm_kwargs = {
            "channel": user_slack_id,
            "blocks": blocks,
            "text": "Your morning pulse is ready",
        }
        if dm_thread_ts is not None:
            dm_kwargs["thread_ts"] = dm_thread_ts
        dm = client.chat_postMessage(**dm_kwargs)
        # dm["channel"] is the resolved DM channel id (e.g. "D0123..."), not
        # the bare user id chat.postMessage was given — files.upload_v2's
        # underlying completeUploadExternal call rejects a bare user id
        # ("input must match regex pattern: ^[CGDZ][A-Z0-9]{8,}$"), unlike
        # chat.postMessage, which auto-opens/resolves the DM. Callers
        # wanting to upload_audio() into this same DM need this, not
        # user_slack_id.
        result = {
            "sent": True,
            "posted_publicly": False,
            "dm_ts": dm["ts"],
            "dm_channel": dm.get("channel"),
        }

        if channel_id is not None:
            if thread_ts is not None:
                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text="sent you that in DM",
                )
            else:
                client.chat_postEphemeral(
                    channel=channel_id, user=user_slack_id, text="sent you that in DM"
                )
            result["ephemeral_ack_channel"] = channel_id

        return result

    def get_permalink(self, channel_id: str, message_ts: str) -> str | None:
        """A clickable link that jumps straight to (and highlights) a
        specific already-sent message — used to point a later "nothing
        new" notice back at the original card without relying on Slack's
        thread UI, which a viewer only sees by scrolling back up and
        expanding it. Returns None on any failure (e.g. the message was
        since deleted) rather than raising — this is always a secondary
        enhancement to a notice that must still get sent either way."""
        if not self.enabled:
            return None
        client = WebClient(self.token)
        try:
            resp = client.chat_getPermalink(channel=channel_id, message_ts=message_ts)
            return resp.get("permalink")
        except SlackApiError:
            return None

    def post_channel_message(self, channel_id: str, text: str) -> dict:
        """A normal, public bot message (not ephemeral) — used for the
        "working on it" progress indicator, which needs a real message ts
        to thread the later completion ack under (response_url posts don't
        return one)."""
        if not self.enabled:
            return {"sent": False, "reason": "SLACK_BOT_TOKEN not configured"}

        client = WebClient(self.token)
        result = client.chat_postMessage(channel=channel_id, text=text)
        return {"sent": True, "ts": result["ts"]}

    def post_thread_reply(
        self,
        channel_id: str,
        blocks: list[dict],
        text: str,
        thread_ts: str | None = None,
    ) -> dict:
        """A blocks message to an arbitrary channel/DM, optionally threaded
        — used for the "See all N items" button's follow-up (always the
        owner's own DM, per the pulse card's own privacy model: the button
        only ever appears on a card that was already DMed, so there's no
        channel-side ack case to handle here, unlike deliver())."""
        if not self.enabled:
            return {"sent": False, "reason": "SLACK_BOT_TOKEN not configured"}

        client = WebClient(self.token)
        kwargs = {"channel": channel_id, "blocks": blocks, "text": text}
        if thread_ts is not None:
            kwargs["thread_ts"] = thread_ts
        result = client.chat_postMessage(**kwargs)
        # channel_id passed in can be a bare user id (chat.postMessage
        # resolves/opens the DM) — result["channel"] is the real resolved
        # channel id, needed as-is for a later reactions.add on this same
        # message (unlike chat.postMessage, that endpoint doesn't document
        # accepting a user id in place of a channel id).
        return {"sent": True, "ts": result["ts"], "channel": result.get("channel")}

    def add_reaction(self, channel_id: str, timestamp: str, emoji: str) -> None:
        """Best-effort: a message can only be reacted to once per emoji per
        user, and a slow double-invocation could race to add the same one —
        already_reacted is not a failure. Used to draw attention back to an
        already-posted "See all N items" reply on a repeat click, instead
        of posting a duplicate."""
        if not self.enabled:
            return

        client = WebClient(self.token)
        try:
            client.reactions_add(channel=channel_id, timestamp=timestamp, name=emoji)
        except SlackApiError as exc:
            if exc.response["error"] != "already_reacted":
                raise

    def send_text_message(self, channel_id: str, text: str) -> None:
        if not self.enabled:
            return

        client = WebClient(self.token)
        client.chat_postMessage(channel=channel_id, text=text)

    def upload_audio(
        self,
        channel_id: str,
        audio_bytes: bytes,
        filename: str,
        title: str,
        thread_ts: str | None = None,
    ) -> dict:
        """The audio-card companion to deliver() — posted as a threaded
        reply under the text card's own message (thread_ts), same DM-only
        privacy model, never a separate top-level message. files_upload_v2
        handles Slack's newer external-upload-URL flow internally (no
        manual getUploadURLExternal/completeUploadExternal dance needed
        here)."""
        if not self.enabled:
            return {"sent": False, "reason": "SLACK_BOT_TOKEN not configured"}

        client = WebClient(self.token)
        result = client.files_upload_v2(
            channel=channel_id,
            content=audio_bytes,
            filename=filename,
            title=title,
            thread_ts=thread_ts,
        )
        return {"sent": True, "ts": result.get("ts")}
