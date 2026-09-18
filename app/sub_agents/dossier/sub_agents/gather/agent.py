"""Layer 6's gather step: assembles one context object from the ingestion
sweep, identity resolution, and (for recurring 1-on-1s) the agenda store.
L6 never queries a connector directly outside this module (AGENT.md §2)."""

import datetime
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.agenda.models import Pair
from app.agenda.scope import resolve_pair_scope
from app.agenda.store import get_agenda
from app.core.clock import Clock
from app.core.models import Commitment, User, WorkItem
from app.core.scope import OwnerScope
from app.identity.models import Person
from app.identity.resolve import resolve
from app.identity.types import RawReference, Resolution
from app.ingest.base import Window


@dataclass(frozen=True)
class ResolvedAttendee:
    raw: dict
    resolution: Resolution
    # "manager" | "report" | "colleague" | "external" | "unknown" — lets
    # the synthesis/audio prompts introduce someone as "your manager" or
    # "a stakeholder" instead of just a bare name. "unknown" only appears
    # for hand-built ResolvedAttendee instances that skip
    # _attendee_relationship entirely (existing tests) — gather_dossier_
    # context itself always computes a real value.
    relationship: str = "unknown"


@dataclass(frozen=True)
class DossierContext:
    event: dict
    resolved_attendees: list[ResolvedAttendee]
    agenda_carryover: list | None
    raw_signals: dict[str, Any]
    open_commitments: list[dict]
    blocked_work_items: list[dict]


def _open_commitments_with_attendees(
    owner_scope: OwnerScope,
    resolved_attendees: list[ResolvedAttendee],
    now: datetime.datetime,
) -> list[dict]:
    """Real, DB-backed 'promised and not delivered' data (design spec §5:
    'Reused as-is: Commitment') — previously never wired in, leaving that
    whole card section to LLM guesswork off raw Slack/Linear text with no
    source_link at all. Commitment doesn't store which side made the
    promise (app/sub_agents/agenda/sub_agents/synthesize/agent.py's
    append_ledger_item_tool never persists created_by_role) — direction is
    carried in `description`'s own natural-language text instead, the same
    convention app/salience/assemble.py's OwedItem already relies on for
    pulse's card. Only commitments promised_to a resolved attendee of THIS
    meeting are relevant here; Unconfirmed/Unattributed attendees have no
    person_id to match against and are silently excluded, same as every
    other identity-gated dossier section."""
    person_ids = {
        getattr(ra.resolution, "person_id", None) for ra in resolved_attendees
    }
    person_ids.discard(None)
    if not person_ids:
        return []

    rows = (
        owner_scope.session.execute(
            select(Commitment).where(
                Commitment.owner_user_id == owner_scope.owner_user_id,
                Commitment.status == "open",
                Commitment.promised_to_person_id.in_(person_ids),
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []

    person_cache: dict[str, str | None] = {}

    def _person_name(person_id: str | None) -> str | None:
        if person_id is None:
            return None
        if person_id not in person_cache:
            person = owner_scope.session.get(Person, person_id)
            person_cache[person_id] = person.canonical_name if person else None
        return person_cache[person_id]

    return [
        {
            "id": row.id,
            "description": row.description,
            "promised_to_person_id": row.promised_to_person_id,
            "promised_to_name": _person_name(row.promised_to_person_id),
            "promised_at": row.promised_at.isoformat(),
            "due_at": row.due_at.isoformat() if row.due_at else None,
            "overdue": row.due_at is not None and row.due_at < now,
        }
        for row in rows
    ]


def _blocked_work_items_with_attendees(
    owner_scope: OwnerScope,
    resolved_attendees: list[ResolvedAttendee],
    now: datetime.datetime,
) -> list[dict]:
    """The "Execution & Career Coach" spec's "flags interpersonal
    blockers": a real, DB-backed WorkItem (GitHub/Linear/Jira, webhook-
    fed) that is (a) blocked and (b) assigned to a resolved attendee of
    THIS meeting — i.e. something a specific person in the room is stuck
    on, worth actually raising. Same "only this meeting's resolved
    attendees, silently excluded otherwise" gate _open_commitments_with_
    attendees already applies to Commitment, and the same reason: an
    Unconfirmed/Unattributed attendee has no person_id to match a
    WorkItem.resolved_person_id against.

    Deliberately code-computed, never LLM-narrated (see this module's
    caller, synthesize_dossier's card.blockers) — dossier_synthesize.md
    explicitly forbids the model inferring tension/mood from signals, and
    a "blocker" phrased by an LLM off raw ticket text would be exactly
    that. status == "blocked" is a fact straight off the row, not an
    inference — "blocked" is never one of app/salience/score.py's
    is_resolved_work_item statuses, so no separate resolved-status check
    is needed here."""
    person_ids = {
        getattr(ra.resolution, "person_id", None) for ra in resolved_attendees
    }
    person_ids.discard(None)
    if not person_ids:
        return []

    rows = (
        owner_scope.session.execute(
            select(WorkItem).where(
                WorkItem.owner_user_id == owner_scope.owner_user_id,
                WorkItem.status == "blocked",
                WorkItem.resolved_person_id.in_(person_ids),
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []

    person_cache: dict[str, str | None] = {}

    def _person_name(person_id: str | None) -> str | None:
        if person_id is None:
            return None
        if person_id not in person_cache:
            person = owner_scope.session.get(Person, person_id)
            person_cache[person_id] = person.canonical_name if person else None
        return person_cache[person_id]

    return [
        {
            "id": row.id,
            "external_id": row.external_id,
            "title": row.title,
            "url": row.url,
            "person_id": row.resolved_person_id,
            "person_name": _person_name(row.resolved_person_id),
            "days_stale": (
                (now - row.updated_at).days if row.updated_at is not None else None
            ),
        }
        for row in rows
    ]


def _attendee_slack_user_ids(
    owner_scope: OwnerScope, resolved_attendees: list["ResolvedAttendee"]
) -> set[str]:
    """Every resolved attendee's own linked Slack account, if they have
    one — an external/unresolved attendee (no person_id) or a resolved
    one who's never run `link-slack` contributes nothing here, same as
    every other identity-gated dossier section. Not the dossier owner's
    own slack_user_id — deliberately: this filters Slack signal down to
    "about this meeting's *attendees*", a different question than "did
    someone mention *me*"."""
    ids: set[str] = set()
    for attendee in resolved_attendees:
        person_id = getattr(attendee.resolution, "person_id", None)
        if person_id is None:
            continue
        user = owner_scope.session.get(User, person_id)
        if user is not None and user.slack_user_id:
            ids.add(user.slack_user_id)
    return ids


def _filter_slack_signals_to_attendees(
    messages: list[dict],
    attendee_slack_user_ids: set[str],
    owner_slack_user_id: str | None = None,
) -> list[dict]:
    """Keeps a message only if its SENDER (actor_reference_key, "slack:
    <id>") is one of this meeting's attendees — REAL CHANGE (requested:
    "only show the messages sent by the attendees"). No attendee slack
    ids to check against at all (nobody resolved, or none of them have
    linked Slack) means nothing can be verified as relevant — empty, not
    an unfiltered fallback, since showing everything unfiltered is
    exactly the problem this exists to fix.

    Used to also keep a message purely for @-mentioning an attendee, with
    no requirement the sender be one — dropped: a bot-delivered card that
    happens to @-mention an attendee by name (e.g. "prepping you for your
    1:1 with <@attendee>") isn't a real signal from that attendee, and
    was slipping through this exact way.

    owner_slack_user_id, if given, doesn't filter anything — it only
    reorders the surviving (attendee-sent) messages so ones that ALSO
    @-mention the dossier owner sort first (requested: "tagging the user
    preferably but not obligatory"). A stable sort, so ties keep their
    original relative order."""
    if not attendee_slack_user_ids:
        return []
    filtered = []
    for message in messages:
        sender_key = message.get("actor_reference_key", "")
        sender_id = sender_key[len("slack:") :] if sender_key.startswith("slack:") else None
        if sender_id in attendee_slack_user_ids:
            filtered.append(message)
    if owner_slack_user_id:
        filtered.sort(
            key=lambda m: owner_slack_user_id
            not in (m.get("mentioned_slack_user_ids") or [])
        )
    return filtered


def _attendee_relationship(owner_scope: OwnerScope, resolution: Resolution) -> str:
    """Same person_id-is-a-User.id convention _resolve_pair_scope_for_
    attendees and app.sub_agents.dossier.agent's _real_is_manager already
    rely on. Unresolved (no person_id) is labeled "external" — not
    provably true (could be an internal teammate identity resolution
    simply failed to match), but a calendar attendee with no internal
    identity match at all is far more often a real external party than a
    resolution miss, and the synthesis/audio prompts are instructed to
    treat this as a soft, non-asserted label rather than a fact."""
    person_id = getattr(resolution, "person_id", None)
    if person_id is None:
        return "external"

    current_pair = owner_scope.session.execute(
        select(Pair).where(
            Pair.report_user_id == owner_scope.owner_user_id, Pair.ended_at.is_(None)
        )
    ).scalar_one_or_none()
    if current_pair is not None and current_pair.manager_user_id == person_id:
        return "manager"

    report_pair = owner_scope.session.execute(
        select(Pair).where(
            Pair.manager_user_id == owner_scope.owner_user_id,
            Pair.report_user_id == person_id,
            Pair.ended_at.is_(None),
        )
    ).scalar_one_or_none()
    if report_pair is not None:
        return "report"

    return "colleague"


def _resolve_pair_scope_for_attendees(
    owner_scope: OwnerScope, resolved_attendees: list[ResolvedAttendee]
):
    """The real event->Pair mapping that was missing (previously always
    called resolve_pair_scope(session, owner_id, owner_id), which trivially
    succeeds via that function's acting_user_id == report_user_id
    self-access branch regardless of whether owner_scope's owner is even a
    report at all — silently wrong for a manager-side owner, whose actual
    agenda with a report lives at report_user_id=<the report>, not
    report_user_id=<the manager>. person_id on a Resolved attendee is the
    same id space as User.id (app.identity.resolve writes/reads person_id
    as a User row id throughout — see app.sub_agents.dossier.agent's
    _real_is_manager for the same assumption already relied on elsewhere),
    so it's directly usable as a Pair.report_user_id/manager_user_id
    candidate.

    Tries both directions per resolved attendee — owner is this attendee's
    current manager, or this attendee is the owner's current manager — and
    returns the first that resolves to a real Pair. A recurring 1:1 has
    exactly one real counterpart, so the first match is the correct one;
    Unconfirmed/Unattributed attendees have no person_id and are skipped,
    same as every other identity-gated dossier section."""
    for attendee in resolved_attendees:
        person_id = getattr(attendee.resolution, "person_id", None)
        if person_id is None:
            continue
        # owner is person_id's manager (owner is the acting manager, person_id the report)
        pair_scope = resolve_pair_scope(
            owner_scope.session, person_id, owner_scope.owner_user_id
        )
        if pair_scope is not None:
            return pair_scope
        # person_id is owner's manager (owner is the report, person_id the manager)
        pair_scope = resolve_pair_scope(
            owner_scope.session, owner_scope.owner_user_id, person_id
        )
        if pair_scope is not None:
            return pair_scope
    return None


def gather_dossier_context(
    event: dict,
    owner_scope: OwnerScope,
    clock: Clock,
    connectors: dict[str, Any],
) -> DossierContext:
    resolved_attendees = []
    for attendee in event.get("attendees", []):
        ref = RawReference(
            source=attendee.get("source", "calendar"),
            external_id=attendee.get("external_id"),
            handle=attendee.get("handle"),
            email=attendee.get("email"),
            display_name=attendee.get("display_name"),
        )
        resolution = resolve(owner_scope, ref, clock)
        relationship = _attendee_relationship(owner_scope, resolution)
        resolved_attendees.append(
            ResolvedAttendee(raw=attendee, resolution=resolution, relationship=relationship)
        )

    agenda_carryover = None
    # Real calendar events (_adapt_calendar_event, app/ingest/live_source.py)
    # never set "is_recurring" — they set "series_id" (from Google's own
    # recurringEventId). event.get("is_recurring") alone is only ever
    # truthy for synthetic/test event dicts; app.triggers.agenda.agenda_scheduler's
    # own meeting-end poller already treats a truthy series_id as the real
    # "this is a recurring event" signal (see its `if not
    # event.get("series_id"):` filter) — matched here rather than requiring
    # the adapter to also synthesize an is_recurring boolean.
    if event.get("is_recurring") or event.get("series_id"):
        pair_scope = _resolve_pair_scope_for_attendees(owner_scope, resolved_attendees)
        if pair_scope is not None:
            agenda_carryover = get_agenda(pair_scope)

    now = clock.now()
    open_commitments = _open_commitments_with_attendees(
        owner_scope, resolved_attendees, now
    )
    blocked_work_items = _blocked_work_items_with_attendees(
        owner_scope, resolved_attendees, now
    )

    raw_signals: dict[str, Any] = {}
    # Backward-looking, not forward: Slack/Linear "what's been said/worked
    # on since we last talked" signals are inherently retrospective, not a
    # preview of the next 15 minutes before the meeting even starts. This
    # was previously masked because LiveSlackClient/LiveLinearClient
    # (app/ingest/live_source.py) ignore the window entirely, but other
    # connectors in that same module (LiveGmailClient, LiveJiraClient,
    # LiveFathomClient) DO read window.start/.end, so a forward window here
    # would become a live bug the moment one of those is wired into
    # `connectors`. No existing lookback convention applies directly here:
    # agenda_scheduler.py's CALENDAR_LOOKBACK_HOURS=6 is sized to avoid
    # re-triggering meeting-end detection on stale calendar entries, not to
    # size a "what happened since we last synced" content window. 7 days is
    # a reasonable default matching a typical weekly-1:1 cadence.
    window = Window(start=now - datetime.timedelta(days=7), end=now)
    for name, client in connectors.items():
        raw_signals[name] = client.fetch(window, owner_scope.owner_user_id)

    # Filtered to THIS meeting's attendees specifically — a raw Slack
    # fetch has no concept of "relevant to this dossier" on its own (it's
    # everything the bot can see, workspace-wide, over the lookback
    # window), and every message not sent by or @-mentioning someone
    # actually in this meeting is noise a dossier has no business citing.
    # Real removal, not a scoring bonus: callers must construct
    # connectors["slack"] unfiltered (no viewer_slack_user_id) so this is
    # the only relevance gate — see LiveSlackClient's own docstring on
    # that constructor param being for a different ("mentions of the
    # dossier owner") use case, not this one.
    if "slack" in raw_signals:
        owner = owner_scope.session.get(User, owner_scope.owner_user_id)
        raw_signals["slack"] = _filter_slack_signals_to_attendees(
            raw_signals["slack"],
            _attendee_slack_user_ids(owner_scope, resolved_attendees),
            owner_slack_user_id=owner.slack_user_id if owner is not None else None,
        )

    return DossierContext(
        event=event,
        resolved_attendees=resolved_attendees,
        agenda_carryover=agenda_carryover,
        raw_signals=raw_signals,
        open_commitments=open_commitments,
        blocked_work_items=blocked_work_items,
    )
