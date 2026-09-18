"""Gather -> synthesize (LLM stubbed via run_agent_sync monkeypatch,
matching every other sub-agent's own test convention) -> deliver -> a
real Confirm & log write-back, exercised as one flow."""
import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import FridayReviewDelivery, User, WorkItem
from app.core.scope import OwnerScope
from app.sub_agents.friday_review.agent import run_friday_review_flow
from app.sub_agents.friday_review.sub_agents.deliver.agent import confirm_and_log_ledger_items
from app.sub_agents.friday_review.sub_agents.synthesize.agent import OUTPUT_KEY

NOW = datetime.datetime(2026, 8, 28, 16, 0, 0, tzinfo=datetime.UTC)


class _FakeDeliverer:
    enabled = True

    def __init__(self):
        self.sent = []

    def deliver(self, user_slack_id, blocks, channel_id=None, thread_ts=None, dm_thread_ts=None):
        self.sent.append(blocks)
        return {"sent": True, "dm_ts": "789.012", "dm_channel": "D9"}


def test_full_flow_from_evidence_to_confirmed_ledger_entry(pg_session, monkeypatch):
    slack_user_id = f"U{uuid.uuid4().hex[:10]}"
    user = User(id=str(uuid.uuid4()), created_at=NOW, slack_user_id=slack_user_id)
    pg_session.add(user)
    work_item_id = str(uuid.uuid4())
    pg_session.add(
        WorkItem(
            id=work_item_id, owner_user_id=user.id, actor_reference_key="me",
            resolved_person_id=None, source="linear", external_id="LIN-1",
            title="Ship the migration", status="Done", url="https://linear.app/x/1",
            due_at=None, updated_at=NOW - datetime.timedelta(days=1),
        )
    )
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)

    def _fake_run_agent_sync(agent, initial_state, kickoff_text="Begin."):
        return {
            OUTPUT_KEY: {
                "wins": [
                    {
                        "source_reference_key": work_item_id,
                        "phrased_text": "Shipped the migration.",
                    }
                ],
                "one_adjustment": None,
                "career_narrative": None,
                "skill_classifications": [
                    {
                        "source_reference_key": work_item_id,
                        "skill_category": "technical_execution",
                    }
                ],
            }
        }

    monkeypatch.setattr("app.core.adk_runner.run_agent_sync", _fake_run_agent_sync)
    monkeypatch.setattr(
        "app.sub_agents.friday_review.agent.commit_batch", lambda *a, **k: None
    )

    deliverer = _FakeDeliverer()
    delivery = run_friday_review_flow(
        scope, FrozenClock(at=NOW), deliverer=deliverer
    )

    assert delivery.sent_at is not None
    assert delivery.proposed_ledger_items == [
        {
            "description": "Shipped the migration.",
            "source_reference_key": work_item_id,
            "skill_category": "technical_execution",
        }
    ]

    created = confirm_and_log_ledger_items(scope, FrozenClock(at=NOW), delivery.id)
    assert len(created) == 1
    assert created[0].source_reference_key == work_item_id
    assert created[0].skill_category == "technical_execution"

    refreshed = pg_session.get(FridayReviewDelivery, delivery.id)
    assert refreshed.ledger_confirmed_at is not None
