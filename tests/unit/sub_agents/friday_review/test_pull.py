import datetime
import uuid

from app.agenda.models import Accomplishment
from app.core.clock import FrozenClock
from app.core.models import User
from app.core.scope import OwnerScope
from app.sub_agents.friday_review.pull import pull_friday_review
from app.sub_agents.friday_review.sub_agents.synthesize.agent import OUTPUT_KEY

TUESDAY = datetime.datetime(2026, 8, 25, 10, 0, 0, tzinfo=datetime.UTC)


class _FakeDeliverer:
    enabled = True

    def __init__(self):
        self.sent = []

    def deliver(self, user_slack_id, blocks, channel_id=None, thread_ts=None, dm_thread_ts=None):
        self.sent.append((user_slack_id, blocks))
        return {"sent": True, "dm_ts": "abc", "dm_channel": "D1"}


def _fake_run_agent_sync(agent, initial_state, kickoff_text="Begin."):
    return {
        OUTPUT_KEY: {
            "wins": [],
            "one_adjustment": None,
            "career_narrative": None,
            "skill_classifications": [],
        }
    }


def test_pull_runs_on_a_non_friday_and_uses_pull_trigger(pg_session, monkeypatch):
    slack_user_id = f"U{uuid.uuid4().hex[:10]}"
    user = User(id=str(uuid.uuid4()), created_at=TUESDAY, slack_user_id=slack_user_id)
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    deliverer = _FakeDeliverer()

    monkeypatch.setattr("app.core.adk_runner.run_agent_sync", _fake_run_agent_sync)

    delivery = pull_friday_review(scope, FrozenClock(at=TUESDAY), deliverer=deliverer)

    assert delivery.trigger == "pull"
    assert delivery.sent_at is not None
    assert len(deliverer.sent) == 1


def test_pull_also_persists_skill_classifications_not_just_the_scheduled_flow(
    pg_session, monkeypatch
):
    """REAL BUG FOUND AND FIXED: pull_friday_review is a separate
    gather->synthesize->deliver sequence from run_friday_review_flow (see
    pull.py's own docstring on why) — the skill-classification
    persistence fix originally landed only in the latter, so anyone
    testing via pull (the natural "give me a fresh one now" path) would
    still see "not enough categorized history yet" after that fix."""
    slack_user_id = f"U{uuid.uuid4().hex[:10]}"
    user = User(id=str(uuid.uuid4()), created_at=TUESDAY, slack_user_id=slack_user_id)
    pg_session.add(user)
    pg_session.commit()
    accomplishment = Accomplishment(
        id=str(uuid.uuid4()),
        owner_user_id=user.id,
        description="Shipped the migration",
        source_reference_key=f"agenda:{user.id}",
        occurred_at=TUESDAY - datetime.timedelta(days=1),
        skill_category=None,
    )
    pg_session.add(accomplishment)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    deliverer = _FakeDeliverer()

    def _fake_run_agent_sync_with_win(agent, initial_state, kickoff_text="Begin."):
        return {
            OUTPUT_KEY: {
                "wins": [
                    {
                        "source_reference_key": f"agenda:{user.id}",
                        "phrased_text": "Shipped the migration",
                        "moved_goal_title": None,
                    }
                ],
                "one_adjustment": None,
                "career_narrative": None,
                "skill_classifications": [
                    {
                        "source_reference_key": f"agenda:{user.id}",
                        "skill_category": "technical_execution",
                    }
                ],
            }
        }

    monkeypatch.setattr(
        "app.core.adk_runner.run_agent_sync", _fake_run_agent_sync_with_win
    )

    pull_friday_review(scope, FrozenClock(at=TUESDAY), deliverer=deliverer)

    pg_session.refresh(accomplishment)
    assert accomplishment.skill_category == "technical_execution"

    sent_text = str(deliverer.sent[0][1])
    assert "Not enough categorized history yet" not in sent_text
    assert "technical_execution: 100%" in sent_text
