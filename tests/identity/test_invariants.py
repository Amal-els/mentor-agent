import ast
import datetime
import uuid
from pathlib import Path

from app.core.clock import FrozenClock
from app.identity.models import Person, RosterVersion
from app.identity.resolve import confirm, reject, resolve, unlink
from app.identity.types import RawReference

NOW = datetime.datetime(2026, 8, 5, tzinfo=datetime.UTC)


def _assert_no_dual_membership(db_session, owner_user_id):
    from app.identity.models import Identity, UnresolvedReference

    identity_keys = {
        row.reference_key
        for row in db_session.query(Identity)
        .filter_by(owner_user_id=owner_user_id)
        .all()
    }
    unresolved_keys = {
        row.reference_key
        for row in db_session.query(UnresolvedReference)
        .filter_by(owner_user_id=owner_user_id)
        .all()
    }
    overlap = identity_keys & unresolved_keys
    assert (
        not overlap
    ), f"reference_key(s) in both Identity and UnresolvedReference: {overlap}"


def test_invariant_holds_after_auto_link(db_session, make_scope):
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()),
        canonical_name="Sarah Ben Youssef",
        primary_email="sarah@acme.com",
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    scope.add(person)
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    scope.commit()

    ref = RawReference(
        source="slack",
        external_id="U1",
        handle="sarah.dev",
        email="sarah@acme.com",
        display_name=None,
    )
    resolve(scope, ref, FrozenClock(at=NOW))

    _assert_no_dual_membership(db_session, scope.owner_user_id)


def test_invariant_holds_after_ask_then_confirm(db_session, make_scope):
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()),
        canonical_name="Sarah Ben Youssef",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    other = Person(
        id=str(uuid.uuid4()),
        canonical_name="Other Person",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    scope.add(person)
    scope.add(other)
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    scope.commit()

    ref = RawReference(
        source="calendar",
        external_id=None,
        handle=None,
        email=None,
        display_name="Sarah Ben Youssef",
    )
    result = resolve(scope, ref, FrozenClock(at=NOW))
    _assert_no_dual_membership(db_session, scope.owner_user_id)

    confirm(scope, result.reference_key, person.id, FrozenClock(at=NOW))
    _assert_no_dual_membership(db_session, scope.owner_user_id)


def test_invariant_holds_after_ask_then_reject(db_session, make_scope):
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()),
        canonical_name="Sarah Ben Youssef",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    other = Person(
        id=str(uuid.uuid4()),
        canonical_name="Other Person",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    scope.add(person)
    scope.add(other)
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    scope.commit()

    ref = RawReference(
        source="calendar",
        external_id=None,
        handle=None,
        email=None,
        display_name="Sarah Ben Youssef",
    )
    result = resolve(scope, ref, FrozenClock(at=NOW))
    reject(scope, result.reference_key, person.id, FrozenClock(at=NOW))

    _assert_no_dual_membership(db_session, scope.owner_user_id)


def test_invariant_holds_after_confirm_then_unlink_then_reresolve(
    db_session, make_scope
):
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()),
        canonical_name="Sarah Ben Youssef",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    other = Person(
        id=str(uuid.uuid4()),
        canonical_name="Other Person",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    scope.add(person)
    scope.add(other)
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    scope.commit()

    ref = RawReference(
        source="calendar",
        external_id=None,
        handle=None,
        email=None,
        display_name="Sarah Ben Youssef",
    )
    result = resolve(scope, ref, FrozenClock(at=NOW))
    confirm(scope, result.reference_key, person.id, FrozenClock(at=NOW))
    unlink(scope, result.reference_key, reason="test", clock=FrozenClock(at=NOW))
    resolve(scope, ref, FrozenClock(at=NOW))

    _assert_no_dual_membership(db_session, scope.owner_user_id)


def test_invariant_holds_after_ask_then_roster_change_then_auto_link(
    db_session, make_scope
):
    """C2 regression: the auto-link path (_write_identity) never cleaned up the
    UnresolvedReference row the way confirm() does, so a reference that first
    landed in the ask band and later auto-linked after a matching-relevant
    roster change ended up in BOTH tables at once — and build_batch() would go
    on asking the user to identify someone already verified-linked."""
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()),
        canonical_name="Sarah Ben Youssef",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    other = Person(
        id=str(uuid.uuid4()),
        canonical_name="Other Person",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    scope.add(person)
    scope.add(other)
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    scope.commit()

    # 1. first ingest: tier-3 exact name lands in the ask band -> UnresolvedReference
    ref = RawReference(
        source="calendar",
        external_id=None,
        handle=None,
        email=None,
        display_name="Sarah Ben Youssef",
    )
    result = resolve(scope, ref, FrozenClock(at=NOW))
    from app.identity.models import UnresolvedReference

    assert (
        db_session.query(UnresolvedReference)
        .filter_by(
            owner_user_id=scope.owner_user_id, reference_key=result.reference_key
        )
        .count()
        == 1
    )

    # 2. a matching-relevant roster change: the person's primary_email is set
    person.primary_email = "sarah@acme.com"
    db_session.get(RosterVersion, scope.owner_user_id).version = 2
    scope.commit()

    # 3. the SAME reference is re-ingested, now carrying the email -> tier-1
    #    auto-link. reference_key is unchanged (it never includes email).
    ref_with_email = RawReference(
        source="calendar",
        external_id=None,
        handle=None,
        email="sarah@acme.com",
        display_name="Sarah Ben Youssef",
    )
    relinked = resolve(scope, ref_with_email, FrozenClock(at=NOW))
    assert relinked.person_id == person.id

    _assert_no_dual_membership(db_session, scope.owner_user_id)


def test_cross_user_isolation_same_handle_email_and_name(db_session, make_scope):
    """The gate the multi-tenancy revision exists for (spec §11 test 1): two
    users whose sources contain the SAME Slack handle, email, and display
    name. Full ingest+resolution for both must produce zero shared Person
    rows, zero shared Identity rows, and each user's roster query returns
    only their own rows."""
    scope_a = make_scope()
    scope_b = make_scope()

    for scope in (scope_a, scope_b):
        person = Person(
            id=str(uuid.uuid4()),
            canonical_name="Sarah Ben Youssef",
            primary_email="sarah@acme.com",
            is_self=False,
            is_active=True,
            created_at=NOW,
        )
        scope.add(person)
        db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
        scope.commit()

    ref = RawReference(
        source="slack",
        external_id="U123",
        handle="sarah.dev",
        email="sarah@acme.com",
        display_name="Sarah Ben Youssef",
    )
    result_a = resolve(scope_a, ref, FrozenClock(at=NOW))
    result_b = resolve(scope_b, ref, FrozenClock(at=NOW))

    from app.identity.roster import load_snapshot

    people_a = {p.id for p in load_snapshot(scope_a).people}
    people_b = {p.id for p in load_snapshot(scope_b).people}

    assert people_a.isdisjoint(people_b)
    assert result_a.person_id != result_b.person_id
    assert result_a.person_id in people_a
    assert result_b.person_id in people_b


def test_scope_leak_static_check():
    """Static gate (spec §8, §11.3): no app/identity/*.py module may call
    session.query(...) or a bare select(...) on an owned model outside
    OwnerScope's own helpers. This walks the AST of every module in
    app/identity/ and flags two forms:

    1. `session.query(...)` / `db_session.query(...)` attribute access —
       the disallowed bare form. `scope.query(...)` is the sanctioned
       OwnerScope method and also matches attr=="query", so we only flag
       when the value is literally named "session"/"db_session", not
       "scope".
    2. A bare `select(...)` call — i.e. `ast.Call` whose func is exactly
       `ast.Name(id="select")` — used directly instead of going through
       `scope.query(...)`. This form is legal inside app/core/scope.py
       (OwnerScope.query's own implementation) but that module lives
       outside app/identity/, so this glob never walks it; nothing in
       app/identity/ imports `select` directly today (confirmed by a
       direct grep of the module at the time this test was written), so
       this half of the check adds forward-covering regression
       protection without any legitimate call site to false-positive on.
       `scope.query(...)`/`scope.session.execute(...)` are unaffected
       since neither matches `ast.Name(id="select")`.
    """
    identity_dir = Path("app/identity")
    violations = []

    for py_file in identity_dir.glob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "query":
                # session.query(...) — the disallowed bare form. scope.query(...)
                # is the sanctioned OwnerScope method and also matches attr=="query",
                # so only flag when the value is literally named "session"/"db_session",
                # not "scope".
                if isinstance(node.value, ast.Name) and node.value.id in (
                    "session",
                    "db_session",
                ):
                    violations.append(
                        f"{py_file}:{node.lineno} bare session.query(...)"
                    )
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "select"
            ):
                # bare select(...) — not routed through scope.query(...).
                violations.append(f"{py_file}:{node.lineno} bare select(...)")

    assert not violations, "scope-leak violations found:\n" + "\n".join(violations)
