import json

import pytest
from google.adk.agents import BaseAgent

from app.salience.pulse.types import ScoredItem
from app.sub_agents.pulse.agent import (
    _MaybeRunRanker,
    _PulseGateStep,
    pulse_agent,
    run_pulse_agent,
)

_SHORTLIST = [
    ScoredItem(
        item_id="evt-1",
        item_type="event",
        score=0.9,
        score_terms={"overdue": True},
        candidate_focus=True,
    ),
]


def _fake_state(verdict="pass", reasons=None):
    return {
        "ranker_result": {
            "ordered_item_ids": ["evt-1"],
            "rationale": {"evt-1": "overdue"},
        },
        "writer_result": {
            "items": [
                {
                    "item_id": "evt-1",
                    "title": "1:1 with Sarah",
                    "why_now": "Overdue",
                    "action": "Deliver today",
                }
            ]
        },
        "critic_result": {"verdict": verdict, "reasons": reasons or []},
    }


def test_run_pulse_agent_seeds_all_the_loop_state_keys(monkeypatch):
    captured = {}

    def fake_run_agent_sync(agent, initial_state, **kwargs):
        captured["agent"] = agent
        captured["state"] = initial_state
        return {**_fake_state(), "pulse_revised": False}

    monkeypatch.setattr("app.sub_agents.pulse.agent.run_agent_sync", fake_run_agent_sync)

    result = run_pulse_agent(
        _SHORTLIST,
        {"evt-1": "1:1 with Sarah"},
        critic_context={"shortlist": [], "degraded_sources": []},
        degradation_line=None,
    )

    assert captured["agent"] is pulse_agent
    state = captured["state"]
    assert json.loads(state["ranker_input_json"]) == [
        {"item_id": "evt-1", "score": 0.9, "score_terms": {"overdue": True}}
    ]
    assert json.loads(state["item_titles_json"]) == {"evt-1": "1:1 with Sarah"}
    assert json.loads(state["item_score_terms_json"]) == {"evt-1": {"overdue": True}}
    assert json.loads(state["critic_context_json"]) == {
        "shortlist": [],
        "degraded_sources": [],
    }
    assert state["degradation_line"] is None
    assert state["previous_critique_json"] == "[]"
    assert state["pulse_loop_iteration"] == 0

    assert result.ranker.ordered_item_ids == ["evt-1"]
    assert result.writer.items[0].item_id == "evt-1"
    assert result.critic_revised is False
    assert result.critic_reasons == []


def test_run_pulse_agent_surfaces_a_revision(monkeypatch):
    reasons = [{"item_id": "evt-1", "reason": "invented name"}]

    def fake_run_agent_sync(agent, initial_state, **kwargs):
        return {
            **_fake_state(),
            "pulse_revised": True,
            "previous_critique_json": json.dumps(reasons),
        }

    monkeypatch.setattr("app.sub_agents.pulse.agent.run_agent_sync", fake_run_agent_sync)

    result = run_pulse_agent(
        _SHORTLIST, {"evt-1": "t"}, critic_context={}, degradation_line=None
    )

    assert result.critic_revised is True
    assert result.critic_reasons == [("evt-1", "invented name")]


def test_run_pulse_agent_raises_on_failure(monkeypatch):
    def _boom(agent, initial_state, **kwargs):
        raise RuntimeError("network error")

    monkeypatch.setattr("app.sub_agents.pulse.agent.run_agent_sync", _boom)

    with pytest.raises(RuntimeError):
        run_pulse_agent(_SHORTLIST, {"evt-1": "t"}, critic_context={}, degradation_line=None)


# --- _PulseGateStep ----------------------------------------------------------


class _FakeSession:
    def __init__(self, state):
        self.state = state


class _FakeContext:
    def __init__(self, state):
        self.session = _FakeSession(state)


async def _run_gate(state):
    step = _PulseGateStep(name="pulse_gate")
    events = [event async for event in step._run_async_impl(_FakeContext(state))]
    assert len(events) == 1
    return events[0]


@pytest.mark.asyncio
async def test_first_iteration_pass_verdict_escalates_with_no_revision():
    state = {"pulse_loop_iteration": 0, "critic_result": {"verdict": "pass", "reasons": []}}

    event = await _run_gate(state)

    assert event.actions.escalate is True
    assert event.actions.state_delta["pulse_revised"] is False
    assert "previous_critique_json" not in event.actions.state_delta


@pytest.mark.asyncio
async def test_first_iteration_revise_verdict_continues_the_loop_with_critique():
    reasons = [{"item_id": "evt-1", "reason": "skipped a blocker", "scope": "ranking"}]
    state = {
        "pulse_loop_iteration": 0,
        "critic_result": {"verdict": "revise", "reasons": reasons},
    }

    event = await _run_gate(state)

    assert event.actions.escalate is False  # loop runs once more
    assert event.actions.state_delta["pulse_revised"] is True
    assert json.loads(event.actions.state_delta["previous_critique_json"]) == reasons


@pytest.mark.asyncio
async def test_ranking_scoped_reason_sets_rerun_ranker_true():
    reasons = [{"item_id": "evt-1", "reason": "skipped a blocker", "scope": "ranking"}]
    state = {
        "pulse_loop_iteration": 0,
        "critic_result": {"verdict": "revise", "reasons": reasons},
    }

    event = await _run_gate(state)

    assert event.actions.state_delta["rerun_ranker"] is True


@pytest.mark.asyncio
async def test_writing_only_reasons_set_rerun_ranker_false():
    reasons = [
        {"item_id": "evt-1", "reason": "invented a name", "scope": "writing"},
        {"item_id": "", "reason": "missing degradation line", "scope": "writing"},
    ]
    state = {
        "pulse_loop_iteration": 0,
        "critic_result": {"verdict": "revise", "reasons": reasons},
    }

    event = await _run_gate(state)

    assert event.actions.state_delta["rerun_ranker"] is False


@pytest.mark.asyncio
async def test_mixed_scopes_set_rerun_ranker_true():
    reasons = [
        {"item_id": "evt-1", "reason": "invented a name", "scope": "writing"},
        {"item_id": "evt-2", "reason": "omitted a blocker", "scope": "ranking"},
    ]
    state = {
        "pulse_loop_iteration": 0,
        "critic_result": {"verdict": "revise", "reasons": reasons},
    }

    event = await _run_gate(state)

    assert event.actions.state_delta["rerun_ranker"] is True


# --- _MaybeRunRanker ---------------------------------------------------------


class _InertChild(BaseAgent):
    """A minimal real BaseAgent (not a duck-typed fake) so _MaybeRunRanker's
    sub_agents=[...] construction goes through ADK's real validation/
    parent-agent wiring, same as the real ranker_agent. Its own run_async is
    monkeypatched per-test below — BaseAgent.run_async needs a full real
    InvocationContext internally that this test has no reason to build."""

    async def _run_async_impl(self, ctx):
        return
        yield  # pragma: no cover — makes this an async generator


@pytest.mark.asyncio
async def test_maybe_run_ranker_skips_when_rerun_ranker_is_false(monkeypatch):
    invoked = []

    async def fake_run_async(self, ctx):
        invoked.append(True)
        return
        yield  # pragma: no cover

    # Pydantic models reject arbitrary instance attributes (no "run_async"
    # field), so this patches the class method instead — plain Python
    # class-dict mutation, not routed through BaseAgent's field validation.
    monkeypatch.setattr(_InertChild, "run_async", fake_run_async)
    child = _InertChild(name="fake_ranker")
    step = _MaybeRunRanker(name="pulse_ranker_step", sub_agents=[child])
    state = {"rerun_ranker": False}

    events = [event async for event in step._run_async_impl(_FakeContext(state))]

    assert events == []
    assert invoked == []


@pytest.mark.asyncio
async def test_maybe_run_ranker_runs_when_rerun_ranker_unset(monkeypatch):
    """Default True — the first-ever pass never has rerun_ranker in state
    yet, and must still run the ranker."""
    invoked = []

    async def fake_run_async(self, ctx):
        invoked.append(True)
        return
        yield  # pragma: no cover

    monkeypatch.setattr(_InertChild, "run_async", fake_run_async)
    child = _InertChild(name="fake_ranker")
    step = _MaybeRunRanker(name="pulse_ranker_step", sub_agents=[child])
    state = {}

    events = [event async for event in step._run_async_impl(_FakeContext(state))]

    assert events == []  # the fake child yields nothing, but it did run
    assert invoked == [True]


@pytest.mark.asyncio
async def test_second_iteration_always_escalates_regardless_of_verdict():
    state = {
        "pulse_loop_iteration": 1,
        "critic_result": {
            "verdict": "revise",
            "reasons": [{"item_id": "evt-1", "reason": "still bad"}],
        },
    }

    event = await _run_gate(state)

    assert event.actions.escalate is True
    assert "previous_critique_json" not in event.actions.state_delta
    assert "pulse_revised" not in event.actions.state_delta
