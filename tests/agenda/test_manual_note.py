import datetime

import pytest

from app.agenda.scope import resolve_pair_scope
from app.agenda.store import add_manual_note
from app.core.clock import FrozenClock

NOW = datetime.datetime(2026, 8, 14, tzinfo=datetime.UTC)


def test_report_can_add_a_note(db_session, make_pair):
    pair = make_pair()
    scope = resolve_pair_scope(db_session, pair.report_user_id, pair.report_user_id)

    item = add_manual_note(
        scope,
        pair.report_user_id,
        "ask about the roadmap",
        "shared",
        FrozenClock(at=NOW),
    )

    assert item.source == "manual"
    assert item.source_link is None
    assert item.created_by_role == "report"
    assert item.visibility == "shared"


def test_manager_can_add_a_private_note(db_session, make_pair):
    pair = make_pair()
    scope = resolve_pair_scope(db_session, pair.report_user_id, pair.manager_user_id)

    item = add_manual_note(
        scope,
        pair.manager_user_id,
        "flag for calibration",
        "manager_only",
        FrozenClock(at=NOW),
    )

    assert item.created_by_role == "manager"
    assert item.visibility == "manager_only"


def test_defaults_to_shared_visibility(db_session, make_pair):
    pair = make_pair()
    scope = resolve_pair_scope(db_session, pair.report_user_id, pair.report_user_id)

    item = add_manual_note(
        scope, pair.report_user_id, "note", None, FrozenClock(at=NOW)
    )

    assert item.visibility == "shared"


def test_manual_notes_never_dedup_against_each_other(db_session, make_pair):
    pair = make_pair()
    scope = resolve_pair_scope(db_session, pair.report_user_id, pair.report_user_id)

    first = add_manual_note(
        scope, pair.report_user_id, "same text", "shared", FrozenClock(at=NOW)
    )
    second = add_manual_note(
        scope, pair.report_user_id, "same text", "shared", FrozenClock(at=NOW)
    )

    assert first.id != second.id


def test_raises_when_report_has_no_current_pair(db_session, make_user):
    import pytest

    from app.agenda.scope import resolve_pair_scope

    report = make_user()  # no Pair created at all
    scope = resolve_pair_scope(db_session, report, report)
    assert scope is not None  # a report always resolves a scope for themself

    with pytest.raises(ValueError):
        add_manual_note(scope, report, "orphaned note", "shared", FrozenClock(at=NOW))


def test_rejects_invalid_visibility(db_session, make_pair):
    pair = make_pair()
    scope = resolve_pair_scope(db_session, pair.report_user_id, pair.report_user_id)

    with pytest.raises(ValueError):
        add_manual_note(
            scope,
            pair.report_user_id,
            "note",
            "report-only",  # typo'd hyphen instead of underscore
            FrozenClock(at=NOW),
        )
