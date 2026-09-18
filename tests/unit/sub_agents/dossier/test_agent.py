import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import DossierDelivery, User
from app.core.scope import OwnerScope
from app.salience.dossier.gate import DOSSIER_PUSH_BUDGET_MAX_PER_DAY
from app.sub_agents.dossier.agent import run_dossier_flow
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
        return {"sent": True, "ts": "abc"}


def _fake_run_agent_sync(agent, initial_state, kickoff_text="Begin."):
    # Real run_dossier_flow builds a real Agent internally (Task 8's fix
    # for the brief's llm_agent=None bug — see app/sub_agents/dossier/
    # agent.py's module docstring) and calls synthesize_dossier(context,
    # agent), which calls run_agent_sync via a *local* import inside
    # synthesize_dossier — `from app.core.adk_runner import
    # run_agent_sync` — so patching must target the source module,
    # app.core.adk_runner.run_agent_sync, not any of the call sites that
    # import it locally (they re-resolve the name at call time). This
    # mirrors Task 6's own tests (tests/unit/sub_agents/dossier/
    # sub_agents/synthesize/test_agent.py).
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


def test_manager_meeting_always_ships_a_dossier(pg_session, monkeypatch):
    monkeypatch.setattr("app.core.adk_runner.run_agent_sync", _fake_run_agent_sync)

    # A random, per-run slack_user_id, not a static "U1": pg_session (tests/
    # unit/conftest.py) has no transaction-rollback teardown, so committed
    # rows persist across test runs against the same DB and a static value
    # collides with uq_users_slack_user_id on any rerun. Matches Task 7's
    # own test precedent (tests/unit/sub_agents/dossier/sub_agents/deliver/
    # test_agent.py).
    slack_user_id = f"U{uuid.uuid4().hex[:8]}"
    user = User(id=str(uuid.uuid4()), created_at=NOW, slack_user_id=slack_user_id)
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)

    event = {
        "external_id": f"evt-mgr-{uuid.uuid4().hex[:8]}",
        "title": "1:1 with manager",
        "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
        "is_recurring": False,
        "attendees": [],
        "is_manager": True,
    }
    connectors = {"slack": _FixtureClient(), "linear": _FixtureClient()}
    deliverer = _FakeDeliverer()

    delivery = run_dossier_flow(event, scope, clock, connectors, deliverer=deliverer)

    assert delivery is not None
    assert len(deliverer.sent) == 1


def test_real_is_manager_matches_current_pairs_manager(pg_session):
    # Finding 2's second half: event.get("is_manager", False) is never set
    # by any real calendar adapter. _real_is_manager derives a real signal
    # instead, off this owner's actual current Pair (app.agenda.scope) and
    # a resolved attendee's person_id -- unit-tested directly here rather
    # than through full identity resolution (roster/matcher setup), since
    # that machinery isn't what this fix touches.
    from app.agenda.models import Pair
    from app.identity.types import Resolved, Unattributed
    from app.sub_agents.dossier.agent import _real_is_manager
    from app.sub_agents.dossier.sub_agents.gather.agent import (
        DossierContext,
        ResolvedAttendee,
    )

    report = User(id=str(uuid.uuid4()), created_at=NOW)
    manager = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add_all([report, manager])
    pg_session.commit()
    pair = Pair(
        id=str(uuid.uuid4()),
        report_user_id=report.id,
        manager_user_id=manager.id,
        started_at=NOW,
        ended_at=None,
    )
    pg_session.add(pair)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=report.id, session=pg_session)

    context_with_manager = DossierContext(
        event={},
        resolved_attendees=[
            ResolvedAttendee(
                raw={},
                resolution=Resolved(
                    person_id=manager.id, tier=1, confidence="verified"
                ),
            )
        ],
        agenda_carryover=None,
        raw_signals={},
        open_commitments=[],
        blocked_work_items=[],
    )
    assert _real_is_manager(context_with_manager, scope) is True

    context_without_manager = DossierContext(
        event={},
        resolved_attendees=[
            ResolvedAttendee(
                raw={},
                resolution=Unattributed(reference_key="x", raw_handle=None),
            )
        ],
        agenda_carryover=None,
        raw_signals={},
        open_commitments=[],
        blocked_work_items=[],
    )
    assert _real_is_manager(context_without_manager, scope) is False


def _make_owner(pg_session):
    slack_user_id = f"U{uuid.uuid4().hex[:8]}"
    user = User(id=str(uuid.uuid4()), created_at=NOW, slack_user_id=slack_user_id)
    pg_session.add(user)
    pg_session.commit()
    return OwnerScope(owner_user_id=user.id, session=pg_session)


def _seed_delivery(scope, *, candidate_score=None, sent_at=None):
    delivery = DossierDelivery(
        id=str(uuid.uuid4()),
        owner_user_id=scope.owner_user_id,
        event_external_id=f"evt-seed-{uuid.uuid4().hex[:8]}",
        sent_at=sent_at,
        prompt_version="dossier_synthesize@v1",
        talking_points_source="fresh",
        card_ref=None,
        feedback="none",
        feedback_at=None,
        created_at=NOW,
        who_summary="Seed",
        why_now="Seed",
        candidate_score=candidate_score,
    )
    scope.session.add(delivery)
    scope.session.commit()
    return delivery


def test_gate_drops_low_scoring_event_once_real_history_exists(pg_session, monkeypatch):
    # Finding 2: event["history_scores"] is never set by any real calendar
    # adapter, so the gate previously fail-opened (pushed) unconditionally
    # for every non-manager event. run_dossier_flow must now derive real
    # history from this owner's past DossierDelivery.candidate_score rows,
    # so a genuinely low-scoring new candidate gets dropped once a real,
    # high-scoring history exists for this owner.
    monkeypatch.setattr("app.core.adk_runner.run_agent_sync", _fake_run_agent_sync)
    scope = _make_owner(pg_session)
    clock = FrozenClock(at=NOW)

    for _ in range(5):
        _seed_delivery(scope, candidate_score=5.0)

    event = {
        "external_id": f"evt-lowscore-{uuid.uuid4().hex[:8]}",
        "title": "Low-signal sync",
        "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
        "is_recurring": False,
        "attendees": [],
        "attendee_rarity": 0.0,
        "is_external": False,
        "deadline_proximity": 0.0,
    }
    connectors = {"slack": _FixtureClient(), "linear": _FixtureClient()}
    deliverer = _FakeDeliverer()

    delivery = run_dossier_flow(event, scope, clock, connectors, deliverer=deliverer)

    assert delivery is None
    assert len(deliverer.sent) == 0


def test_gate_still_pushes_on_cold_start_with_no_history(pg_session, monkeypatch):
    # The flip side of the above: an owner with NO real history yet (a new
    # owner, or one who has never had a push) must still get the
    # permissive cold-start behavior _is_top_percentile's own docstring
    # promises -- this fix must not turn "no history" into "always drop."
    monkeypatch.setattr("app.core.adk_runner.run_agent_sync", _fake_run_agent_sync)
    scope = _make_owner(pg_session)
    clock = FrozenClock(at=NOW)

    event = {
        "external_id": f"evt-coldstart-{uuid.uuid4().hex[:8]}",
        "title": "Low-signal sync",
        "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
        "is_recurring": False,
        "attendees": [],
        "attendee_rarity": 0.0,
        "is_external": False,
        "deadline_proximity": 0.0,
    }
    connectors = {"slack": _FixtureClient(), "linear": _FixtureClient()}
    deliverer = _FakeDeliverer()

    delivery = run_dossier_flow(event, scope, clock, connectors, deliverer=deliverer)

    assert delivery is not None
    assert len(deliverer.sent) == 1


def test_push_budget_caps_pushes_per_day(pg_session, monkeypatch):
    # Finding 3: DOSSIER_PUSH_BUDGET_MAX_PER_DAY was declared but never
    # enforced anywhere. Once an owner has DOSSIER_PUSH_BUDGET_MAX_PER_DAY
    # already-sent DossierDelivery rows today, the next event that would
    # otherwise pass the gate must be skipped (treated like "queue" --
    # run_dossier_flow returns None, same contract as a drop) instead of
    # being pushed unconditionally.
    monkeypatch.setattr("app.core.adk_runner.run_agent_sync", _fake_run_agent_sync)
    scope = _make_owner(pg_session)
    clock = FrozenClock(at=NOW)

    # candidate_score=None so these don't feed history_scores (fail-open
    # cold start for the gate itself) -- isolates the budget check as the
    # only thing blocking the next push.
    for _ in range(DOSSIER_PUSH_BUDGET_MAX_PER_DAY):
        _seed_delivery(scope, candidate_score=None, sent_at=NOW)

    event = {
        "external_id": f"evt-overbudget-{uuid.uuid4().hex[:8]}",
        "title": "1:1 with manager",
        "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
        "is_recurring": False,
        "attendees": [],
        "is_manager": True,
    }
    connectors = {"slack": _FixtureClient(), "linear": _FixtureClient()}
    deliverer = _FakeDeliverer()

    delivery = run_dossier_flow(event, scope, clock, connectors, deliverer=deliverer)

    assert delivery is None
    assert len(deliverer.sent) == 0


def test_push_budget_allows_pushes_under_the_cap(pg_session, monkeypatch):
    monkeypatch.setattr("app.core.adk_runner.run_agent_sync", _fake_run_agent_sync)
    scope = _make_owner(pg_session)
    clock = FrozenClock(at=NOW)

    for _ in range(DOSSIER_PUSH_BUDGET_MAX_PER_DAY - 1):
        _seed_delivery(scope, candidate_score=None, sent_at=NOW)

    event = {
        "external_id": f"evt-underbudget-{uuid.uuid4().hex[:8]}",
        "title": "1:1 with manager",
        "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
        "is_recurring": False,
        "attendees": [],
        "is_manager": True,
    }
    connectors = {"slack": _FixtureClient(), "linear": _FixtureClient()}
    deliverer = _FakeDeliverer()

    delivery = run_dossier_flow(event, scope, clock, connectors, deliverer=deliverer)

    assert delivery is not None
    assert len(deliverer.sent) == 1
