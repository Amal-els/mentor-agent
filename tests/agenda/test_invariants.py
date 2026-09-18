# tests/agenda/test_invariants.py — created now, more tests appended by
# later tasks (mirrors identity resolution's Task 15 pattern, but built
# from the start here rather than retrofitted at the end, per spec §3.2).
import ast
import datetime
import uuid
from pathlib import Path

from app.agenda.scope import resolve_pair_scope
from app.agenda.store import (
    add_manual_note,
    append_agenda_item,
    append_ledger_item,
    get_agenda,
    mark_resolved,
    update_agenda_item,
)
from app.core.clock import FrozenClock
from app.core.scope import OwnerScope

NOW = datetime.datetime(2026, 8, 14, tzinfo=datetime.UTC)

# Tables backing models that carry report_user_id (app/agenda/models.py):
# Pair and AgendaItem. AgendaItemHistory has no report_user_id (it's keyed
# off agenda_item_id) and Accomplishment is owner-scoped (owner_user_id,
# not report_user_id) — neither belongs in this list.
_PAIR_SCOPED_TABLE_NAMES = ("agenda_items", "pairs")


def _literal_sql_text(node: ast.AST) -> str | None:
    """Best-effort extraction of the literal SQL string a text(...) call's
    first argument represents, for substring analysis. Handles a plain
    string constant and an f-string (joining only its literal pieces,
    since the interpolated parts are opaque to static analysis anyway —
    if a violation is hiding entirely inside an f-string's {...} expr, no
    amount of AST-only checking will find it, so this is a deliberate,
    documented gap rather than a silent one). Returns None (skip
    analysis) for anything else, e.g. a bare Name/variable holding SQL
    built up elsewhere — this check is defense-in-depth, not a full data-
    flow analysis."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return None


def find_scope_leak_violations(source: str, label: str) -> list[str]:
    """No app/agenda/*.py module may call session.query(...)/db_session
    .query(...), <anything>.session.query(...) (the bypass reached by
    pulling the public .session field off a PairScope/OwnerScope and
    querying it directly), a bare select(...), or a text(...) call whose
    literal SQL references a pair-scoped table (agenda_items, pairs)
    without also filtering on report_user_id — outside PairScope's own
    helpers (app/agenda/scope.py). scope.query(...) is the sanctioned
    form; scope.session.execute(...) is unaffected since its attr is
    "execute", not "query". A text(...) call against a non-pair-scoped
    table (e.g. agenda_item_history, which has no report_user_id column
    and thus structurally cannot route through scope.query(...)) is not
    flagged."""
    violations = []
    tree = ast.parse(source, filename=label)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "query":
            base = node.value
            if isinstance(base, ast.Name) and base.id in ("session", "db_session"):
                violations.append(f"{label}:{node.lineno} bare session.query(...)")
            elif isinstance(base, ast.Attribute) and base.attr == "session":
                violations.append(
                    f"{label}:{node.lineno} <scope>.session.query(...) bypass"
                )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "select":
                violations.append(f"{label}:{node.lineno} bare select(...)")
            elif node.func.id == "text" and node.args:
                sql = _literal_sql_text(node.args[0])
                if sql is not None:
                    hits_pair_scoped_table = any(
                        table in sql for table in _PAIR_SCOPED_TABLE_NAMES
                    )
                    if hits_pair_scoped_table and "report_user_id" not in sql:
                        violations.append(
                            f"{label}:{node.lineno} text(...) referencing a "
                            "pair-scoped table with no report_user_id filter"
                        )

    return violations


def test_scope_leak_static_check():
    """No app/agenda/*.py module may call session.query(...)/db_session
    .query(...) or a bare select(...) outside PairScope's own helpers
    (app/agenda/scope.py). scope.query(...) is the sanctioned form."""
    agenda_dir = Path("app/agenda")
    violations = []

    for py_file in agenda_dir.glob("*.py"):
        if py_file.name == "scope.py":
            continue  # the sanctioned implementation itself
        source = py_file.read_text(encoding="utf-8")
        violations.extend(find_scope_leak_violations(source, str(py_file)))

    assert not violations, "scope-leak violations found:\n" + "\n".join(violations)


def test_scope_leak_check_flags_dot_session_query_bypass():
    """Regression for the bypass a reviewer flagged: grabbing .session off
    a PairScope/OwnerScope and querying it directly skips the sanctioned
    scope.query(...) path entirely, and the naive check (bare
    session.query(...)/db_session.query(...) only) would miss it because
    scope.session is an ast.Attribute, not an ast.Name."""
    violating_sources = [
        "scope.session.query(SomeModel)\n",
        "pair_scope.session.query(SomeModel)\n",
        "self.pair_scope.session.query(SomeModel)\n",
    ]
    for source in violating_sources:
        violations = find_scope_leak_violations(source, "<test>")
        assert violations, f"expected a violation for: {source!r}"


def test_scope_leak_check_does_not_false_positive_on_sanctioned_patterns():
    """scope.query(Model) (the sanctioned form) and scope.session.execute
    (a legitimate pattern used elsewhere, e.g. inside PairScope.commit's
    sibling helpers) must not be flagged."""
    clean_sources = [
        "scope.query(SomeModel)\n",
        "db_session.execute(scope.query(SomeModel))\n",
        "scope.session.execute(scope.query(SomeModel))\n",
    ]
    for source in clean_sources:
        violations = find_scope_leak_violations(source, "<test>")
        assert not violations, f"unexpected violation(s) for: {source!r}: {violations}"


def test_scope_leak_check_flags_unfiltered_text_on_pair_scoped_table():
    """A raw text(...) query against a pair-scoped table (agenda_items,
    pairs) with no report_user_id filter is exactly the kind of bypass
    scope.query(...) exists to prevent — it must be flagged even though
    it isn't a session.query(...) or select(...) call."""
    violating_sources = [
        'text("SELECT * FROM agenda_items")\n',
        'text("SELECT * FROM agenda_items WHERE status = :status")\n',
        'text("SELECT id FROM pairs")\n',
        'text(f"SELECT * FROM agenda_items WHERE id = {item_id}")\n',
    ]
    for source in violating_sources:
        violations = find_scope_leak_violations(source, "<test>")
        assert violations, f"expected a violation for: {source!r}"


def test_scope_leak_check_does_not_flag_legitimate_history_count_query():
    """The one hand-reviewed, approved text(...) usage in the codebase
    (app/agenda/store.py::_write_history's AgendaItemHistory count) must
    not be flagged: agenda_item_history is not a pair-scoped table (no
    report_user_id column), so there is nothing to filter on."""
    source = (
        "text(\n"
        '    "SELECT COUNT(*) FROM agenda_item_history '
        'WHERE agenda_item_id = :agenda_item_id"\n'
        ")\n"
    )
    violations = find_scope_leak_violations(source, "<test>")
    assert not violations, f"unexpected violation(s): {violations}"


def test_scope_leak_check_does_not_flag_text_filtered_on_report_user_id():
    """A text(...) query against a pair-scoped table that itself includes
    a report_user_id filter in the SQL string is treated as scoped (the
    pragmatic escape hatch this heuristic allows for) and not flagged."""
    source = 'text("SELECT * FROM agenda_items WHERE report_user_id = :rid")\n'
    violations = find_scope_leak_violations(source, "<test>")
    assert not violations, f"unexpected violation(s): {violations}"


# --- Task 19: cross-cutting invariants, exercising Tasks 1-18 end-to-end ---


def test_two_reports_sharing_identical_source_link_never_leak(db_session, make_pair):
    """The single worst possible bug this feature could ship (mirrors
    identity resolution's own framing for its cross-tenant isolation
    gate): two different report/manager pairs both referencing the
    identical Jira ticket must never see each other's agenda item."""
    pair_a = make_pair()
    pair_b = make_pair()
    scope_a = resolve_pair_scope(
        db_session, pair_a.report_user_id, pair_a.report_user_id
    )
    scope_b = resolve_pair_scope(
        db_session, pair_b.report_user_id, pair_b.report_user_id
    )
    item = {
        "text": "same ticket, different pairs",
        "source": "jira",
        "source_link": "JIRA-999",
        "visibility": "shared",
        "created_by_user_id": pair_a.report_user_id,
        "created_by_role": "report",
    }

    append_agenda_item(scope_a, item, FrozenClock(at=NOW))
    item_b = dict(item, created_by_user_id=pair_b.report_user_id)
    append_agenda_item(scope_b, item_b, FrozenClock(at=NOW))

    items_a = get_agenda(scope_a)
    items_b = get_agenda(scope_b)
    assert len(items_a) == 1
    assert len(items_b) == 1
    assert items_a[0].id != items_b[0].id


def test_manager_transition_full_lifecycle(db_session, make_pair, make_user):
    """End to end: current manager writes, transition happens, former
    manager is fully cut off, new manager and report retain full history."""
    pair = make_pair()
    scope_old_manager = resolve_pair_scope(
        db_session, pair.report_user_id, pair.manager_user_id
    )
    item = add_manual_note(
        scope_old_manager,
        pair.manager_user_id,
        "before transition",
        "shared",
        FrozenClock(at=NOW),
    )

    from app.agenda.models import Pair

    pair_row = db_session.get(Pair, pair.id)
    pair_row.ended_at = NOW
    # Force the UPDATE to flush before the new Pair's INSERT is even
    # queued — mirrors app/ingest/notion_pair_sync.py::sync_notion_pair_
    # edge's own fix for this exact bug class. Without this, whether the
    # UPDATE or the INSERT lands first within the single db_session.commit() below
    # is emergent SQLAlchemy unit-of-work ordering, not something this
    # test can rely on, and the partial unique index
    # uq_pair_one_active_per_report (report_user_id WHERE ended_at IS
    # NULL) would intermittently reject the INSERT if it flushed first
    # while the old row's ended_at was still NULL.
    db_session.flush()
    new_manager = make_user()
    db_session.add(
        Pair(
            id=str(uuid.uuid4()),
            report_user_id=pair.report_user_id,
            manager_user_id=new_manager,
            started_at=NOW,
            ended_at=None,
        )
    )
    db_session.commit()

    assert (
        resolve_pair_scope(db_session, pair.report_user_id, pair.manager_user_id)
        is None
    )
    scope_new_manager = resolve_pair_scope(db_session, pair.report_user_id, new_manager)
    assert scope_new_manager is not None
    items = get_agenda(scope_new_manager)
    assert any(i.id == item.id for i in items)  # history survives the transition


def test_history_is_append_only_across_every_mutation_path(db_session, make_pair):
    """Every function in app/agenda/store.py that mutates an AgendaItem
    must leave a contiguous, gap-free AgendaItemHistory trail. The two
    functions that actually call _write_history directly are
    append_agenda_item and update_agenda_item; mark_resolved and
    append_ledger_item are thin wrappers over those two. This test drives
    all five public mutation entry points at least once each so the name
    "every mutation path" is literally true, not just true of the two
    primitives underneath."""
    from app.agenda.models import AgendaItemHistory

    pair = make_pair()
    scope = resolve_pair_scope(db_session, pair.report_user_id, pair.report_user_id)
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)

    def versions_for(item_id: str) -> list[int]:
        history = (
            db_session.query(AgendaItemHistory).filter_by(agenda_item_id=item_id).all()
        )
        return sorted(h.version for h in history)

    # 1. append_agenda_item: create branch.
    jira_item = append_agenda_item(
        scope,
        {
            "text": "jira blocker",
            "source": "jira",
            "source_link": "JIRA-1",
            "visibility": "shared",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        FrozenClock(at=NOW),
    )
    assert versions_for(jira_item.id) == [1]

    # 2. append_agenda_item: dedup/re-surface branch (same source_link).
    append_agenda_item(
        scope,
        {
            "text": "jira blocker",
            "source": "jira",
            "source_link": "JIRA-1",
            "visibility": "shared",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        FrozenClock(at=NOW),
    )
    v = versions_for(jira_item.id)
    assert v == [1, 2]  # strictly increasing, no gaps, AND actually grew

    # 3. update_agenda_item directly.
    update_agenda_item(
        scope,
        jira_item.id,
        {"text": "jira blocker, updated"},
        pair.report_user_id,
        FrozenClock(at=NOW),
    )
    v = versions_for(jira_item.id)
    assert v == [1, 2, 3]

    # 4. mark_resolved (wraps update_agenda_item).
    mark_resolved(scope, jira_item.id, pair.report_user_id, FrozenClock(at=NOW))
    v = versions_for(jira_item.id)
    assert v == [1, 2, 3, 4]

    # 5. add_manual_note (independent write path, own item).
    note_item = add_manual_note(
        scope, pair.report_user_id, "track me", "shared", FrozenClock(at=NOW)
    )
    assert versions_for(note_item.id) == [1]

    # 6. append_ledger_item (wraps append_agenda_item, own item).
    ledger_item = append_ledger_item(
        scope,
        owner_scope,
        "commitment",
        {
            "description": "ship the thing",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        FrozenClock(at=NOW),
    )
    assert versions_for(ledger_item.id) == [1]
