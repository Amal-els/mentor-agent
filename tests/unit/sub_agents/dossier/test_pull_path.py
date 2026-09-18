import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import User
from app.core.scope import OwnerScope
from app.sub_agents.dossier.pull import pull_dossier
from app.sub_agents.dossier.sub_agents.synthesize.agent import OUTPUT_KEY

NOW = datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC)


class _FixtureClient:
    def fetch(self, window, owner_user_id):
        return []


class _FakeDeliverer:
    enabled = True

    def __init__(self):
        self.sent = []

    def deliver(self, user_slack_id, blocks, channel_id=None, thread_ts=None, dm_thread_ts=None):
        self.sent.append((user_slack_id, blocks))
        return {"sent": True, "ts": "pulled"}


def _fake_run_agent_sync(agent, initial_state, kickoff_text="Begin."):
    # Real pull_dossier builds a real Agent internally (the same
    # llm_agent=None fix Task 8 already made in run_dossier_flow — see
    # app/sub_agents/dossier/pull.py's module docstring) and calls
    # synthesize_dossier(context, agent), which calls run_agent_sync via a
    # *local* import inside synthesize_dossier — `from app.core.adk_runner
    # import run_agent_sync` — so patching must target the source module,
    # app.core.adk_runner.run_agent_sync, not any call site that imports it
    # locally (those re-resolve the name at call time). Mirrors Task 8's
    # own fixed test (tests/unit/sub_agents/dossier/test_agent.py).
    assert "context_json" in initial_state
    return {
        OUTPUT_KEY: {
            "who": ["Team"],
            "why_now": "Weekly standup.",
            "talking_points": [
                {"text": "Check on yesterday's blockers", "source_link": "https://linear.app/x/1"},
            ],
            "promised_and_not_delivered": [],
            "suggested_opener": None,
            "short_version": False,
        }
    }


def test_pull_bypasses_the_gate_for_a_low_score_event(pg_session, monkeypatch):
    monkeypatch.setattr("app.core.adk_runner.run_agent_sync", _fake_run_agent_sync)

    # A random, per-run slack_user_id, not a static "U9": pg_session (tests/
    # unit/conftest.py) has no transaction-rollback teardown, so committed
    # rows persist across test runs against the same DB and a static value
    # collides with uq_users_slack_user_id on any rerun. Matches Task 8's
    # own test precedent.
    slack_user_id = f"U{uuid.uuid4().hex[:8]}"
    user = User(id=str(uuid.uuid4()), created_at=NOW, slack_user_id=slack_user_id)
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)

    # A recurring standup, is_manager=False, no urgency signals -- would be
    # dropped by the push gate, but /mentor prep must still return it.
    event = {
        "external_id": f"evt-pull-{uuid.uuid4().hex[:8]}",
        "title": "Standup",
        "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
        "is_recurring": True,
        "attendees": [],
    }
    connectors = {"slack": _FixtureClient(), "linear": _FixtureClient()}
    deliverer = _FakeDeliverer()

    delivery = pull_dossier(event, scope, clock, connectors, deliverer=deliverer)

    assert delivery is not None
    assert len(deliverer.sent) == 1
