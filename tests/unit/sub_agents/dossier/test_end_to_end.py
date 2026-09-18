"""Task 12: full end-to-end pipeline test -- event in -> DossierDelivery
row written + delivered out, through run_dossier_flow (Task 8's
orchestrator), with no live LLM call (app.core.adk_runner.run_agent_sync
patched, per Task 8's own end-to-end test:
tests/unit/sub_agents/dossier/test_agent.py)."""

import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import DossierDelivery, User
from app.core.scope import OwnerScope
from app.sub_agents.dossier.agent import run_dossier_flow
from app.sub_agents.dossier.sub_agents.synthesize.agent import OUTPUT_KEY

NOW = datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC)


class _EmptyClient:
    def fetch(self, window, owner_user_id):
        return []


class _FakeDeliverer:
    enabled = True

    def __init__(self):
        self.sent = []

    def deliver(
        self, user_slack_id, blocks, channel_id=None, thread_ts=None, dm_thread_ts=None
    ):
        self.sent.append((user_slack_id, blocks))
        return {"sent": True, "ts": "e2e"}


def _fake_run_agent_sync(agent, initial_state, kickoff_text="Begin."):
    # run_dossier_flow builds a real Agent internally (no llm_agent param)
    # and calls synthesize_dossier(context, agent), which resolves
    # run_agent_sync via a local import -- patching must target the
    # source module app.core.adk_runner.run_agent_sync, matching Task 8's
    # own test (tests/unit/sub_agents/dossier/test_agent.py).
    assert "context_json" in initial_state
    return {
        OUTPUT_KEY: {
            "who": ["Manager"],
            "why_now": "Weekly 1:1.",
            "talking_points": [
                {"text": "Discuss roadmap", "source_link": "https://linear.app/x/1"},
            ],
            "promised_and_not_delivered": [],
            "suggested_opener": None,
            "short_version": False,
        }
    }


def test_full_pipeline_manager_meeting_writes_and_delivers(pg_session, monkeypatch):
    monkeypatch.setattr("app.core.adk_runner.run_agent_sync", _fake_run_agent_sync)

    slack_user_id = f"U{uuid.uuid4().hex[:8]}"
    user = User(id=str(uuid.uuid4()), created_at=NOW, slack_user_id=slack_user_id)
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)

    event = {
        "external_id": f"evt-e2e-{uuid.uuid4().hex[:8]}",
        "title": "1:1 with manager",
        "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
        "is_recurring": False,
        "attendees": [],
        "is_manager": True,
    }
    connectors = {"slack": _EmptyClient(), "linear": _EmptyClient()}
    deliverer = _FakeDeliverer()

    delivery = run_dossier_flow(event, scope, clock, connectors, deliverer=deliverer)

    assert delivery is not None
    row = pg_session.get(DossierDelivery, delivery.id)
    assert row is not None
    assert row.sent_at is not None
    assert len(deliverer.sent) == 1
