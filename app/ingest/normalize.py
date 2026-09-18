"""L3 normalization: raw connector dicts -> canonical Event/WorkItem/Message
rows, idempotent on (owner_user_id, source, external_id). Actor resolution
goes through app.ingest.identity_seam.resolve_actor only — never a new
matcher here (AGENT.md L3 invariant: zero LLM calls, and per the
morning-pulse plan, no tier-ladder extension either)."""

import datetime
import uuid

from app.core.clock import Clock
from app.core.models import Event, Goal, Message, OneOnOneNote, RawIngestRef, WorkItem
from app.core.scope import OwnerScope
from app.ingest.identity_seam import resolve_actor, resolve_slack_actor
from app.graph.resolver import project_source_record


def _parse(ts: str | None) -> datetime.datetime | None:
    return datetime.datetime.fromisoformat(ts) if ts is not None else None


def _find_existing(scope: OwnerScope, model, source: str, external_id: str):
    return scope.session.execute(
        scope.query(model).where(
            model.source == source, model.external_id == external_id
        )
    ).scalar_one_or_none()


def _stamp_provenance(
    scope: OwnerScope,
    source: str,
    external_id: str,
    canonical_table: str,
    canonical_id: str,
    clock: Clock,
) -> None:
    existing = scope.session.execute(
        scope.query(RawIngestRef).where(
            RawIngestRef.source == source, RawIngestRef.external_id == external_id
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.fetched_at = clock.now()
        existing.canonical_id = canonical_id
    else:
        scope.add(
            RawIngestRef(
                id=str(uuid.uuid4()),
                source=source,
                external_id=external_id,
                canonical_table=canonical_table,
                canonical_id=canonical_id,
                fetched_at=clock.now(),
                checksum=None,
            )
        )
    scope.commit()


def _upsert(
    scope: OwnerScope,
    model,
    source: str,
    external_id: str,
    actor_reference_key: str,
    extra_fields: dict,
    canonical_table: str,
    clock: Clock,
):
    resolved_person_id = resolve_actor(scope, actor_reference_key)
    existing = _find_existing(scope, model, source, external_id)

    if existing is not None:
        existing.actor_reference_key = actor_reference_key
        existing.resolved_person_id = resolved_person_id
        for key, value in extra_fields.items():
            setattr(existing, key, value)
        row = existing
    else:
        row = model(
            id=str(uuid.uuid4()),
            actor_reference_key=actor_reference_key,
            resolved_person_id=resolved_person_id,
            source=source,
            external_id=external_id,
            **extra_fields,
        )
        scope.add(row)
    scope.commit()

    _stamp_provenance(scope, source, external_id, canonical_table, row.id, clock)
    return row


def normalize_event(scope: OwnerScope, raw: dict, clock: Clock) -> Event:
    row = _upsert(
        scope,
        Event,
        raw["source"],
        raw["external_id"],
        raw["actor_reference_key"],
        {
            "title": raw["title"],
            "starts_at": _parse(raw["starts_at"]),
            "ends_at": _parse(raw["ends_at"]),
            "status": raw["status"],
            "url": raw.get("url"),
            "series_id": raw.get("series_id"),
            "attendees": raw.get("attendees", []),
        },
        "events",
        clock,
    )
    project_source_record(scope, row, "event", clock)
    return row


def normalize_work_item(scope: OwnerScope, raw: dict, clock: Clock) -> WorkItem:
    row = _upsert(
        scope,
        WorkItem,
        raw["source"],
        raw["external_id"],
        raw["actor_reference_key"],
        {
            "title": raw["title"],
            "status": raw["status"],
            "url": raw.get("url"),
            "due_at": _parse(raw.get("due_at")),
            "updated_at": _parse(raw.get("updated_at")),
            "blocks_others": raw.get("blocks_others", False),
        },
        "work_items",
        clock,
    )
    project_source_record(scope, row, "work_item", clock)
    return row


def normalize_message(scope: OwnerScope, raw: dict, clock: Clock) -> Message:
    source_echo_of = raw.get("source_echo_of")
    if source_echo_of is None and (raw.get("source") == "gmail" or raw.get("from") or raw.get("subject")):
        from app.ingest.email_echo import extract_source_echo
        echo_ref = extract_source_echo(raw)
        if echo_ref is not None:
            source_echo_of = echo_ref.to_dict()

    row = _upsert(
        scope,
        Message,
        raw["source"],
        raw["external_id"],
        raw["actor_reference_key"],
        {
            "channel": raw.get("channel"),
            "channel_name": raw.get("channel_name"),
            "sender_display_name": raw.get("sender_display_name"),
            "sent_at": _parse(raw.get("sent_at")),
            "url": raw.get("url"),
            "is_dm": raw.get("is_dm", False),
            "body_ref": raw["body_ref"],
            # Only ever set by a connector that classifies at fetch time
            # (currently Gmail's triageInbox) — every other source omits
            # these, leaving action_requested False/requires_reply None,
            # which score_message() treats as "unknown, behave as before."
            "action_requested": raw.get("action_requested", False),
            "requires_reply": raw.get("requires_reply"),
            "source_echo_of": source_echo_of,
        },
        "messages",
        clock,
    )

    # Slack-specific: if the sender's opaque user ID (e.g. "U08AMALID") was
    # not resolved to a Person by resolve_actor() (which only handles emails
    # and cached Identity rows), try the Slack users.info API to fetch their
    # email and run the full resolve() pipeline.  On success, resolve() writes
    # a new Identity row so the NEXT message from this sender is free (hits
    # the Identity cache).  Back-patch the row so the graph edge is correct
    # from this first message onwards.
    if raw.get("source") == "slack" and row.resolved_person_id is None:
        actor_key = raw.get("actor_reference_key", "")
        _, _, slack_uid = actor_key.partition(":")
        if slack_uid and "@" not in slack_uid:
            person_id = resolve_slack_actor(scope, slack_uid, clock)
            if person_id is not None:
                row.resolved_person_id = person_id
                scope.commit()

    project_source_record(scope, row, "message", clock)
    return row


def _upsert_unattributed(
    scope: OwnerScope,
    model,
    source: str,
    external_id: str,
    extra_fields: dict,
    canonical_table: str,
    clock: Clock,
):
    """Same idempotent-upsert-plus-provenance shape as _upsert(), for
    models that have no actor (Goal/OneOnOneNote are the owner's own
    data, already scoped by owner_user_id — there is no "who sent this"
    to resolve, unlike Event/WorkItem/Message). Does not call
    resolve_actor() and does not set actor_reference_key/
    resolved_person_id, since Goal/OneOnOneNote don't have those columns."""
    existing = _find_existing(scope, model, source, external_id)

    if existing is not None:
        for key, value in extra_fields.items():
            setattr(existing, key, value)
        # Goal is the only model routed through this shared helper that
        # currently has this column (OneOnOneNote doesn't) — hasattr
        # guards it so this stays generic instead of hardcoding a
        # Goal-only branch here. See Goal.updated_at's own docstring for
        # why this needs stamping: app.triggers.live_notifications_
        # router's SSE relay watches it to detect "this goal changed"
        # (e.g. a seed_live re-fetch picking up a Notion-side progress
        # edit) without a full audit-log table.
        if hasattr(existing, "updated_at"):
            existing.updated_at = clock.now()
        row = existing
    else:
        row = model(
            id=str(uuid.uuid4()),
            source=source,
            external_id=external_id,
            created_at=clock.now(),
            **extra_fields,
        )
        scope.add(row)
    scope.commit()

    _stamp_provenance(scope, source, external_id, canonical_table, row.id, clock)
    return row


def normalize_goal(scope: OwnerScope, raw: dict, clock: Clock) -> Goal:
    """goal_type/parent_external_id carry the OKR-vs-career-goal
    distinction and the KR->Objective/Objective->CareerGoal hierarchy —
    see Goal's own docstring (app/core/models.py)."""
    return _upsert_unattributed(
        scope,
        Goal,
        raw["source"],
        raw["external_id"],
        {
            "title": raw["title"],
            "status": raw.get("status") or "active",
            "goal_type": raw["goal_type"],
            "parent_external_id": raw.get("parent_external_id"),
            "quarter": raw.get("quarter"),
            "progress": raw.get("progress"),
            "current_value": raw.get("current_value"),
            "target_value": raw.get("target_value"),
        },
        "goals",
        clock,
    )


def normalize_one_on_one_note(scope: OwnerScope, raw: dict, clock: Clock) -> OneOnOneNote:
    return _upsert_unattributed(
        scope,
        OneOnOneNote,
        raw["source"],
        raw["external_id"],
        {
            "title": raw.get("title"),
            "meeting_id": raw.get("meeting_id"),
            "linked_key_result_external_ids": raw.get(
                "linked_key_result_external_ids"
            ),
        },
        "one_on_one_notes",
        clock,
    )
