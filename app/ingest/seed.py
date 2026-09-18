"""Orchestrates M2's demo path: fetch from each fixture-backed SourceClient,
normalize, and upsert — the same code path `app/cli.py seed` drives."""

import concurrent.futures
import datetime
import logging
import time
import uuid

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.clock import Clock, SystemClock
from app.core.models import (
    Commitment,
    Event,
    FeedbackEvent,
    Goal,
    Message,
    OneOnOneNote,
    PulseDelivery,
    RawIngestRef,
    Suppression,
    User,
    WorkItem,
)
from app.core.scope import OwnerScope
from app.identity.models import Person
from app.ingest.base import Healthy, Unauthorized, Window
from app.ingest.fixture_source import (
    FixtureCalendarClient,
    FixtureLinearClient,
    FixtureSlackClient,
    load_day_fixture,
)
from app.ingest.live_source_factory import (
    calendar_client_for,
    gmail_client_for,
    google_docs_client_for,
)
from app.ingest.normalize import (
    normalize_event,
    normalize_message,
    normalize_work_item,
)

logger = logging.getLogger(__name__)


def _ensure_user(session: Session, fixture: dict) -> str:
    """A fixture is a fully-specified, reproducible scenario (same spirit as
    FrozenClock's own "a demo must be reproducible regardless of when it's
    actually run") — tz/pulse_fire_time_local/late_cutoff_local are always
    synced to the fixture's own values, on an existing row too, not just at
    creation. Without this, an id that's also used outside fixtures (e.g. a
    real linked Slack user reusing a fixture's owner id for live testing)
    can silently leave stale values in place and desync fixture tests from
    what the fixture file actually says — found for real when fixing a
    live user's tz broke test_day_times_are_shown_in_the_owners_local_
    timezone, which hardcodes normal_day.yaml's owner id and expects its
    declared tz. slack_user_id is deliberately left alone here — it's not
    part of the fixture, set separately via `link-slack`, and resetting it
    on every seed would un-link a real user's Slack account."""
    owner = fixture["owner"]
    tz = owner["tz"]
    pulse_fire_time_local = datetime.time.fromisoformat(owner["pulse_fire_time_local"])
    late_cutoff_local = datetime.time.fromisoformat(owner["late_cutoff_local"])

    existing = session.get(User, owner["id"])
    if existing is None:
        session.add(
            User(
                id=owner["id"],
                created_at=datetime.datetime.now(datetime.UTC),
                tz=tz,
                pulse_fire_time_local=pulse_fire_time_local,
                late_cutoff_local=late_cutoff_local,
            )
        )
    else:
        existing.tz = tz
        existing.pulse_fire_time_local = pulse_fire_time_local
        existing.late_cutoff_local = late_cutoff_local
    session.commit()
    return owner["id"]


_ALL_INGESTED_MODELS = (
    Event,
    WorkItem,
    Message,
    Commitment,
    Suppression,
    RawIngestRef,
    Goal,
    OneOnOneNote,
)


def _reset_owner_ingested_data(
    session: Session,
    owner_id: str,
    reset_deliveries: bool = True,
    models: tuple = _ALL_INGESTED_MODELS,
) -> None:
    """Fixtures declare a full simulated day for a fixed owner id — without
    this, seeding a second fixture for the same owner would merge onto
    whatever the previous fixture left behind instead of representing a
    clean day (caught by test_reseeding_a_different_fixture_for_the_same_
    owner_does_not_accumulate). Roster (Person) rows are intentionally left
    alone — identity persists across simulated days, it isn't part of the
    day being reset.

    reset_deliveries=False (seed_live's own call) skips FeedbackEvent/
    PulseDelivery — those are delivery/idempotency *history*, not ingested
    day content, and wiping them is only correct for a fixture demo
    wanting a clean slate. A live run of app/triggers/cron_scheduler.py
    caught this for real: seed_live() runs at the top of every cron poll,
    so wiping PulseDelivery there deleted the cron trigger's own
    idempotency guard on every single poll, causing a real Slack DM to be
    redelivered every 60 seconds indefinitely instead of firing once.

    models, if given, narrows which tables get wiped — seed_live's own
    call excludes WorkItem now that GitHub/Linear/Jira are webhook-fed
    (app/triggers/webhook_router.py) rather than pull-refetched here: with
    no pull WorkItem source left in its fetch_plan, wiping WorkItem on
    every /mentor pulse would delete everything the webhooks already
    populated in the background and never refetch it, leaving work items
    empty until the next webhook event happened to land."""
    if reset_deliveries:
        models = (FeedbackEvent, PulseDelivery, *models)  # FK -> PulseDelivery, first
    for model in models:
        session.execute(delete(model).where(model.owner_user_id == owner_id))
    session.commit()


_PULL_MESSAGE_SOURCES = ("google_docs",)


def _reset_pull_sourced_messages(
    session: Session, owner_id: str, sources: tuple[str, ...] = _PULL_MESSAGE_SOURCES
) -> None:
    """seed_live's narrower counterpart to _reset_owner_ingested_data for
    Message specifically — see seed_live's own docstring for why Message as
    a whole can't just go through the generic reset (Slack is push-only
    now, with nothing to refill it) while Google Docs still needs
    reset-then-refetch (still a pull source, with no push path of its
    own, and no cheap native "since" filter the way Gmail has — see
    seed_live's own docstring). Gmail is deliberately NOT in
    _PULL_MESSAGE_SOURCES any more: it's upsert-only now (normalize_
    message's own get-or-create), relying on its narrowed incremental
    fetch window to naturally stop returning stale rows rather than a
    reset ever deleting them outright. Scoped to source IN sources (all
    of _PULL_MESSAGE_SOURCES by default, or a narrower single-source
    subset — see seed_live's own call site) so push-sourced
    (source='slack') and Gmail rows are never touched here.

    Callable only AFTER that source's own fetch has already succeeded
    (seed_live's own call site) — REAL BUG FOUND AND FIXED (confirmed
    live): this used to run unconditionally before the fetch even
    started. Google Docs' own MCP subprocess (a real Node process,
    @a-bonus/google-docs-mcp) genuinely crashed with an out-of-memory
    V8 heap error in production — caught cleanly by seed_live's
    _fetch_or_degrade (reported as a degraded source, the whole seed
    doesn't crash), but by then this function had already deleted every
    existing Google Docs comment with nothing to replace them. A
    transient, self-healing-on-the-next-poll failure was instead
    silently destroying real, previously-fetched data on every
    occurrence. Delete-after-success (fetch first, reset only once rows
    are in hand to replace them with) closes that gap — a failed fetch
    now just leaves the prior cycle's rows in place, stale but not
    gone, exactly like Calendar's own TTL-skip already behaves."""
    if not sources:
        return
    session.execute(
        delete(Message).where(
            Message.owner_user_id == owner_id,
            Message.source.in_(sources),
        )
    )
    session.commit()


def _ensure_roster(scope: OwnerScope, fixture: dict) -> None:
    for person in fixture.get("people", []):
        existing = None
        if person.get("primary_email"):
            existing = scope.session.execute(
                scope.query(Person).where(
                    Person.primary_email == person["primary_email"]
                )
            ).scalar_one_or_none()
        if existing is None:
            scope.add(
                Person(
                    id=person.get("id", str(uuid.uuid4())),
                    canonical_name=person["canonical_name"],
                    primary_email=person.get("primary_email"),
                    is_self=person.get("is_self", False),
                    is_active=True,
                    created_at=datetime.datetime.now(datetime.UTC),
                )
            )
    scope.commit()


def _ensure_commitments(scope: OwnerScope, fixture: dict) -> None:
    for commitment in fixture.get("commitments", []):
        existing = None
        if commitment.get("source_reference_key"):
            existing = scope.session.execute(
                scope.query(Commitment).where(
                    Commitment.source_reference_key
                    == commitment["source_reference_key"],
                    Commitment.description == commitment["description"],
                )
            ).scalar_one_or_none()
        if existing is not None:
            continue
        scope.add(
            Commitment(
                id=str(uuid.uuid4()),
                promised_to_person_id=commitment.get("promised_to_person_id"),
                description=commitment["description"],
                source_reference_key=commitment.get("source_reference_key", ""),
                promised_at=datetime.datetime.fromisoformat(commitment["promised_at"]),
                due_at=(
                    datetime.datetime.fromisoformat(commitment["due_at"])
                    if commitment.get("due_at")
                    else None
                ),
                delivered_at=(
                    datetime.datetime.fromisoformat(commitment["delivered_at"])
                    if commitment.get("delivered_at")
                    else None
                ),
                status=commitment.get("status", "open"),
            )
        )
    scope.commit()


def _ensure_suppressions(scope: OwnerScope, fixture: dict, clock: Clock) -> None:
    for suppression in fixture.get("suppressions", []):
        scope.add(
            Suppression(
                id=str(uuid.uuid4()),
                scope=suppression["scope"],
                target_ref=suppression["target_ref"],
                reason=suppression["reason"],
                created_at=clock.now(),
                expires_at=datetime.datetime.fromisoformat(suppression["expires_at"]),
                created_by="user",
            )
        )
    scope.commit()


def _health_status(client) -> dict | None:
    """None means healthy — degraded_sources only lists the unhealthy ones."""
    health = client.health()
    if isinstance(health, Healthy):
        return None
    if isinstance(health, Unauthorized):
        return {"source": client.source, "status": "unauthorized", "stale_as_of": None}
    return {
        "source": client.source,
        "status": "error",
        "stale_as_of": health.stale_as_of,
    }


def seed_fixture(
    session: Session, fixture_name: str, clock: Clock | None = None
) -> dict:
    clock = clock or SystemClock()
    fixture = load_day_fixture(fixture_name)
    owner_id = _ensure_user(session, fixture)
    _reset_owner_ingested_data(session, owner_id)
    scope = OwnerScope(owner_user_id=owner_id, session=session)
    _ensure_roster(scope, fixture)

    now = datetime.datetime.fromisoformat(fixture["now"])
    window = Window(
        start=now.replace(hour=0, minute=0, second=0, microsecond=0),
        end=now.replace(hour=23, minute=59, second=59, microsecond=0),
    )

    counts = {"events": 0, "work_items": 0, "messages": 0}
    degraded_sources = []

    calendar = FixtureCalendarClient(fixture)
    status = _health_status(calendar)
    if status is not None:
        degraded_sources.append(status)
    else:
        for raw in calendar.fetch(window, owner_id):
            normalize_event(scope, raw, clock)
            counts["events"] += 1

    slack = FixtureSlackClient(fixture)
    status = _health_status(slack)
    if status is not None:
        degraded_sources.append(status)
    else:
        for raw in slack.fetch(window, owner_id):
            normalize_message(scope, raw, clock)
            counts["messages"] += 1

    linear = FixtureLinearClient(fixture)
    status = _health_status(linear)
    if status is not None:
        degraded_sources.append(status)
    else:
        for raw in linear.fetch(window, owner_id):
            normalize_work_item(scope, raw, clock)
            counts["work_items"] += 1

    _ensure_commitments(scope, fixture)
    counts["commitments"] = len(fixture.get("commitments", []))

    _ensure_suppressions(scope, fixture, clock)

    return {"owner_user_id": owner_id, "degraded_sources": degraded_sources, **counts}


def _fetch_or_degrade(
    client, window: Window, owner_user_id: str
) -> tuple[list[dict], dict | None]:
    """health() on a live client only checks credential presence (see
    app/ingest/live_source.py) — it can't know the server/network is
    actually reachable. A fetch() failure at runtime is therefore still a
    real possibility and gets folded into the same degraded_sources shape
    seed_fixture reports for an unhealthy source, rather than crashing the
    whole seed.

    Returns (rows, degraded_entry_or_None) instead of appending to a shared
    degraded_sources list — seed_live runs one of these per connector
    concurrently (ThreadPoolExecutor), and a plain list isn't safe to
    mutate from multiple threads at once; callers merge the per-connector
    results back together sequentially once every future is done."""
    status = _health_status(client)
    if status is not None:
        return [], status
    started = time.perf_counter()
    try:
        return client.fetch(window, owner_user_id), None
    except Exception as exc:
        return [], {
            "source": client.source,
            "status": "error",
            "stale_as_of": None,
            "detail": str(exc),
        }
    finally:
        logger.info(
            "seed_live fetch source=%s latency_ms=%.1f",
            client.source,
            (time.perf_counter() - started) * 1000,
        )


# How recent owner.last_live_seed_at has to be for seed_live to skip
# Calendar entirely and reuse whatever Event rows are already there.
# Deliberately short — long enough to collapse cron_scheduler's own
# every-60s re-polling (see run_cron_pulse_for_user: seed_live runs on
# every poll once _should_fire flips true for the day, not once) down to
# a handful of real Calendar fetches instead of ~1400/day, short enough
# that a manual /mentor pulse minutes apart still sees a genuinely
# current "today" window. No missed-data risk from skipping: unlike
# Gmail (an incremental "since" query, which really could miss something
# if skipped), Calendar's query is always the same "today" window
# regardless of when it runs, so the ONLY question skipping answers is
# "do we already know this," never "might we miss something new" — the
# next real fetch, whenever it happens, always reflects the true current
# state of the window.
CALENDAR_SEED_TTL_SECONDS = 120


def seed_live(session: Session, owner_user_id: str, clock: Clock | None = None) -> dict:
    """Live counterpart of seed_fixture, driven by the real MCP-backed
    connectors (app/ingest/live_source.py) instead of a fixture. Unlike
    seed_fixture, there is no fixture to source pulse_fire_time_local/tz/
    late_cutoff_local or roster/commitments/suppressions from, so this only
    ever updates an *existing* user's ingested data (events/messages) for
    today — it never creates a user or touches the roster.

    work_items are deliberately NOT touched here (not reset, not
    re-fetched) — GitHub, Linear, and Jira are all webhook-fed now
    (app/triggers/webhook_router.py), continuously upserting WorkItem rows
    in the background, decoupled from any /mentor pulse request. Pulling
    them again here would be redundant (they're already fresh) and, worse,
    resetting them here without a pull path to refill them would wipe
    whatever the webhooks already populated. See _reset_owner_ingested_
    data's own docstring for the reset-scope reasoning.

    Message as a whole is excluded from the generic reset for the same
    reason as work_items: app/triggers/slack_socket_listener.py now
    persists Slack messages in real time via events_api (push), and
    LiveSlackClient's pull fetch is gone from fetch_plan below. Found live:
    wiping Message here while still calling seed_live from a pulse-alias DM
    trigger (the same events_api envelope that pushes the message) deleted
    the just-persisted DM the instant seed_live ran, since pull re-fetch
    never covered DM history to refill it — a real data-loss race, not
    just a theoretical one.

    Google Docs and Calendar (Event) are both reset-then-refetch, but
    delete-AFTER-success, never before (REAL BUG FOUND AND FIXED,
    confirmed live — see the fetch_plan/reset_kind comment right above
    the fetch loop below for the full story): each one's own existing
    rows are only deleted once its fetch has actually returned rows to
    replace them with, so a real, transient runtime failure (confirmed
    live: Google Docs' own MCP subprocess crashing with an out-of-memory
    error) just leaves the prior cycle's data in place — stale, reported
    as a degraded source, never silently destroyed. Google Docs has no
    incremental/TTL treatment on top of that, deliberately: it has no
    push path of its own, and unlike Gmail there's no cheap native
    "since" filter to narrow listComments by (app.ingest.live_source's
    LiveGoogleDocsClient docstring covers the connector's own shape).
    Calendar additionally gets the TTL-skip below (CALENDAR_SEED_TTL_
    SECONDS) on top of delete-after-success — skipping it entirely some
    cycles, safely, since Calendar's own window is always "today," never
    a moving target a skip could cause staleness in.

    Gmail is upsert-only (normalize_message's own get-or-create) — never
    reset here at all. Its fetch window narrows to owner.last_live_seed_at
    when set — LiveGmailClient.fetch() turns that into a real
    after:<timestamp> Gmail query (see its own comment) — so a message no
    longer returned by a pull fetch (read, archived, newly filtered as
    noise) simply stays in the DB rather than being pruned; the narrowed
    incremental window is what keeps this from accumulating truly stale
    rows, not a delete.

    Notion goals/notes are NOT fetched here at all any more — moved to
    app.triggers.agenda.agenda_scheduler's own background sync job (see that
    module's own docstring for why: the ownership-resolution chain there
    needs a full scan every time, which made a real incremental filter
    unsafe, so it now runs independently on a slower timer instead of on
    every seed_live call)."""
    clock = clock or SystemClock()
    owner = session.get(User, owner_user_id)
    if owner is None:
        raise ValueError(
            f"no such user: {owner_user_id!r} — seed_live updates an existing "
            "user's ingested data, it doesn't create one (seed a fixture first, "
            "or create the user another way)"
        )

    now = clock.now()
    last_seed_at = owner.last_live_seed_at
    skip_calendar = (
        last_seed_at is not None
        and (now - last_seed_at).total_seconds() < CALENDAR_SEED_TTL_SECONDS
    )

    # Event is no longer reset here at all — see the fetch-success loop
    # below (REAL BUG FOUND AND FIXED, confirmed live) for why "reset
    # eagerly, then maybe refetch" is unsafe for any pull source whose
    # fetch can genuinely fail at runtime, Calendar included.
    #
    # Commitment/Suppression/RawIngestRef are no longer reset here EITHER
    # — REAL, LIVE DATA-LOSS BUG FOUND AND FIXED (confirmed live, same
    # root cause as the Event/Message fix above, one rung further out):
    # this reset used to run unconditionally, every single seed_live
    # call, on the stated reasoning "nothing here or elsewhere writes
    # them from a live source, so resetting them is a no-op." That's true
    # only for the narrow "populated by seed_live's OWN fetch_plan" sense
    # — it's false in the sense that actually mattered: Commitment IS
    # written elsewhere, by a real, independent, user-facing path
    # (app/agenda/store.py's append_ledger_item, called from meeting-
    # synthesis's own LLM tool, the ledger_event trigger, and manual
    # "add to ledger" — app/triggers/agenda_router.py's add_commitment_
    # webhook/add_accomplishment_webhook), and Suppression likewise
    # (app/delivery/affordances.py's snooze_item, a multi-day TTL meant
    # to survive many pulse cycles). Confirmed live: cron_scheduler polls
    # seed_live every 60s all day (see that module's own POLL_INTERVAL_
    # SECONDS) for the SAME owner a real meeting-synthesis run had just
    # written a Commitment for — the very next poll silently deleted it,
    # while the agenda's own "commitment_ledger" mirror item (never reset
    # here) survived, left pointing at a source_link for a row that no
    # longer existed. Exactly the same shape as work_items being
    # deliberately excluded above ("resetting them here without a pull
    # path to refill them would wipe whatever [was] already populated")
    # — that principle just wasn't applied consistently to these three.
    # RawIngestRef genuinely is a no-op to reset OR not (grep confirms
    # nothing anywhere ever reads it — write-only, self-healing via its
    # own upsert-by-(source,external_id) in app/ingest/normalize.py's
    # _stamp_provenance), so leaving it alone too costs nothing and keeps
    # this whole block one consistent rule: seed_live never resets
    # anything it doesn't itself refill.
    scope = OwnerScope(owner_user_id=owner_user_id, session=session)

    full_day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window = Window(start=full_day_start, end=now.replace(hour=23, minute=59, second=59, microsecond=0))
    # Gmail's own window narrows to "since last time" when we have one —
    # LiveGmailClient.fetch() is what actually turns window.start into a
    # real after:<timestamp> query.
    gmail_window = (
        Window(start=last_seed_at, end=window.end)
        if last_seed_at is not None
        else window
    )

    counts = {
        "events": 0,
        "work_items": 0,
        "messages": 0,
        "commitments": 0,
    }
    degraded_sources: list[dict] = []
    seed_started = time.perf_counter()

    # Only the still-pull sources remain here — GitHub/Linear/Jira moved to
    # webhooks (see this function's own docstring), Slack messages are
    # pushed in real time by app/triggers/slack_socket_listener.py, and
    # Notion goals/notes moved to app.triggers.agenda.agenda_scheduler's own
    # background job. Calendar and Google Docs/Gmail stay pull-based here
    # (see this function's own docstring for each one's specific caching
    # treatment); still run concurrently, same reasoning as before: these
    # are independent network calls, no reason to pay their sum instead of
    # their max.
    #
    # reset_kind (5th element, per-entry) drives the delete-on-success
    # pattern right below — REAL BUG FOUND AND FIXED, confirmed live: both
    # Event and the Google-Docs Message rows used to be deleted
    # unconditionally BEFORE their fetch even started, on the assumption
    # the refetch would always repopulate them. A real, reproducible
    # runtime failure (Google Docs' own MCP subprocess, a Node process,
    # genuinely crashing with an out-of-memory V8 heap error — seen live,
    # not hypothetical) is caught cleanly by _fetch_or_degrade below (the
    # whole seed doesn't crash, it's reported as a degraded source), but
    # the eager delete had already destroyed real, previously-fetched
    # data with nothing to replace it. "event"/"google_docs_message" mark
    # the two sources that need a stale-row cleanup at all (an upsert
    # alone can't detect "no longer returned by the live source" —
    # that's what the delete is FOR); None (Gmail) needs no reset, same
    # upsert-only reasoning _reset_pull_sourced_messages' own docstring
    # already gives for excluding it from _PULL_MESSAGE_SOURCES.
    fetch_plan: list[tuple[str, object, callable, Window, str | None]] = []
    if not skip_calendar:
        fetch_plan.append(
            (
                "events",
                calendar_client_for(session, owner_user_id),
                normalize_event,
                window,
                "event",
            )
        )
    fetch_plan.append(
        (
            "messages",
            google_docs_client_for(session, owner_user_id),
            normalize_message,
            window,
            "google_docs_message",
        )
    )
    fetch_plan.append(
        (
            "messages",
            gmail_client_for(session, owner_user_id),
            normalize_message,
            gmail_window,
            None,
        )
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(fetch_plan)) as executor:
        futures = [
            executor.submit(_fetch_or_degrade, client, fetch_window, owner_user_id)
            for _, client, _, fetch_window, _ in fetch_plan
        ]
        fetch_results = [future.result() for future in futures]

    for (count_key, client, normalize, _window, reset_kind), (
        rows,
        degraded_entry,
    ) in zip(fetch_plan, fetch_results):
        if degraded_entry is not None:
            degraded_sources.append(degraded_entry)
            continue
        # Only reached once this source's fetch has already succeeded —
        # rows are in hand, ready to replace whatever's being deleted.
        if reset_kind == "event":
            session.execute(delete(Event).where(Event.owner_user_id == owner_user_id))
            session.commit()
        elif reset_kind == "google_docs_message":
            _reset_pull_sourced_messages(session, owner_user_id, sources=(client.source,))
        for raw in rows:
            normalize(scope, raw, clock)
            counts[count_key] += 1

    owner.last_live_seed_at = now
    session.commit()

    logger.info(
        "seed_live total latency_ms=%.1f owner=%s calendar_skipped=%s",
        (time.perf_counter() - seed_started) * 1000,
        owner_user_id,
        skip_calendar,
    )
    return {
        "owner_user_id": owner_user_id,
        "degraded_sources": degraded_sources,
        "calendar_skipped": skip_calendar,
        **counts,
    }
