from app.sub_agents.checklist.agent import (
    ChecklistResponseOutput,
    _fallback_response,
    generate_supportive_response,
)


def test_generate_supportive_response_uses_llm_result_when_call_succeeds(monkeypatch):
    def fake_run_agent_sync(agent, initial_state, **kwargs):
        assert initial_state == {
            "item_title": "Fix flaky roster test",
            "done_count": 2,
            "total_count": 5,
        }
        return {"checklist_response_result": {"message": "Great work on that one."}}

    monkeypatch.setattr(
        "app.sub_agents.checklist.agent.run_agent_sync", fake_run_agent_sync
    )

    result = generate_supportive_response("Fix flaky roster test", 2, 5)

    assert result == "Great work on that one."


def test_generate_supportive_response_falls_back_on_llm_failure(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("no credentials")

    monkeypatch.setattr("app.sub_agents.checklist.agent.run_agent_sync", _boom)

    result = generate_supportive_response("Fix flaky roster test", 2, 5)

    assert "Fix flaky roster test" in result
    assert "2" in result and "5" in result


def test_generate_supportive_response_falls_back_on_empty_llm_message(monkeypatch):
    def fake_run_agent_sync(agent, initial_state, **kwargs):
        return {"checklist_response_result": {"message": "   "}}

    monkeypatch.setattr(
        "app.sub_agents.checklist.agent.run_agent_sync", fake_run_agent_sync
    )

    result = generate_supportive_response("Fix flaky roster test", 1, 1)

    assert result  # non-empty deterministic fallback, not the blank LLM output


def test_fallback_response_notes_when_list_is_fully_cleared():
    result = _fallback_response("Last item", done_count=3, total_count=3)

    assert "Last item" in result
    assert "clear" in result.lower()


def test_fallback_response_is_deterministic_for_same_inputs():
    a = _fallback_response("Same item", done_count=1, total_count=4)
    b = _fallback_response("Same item", done_count=1, total_count=4)

    assert a == b


def test_checklist_response_output_requires_message_field():
    parsed = ChecklistResponseOutput.model_validate({"message": "ok"})
    assert parsed.message == "ok"
