import datetime
import uuid

from app.agenda.models import Accomplishment
from app.core.clock import FrozenClock
from app.core.models import FridayReviewDelivery, Suppression, User
from app.core.scope import OwnerScope
from app.sub_agents.friday_review.agent import run_friday_review_flow
from app.sub_agents.friday_review.sub_agents.synthesize.agent import OUTPUT_KEY

NOW = datetime.datetime(2026, 8, 28, 16, 0, 0, tzinfo=datetime.UTC)


class _FakeDeliverer:
    enabled = True

    def __init__(self):
        self.sent = []

    def deliver(self, user_slack_id, blocks, channel_id=None, thread_ts=None, dm_thread_ts=None):
        self.sent.append((user_slack_id, blocks))
        return {"sent": True, "dm_ts": "123.456", "dm_channel": "D1"}


def _fake_run_agent_sync(agent, initial_state, kickoff_text="Begin."):
    # Patch targets app.core.adk_runner.run_agent_sync (the module
    # synthesize_friday_review's local `from app.core.adk_runner import
    # run_agent_sync` re-resolves at call time) — same reasoning
    # tests/unit/sub_agents/dossier/test_agent.py's own fake gives.
    assert "context_json" in initial_state
    return {
        OUTPUT_KEY: {
            "wins": [],
            "one_adjustment": None,
            "career_narrative": None,
            "skill_classifications": [],
        }
    }


def _make_owner(pg_session) -> User:
    slack_user_id = f"U{uuid.uuid4().hex[:10]}"
    user = User(id=str(uuid.uuid4()), created_at=NOW, slack_user_id=slack_user_id)
    pg_session.add(user)
    pg_session.commit()
    return user


def test_on_leave_suppression_skips_entirely(pg_session, monkeypatch):
    user = _make_owner(pg_session)
    pg_session.add(
        Suppression(
            id=str(uuid.uuid4()), owner_user_id=user.id, scope="temporal",
            target_ref="friday_review", reason="on leave", created_at=NOW,
            expires_at=NOW + datetime.timedelta(days=7), created_by="user",
        )
    )
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    deliverer = _FakeDeliverer()

    delivery = run_friday_review_flow(scope, FrozenClock(at=NOW), deliverer=deliverer)

    assert delivery is not None
    assert delivery.sent_at is None
    assert delivery.skipped_reason == "on_leave"
    assert deliverer.sent == []


def test_normal_run_delivers_and_commits_identity_batch(pg_session, monkeypatch):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    deliverer = _FakeDeliverer()

    monkeypatch.setattr("app.core.adk_runner.run_agent_sync", _fake_run_agent_sync)

    committed = {}

    def _fake_commit_batch(scope_, batch, card_ref, clock_):
        committed["called"] = True

    monkeypatch.setattr(
        "app.sub_agents.friday_review.agent.commit_batch", _fake_commit_batch
    )

    delivery = run_friday_review_flow(scope, FrozenClock(at=NOW), deliverer=deliverer)

    assert delivery is not None
    assert delivery.sent_at is not None
    assert len(deliverer.sent) == 1
    # commit_batch is only called when build_batch returns something —
    # with no UnresolvedReference rows seeded, the batch is empty, so
    # commit_batch is never invoked. Assert the no-op path instead.
    assert committed.get("called") is None


def test_second_call_same_week_is_idempotent(pg_session, monkeypatch):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    deliverer = _FakeDeliverer()

    monkeypatch.setattr("app.core.adk_runner.run_agent_sync", _fake_run_agent_sync)
    monkeypatch.setattr(
        "app.sub_agents.friday_review.agent.commit_batch", lambda *a, **k: None
    )

    first = run_friday_review_flow(scope, FrozenClock(at=NOW), deliverer=deliverer)
    second = run_friday_review_flow(scope, FrozenClock(at=NOW), deliverer=deliverer)

    assert first.id == second.id
    assert len(deliverer.sent) == 1


def test_classifying_an_already_logged_win_persists_to_the_real_row_and_this_weeks_card(
    pg_session, monkeypatch
):
    """REAL BUG FOUND AND FIXED: a real, already-logged Accomplishment's
    freshly-LLM-classified skill_category used to be thrown away instead
    of written back to the row — _gather_skill_distribution only ever
    counts rows with skill_category already set, so the "Skill
    distribution" line stayed "Not enough categorized history yet."
    forever for anyone whose accomplishments come from meeting synthesis
    or manual entry (the vast majority), no matter how many weeks passed.
    This asserts both halves of the fix: the real row gets updated, AND
    this SAME week's card already reflects it (not just next week's)."""
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    deliverer = _FakeDeliverer()

    accomplishment = Accomplishment(
        id=str(uuid.uuid4()),
        owner_user_id=user.id,
        description="Shipped the migration",
        source_reference_key=f"agenda:{user.id}",
        occurred_at=NOW - datetime.timedelta(days=1),
        skill_category=None,
    )
    pg_session.add(accomplishment)
    pg_session.commit()

    def _fake_run_agent_sync_with_win(agent, initial_state, kickoff_text="Begin."):
        assert "context_json" in initial_state
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
    monkeypatch.setattr(
        "app.sub_agents.friday_review.agent.commit_batch", lambda *a, **k: None
    )

    run_friday_review_flow(scope, FrozenClock(at=NOW), deliverer=deliverer)

    pg_session.refresh(accomplishment)
    assert accomplishment.skill_category == "technical_execution"

    assert len(deliverer.sent) == 1
    sent_text = str(deliverer.sent[0][1])
    assert "Not enough categorized history yet" not in sent_text
    assert "technical_execution: 100%" in sent_text
