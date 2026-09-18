import datetime

import pytest

from app.agenda.models import AgendaItem, AgendaItemHistory
from app.agenda.scope import resolve_pair_scope
from app.agenda.store import (
    append_agenda_item,
    get_agenda,
    is_visible_to,
    mark_resolved,
    update_agenda_item,
)
from app.core.clock import FrozenClock

NOW = datetime.datetime(2026, 8, 14, tzinfo=datetime.UTC)


def _scope(db_session, pair):
    return resolve_pair_scope(db_session, pair.report_user_id, pair.report_user_id)


def test_append_agenda_item_writes_a_row_and_history(db_session, make_pair):
    pair = make_pair()
    scope = _scope(db_session, pair)

    item = append_agenda_item(
        scope,
        {
            "text": "unblock the CI flake",
            "source": "jira",
            "source_link": "JIRA-123",
            "visibility": "shared",
            "created_by_user_id": pair.manager_user_id,
            "created_by_role": "manager",
        },
        FrozenClock(at=NOW),
    )

    assert item.status == "open"
    assert item.surfaced_count == 0
    row = db_session.get(AgendaItem, item.id)
    assert row is not None
    history = (
        db_session.query(AgendaItemHistory).filter_by(agenda_item_id=item.id).all()
    )
    assert len(history) == 1
    assert history[0].version == 1


def test_get_agenda_returns_only_this_reports_items(db_session, make_pair):
    pair_a = make_pair()
    pair_b = make_pair()
    scope_a = _scope(db_session, pair_a)
    scope_b = _scope(db_session, pair_b)
    append_agenda_item(
        scope_a,
        {
            "text": "A's item",
            "source": "manual",
            "source_link": None,
            "visibility": "shared",
            "created_by_user_id": pair_a.report_user_id,
            "created_by_role": "report",
        },
        FrozenClock(at=NOW),
    )

    items_b = get_agenda(scope_b)

    assert items_b == []


def test_dedup_on_source_link_bumps_surfaced_count_not_a_new_row(db_session, make_pair):
    pair = make_pair()
    scope = _scope(db_session, pair)
    item = {
        "text": "unblock the CI flake",
        "source": "jira",
        "source_link": "JIRA-123",
        "visibility": "shared",
        "created_by_user_id": pair.manager_user_id,
        "created_by_role": "manager",
    }
    first = append_agenda_item(scope, item, FrozenClock(at=NOW))

    second = append_agenda_item(scope, item, FrozenClock(at=NOW))

    assert second.id == first.id
    assert second.surfaced_count == 1
    # Filtered by report_user_id, not a global count: other tests in this
    # same live Postgres leave rows behind (same precedent as
    # tests/unit/ingest/test_notion_pair_sync.py's Pair.count() comment).
    assert (
        db_session.query(AgendaItem)
        .filter_by(report_user_id=pair.report_user_id)
        .count()
        == 1
    )


def test_dedup_ignores_resolved_items_and_creates_a_fresh_one(db_session, make_pair):
    pair = make_pair()
    scope = _scope(db_session, pair)
    item = {
        "text": "unblock the CI flake",
        "source": "jira",
        "source_link": "JIRA-123",
        "visibility": "shared",
        "created_by_user_id": pair.manager_user_id,
        "created_by_role": "manager",
    }
    first = append_agenda_item(scope, item, FrozenClock(at=NOW))
    first_row = db_session.get(AgendaItem, first.id)
    first_row.status = "resolved"
    first_row.resolved_at = NOW
    db_session.commit()

    second = append_agenda_item(scope, item, FrozenClock(at=NOW))

    assert second.id != first.id
    assert (
        db_session.query(AgendaItem)
        .filter_by(report_user_id=pair.report_user_id)
        .count()
        == 2
    )


def test_get_agenda_filters_report_only_items_from_manager(db_session, make_pair):
    pair = make_pair()
    report_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    manager_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.manager_user_id
    )
    append_agenda_item(
        report_scope,
        {
            "text": "private report note",
            "source": "manual",
            "source_link": None,
            "visibility": "report_only",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        FrozenClock(at=NOW),
    )

    assert len(get_agenda(report_scope)) == 1
    assert get_agenda(manager_scope) == []


def test_get_agenda_filters_manager_only_items_from_report(db_session, make_pair):
    pair = make_pair()
    report_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    manager_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.manager_user_id
    )
    append_agenda_item(
        manager_scope,
        {
            "text": "private manager note",
            "source": "manual",
            "source_link": None,
            "visibility": "manager_only",
            "created_by_user_id": pair.manager_user_id,
            "created_by_role": "manager",
        },
        FrozenClock(at=NOW),
    )

    assert len(get_agenda(manager_scope)) == 1
    assert get_agenda(report_scope) == []


def test_get_agenda_shared_items_visible_to_both_parties(db_session, make_pair):
    pair = make_pair()
    report_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    manager_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.manager_user_id
    )
    append_agenda_item(
        report_scope,
        {
            "text": "shared item",
            "source": "manual",
            "source_link": None,
            "visibility": "shared",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        FrozenClock(at=NOW),
    )

    assert len(get_agenda(report_scope)) == 1
    assert len(get_agenda(manager_scope)) == 1


# ---- Finding 2: dedup must respect visibility ----


def test_dedup_skips_a_non_visible_existing_item_and_creates_a_new_one(
    db_session, make_pair
):
    pair = make_pair()
    report_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    manager_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.manager_user_id
    )
    private = append_agenda_item(
        report_scope,
        {
            "text": "private report note",
            "source": "jira",
            "source_link": "JIRA-555",
            "visibility": "report_only",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        FrozenClock(at=NOW),
    )

    # The manager cannot see `private`, so this must NOT dedup-bump it —
    # it must create a brand new, manager-visible row for the same
    # source_link instead.
    fresh = append_agenda_item(
        manager_scope,
        {
            "text": "manager's own view of the same link",
            "source": "jira",
            "source_link": "JIRA-555",
            "visibility": "shared",
            "created_by_user_id": pair.manager_user_id,
            "created_by_role": "manager",
        },
        FrozenClock(at=NOW),
    )

    assert fresh.id != private.id
    db_session.expire_all()
    private_row = db_session.get(AgendaItem, private.id)
    assert private_row.surfaced_count == 0
    assert private_row.status == "open"


# ---- Finding 3: is_visible_to fails closed, visibility is validated ----


def test_is_visible_to_fails_closed_on_unknown_visibility(db_session, make_pair):
    pair = make_pair()
    scope = resolve_pair_scope(db_session, pair.report_user_id, pair.report_user_id)
    item = AgendaItem(
        id="garbage-item",
        report_user_id=pair.report_user_id,
        pair_id="whatever",
        text="x",
        source="manual",
        source_link=None,
        visibility="report-only",  # typo'd hyphen instead of underscore
        status="open",
        surfaced_count=0,
        created_at=NOW,
        created_by_user_id=pair.report_user_id,
        created_by_role="report",
    )

    assert is_visible_to(item, scope) is False


def test_append_agenda_item_rejects_invalid_visibility(db_session, make_pair):
    pair = make_pair()
    scope = resolve_pair_scope(db_session, pair.report_user_id, pair.report_user_id)

    with pytest.raises(ValueError):
        append_agenda_item(
            scope,
            {
                "text": "bad visibility",
                "source": "manual",
                "source_link": None,
                "visibility": "report-only",
                "created_by_user_id": pair.report_user_id,
                "created_by_role": "report",
            },
            FrozenClock(at=NOW),
        )


def test_update_agenda_item_visibility_change_colliding_with_existing_row_is_clean_valueerror(
    db_session, make_pair
):
    """Composition-review finding: a field_edit that flips visibility into
    a bucket another open row already occupies for the same (report,
    source_link) must raise a clean, catchable ValueError, not let an
    uncaught IntegrityError from the widened unique index escape."""
    pair = make_pair()
    report_scope = _scope(db_session, pair)
    manager_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.manager_user_id
    )

    # append_agenda_item's dedup only matches an EXISTING row that's
    # already visible to the acting party — a report_only row is invisible
    # to the manager, so the manager's append creates a genuine second row
    # for the same source_link rather than bumping the first (this is
    # exactly the scenario uq_agenda_item_report_source_link_visibility's
    # widening was added to legally allow).
    private_item = append_agenda_item(
        report_scope,
        {
            "text": "private view",
            "source": "jira",
            "source_link": "JIRA-COLLIDE",
            "visibility": "report_only",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        FrozenClock(at=NOW),
    )
    append_agenda_item(
        manager_scope,
        {
            "text": "shared view",
            "source": "jira",
            "source_link": "JIRA-COLLIDE",
            "visibility": "shared",
            "created_by_user_id": pair.manager_user_id,
            "created_by_role": "manager",
        },
        FrozenClock(at=NOW),
    )

    with pytest.raises(ValueError):
        update_agenda_item(
            report_scope,
            private_item.id,
            {"visibility": "shared"},
            pair.report_user_id,
            FrozenClock(at=NOW),
        )


def test_update_agenda_item_visibility_change_on_a_resolved_item_is_never_rejected(
    db_session, make_pair
):
    """Re-review finding: the partial unique index excludes resolved rows
    entirely (status != 'resolved'), so it can never actually be violated
    by editing a resolved item's visibility — the collision check must
    not reject this just because an unrelated OPEN row happens to already
    occupy that (source_link, visibility) bucket."""
    pair = make_pair()
    scope = _scope(db_session, pair)

    resolved_item = append_agenda_item(
        scope,
        {
            "text": "already handled",
            "source": "jira",
            "source_link": "JIRA-RESOLVED",
            "visibility": "report_only",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        FrozenClock(at=NOW),
    )
    mark_resolved(scope, resolved_item.id, pair.report_user_id, FrozenClock(at=NOW))

    append_agenda_item(
        scope,
        {
            "text": "still open",
            "source": "jira",
            "source_link": "JIRA-RESOLVED",
            "visibility": "shared",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        FrozenClock(at=NOW),
    )

    updated = update_agenda_item(
        scope,
        resolved_item.id,
        {"visibility": "shared"},
        pair.report_user_id,
        FrozenClock(at=NOW),
    )
    assert updated.visibility == "shared"


def test_update_agenda_item_resolving_and_changing_visibility_in_one_call_is_never_rejected(
    db_session, make_pair
):
    """Re-review finding: the collision guard checked the row's
    PRE-mutation status, so resolving an item and changing its
    visibility in the same call could be falsely rejected even though
    the resulting (resolved) row is exempt from the partial unique
    index. Must check the post-mutation status implied by changes."""
    pair = make_pair()
    scope = _scope(db_session, pair)

    target_item = append_agenda_item(
        scope,
        {
            "text": "about to be resolved and re-visibilitied",
            "source": "jira",
            "source_link": "JIRA-RESOLVE-AND-FLIP",
            "visibility": "report_only",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        FrozenClock(at=NOW),
    )
    append_agenda_item(
        scope,
        {
            "text": "already shared",
            "source": "jira",
            "source_link": "JIRA-RESOLVE-AND-FLIP",
            "visibility": "shared",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        FrozenClock(at=NOW),
    )

    updated = update_agenda_item(
        scope,
        target_item.id,
        {"status": "resolved", "visibility": "shared"},
        pair.report_user_id,
        FrozenClock(at=NOW),
    )
    assert updated.status == "resolved"
    assert updated.visibility == "shared"


def test_mark_resolved_is_idempotent_on_an_already_resolved_item(
    db_session, make_pair
):
    """REAL BUG FOUND (confirmed live via agenda_item_history): the same
    item got resolve_item_tool called on it 3 times across 3 separate
    meetings, each silently overwriting resolved_at and adding a
    redundant history row — because get_full_agenda (which the model
    reads via get_agenda_tool) still includes resolved items, so a
    resolved item never stops appearing for the model to act on again.
    A second mark_resolved call must be a no-op, not a fresh mutation."""
    pair = make_pair()
    scope = _scope(db_session, pair)

    item = append_agenda_item(
        scope,
        {
            "text": "set up alerting for the ingestion job",
            "source": "commitment_ledger",
            "source_link": "commitment-abc",
            "visibility": "shared",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        FrozenClock(at=NOW),
    )

    first = mark_resolved(scope, item.id, pair.report_user_id, FrozenClock(at=NOW))
    assert first.status == "resolved"
    first_resolved_at = first.resolved_at

    later = FrozenClock(at=NOW + datetime.timedelta(days=7))
    second = mark_resolved(scope, item.id, pair.report_user_id, later)

    assert second.status == "resolved"
    assert second.resolved_at == first_resolved_at

    history_count = (
        db_session.query(AgendaItemHistory)
        .filter(AgendaItemHistory.agenda_item_id == item.id)
        .count()
    )
    # created + one real resolve = 2, never 3
    assert history_count == 2
