"""Same shape as F1's and the identity design's own cross-owner isolation
tests: seed two owners with overlapping evidence, assert zero cross-owner
leakage in every gather query."""
import datetime
import uuid

from app.agenda.models import Accomplishment
from app.core.clock import FrozenClock
from app.core.models import Commitment, Goal, User
from app.core.scope import OwnerScope
from app.sub_agents.friday_review.sub_agents.gather.agent import gather_friday_review_context

NOW = datetime.datetime(2026, 8, 28, 16, 0, 0, tzinfo=datetime.UTC)


def test_zero_cross_owner_leakage(pg_session):
    owner_a = User(id=str(uuid.uuid4()), created_at=NOW)
    owner_b = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add_all([owner_a, owner_b])
    pg_session.commit()

    commitment_ids: dict[str, str] = {}
    for owner in (owner_a, owner_b):
        commitment_id = str(uuid.uuid4())
        commitment_ids[owner.id] = commitment_id
        pg_session.add_all(
            [
                Accomplishment(
                    id=str(uuid.uuid4()), owner_user_id=owner.id, description="win",
                    source_reference_key=f"acc-{owner.id}",
                    occurred_at=NOW - datetime.timedelta(days=1),
                ),
                Commitment(
                    id=commitment_id, owner_user_id=owner.id, promised_to_person_id=None,
                    description="slipped", source_reference_key=f"src-{owner.id}",
                    promised_at=NOW - datetime.timedelta(days=10),
                    due_at=NOW - datetime.timedelta(days=1), delivered_at=None, status="open",
                ),
                Goal(
                    id=str(uuid.uuid4()), owner_user_id=owner.id, title="goal",
                    status="active", external_ref=None, created_at=NOW,
                    goal_type="key_result", progress=0.5, current_value=5, target_value=10,
                ),
            ]
        )
    pg_session.commit()

    scope_a = OwnerScope(owner_user_id=owner_a.id, session=pg_session)
    context_a = gather_friday_review_context(scope_a, FrozenClock(at=NOW))

    assert all(w.source_reference_key == f"acc-{owner_a.id}" for w in context_a.wins)
    assert all(
        s.source_reference_key == commitment_ids[owner_a.id] for s in context_a.slipped
    )
    assert all(g.title == "goal" for g in context_a.okr_progress)
    assert len(context_a.wins) == 1
    assert len(context_a.slipped) == 1
    assert len(context_a.okr_progress) == 1
