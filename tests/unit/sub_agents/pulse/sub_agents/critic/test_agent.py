import json

from app.sub_agents.pulse.sub_agents.critic.agent import (
    CriticOutput,
    _prepare_critic_input,
    critic_agent,
    run_critic_agent,
)

_DRAFT = {
    "prompt_id": "pulse_writer.v1",
    "temperature": 0,
    "degradation_line": None,
    "items": [
        {
            "item_id": "evt-1",
            "title": "1:1 with Sarah",
            "why_now": "Sarah is waiting",
            "action": "Prep notes",
        }
    ],
}
_CONTEXT = {
    "shortlist": [
        {
            "item_id": "evt-1",
            "score": 8.5,
            "score_terms": {"person_waiting": "Sarah"},
            "candidate_focus": True,
        }
    ],
    "degraded_sources": [],
}


def test_run_critic_agent_seeds_state_with_draft_and_context_as_json(monkeypatch):
    captured = {}

    def fake_run_agent_sync(agent, initial_state, **kwargs):
        captured["agent"] = agent
        captured["state"] = initial_state
        return {"critic_result": {"verdict": "pass", "reasons": []}}

    monkeypatch.setattr(
        "app.sub_agents.pulse.sub_agents.critic.agent.run_agent_sync",
        fake_run_agent_sync,
    )

    result = run_critic_agent(_DRAFT, _CONTEXT)

    assert captured["agent"] is critic_agent
    assert captured["state"]["critic_draft_json"]
    assert captured["state"]["critic_context_json"]
    assert isinstance(result, CriticOutput)
    assert result.verdict == "pass"
    assert result.reasons == []


def test_run_critic_agent_parses_revise_verdict_with_reasons(monkeypatch):
    def fake_run_agent_sync(agent, initial_state, **kwargs):
        return {
            "critic_result": {
                "verdict": "revise",
                "reasons": [
                    {
                        "item_id": "evt-1",
                        "reason": "why_now names 'Bob', not this item's person_waiting",
                        "scope": "writing",
                    }
                ],
            }
        }

    monkeypatch.setattr(
        "app.sub_agents.pulse.sub_agents.critic.agent.run_agent_sync",
        fake_run_agent_sync,
    )

    result = run_critic_agent(_DRAFT, _CONTEXT)

    assert result.verdict == "revise"
    assert result.reasons[0].item_id == "evt-1"
    assert result.reasons[0].scope == "writing"


def test_run_critic_agent_raises_on_failure(monkeypatch):
    def _boom(agent, initial_state, **kwargs):
        raise RuntimeError("malformed response")

    monkeypatch.setattr(
        "app.sub_agents.pulse.sub_agents.critic.agent.run_agent_sync", _boom
    )

    try:
        run_critic_agent(_DRAFT, _CONTEXT)
        raised = False
    except RuntimeError:
        raised = True

    assert raised is True


class _FakeCallbackContext:
    def __init__(self, state):
        self.state = state


def test_prepare_critic_input_is_a_noop_for_standalone_callers():
    state = {"critic_draft_json": json.dumps(_DRAFT)}

    _prepare_critic_input(_FakeCallbackContext(state))

    assert state["critic_draft_json"] == json.dumps(_DRAFT)


def test_prepare_critic_input_builds_draft_from_writer_result():
    state = {
        "writer_result": {
            "items": [
                {
                    "item_id": "evt-1",
                    "title": "1:1 with Sarah",
                    "why_now": "Sarah is waiting",
                    "action": "Prep notes",
                }
            ]
        },
        "degradation_line": "no calendar — briefing on what else exists",
    }

    _prepare_critic_input(_FakeCallbackContext(state))

    built = json.loads(state["critic_draft_json"])
    assert built["degradation_line"] == "no calendar — briefing on what else exists"
    assert built["items"][0]["item_id"] == "evt-1"


def test_prepare_critic_input_rebuilds_on_a_second_loop_iteration():
    """Same staleness bug writer_agent's callback has to guard against:
    state persists across LoopAgent iterations, so a leftover
    critic_draft_json from iteration 1 must not survive unrebuilt once a
    fresh writer_result lands for iteration 2."""
    state = {
        "critic_draft_json": json.dumps({"items": [{"item_id": "stale"}]}),
        "writer_result": {
            "items": [
                {
                    "item_id": "evt-1",
                    "title": "T",
                    "why_now": "W",
                    "action": "A",
                }
            ]
        },
        "degradation_line": None,
    }

    _prepare_critic_input(_FakeCallbackContext(state))

    built = json.loads(state["critic_draft_json"])
    assert [item["item_id"] for item in built["items"]] == ["evt-1"]
