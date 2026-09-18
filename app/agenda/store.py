"""The one module in app/agenda with side effects (design spec §4.5).
Owns get_agenda, get_agenda_item, append_agenda_item, append_ledger_item,
update_agenda_item, mark_resolved, add_manual_note. Every function takes
scope: PairScope as its first argument — no bare Session, ever (except
Accomplishment writes inside append_ledger_item, which take OwnerScope,
since Accomplishment is owner-scoped, not pair-scoped). Every mutation
appends an AgendaItemHistory row — the audit trail is not optional.

is_visible_to(item, scope) is the single visibility rule (design spec
§4.3.1/§7): get_agenda filters through it on every read, and
app.triggers.agenda.agenda_signal_handler imports it to reject a keep/drop/
field_edit signal from the non-visible party before it reaches
update_agenda_item/mark_resolved — one rule, not two that could drift."""

import datetime
import logging
import uuid

from sqlalchemy import text

from app.agenda import config
from app.agenda.models import Accomplishment, AgendaItem, AgendaItemHistory
from app.agenda.scope import PairScope
from app.core.clock import Clock
from app.core.models import Commitment
from app.core.scope import OwnerScope

logger = logging.getLogger(__name__)

_VALID_VISIBILITY = {"shared", "report_only", "manager_only"}


def _validate_visibility(visibility: str) -> None:
    """Single choke point for every write path that accepts a caller-
    supplied visibility string (add_manual_note, append_agenda_item,
    field_edit's changes dict via update_agenda_item, the HTTP trigger
    route's body). visibility was previously unvalidated free text, and
    is_visible_to's old fallback (return True for anything unrecognized)
    meant a typo'd/malicious value like "report-only" was treated as
    visible to everyone — fail closed at the write boundary instead of
    letting a bad value reach storage at all."""
    if visibility not in _VALID_VISIBILITY:
        raise ValueError(
            f"invalid visibility={visibility!r}; must be one of {sorted(_VALID_VISIBILITY)}"
        )


def is_visible_to(item: AgendaItem, scope: PairScope) -> bool:
    """Visibility rule (design spec §4.3.1/§7): a report_only/manager_only
    item is only visible to the matching party. scope.acting_user_id ==
    scope.report_user_id is exactly "the report themself" — resolve_pair_
    scope only ever returns a non-None scope for the report or the
    report's CURRENT manager (scope.py's own docstring), so there is no
    third case to handle here. Exported (not underscore-prefixed) because
    app.triggers.agenda.agenda_signal_handler needs the identical rule to decide
    whether a keep/drop/field_edit signal applies at all — duplicating a
    second, possibly-drifting copy of this check there would be worse
    than a cross-module import.

    Fails CLOSED on anything outside the three known visibility strings:
    an unknown/malformed value (e.g. a typo'd "report-only", or a value
    that predates _validate_visibility being added) hides the item rather
    than exposing it to both parties."""
    if item.visibility == "shared":
        return True
    is_report = scope.acting_user_id == scope.report_user_id
    if item.visibility == "report_only":
        return is_report
    if item.visibility == "manager_only":
        return not is_report
    return False


def get_agenda(scope: PairScope) -> list[AgendaItem]:
    items = scope.session.execute(scope.query(AgendaItem)).scalars().all()
    return [item for item in items if is_visible_to(item, scope)]


def get_full_agenda(scope: PairScope) -> list[AgendaItem]:
    """Every item for scope.report_user_id, regardless of visibility —
    for internal post-meeting pipeline use only (synthesis dedup,
    phrasing, and the returned/state_delta payload). Deliberately ignores
    scope.acting_user_id and is_visible_to: the internal pipeline must
    not be blind to report_only or manager_only items just because of
    which party happened to trigger meeting_end (a manager-triggered run
    was blind to report_only items for dedup; a report-triggered run was
    symmetrically blind to manager_only items for phrasing — both fixed
    by giving the internal pipeline the full picture here instead of
    routing it through a single acting party's visibility).

    NEVER use this for anything that reaches an external caller directly
    — every external read (the GET payload route, Slack DM delivery)
    must keep going through get_agenda(scope), which enforces
    is_visible_to per actual recipient."""
    return scope.session.execute(scope.query(AgendaItem)).scalars().all()


def get_agenda_item(scope: PairScope, item_id: str) -> AgendaItem | None:
    """Single-row fetch, still scope-filtered (via scope.query), for
    callers that need to inspect an item (e.g. its visibility) before
    deciding whether to act on it — app.triggers.agenda.agenda_signal_handler is
    the first such caller. Returns None rather than raising when the row
    doesn't exist or isn't in this report's agenda, matching get_agenda's
    own "just isn't there" posture rather than update_agenda_item's
    scalar_one() (which is allowed to raise, since it's only ever called
    after a caller already knows the row exists)."""
    return scope.session.execute(
        scope.query(AgendaItem).where(AgendaItem.id == item_id)
    ).scalar_one_or_none()


def _write_history(
    scope: PairScope,
    item: AgendaItem,
    changed_by_user_id: str,
    diff: dict,
    clock: Clock,
) -> None:
    # AgendaItemHistory carries no report_user_id (it's keyed off
    # agenda_item_id, not the report), so it cannot be routed through
    # scope.query(...) — that helper filters on model.report_user_id,
    # which this model doesn't have. The scope-leak gate (tests/agenda/
    # test_invariants.py) bans bare select(...) anywhere in app/agenda/
    # outside scope.py, so this count goes through text() instead of
    # select(AgendaItemHistory)...) — the agenda_item_id filter itself
    # came from an already-scope-checked AgendaItem, so there is no
    # cross-report leak risk here despite the raw SQL.
    existing_count = scope.session.execute(
        text(
            "SELECT COUNT(*) FROM agenda_item_history WHERE agenda_item_id = :agenda_item_id"
        ),
        {"agenda_item_id": item.id},
    ).scalar_one()
    # diff lands in a JSONB column, which the stdlib json encoder can't
    # serialize datetimes into directly — mark_resolved's {"resolved_at":
    # <datetime>} diff hit this. Isoformat any datetime values so the
    # history row is always writable regardless of what a caller's
    # changes dict contains.
    serializable_diff = {
        key: (value.isoformat() if isinstance(value, datetime.datetime) else value)
        for key, value in diff.items()
    }
    scope.session.add(
        AgendaItemHistory(
            id=str(uuid.uuid4()),
            agenda_item_id=item.id,
            version=existing_count + 1,
            changed_by_user_id=changed_by_user_id,
            changed_at=clock.now(),
            diff=serializable_diff,
        )
    )


def append_agenda_item(
    scope: PairScope,
    item: dict,
    clock: Clock,
    dedup_guard: set[str] | None = None,
) -> AgendaItem:
    """dedup_guard, when given, is a plain set the CALLER owns and reuses
    across every append_agenda_item/append_ledger_item call within one
    synthesis run (see app.sub_agents.agenda.sub_agents.synthesize.agent's
    synthesize_tools, the only real caller that passes one). It caps a
    single existing item at ONE surfaced_count bump per run, no matter
    how many entries in the batch resolve to it.

    REAL BUG FOUND (confirmed live via agenda_item_history): the
    "always include a re-mention, even with no new info" prompt
    instruction (agenda_synthesize.v1.md) doesn't reliably stop the model
    from treating the SAME point mentioned twice in ONE transcript as two
    separate re-mentions, each with existing_item_id set to the same id —
    despite the prompt's own separate "one transcript point = one tool
    entry" rule. A real run bumped one item's surfaced_count by 2 in the
    same few seconds, well past what that meeting's actual re-mention
    count justified. Never trusting the model's own batching discipline
    on faith (same posture as drop_unsourced_wins/enforce_ranker_output
    elsewhere in this codebase): the second+ call for an id already
    bumped this run is a no-op on surfaced_count (still returns the row,
    just without a further increment or history write) rather than
    compounding the model's own duplication."""
    _validate_visibility(item.get("visibility", "shared"))
    now = clock.now()
    existing = None
    existing_item_id = item.get("existing_item_id")
    if existing_item_id is not None:
        candidate = get_agenda_item(scope, existing_item_id)
        if (
            candidate is not None
            and is_visible_to(candidate, scope)
            and candidate.status != "resolved"
        ):
            existing = candidate
        else:
            logger.warning(
                "append_agenda_item: existing_item_id=%s did not resolve to a live visible open item; creating a new row",
                existing_item_id,
            )
    if existing is None and item.get("source_link") is not None:
        # Only consulted when existing_item_id above didn't already
        # resolve a match — existing_item_id is the more specific, more
        # direct signal (an explicit id the model read straight out of
        # get_agenda_tool's own output), so it must win outright rather
        # than being re-derived and possibly clobbered by this fallback.
        # REAL BUG FOUND: this used to run unconditionally whenever
        # source_link was present, even after existing_item_id had
        # already resolved a valid candidate above — silently
        # overwriting `existing` with whatever (or nothing) this second
        # lookup found, including wiping it back to None if no row
        # happened to share that source_link.
        #
        # Fetch every non-resolved row sharing this source_link, not just
        # one: the uq_agenda_item_report_source_link_visibility index
        # allows one open row per (report, source_link, visibility)
        # bucket, so more than one can legitimately exist (e.g. a
        # report_only row and a shared row for the same source_link).
        # Dedup must respect visibility (composition review finding): a
        # same-source_link row that exists but isn't visible to
        # scope.acting_user_id is otherwise an existence oracle (its
        # id/surfaced_count leak through the dedup branch's return value)
        # AND a mutation route that bypasses the rule get_agenda already
        # enforces on every read. Skipping straight to the first VISIBLE
        # candidate (falling back to None if none match) — NOT rejecting
        # — means a non-visible item existing never blocks the acting
        # party from adding their own visible entry for the same
        # source_link (mirrors the existing "no match at all" behavior).
        candidates = (
            scope.session.execute(
                scope.query(AgendaItem).where(
                    AgendaItem.source_link == item["source_link"],
                    AgendaItem.status != "resolved",
                )
            )
            .scalars()
            .all()
        )
        existing = next(
            (candidate for candidate in candidates if is_visible_to(candidate, scope)),
            None,
        )

    if existing is not None:
        if dedup_guard is not None and existing.id in dedup_guard:
            # Already bumped once this run (by an earlier entry in this
            # same batch, or a separate append_agenda_items_tool/
            # append_ledger_items_tool call within the same synthesis
            # turn) — this is the model re-surfacing the same point a
            # second time within ONE transcript, which the prompt's own
            # "one transcript point = one tool entry" rule already says
            # should never happen. Return the row as-is rather than
            # compounding the duplicate bump.
            logger.warning(
                "append_agenda_item: existing_item_id=%s already bumped "
                "earlier this run; skipping a second surfaced_count "
                "increment (likely the same transcript point tool-called "
                "more than once)",
                existing.id,
            )
            return existing
        existing.surfaced_count += 1
        if existing.surfaced_count >= config.PENDING_CONSENT_THRESHOLD:
            existing.status = "pending_consent"
        _write_history(
            scope,
            existing,
            item["created_by_user_id"],
            {"surfaced_count": existing.surfaced_count, "status": existing.status},
            clock,
        )
        scope.commit()
        if dedup_guard is not None:
            dedup_guard.add(existing.id)
        return existing

    current_pair_id = item.get("pair_id")
    if current_pair_id is None:
        from app.agenda.models import Pair

        # Pair does carry report_user_id, so it routes through
        # scope.query(...) normally (unlike AgendaItemHistory above) —
        # no bare select(...) needed here.
        current_pair = scope.session.execute(
            scope.query(Pair).where(Pair.ended_at.is_(None))
        ).scalar_one_or_none()
        current_pair_id = current_pair.id if current_pair is not None else None
        if current_pair_id is None:
            # A report can resolve a PairScope with zero Pair rows (e.g.
            # no manager assigned yet) — halt rather than guess, matching
            # spec §7's "no resolvable Pair ⇒ reject" rule.
            raise ValueError(
                f"no current Pair for report_user_id={scope.report_user_id!r}; "
                "cannot write an agenda item with no manager-era to attribute it to"
            )

    row = AgendaItem(
        id=str(uuid.uuid4()),
        pair_id=current_pair_id,
        text=item["text"],
        source=item["source"],
        source_link=item.get("source_link"),
        visibility=item.get("visibility", "shared"),
        status="open",
        surfaced_count=0,
        created_at=now,
        created_by_user_id=item["created_by_user_id"],
        created_by_role=item["created_by_role"],
    )
    scope.add(row)
    scope.session.flush()  # so row.id is stable before the history FK reads it
    _write_history(scope, row, item["created_by_user_id"], {"created": True}, clock)
    scope.commit()
    if dedup_guard is not None:
        dedup_guard.add(row.id)
    return row


def _parse_occurred_at(value: str | datetime.datetime) -> datetime.datetime:
    return (
        value
        if isinstance(value, datetime.datetime)
        else datetime.datetime.fromisoformat(value)
    )


def _parse_optional_due_at(
    value: str | datetime.datetime | None,
) -> datetime.datetime | None:
    """due_at is documented to append_ledger_item_tool's caller (the
    synthesize LLM) as "ISO timestamp, if known" — but a live run against
    a real transcript proved the model doesn't reliably follow that: it
    extracted a real commitment ("finalize migration tests") with
    due_at="Thursday" (a relative-date phrase straight from the
    transcript, not ISO), which crashed the whole run_post_meeting_flow
    call with an uncaught DB error (raw string into a `TIMESTAMP WITH
    TIME ZONE` column) — losing every other decision/commitment/
    accomplishment extracted in that same turn along with it. due_at is
    optional and best-effort by design; an unparseable value is treated
    as "not confidently known" (stored as None) rather than aborting the
    entire synthesis over one field."""
    if value is None or isinstance(value, datetime.datetime):
        return value
    try:
        return datetime.datetime.fromisoformat(value)
    except (ValueError, TypeError):
        # TypeError: fromisoformat requires a str — the LLM already got
        # the type wrong once (due_at="Thursday" was a string but not
        # ISO), so a non-string value (number, bool, list) is exactly as
        # plausible and must degrade the same way, not crash differently.
        logging.getLogger(__name__).warning(
            "append_ledger_item: commitment due_at=%r is not a parseable "
            "ISO timestamp, storing as None rather than failing the whole "
            "synthesis run",
            value,
        )
        return None


def _has_live_visible_item(scope: PairScope, source_link: str) -> bool:
    """Mirrors append_agenda_item's own dedup candidate selection (same
    non-resolved + is_visible_to filter) — used by append_ledger_item to
    verify a caller-supplied source_link is real before trusting it to
    mean 'skip the durable write', rather than trusting it blindly."""
    candidates = (
        scope.session.execute(
            scope.query(AgendaItem).where(
                AgendaItem.source_link == source_link,
                AgendaItem.status != "resolved",
            )
        )
        .scalars()
        .all()
    )
    return any(is_visible_to(candidate, scope) for candidate in candidates)


def append_ledger_item(
    pair_scope: PairScope,
    owner_scope: OwnerScope,
    kind: str,
    item: dict,
    clock: Clock,
    dedup_guard: set[str] | None = None,
) -> AgendaItem:
    """The load-bearing branch (design spec §4.2): commitment/accomplishment
    write to their durable, owner-scoped table FIRST, then mirror a
    lightweight pointer onto the agenda via append_agenda_item — never a
    second full copy. jira_blocker/slack_q never come through here; they
    call append_agenda_item directly.

    item["source_link"] is optional and is the dedup handle the
    agenda_synthesize.v1 prompt instructs the LLM to pass back (the
    source_link of an existing ledger-mirrored item it read via
    get_agenda_tool first) when re-recording something already on the
    agenda. When given AND it genuinely matches a live, visible mirror
    item, this skips minting a brand-new Commitment/Accomplishment row
    entirely and delegates straight to append_agenda_item with that
    source_link, so its own dedup-by-source_link path bumps the existing
    mirror's surfaced_count instead of creating a duplicate durable row +
    duplicate mirror. A wrong/stale/hallucinated source_link (one that
    doesn't resolve to a live item) is NOT trusted blindly — that would
    silently skip the durable row while still creating an agenda mirror,
    permanently orphaning it from the ledger this function's docstring
    promises to write to first. In that case a fresh durable row is
    minted exactly as if no source_link had been given."""
    now = clock.now()
    existing_item_id = item.get("existing_item_id")
    if existing_item_id is not None:
        existing_item = get_agenda_item(pair_scope, existing_item_id)
        if (
            existing_item is not None
            and is_visible_to(existing_item, pair_scope)
            and existing_item.status != "resolved"
        ):
            return append_agenda_item(
                pair_scope,
                {
                    "text": item["description"],
                    "source": (
                        "commitment_ledger"
                        if kind == "commitment"
                        else "accomplishment_ledger"
                    ),
                    "existing_item_id": existing_item_id,
                    "source_link": existing_item.source_link,
                    "visibility": "shared",
                    "created_by_user_id": item["created_by_user_id"],
                    "created_by_role": item["created_by_role"],
                },
                clock,
                dedup_guard=dedup_guard,
            )
        logger.warning(
            "append_ledger_item: existing_item_id=%s did not resolve to a live visible open item; falling back to durable row creation",
            existing_item_id,
        )

    if kind == "commitment":
        existing_source_link = item.get("source_link")
        if existing_source_link is not None and not _has_live_visible_item(
            pair_scope, existing_source_link
        ):
            existing_source_link = None
        if existing_source_link is None:
            row = Commitment(
                id=str(uuid.uuid4()),
                promised_to_person_id=item.get("promised_to_person_id"),
                description=item["description"],
                source_reference_key=f"agenda:{pair_scope.report_user_id}",
                promised_at=now,
                due_at=_parse_optional_due_at(item.get("due_at")),
                delivered_at=None,
                status="open",
            )
            owner_scope.add(row)
            owner_scope.commit()
            existing_source_link = row.id
        return append_agenda_item(
            pair_scope,
            {
                "text": item["description"],
                "source": "commitment_ledger",
                "source_link": existing_source_link,
                "visibility": "shared",
                "created_by_user_id": item["created_by_user_id"],
                "created_by_role": item["created_by_role"],
            },
            clock,
            dedup_guard=dedup_guard,
        )

    if kind == "accomplishment":
        existing_source_link = item.get("source_link")
        if existing_source_link is not None and not _has_live_visible_item(
            pair_scope, existing_source_link
        ):
            existing_source_link = None
        if existing_source_link is None:
            row = Accomplishment(
                id=str(uuid.uuid4()),
                description=item["description"],
                source_reference_key=f"agenda:{pair_scope.report_user_id}",
                occurred_at=_parse_occurred_at(item["occurred_at"]),
                goal_id=item.get("goal_id"),
            )
            owner_scope.add(row)
            owner_scope.commit()
            existing_source_link = row.id
        return append_agenda_item(
            pair_scope,
            {
                "text": item["description"],
                "source": "accomplishment_ledger",
                "source_link": existing_source_link,
                "visibility": "shared",
                "created_by_user_id": item["created_by_user_id"],
                "created_by_role": item["created_by_role"],
            },
            clock,
            dedup_guard=dedup_guard,
        )

    raise ValueError(
        f"append_ledger_item only handles commitment/accomplishment, got {kind!r}"
    )


def update_agenda_item(
    scope: PairScope, item_id: str, changes: dict, changed_by_user_id: str, clock: Clock
) -> AgendaItem:
    if "visibility" in changes:
        _validate_visibility(changes["visibility"])
    row = scope.session.execute(
        scope.query(AgendaItem).where(AgendaItem.id == item_id)
    ).scalar_one()
    if (
        "visibility" in changes
        and changes["visibility"] != row.visibility
        and row.source_link is not None
        and changes.get("status", row.status) != "resolved"
    ):
        # The partial unique index excludes resolved rows entirely
        # (postgresql_where: status != 'resolved'), so a visibility change
        # on an already-resolved row can never violate it — checking
        # row.status here (not just the candidate's) avoids rejecting a
        # harmless edit just because an unrelated open row happens to
        # already occupy the target visibility bucket.
        #
        # uq_agenda_item_report_source_link_visibility allows one open row
        # per (report_user_id, source_link, visibility) bucket. Changing
        # visibility can move this row into a bucket another open row
        # already occupies (e.g. flipping a report_only row to shared
        # while a shared row for the same source_link already exists) —
        # checked proactively so this raises a clean, catchable ValueError
        # (agenda_router.py's routes and handle_agenda_signal's callers
        # already expect ValueError -> a rejected/400 response) instead of
        # an uncaught IntegrityError from scope.commit() below.
        collision = scope.session.execute(
            scope.query(AgendaItem).where(
                AgendaItem.source_link == row.source_link,
                AgendaItem.visibility == changes["visibility"],
                AgendaItem.status != "resolved",
                AgendaItem.id != row.id,
            )
        ).scalar_one_or_none()
        if collision is not None:
            raise ValueError(
                f"cannot change visibility to {changes['visibility']!r}: another "
                "open item already occupies this (report, source_link, "
                "visibility) slot"
            )
    for key, value in changes.items():
        setattr(row, key, value)
    # Mirror append_agenda_item's consent-on-stale-items gate (spec
    # §4.3.1): any caller that bumps surfaced_count across the threshold
    # gets the same auto-flip to pending_consent, not just the dedup path
    # in append_agenda_item. Without this, a caller that re-surfaces an
    # item via update_agenda_item (rather than append_agenda_item's
    # dedup branch) could push surfaced_count past the threshold forever
    # without ever tripping consent — silently defeating the gate.
    if (
        "surfaced_count" in changes
        and row.surfaced_count >= config.PENDING_CONSENT_THRESHOLD
    ):
        row.status = "pending_consent"
    _write_history(scope, row, changed_by_user_id, changes, clock)
    scope.commit()
    return row


def mark_resolved(
    scope: PairScope, item_id: str, changed_by_user_id: str, clock: Clock
) -> AgendaItem:
    """Idempotent on an already-resolved item (REAL BUG FOUND: confirmed
    live via agenda_item_history — the same item got resolve_item_tool
    called on it 3 times across 3 separate meetings, each one silently
    overwriting resolved_at and adding a redundant history row, because
    append_agenda_item's own existing_item_id lookup excludes resolved
    items from dedup matching, so a resolved item never stops appearing
    in get_full_agenda's output for the model to act on again). Returns
    the row as-is on a second+ call rather than compounding it — mirrors
    append_agenda_item's own dedup_guard posture of never trusting the
    model to only call a write tool once per real-world event."""
    row = get_agenda_item(scope, item_id)
    if row is not None and row.status == "resolved":
        logger.warning(
            "mark_resolved: item_id=%s is already resolved; skipping a "
            "redundant resolved_at overwrite and history write",
            item_id,
        )
        return row
    now = clock.now()
    return update_agenda_item(
        scope,
        item_id,
        {"status": "resolved", "resolved_at": now},
        changed_by_user_id,
        clock,
    )


def add_manual_note(
    scope: PairScope,
    acting_user_id: str,
    text: str,
    visibility: str | None,
    clock: Clock,
) -> AgendaItem:
    """Standalone entry, independent of the meeting cadence (design spec
    §4.3). Skips append_agenda_item's dedup-on-source_link entirely — a
    manual note has source_link=None and nothing external to dedup
    against, so every call creates a new row."""
    from app.agenda.models import Pair

    _validate_visibility(visibility or "shared")
    now = clock.now()
    role = "report" if acting_user_id == scope.report_user_id else "manager"

    # Pair carries report_user_id, so this routes through scope.query(...)
    # rather than a bare select(...) — the scope-leak gate (tests/agenda/
    # test_invariants.py) bans bare select(...) on pair-scoped tables
    # anywhere in app/agenda/ outside scope.py, mirroring
    # append_agenda_item's own Pair lookup above.
    current_pair = scope.session.execute(
        scope.query(Pair).where(Pair.ended_at.is_(None))
    ).scalar_one_or_none()
    current_pair_id = current_pair.id if current_pair is not None else None
    if current_pair_id is None:
        # Same "halt, don't guess" rule append_agenda_item follows (spec
        # §7): a report can resolve a PairScope with zero Pair rows (no
        # manager assigned yet), and a manual note has no manager-era to
        # attribute itself to in that case.
        raise ValueError(
            f"no current Pair for report_user_id={scope.report_user_id!r}; "
            "cannot add a manual note with no manager-era to attribute it to"
        )

    row = AgendaItem(
        id=str(uuid.uuid4()),
        pair_id=current_pair_id,
        text=text,
        source="manual",
        source_link=None,
        visibility=visibility or "shared",
        status="open",
        surfaced_count=0,
        created_at=now,
        created_by_user_id=acting_user_id,
        created_by_role=role,
    )
    scope.add(row)
    scope.session.flush()
    _write_history(
        scope, row, acting_user_id, {"created": True, "source": "manual"}, clock
    )
    scope.commit()
    return row
