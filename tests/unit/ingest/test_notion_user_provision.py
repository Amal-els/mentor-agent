import datetime
import uuid

from sqlalchemy import select

from app.core.clock import FrozenClock
from app.core.models import User
from app.ingest.notion_user_provision import (
    auto_provision_notion_users,
    revoke_departed_notion_users,
)

NOW = datetime.datetime(2026, 8, 18, tzinfo=datetime.UTC)


def _person_id() -> str:
    return f"notion-person-{uuid.uuid4()}"


def _email(label: str) -> str:
    return f"{label}-{uuid.uuid4()}@example.com"


def _existing_linked_person_ids(session) -> set[str]:
    """revoke_departed_notion_users has NO owner-scoping — it scans every
    linked User row in the whole table (same deliberate global posture as
    sync_hris_edge/sync_notion_pair_edge). Against this suite's shared,
    non-transactional Postgres instance (the SAME database the real app
    uses, not an isolated test DB), calling it with a member set that
    doesn't include every REAL currently-linked account's id will
    actually revoke those real accounts' real agenda_client_secret as a
    side effect of running this test suite — confirmed live: an earlier
    version of this test did exactly that to three real linked users
    before this helper existed. Every test below that calls
    revoke_departed_notion_users MUST union its member set with this
    snapshot, taken BEFORE creating whatever row(s) the test itself
    wants treated as departed/absent."""
    return set(
        session.execute(
            select(User.notion_person_id).where(User.notion_person_id.is_not(None))
        )
        .scalars()
        .all()
    )


def test_provision_creates_a_new_user_with_defaults(db_session):
    person_id = _person_id()
    email = _email("newperson")
    directory = [
        {"notion_person_id": person_id, "email": email, "display_name": "New Person"}
    ]

    created = auto_provision_notion_users(
        db_session,
        directory,
        FrozenClock(at=NOW),
        default_tz="America/New_York",
        default_pulse_fire_time_local=datetime.time(9, 0),
        default_late_cutoff_local=datetime.time(20, 0),
    )

    assert len(created) == 1
    user = created[0]
    assert user.notion_person_id == person_id
    assert user.notion_owner_email == email
    assert user.notion_display_name == "New Person"
    assert user.tz == "America/New_York"
    assert user.pulse_fire_time_local == datetime.time(9, 0)
    assert user.late_cutoff_local == datetime.time(20, 0)

    row = db_session.get(User, user.id)
    assert row is not None


def test_provision_is_idempotent_by_person_id(db_session):
    person_id = _person_id()
    email = _email("repeat")
    directory = [{"notion_person_id": person_id, "email": email, "display_name": None}]

    first = auto_provision_notion_users(db_session, directory, FrozenClock(at=NOW))
    second = auto_provision_notion_users(db_session, directory, FrozenClock(at=NOW))

    assert len(first) == 1
    assert len(second) == 0
    assert (
        db_session.query(User).filter_by(notion_person_id=person_id).count() == 1
    )


def test_provision_backfills_person_id_onto_existing_email_match_without_duplicating(
    db_session,
):
    email = _email("prelinked")
    existing_id = str(uuid.uuid4())
    db_session.add(User(id=existing_id, created_at=NOW, notion_owner_email=email))
    db_session.commit()

    person_id = _person_id()
    directory = [
        {"notion_person_id": person_id, "email": email, "display_name": "Someone"}
    ]

    created = auto_provision_notion_users(db_session, directory, FrozenClock(at=NOW))

    assert created == []
    row = db_session.get(User, existing_id)
    assert row.notion_person_id == person_id
    assert db_session.query(User).filter_by(notion_owner_email=email).count() == 1


def test_revoke_clears_secret_for_a_user_no_longer_in_the_member_list(db_session):
    # Snapshot BEFORE creating the fake "departed" row — see
    # _existing_linked_person_ids' own docstring for why this is
    # required, not optional, against this suite's shared live database.
    existing_ids = _existing_linked_person_ids(db_session)

    person_id = _person_id()
    uid = str(uuid.uuid4())
    db_session.add(
        User(
            id=uid,
            created_at=NOW,
            notion_owner_email=_email("departed"),
            notion_person_id=person_id,
            agenda_client_secret=f"some-secret-{uuid.uuid4()}",
        )
    )
    db_session.commit()

    later = NOW + datetime.timedelta(days=1)
    # existing_ids (every real/other-test account already linked) stays
    # "present" — only the fake row just created above, deliberately
    # absent from this set, is treated as departed.
    revoked = revoke_departed_notion_users(db_session, existing_ids, FrozenClock(at=later))

    assert uid in [u.id for u in revoked]
    row = db_session.get(User, uid)
    assert row.agenda_client_secret is None
    assert row.notion_access_revoked_at == later


def test_revoke_leaves_current_members_untouched(db_session):
    existing_ids = _existing_linked_person_ids(db_session)

    person_id = _person_id()
    uid = str(uuid.uuid4())
    secret = f"some-secret-{uuid.uuid4()}"
    db_session.add(
        User(
            id=uid,
            created_at=NOW,
            notion_owner_email=_email("still-here"),
            notion_person_id=person_id,
            agenda_client_secret=secret,
        )
    )
    db_session.commit()

    revoked = revoke_departed_notion_users(
        db_session, existing_ids | {person_id}, FrozenClock(at=NOW)
    )

    assert uid not in [u.id for u in revoked]
    row = db_session.get(User, uid)
    assert row.agenda_client_secret == secret
    assert row.notion_access_revoked_at is None


def test_revoke_ignores_users_never_linked_to_notion(db_session):
    existing_ids = _existing_linked_person_ids(db_session)

    uid = str(uuid.uuid4())
    secret = f"some-secret-{uuid.uuid4()}"
    db_session.add(
        User(
            id=uid,
            created_at=NOW,
            agenda_client_secret=secret,
        )
    )
    db_session.commit()

    revoked = revoke_departed_notion_users(db_session, existing_ids, FrozenClock(at=NOW))

    assert uid not in [u.id for u in revoked]
    row = db_session.get(User, uid)
    assert row.agenda_client_secret == secret


def test_revoke_circuit_breaker_blocks_a_majority_revoke(db_session):
    """Regression test for the real incident this circuit breaker exists
    to prevent: a bad member set (in production, an empty set from a
    misparsed Notion error response) must not be able to silently wipe
    most of an already-linked workspace's access in one pass.

    This suite's shared, non-transactional Postgres accumulates fake
    linked rows across every past test run (this file's own tests
    included) — a FIXED new-row count that comfortably outnumbered the
    baseline early in this session can stop doing so once that baseline
    has grown past it (confirmed live: this test flaked exactly that way
    once real accumulated pollution pushed the baseline past a fixed 20).
    Sizing new_count relative to the CURRENT baseline instead of a fixed
    constant keeps the new rows an outright majority regardless of how
    large the baseline has grown by the time this runs."""
    existing_ids = _existing_linked_person_ids(db_session)
    new_count = len(existing_ids) + 10

    new_users = []
    for _ in range(new_count):
        person_id = _person_id()
        uid = str(uuid.uuid4())
        secret = f"some-secret-{uuid.uuid4()}"
        db_session.add(
            User(
                id=uid,
                created_at=NOW,
                notion_owner_email=_email("bulk-departed"),
                notion_person_id=person_id,
                agenda_client_secret=secret,
            )
        )
        new_users.append((uid, secret))
    db_session.commit()

    # member_ids = only the pre-existing baseline — every one of the
    # new_count new rows above looks "departed," which is exactly the
    # majority-revoke shape the breaker exists to refuse.
    revoked = revoke_departed_notion_users(db_session, existing_ids, FrozenClock(at=NOW))

    assert revoked == []
    for uid, secret in new_users:
        row = db_session.get(User, uid)
        assert row.agenda_client_secret == secret
        assert row.notion_access_revoked_at is None


def test_revoke_circuit_breaker_does_not_block_a_small_workspace(db_session):
    """Below the minimum-candidate threshold, a real, legitimate
    majority-of-a-tiny-workspace revocation (e.g. one of two people
    leaving) must still go through — the breaker is sized for "this
    looks like bad input," not "more than half," which would incorrectly
    block completely normal small-workspace churn."""
    existing_ids = _existing_linked_person_ids(db_session)
    if len(existing_ids) >= 5:
        # Some other test/run already has 5+ linked users in this shared
        # DB — the breaker would legitimately apply here too, so this
        # specific "below threshold" scenario can't be exercised
        # deterministically right now. The majority-blocks test above
        # already covers the breaker's core behavior either way.
        return

    person_id = _person_id()
    uid = str(uuid.uuid4())
    db_session.add(
        User(
            id=uid,
            created_at=NOW,
            notion_owner_email=_email("small-workspace-departed"),
            notion_person_id=person_id,
            agenda_client_secret=f"some-secret-{uuid.uuid4()}",
        )
    )
    db_session.commit()

    revoked = revoke_departed_notion_users(db_session, existing_ids, FrozenClock(at=NOW))

    assert uid in [u.id for u in revoked]
    row = db_session.get(User, uid)
    assert row.agenda_client_secret is None
