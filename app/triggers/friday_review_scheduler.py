"""L1 trigger: the Friday-afternoon (per-user friday_review_fire_time_local)
poll loop for F4. Templated on cron_scheduler.py's own shape (design spec
§4: "own scheduler, own table, own gate-equivalent — the established
per-ritual pattern") — a fourth near-identical poll loop alongside
cron_scheduler.py/agenda_scheduler.py/dossier_scheduler.py, deliberately
not merged into any of them this pass.

One deliberate deviation from cron_scheduler.py's own _poll_once: this
module's _poll_once takes an injectable `clock` (default SystemClock, same
seam app.triggers.dossier.dossier_scheduler.poll_dossier_window_once already
uses) rather than reading real wall-clock time directly.
cron_scheduler.py can get away with a bare `datetime.datetime.now(...)`
because its own _should_fire has no day-of-week term — any time-of-day
value is enough to make that gate deterministic in a test. _should_fire
here also gates on weekday == Friday, so a real-wall-clock read would make
this module's own tests pass or fail depending on what day they happen to
run — an injectable clock avoids that."""

import datetime
import logging
import time
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.clock import Clock, SystemClock
from app.core.config import get_settings
from app.core.db import get_engine, get_session_factory
from app.core.models import User
from app.core.scope import OwnerScope
from app.sub_agents.friday_review.agent import run_friday_review_flow

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60
FRIDAY_WEEKDAY = 4  # Monday=0 .. Sunday=6


def _should_fire(now_local: datetime.datetime, fire_time: datetime.time) -> bool:
    return now_local.weekday() == FRIDAY_WEEKDAY and now_local.time() >= fire_time


def _get_friday_review_scheduled_owner_ids() -> list[str]:
    import os

    raw = os.environ.get("FRIDAY_REVIEW_SCHEDULED_OWNER_IDS", "")
    owner_user_ids = [o.strip() for o in raw.split(",") if o.strip()]
    if not owner_user_ids:
        raise RuntimeError(
            "FRIDAY_REVIEW_SCHEDULED_OWNER_IDS is not set — refusing to start rather "
            "than silently scheduling every linked user in the database. Set it to a "
            "comma-separated list of owner_user_id(s), e.g. "
            "FRIDAY_REVIEW_SCHEDULED_OWNER_IDS=usr_amal"
        )
    return owner_user_ids


def _poll_once(session_factory, owner_user_ids: list[str], clock: Clock | None = None) -> None:
    clock = clock or SystemClock()
    now = clock.now()

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
        now_local = now.astimezone(ZoneInfo(user.tz))
        if not _should_fire(now_local, user.friday_review_fire_time_local):
            continue

        session = session_factory()
        try:
            scope = OwnerScope(owner_user_id=user.id, session=session)
            run_friday_review_flow(scope, clock)
        except Exception:
            logger.exception(
                "friday review scheduler: run failed for owner=%s, will retry next poll",
                user.id,
            )
        finally:
            session.close()


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    owner_user_ids = _get_friday_review_scheduled_owner_ids()
    engine = get_engine(get_settings().database_url)
    session_factory = get_session_factory(engine)

    logger.info(
        "friday review scheduler: polling every %ss for owner_user_ids=%s",
        POLL_INTERVAL_SECONDS,
        owner_user_ids,
    )
    while True:
        try:
            _poll_once(session_factory, owner_user_ids)
        except Exception:
            logger.exception("friday review scheduler: poll failed, will retry next interval")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()