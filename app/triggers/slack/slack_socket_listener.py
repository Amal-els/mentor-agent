import collections
import logging
import os
import threading
import requests
from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.db import get_engine, get_session_factory
from app.core.models import User
from app.core.scope import OwnerScope
from app.delivery.affordances import record_feedback
from app.delivery.cards import (
    FEEDBACK_DOWN_ACTION_ID,
    FEEDBACK_UP_ACTION_ID,
    FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID,
    SHOW_FULL_SHORTLIST_ACTION_ID,
    parse_feedback_value,
)
from app.delivery.slack_deliverer import SlackDeliverer
from app.ingest.live_source import _mentions_viewer
from app.sub_agents.friday_review.sub_agents.deliver.agent import confirm_and_log_ledger_items
from app.triggers.slack.slack_command import (
    parse_slash_command,
    resolve_owner_user_id,
    run_prep_command,
    run_pulse_command,
    run_review_command,
    run_shortlist_command,
)
from app.triggers.slack.slack_message_ingest import (
    _AgendaMentionContext,
    _persist_slack_message_with_own_session,
    _resolve_agenda_pair_contexts,
)

_FEEDBACK_SIGNAL_BY_ACTION_ID = {
    FEEDBACK_UP_ACTION_ID: "up",
    FEEDBACK_DOWN_ACTION_ID: "down",
}

load_dotenv()

logger = logging.getLogger(__name__)

PULSE_ALIASES = ("pulse", "morning", "brief", "today")
PREP_ALIASES = ("prep", "dossier", "meeting", "briefing")
REVIEW_ALIASES = ("review", "reflection", "friday", "weekly")
NOT_LINKED_TEXT = (
    "Your Slack account isn't linked to a Mentor Agent user yet "
    "— ask an admin to run `link-slack`."
)

PULSE_ACK_TEXT = "🎯 Got it — pulling your pulse together..."

_seen_event_ids: collections.deque = collections.deque(maxlen=500)

def _already_seen(event_id: str | None) -> bool:
    if not event_id:
        return False
    if event_id in _seen_event_ids:
        return True
    _seen_event_ids.append(event_id)
    return False

def _matches_pulse_alias(text: str) -> bool:
    lowered = text.lower()
    return any(alias in lowered for alias in PULSE_ALIASES)

def _matches_prep_alias(text: str) -> bool:
    lowered = text.lower()
    return any(alias in lowered for alias in PREP_ALIASES)

def _matches_review_alias(text: str) -> bool:
    lowered = text.lower()
    return any(alias in lowered for alias in REVIEW_ALIASES)

def _reply(response_url: str | None, text: str) -> None:
    if not response_url:
        logger.warning("slack socket mode: no response_url to reply with %r", text)
        return
    try:
        requests.post(response_url, json={"text": text}, timeout=5)
    except requests.RequestException:
        logger.exception("slack socket mode: failed to POST reply to response_url")

def _post_ack_message(channel_id: str, requester_slack_id: str | None = None) -> str | None:
    text = PULSE_ACK_TEXT
    if requester_slack_id is not None:
        text = f"<@{requester_slack_id}> {text}"
    deliverer = SlackDeliverer()
    result = deliverer.post_channel_message(channel_id, text)
    return result.get("ts")

def _run_pulse_command_with_own_session(
    session_factory: sessionmaker,
    owner_user_id: str,
    channel_id: str | None,
    thread_ts: str | None = None,
    dm_thread_ts: str | None = None,
) -> None:
    session = session_factory()
    try:
        run_pulse_command(
            session,
            owner_user_id,
            channel_id,
            thread_ts=thread_ts,
            dm_thread_ts=dm_thread_ts,
        )
    finally:
        session.close()

def _run_prep_command_with_own_session(
    session_factory: sessionmaker,
    owner_user_id: str,
) -> None:
    session = session_factory()
    try:
        run_prep_command(session, owner_user_id)
    finally:
        session.close()

def _run_review_command_with_own_session(
    session_factory: sessionmaker,
    owner_user_id: str,
) -> None:
    session = session_factory()
    try:
        run_review_command(session, owner_user_id)
    finally:
        session.close()

def _run_confirm_log_with_own_session(
    session_factory: sessionmaker,
    owner_user_id: str,
    delivery_id: str,
    dm_channel_id: str,
    message_ts: str | None,
) -> None:
    session = session_factory()
    try:
        scope = OwnerScope(owner_user_id=owner_user_id, session=session)
        created = confirm_and_log_ledger_items(scope, SystemClock(), delivery_id)
        deliverer = SlackDeliverer()
        if deliverer.enabled and dm_channel_id:
            text = (
                f"Logged {len(created)} item(s) to your ledger."
                if created
                else "Nothing new to log."
            )
            deliverer.post_thread_reply(
                dm_channel_id,
                [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
                text,
                thread_ts=message_ts,
            )
    except Exception:
        logger.exception(
            "slack friday review confirm-and-log: failed for owner=%s delivery=%s",
            owner_user_id,
            delivery_id,
        )
    finally:
        session.close()

def _run_shortlist_command_with_own_session(
    session_factory: sessionmaker,
    owner_user_id: str,
    dm_thread_ts: str | None,
) -> None:
    session = session_factory()
    try:
        run_shortlist_command(session, owner_user_id, dm_thread_ts=dm_thread_ts)
    finally:
        session.close()

def _run_feedback_command_with_own_session(
    session_factory: sessionmaker,
    owner_user_id: str,
    delivery_id: str,
    item_id: str,
    item_type: str,
    signal: str,
) -> None:
    session = session_factory()
    try:
        scope = OwnerScope(owner_user_id=owner_user_id, session=session)
        record_feedback(scope, SystemClock(), delivery_id, item_id, item_type, signal)
    except Exception:
        logger.exception(
            "slack pulse feedback: failed to record for owner=%s item_id=%s",
            owner_user_id,
            item_id,
        )
    finally:
        session.close()

def _schedule(target, *args) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()

def _handle_slash_command(payload: dict, session_factory: sessionmaker) -> None:
    response_url = payload.get("response_url")
    command_payload = parse_slash_command(payload)

    if command_payload.text not in ("", "pulse", "prep", "review"):
        _reply(
            response_url,
            f"`{command_payload.text}` isn't supported yet — only "
            "`/mentor pulse`, `/mentor prep`, and `/mentor review`.",
        )
        return

    session: Session = session_factory()
    try:
        owner_user_id = resolve_owner_user_id(session, command_payload.slack_user_id)
    finally:
        session.close()

    if owner_user_id is None:
        _reply(response_url, NOT_LINKED_TEXT)
        return

    if command_payload.text == "prep":
        _reply(response_url, "On it — your dossier is on its way.")
        _schedule(
            _run_prep_command_with_own_session,
            session_factory,
            owner_user_id,
        )
        return

    if command_payload.text == "review":
        _reply(response_url, "On it — your Friday reflection is on its way.")
        _schedule(
            _run_review_command_with_own_session,
            session_factory,
            owner_user_id,
        )
        return

    is_channel = command_payload.channel_id.startswith("C")
    ack_ts = _post_ack_message(
        command_payload.channel_id,
        requester_slack_id=command_payload.slack_user_id if is_channel else None,
    )

    if is_channel:
        _schedule(
            _run_pulse_command_with_own_session,
            session_factory,
            owner_user_id,
            command_payload.channel_id,
            ack_ts,
            None,
        )
    else:
        _schedule(
            _run_pulse_command_with_own_session,
            session_factory,
            owner_user_id,
            None,
            None,
            ack_ts,
        )

def _scheduled_linked_users(session: Session) -> list[User]:
    raw = os.environ.get("PULSE_SCHEDULED_OWNER_IDS", "")
    ids = [o.strip() for o in raw.split(",") if o.strip()]
    if not ids:
        return []
    return list(
        session.execute(
            select(User).where(User.id.in_(ids), User.slack_user_id.is_not(None))
        )
        .scalars()
        .all()
    )

def _handle_events_api(payload: dict, session_factory: sessionmaker) -> None:
    if _already_seen(payload.get("event_id")):
        return
    event = payload.get("event", {})
    if (
        event.get("type") != "message"
        or event.get("bot_id") is not None
        or event.get("subtype") is not None
    ):
        return
    channel_id = event.get("channel", "")
    text = event.get("text", "")
    if event.get("channel_type") == "im":
        session: Session = session_factory()
        try:
            owner_user_id = resolve_owner_user_id(session, event.get("user"))
            if owner_user_id is None:
                if _matches_pulse_alias(text):
                    SlackDeliverer().send_text_message(channel_id, NOT_LINKED_TEXT)
                return
            mentioned_owner_ids = (
                [
                    user.id
                    for user in _scheduled_linked_users(session)
                    if user.id != owner_user_id
                    and _mentions_viewer(event, user.slack_user_id)
                ]
                if text
                else []
            )
        finally:
            session.close()
        for persist_owner_id in mentioned_owner_ids or [owner_user_id]:
            _schedule(
                _persist_slack_message_with_own_session,
                session_factory,
                persist_owner_id,
                event,
                channel_id,
            )
        if _matches_prep_alias(text):
            _schedule(
                _run_prep_command_with_own_session,
                session_factory,
                owner_user_id,
            )
        elif _matches_review_alias(text):
            _schedule(
                _run_review_command_with_own_session,
                session_factory,
                owner_user_id,
            )
        elif _matches_pulse_alias(text):
            ack_ts = _post_ack_message(channel_id)
            _schedule(
                _run_pulse_command_with_own_session,
                session_factory,
                owner_user_id,
                None,
                None,
                ack_ts,
            )
        return

    if not text:
        return
    if not any(o.strip() for o in os.environ.get("PULSE_SCHEDULED_OWNER_IDS", "").split(",")):
        return
    session = session_factory()
    try:
        matches: list[tuple[str, list[tuple[str, _AgendaMentionContext]]]] = []
        for user in _scheduled_linked_users(session):
            pair_contexts = _resolve_agenda_pair_contexts(session, user.id, event)
            directly_mentions = _mentions_viewer(event, user.slack_user_id)
            if pair_contexts or directly_mentions:
                matches.append((user.id, pair_contexts))
    finally:
        session.close()
    for owner_user_id, pair_contexts in matches:
        _schedule(
            _persist_slack_message_with_own_session,
            session_factory,
            owner_user_id,
            event,
            channel_id,
            pair_contexts,
        )

def _handle_interactive(payload: dict, session_factory: sessionmaker) -> None:
    actions = payload.get("actions") or []
    action_id = actions[0].get("action_id") if actions else None
    if action_id not in (
        SHOW_FULL_SHORTLIST_ACTION_ID,
        FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID,
        *_FEEDBACK_SIGNAL_BY_ACTION_ID,
    ):
        return

    slack_user_id = payload.get("user", {}).get("id", "")
    dm_channel_id = payload.get("channel", {}).get("id", "")

    session: Session = session_factory()
    try:
        owner_user_id = resolve_owner_user_id(session, slack_user_id)
    finally:
        session.close()

    if owner_user_id is None:
        return

    if action_id in _FEEDBACK_SIGNAL_BY_ACTION_ID:
        delivery_id, item_type, item_id = parse_feedback_value(actions[0]["value"])
        _schedule(
            _run_feedback_command_with_own_session,
            session_factory,
            owner_user_id,
            delivery_id,
            item_id,
            item_type,
            _FEEDBACK_SIGNAL_BY_ACTION_ID[action_id],
        )
        return

    if action_id == FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID:
        delivery_id = actions[0]["value"]
        message_ts = payload.get("message", {}).get("ts")
        _schedule(
            _run_confirm_log_with_own_session,
            session_factory,
            owner_user_id,
            delivery_id,
            dm_channel_id,
            message_ts,
        )
        return

    if not dm_channel_id:
        return
    message_ts = payload.get("message", {}).get("ts")
    _schedule(
        _run_shortlist_command_with_own_session,
        session_factory,
        owner_user_id,
        message_ts,
    )

def handle_socket_mode_request(client, req, session_factory: sessionmaker) -> None:
    from slack_sdk.socket_mode.response import SocketModeResponse

    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

    if req.type == "slash_commands":
        _handle_slash_command(req.payload, session_factory)
    elif req.type == "events_api":
        _handle_events_api(req.payload, session_factory)
    elif req.type == "interactive":
        _handle_interactive(req.payload, session_factory)

def main() -> None:
    import os
    from threading import Event

    from slack_sdk import WebClient
    from slack_sdk.socket_mode.builtin import SocketModeClient

    settings = get_settings()
    engine = get_engine(settings.database_url)
    session_factory = get_session_factory(engine)

    web_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))
    client = SocketModeClient(app_token=settings.slack_app_token, web_client=web_client)

    def _process(client, req) -> None:
        handle_socket_mode_request(client, req, session_factory)

    client.socket_mode_request_listeners.append(_process)
    client.connect()
    logger.info(
        "slack socket mode: connected, listening for /mentor pulse and /mentor prep"
    )
    Event().wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
