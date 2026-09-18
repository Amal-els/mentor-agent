import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import Suppression, User
from app.core.scope import OwnerScope
from app.salience.dossier.gate import apply_dossier_gate

NOW = datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC)


def _make_owner(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.flush()
    return user


def test_below_top_20_percent_drops(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)
    history = [1.0] * 100  # candidate scores below all history -> not top 20%

    decision = apply_dossier_gate(
        scope, clock, candidate_score=0.1, event_external_id="evt-1",
        is_manager=False, history_scores=history,
    )
    assert decision == "drop"


def test_top_20_percent_with_budget_pushes(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)
    history = [0.1] * 100  # candidate score is far above history -> top 20%

    decision = apply_dossier_gate(
        scope, clock, candidate_score=5.0, event_external_id="evt-2",
        is_manager=False, history_scores=history,
    )
    assert decision == "push"


def test_manager_structural_floor_always_pushes(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)
    history = [10.0] * 100  # candidate score would otherwise drop

    decision = apply_dossier_gate(
        scope, clock, candidate_score=0.0, event_external_id="evt-3",
        is_manager=True, history_scores=history,
    )
    assert decision == "push"


def test_active_suppression_forces_drop_even_for_manager(pg_session):
    user = _make_owner(pg_session)
    pg_session.add(
        Suppression(
            id=str(uuid.uuid4()),
            owner_user_id=user.id,
            scope="instance",
            target_ref="evt-4",
            reason="user muted this one",
            created_at=NOW,
            # Suppression.expires_at is NOT NULL at the DB level (see
            # migrations/versions/18fc1fbd1eec_pulse_ritual_schema.py and
            # app/salience/gate.py's `Suppression.expires_at > clock.now()`
            # convention) -- there is no "permanent" suppression encoded as
            # None. A far-future expiry stands in for "still active now".
            expires_at=NOW + datetime.timedelta(days=3650),
            created_by="user",
        )
    )
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)

    decision = apply_dossier_gate(
        scope, clock, candidate_score=5.0, event_external_id="evt-4",
        is_manager=True, history_scores=[0.0] * 100,
    )
    assert decision == "drop"
