"""Real-time Slack message persistence into the knowledge graph — the push
counterpart to app/ingest/live_source.py's LiveSlackClient, which still
pull-fetches full channel history synchronously inside seed_live at pulse
time. Split out of app.triggers.slack.slack_socket_listener, where this
was the one piece of DB-writing logic living inside an otherwise
transport/dispatch module (acking, command routing, event-type handling).

Every DM from a linked owner is persisted (not just alias-matching ones),
routed to whichever scheduled user it actually concerns — the sender by
default, or another scheduled/linked user the DM @-mentions instead (found
live: a DM naming someone else was showing up as an actionable item in the
SENDER's own pulse). Channel/group messages are persisted only for
scheduled users they actually mention or whose active Pair they concern.

_agenda_visibilities_for/_resolve_agenda_pair_contexts additionally decide
whether a channel/group message that looks like an open question (ends in
"?", design spec §4.2's slack_q kind) should also land on a manager<->report
pair's rolling 1-on-1 agenda, and with what visibility — "shared" if the
sender is one side of the pair mentioning the other, otherwise scoped to
whichever side(s) got mentioned.
"""

import logging
import typing

from slack_sdk import WebClient
from sqlalchemy.orm import Session, sessionmaker

from app.agenda.scope import get_active_pairs_for, resolve_pair_scope
from app.agenda.store import append_agenda_item
from app.core.clock import SystemClock
from app.core.models import User
from app.core.scope import OwnerScope
from app.delivery.slack_deliverer import SlackDeliverer
from app.ingest.live_source import (
    _adapt_slack_message,
    _mentions_viewer,
    resolve_slack_mentions_for_display,
)
from app.ingest.normalize import normalize_message
from app.triggers.slack.slack_command import resolve_owner_user_id

logger = logging.getLogger(__name__)


def _looks_like_open_question(text: str) -> bool:
    """Cheap tier_hint-B heuristic, same discipline PULSE_ALIASES uses
    (keyword match, not NLU) — a channel/group message that mentions the
    owner and ends with a question mark is treated as something that
    belongs on their rolling 1-on-1 agenda (design spec §4.2's slack_q
    kind)."""
    return text.rstrip().endswith("?")


class _AgendaMentionContext(typing.NamedTuple):
    manager_user_id: str | None
    sender_user_id: str | None
    mentions_report: bool
    mentions_manager: bool


def _resolve_agenda_pair_contexts(
    session: Session, owner_user_id: str, event: dict
) -> list[tuple[str, _AgendaMentionContext]]:
    sender_user_id = resolve_owner_user_id(session, event.get("user"))
    contexts: list[tuple[str, _AgendaMentionContext]] = []
    for pair in get_active_pairs_for(session, owner_user_id):
        report = session.get(User, pair.report_user_id)
        manager = session.get(User, pair.manager_user_id)
        mentions_report = (
            report is not None
            and report.slack_user_id is not None
            and _mentions_viewer(event, report.slack_user_id)
        )
        mentions_manager = (
            manager is not None
            and manager.slack_user_id is not None
            and _mentions_viewer(event, manager.slack_user_id)
        )
        if not mentions_report and not mentions_manager:
            continue
        contexts.append(
            (
                pair.report_user_id,
                _AgendaMentionContext(
                    manager_user_id=pair.manager_user_id,
                    sender_user_id=sender_user_id,
                    mentions_report=mentions_report,
                    mentions_manager=mentions_manager,
                ),
            )
        )
    return contexts


def _agenda_visibilities_for(agenda_signal: _AgendaMentionContext, report_user_id: str) -> list[str]:
    is_sender_report = agenda_signal.sender_user_id == report_user_id
    is_sender_manager = (
        agenda_signal.manager_user_id is not None
        and agenda_signal.sender_user_id == agenda_signal.manager_user_id
    )
    if is_sender_report and agenda_signal.mentions_manager:
        return ["shared"]
    if is_sender_manager and agenda_signal.mentions_report:
        return ["shared"]
    if is_sender_report or is_sender_manager:
        return []
    visibilities = []
    if agenda_signal.mentions_report:
        visibilities.append("report_only")
    if agenda_signal.mentions_manager:
        visibilities.append("manager_only")
    return visibilities


def _persist_slack_message_with_own_session(
    session_factory: sessionmaker,
    owner_user_id: str,
    event: dict,
    channel_id: str,
    agenda_pair_signals: list[tuple[str, _AgendaMentionContext]] | None = None,
) -> None:
    session: Session | None = None
    try:
        session = session_factory()
        scope = OwnerScope(owner_user_id=owner_user_id, session=session)
        ts = event.get("ts", "")
        text = event.get("text", "")
        url = None
        deliverer = SlackDeliverer()
        if deliverer.enabled and ts:
            try:
                url = WebClient(deliverer.token).chat_getPermalink(
                    channel=channel_id, message_ts=ts
                ).get("permalink")
            except Exception:
                url = None
        raw = _adapt_slack_message(event, channel_id, url)
        normalize_message(scope, raw, SystemClock())

        if agenda_pair_signals and _looks_like_open_question(text):
            display_text = resolve_slack_mentions_for_display(
                text, getattr(deliverer, "token", None)
            )
            for report_user_id, agenda_signal in agenda_pair_signals:
                for visibility in _agenda_visibilities_for(agenda_signal, report_user_id):
                    pair_scope = resolve_pair_scope(
                        session, report_user_id, report_user_id
                    )
                    try:
                        append_agenda_item(
                            pair_scope,
                            {
                                "text": display_text,
                                "source": "slack",
                                "source_link": url or f"slack:{channel_id}:{ts}",
                                "visibility": visibility,
                                "created_by_user_id": report_user_id,
                                "created_by_role": "report",
                            },
                            SystemClock(),
                        )
                    except ValueError:
                        logger.info(
                            "slack agenda signal: no current Pair for report=%s, skipping",
                            report_user_id,
                        )
    except Exception:
        logger.exception(
            "slack events_api: failed to persist message channel=%s owner=%s",
            channel_id,
            owner_user_id,
        )
    finally:
        if session is not None:
            session.close()
