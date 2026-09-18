"""L3 normalization for Notion Objectives' Owner/Manager fields -> Pair
rows — this app's only org-data source (a real "Manager" people property
added to the Objectives db alongside the existing "Owner" field,
confirmed live this session — Notion's own Teamspace grouping feature is
NOT usable for this: confirmed live via session.list_tools() that this
MCP server exposes no team/teamspace-membership tool at all, only a
flat, ungrouped API-get-users).

DELIBERATE SCOPE EXCEPTION: takes a bare Session, not PairScope/
OwnerScope, since creating a Pair is cross-user, system-level
bootstrapping — neither party's scope can exist until this runs."""

import datetime
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agenda.models import Pair
from app.core.clock import Clock
from app.core.models import User
from app.identity.models import Person

logger = logging.getLogger(__name__)


def _ensure_person(
    session: Session, owner_user_id: str, subject: User, clock: Clock
) -> None:
    """Without this, resolve() (app/identity/resolve.py) always falls
    through to Unattributed for a real calendar attendee — its whole
    tiered-matcher pipeline only ever compares against
    app.identity.roster.load_snapshot's Person rows for THIS owner, and
    in live operation nothing ever wrote one (the only other Person-
    creation code in this repo, app/ingest/seed.py's _ensure_roster, is
    fixture-only). Called for both sides of a synced Pair — the natural
    place a real relationship becomes KNOWN — so a report can resolve
    their manager as a meeting attendee and vice versa.

    id=subject.id (not a fresh uuid) deliberately matches the existing
    "person_id is the same id space as User.id for an internal teammate"
    convention every dossier consumer already relies on (see
    app.sub_agents.dossier.agent._real_is_manager's own docstring, and
    this fix's own Commitment/Pair-matching wiring) — needed so
    resolution.person_id compares directly against Pair.manager_user_id/
    report_user_id without an extra lookup.

    KNOWN LIMITATION: Person.id is the table's sole primary key (not
    composite with owner_user_id — see app/identity/models.py), so this
    convention only holds cleanly while a subject appears in at most one
    owner's roster. If the same person is ever discovered as a subject
    under a second, different owner_user_id, this silently skips writing
    that second roster entry (logged) rather than crashing on the PK
    collision it would otherwise cause. Fine at today's scale (one Pair);
    a real multi-manager/large-team rollout would need Person's PK to
    become (owner_user_id, id) or an owner-scoped alias table instead."""
    existing = session.get(Person, subject.id)
    if existing is not None:
        if existing.owner_user_id != owner_user_id:
            logger.warning(
                "notion_pair_sync: skipping Person row for subject=%s under "
                "owner=%s — already exists under a different owner=%s (see "
                "_ensure_person's own KNOWN LIMITATION note)",
                subject.id,
                owner_user_id,
                existing.owner_user_id,
            )
        return
    session.add(
        Person(
            id=subject.id,
            owner_user_id=owner_user_id,
            canonical_name=(
                subject.notion_display_name or subject.notion_owner_email or subject.id
            ),
            primary_email=subject.notion_owner_email,
            is_self=False,
            is_active=True,
            roster_source="notion_pair_sync",
            created_at=clock.now(),
        )
    )
    session.commit()


def sync_notion_pair_edge(session: Session, raw: dict, clock: Clock) -> Pair | None:
    """raw: {"report_notion_email", "manager_notion_email"} — one row per
    Objective in Notion whose Owner and Manager people fields are both
    set and differ (Owner == Manager means "no manager tracked for this
    person," not a Pair — see LiveNotionPairClient.fetch()). Matches
    against User.notion_owner_email (the same identity anchor `mentor
    link-notion` — or auto-provisioning, see app/ingest/notion_user_
    provision.py — already sets for goal ingestion)."""
    report = session.execute(
        select(User).where(User.notion_owner_email == raw["report_notion_email"])
    ).scalar_one_or_none()
    manager = session.execute(
        select(User).where(User.notion_owner_email == raw["manager_notion_email"])
    ).scalar_one_or_none()
    if report is None or manager is None:
        # Real, expected steady-state — link-notion runs independently of
        # the ingestion schedule; a Notion Manager field can reference
        # someone who hasn't been linked to a User row yet. Never raise.
        return None

    # Runs regardless of whether the Pair itself is new/changed/unchanged
    # below — re-syncing an already-known Pair must still backfill roster
    # entries that didn't exist before this fix.
    _ensure_person(session, report.id, manager, clock)
    _ensure_person(session, manager.id, report, clock)

    effective_date = clock.now()

    current = session.execute(
        select(Pair).where(Pair.report_user_id == report.id, Pair.ended_at.is_(None))
    ).scalar_one_or_none()

    if current is not None and current.manager_user_id == manager.id:
        return current  # idempotent: no change

    if current is not None:
        current.ended_at = effective_date
        # Flush the UPDATE before the new Pair's INSERT below — Postgres
        # enforces "at most one active Pair per report" via a partial
        # unique index (ended_at IS NULL).
        session.flush()

    new_pair = Pair(
        id=str(uuid.uuid4()),
        report_user_id=report.id,
        manager_user_id=manager.id,
        started_at=effective_date,
        ended_at=None,
    )
    session.add(new_pair)
    session.commit()
    return new_pair
