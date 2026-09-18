import datetime
import logging
import time
from zoneinfo import ZoneInfo
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.clock import Clock, SystemClock
from app.core.config import get_settings
from app.core.db import get_engine, get_session_factory
from app.core.models import User
from app.core.scope import OwnerScope
from app.delivery.cards import build_card, render_blocks
from app.delivery.slack_deliverer import SlackDeliverer
from app.delivery.tts import deliver_pulse_audio
from app.ingest.live_source import (
    LiveJiraClient,
    LiveLinearClient,
    LiveSlackClient,
)
from app.ingest.live_source_factory import calendar_client_for, google_docs_client_for
from app.ingest.seed import seed_live
from app.salience.pulse.weight_consolidation import consolidate_weights
from app.triggers.dispatch import handle_trigger
from app.triggers.pulse_trigger import build_cron_trigger

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60

NIGHTLY_CONSOLIDATION_TIME = datetime.time(3, 0)

_last_consolidation_date: datetime.date | None = None


def _should_fire(now_local_time: datetime.time, fire_time: datetime.time) -> bool:
    """Once local time reaches the fire time it's true for the rest of
    that day — handle_trigger's own PulseDelivery uniqueness (per owner,
    ritual, local_date, trigger) is what actually prevents re-delivery on
    every later poll, not this check."""
    return now_local_time >= fire_time

def run_cron_pulse_for_user(
    session: Session, owner_user_id: str, clock: Clock | None = None
) -> None:
    """The actual cron work for one user. Failures are logged, not
    raised — there is no request to fail, this runs inside a forever
    loop."""
    clock = clock or SystemClock()
    try:
        seed_live(session, owner_user_id, clock=clock)
    except Exception:
        logger.exception(
            "cron pulse: seed_live failed for owner=%s, pulsing off whatever "
            "is already ingested",
            owner_user_id,
        )
    try:
        scope = OwnerScope(owner_user_id=owner_user_id, session=session)
        user = session.get(User, owner_user_id)
        source_clients = [
            calendar_client_for(session, owner_user_id),
            LiveSlackClient(
                viewer_slack_user_id=user.slack_user_id if user else None
            ),
            LiveLinearClient(),
            LiveJiraClient(),
            google_docs_client_for(session, owner_user_id),
        ]
        event = build_cron_trigger(owner_user_id, clock.now())
        trigger_result = handle_trigger(scope, clock, source_clients, event)

        if trigger_result.idempotent_skip:
            return

        deliverer = SlackDeliverer()
        if not deliverer.enabled:
            logger.warning(
                "cron pulse: SLACK_BOT_TOKEN not configured, owner=%s got no card",
                owner_user_id,
            )
            return

        card = build_card(trigger_result)
        delivery = deliverer.deliver(user.slack_user_id, blocks=render_blocks(card))
        if delivery.get("sent") and delivery.get("dm_ts") and delivery.get("dm_channel"):
            deliver_pulse_audio(
                deliverer, card, delivery["dm_channel"], delivery["dm_ts"]
            )
    except Exception:
        logger.exception(
            "cron pulse: failed to render/deliver for owner=%s", owner_user_id
        )

def _should_run_nightly_consolidation(
    now: datetime.datetime, last_run_date: datetime.date | None
) -> bool:
    return now.time() >= NIGHTLY_CONSOLIDATION_TIME and last_run_date != now.date()


def _maybe_consolidate_weights(session_factory, clock: Clock) -> None:
    global _last_consolidation_date
    now = clock.now()
    if not _should_run_nightly_consolidation(now, _last_consolidation_date):
        return

    session = session_factory()
    try:
        result = consolidate_weights(session, clock)
    except Exception:
        logger.exception("weight consolidation: batch failed, will retry next poll")
        return
    finally:
        session.close()
    _last_consolidation_date = now.date()
    logger.info(
        "weight consolidation: events_consolidated=%s weights_updated=%s",
        result["events_consolidated"],
        result["weights_updated"],
    )


def _poll_once(session_factory, owner_user_ids: list[str]) -> None:
    session = session_factory()
    try:
        linked_users = (
            session.execute(
                select(User).where(
                    User.id.in_(owner_user_ids), User.slack_user_id.is_not(None)
                )
            )
            .scalars()
            .all()
        )
    finally:
        session.close()

    for user in linked_users:
        now_local = datetime.datetime.now(ZoneInfo(user.tz))
        if not _should_fire(now_local.time(), user.pulse_fire_time_local):
            continue

        session = session_factory()
        try:
            run_cron_pulse_for_user(session, user.id)
        finally:
            session.close()


def _get_scheduled_owner_ids() -> list[str]:
    import os

    raw_owner_ids = os.environ.get("PULSE_SCHEDULED_OWNER_IDS", "")
    owner_user_ids = [o.strip() for o in raw_owner_ids.split(",") if o.strip()]
    if not owner_user_ids:
        raise RuntimeError(
            "PULSE_SCHEDULED_OWNER_IDS is not set — refusing to start rather "
            "than silently scheduling every linked user in the database "
            "(this dev DB is shared with the test suite, which creates its "
            "own throwaway linked users). Set it to a comma-separated list "
            "of owner_user_id(s), e.g. PULSE_SCHEDULED_OWNER_IDS=usr_amal"
        )
    return owner_user_ids


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()

    owner_user_ids = _get_scheduled_owner_ids()

    engine = get_engine(get_settings().database_url)
    session_factory = get_session_factory(engine)

    logger.info(
        "cron scheduler: polling every %ss for owner_user_ids=%s",
        POLL_INTERVAL_SECONDS,
        owner_user_ids,
    )
    while True:
        try:
            _poll_once(session_factory, owner_user_ids)
        except Exception:
            logger.exception("cron scheduler: poll failed, will retry next interval")
        _maybe_consolidate_weights(session_factory, SystemClock())
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()