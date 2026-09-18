from unittest.mock import patch

from app.sub_agents.agenda.sub_agents.capture.agent import (
    PROMPT_ID,
    CaptureOutput,
    _build_fetch_transcript_tool,
    capture_agent,
    fetch_transcript,
)


def test_fetch_transcript_returns_none_when_missing():
    with patch(
        "app.sub_agents.agenda.sub_agents.capture.agent._load_transcript",
        return_value=None,
    ):
        result = fetch_transcript("meeting-1")

    assert result is None


def test_fetch_transcript_returns_text_when_present():
    with patch(
        "app.sub_agents.agenda.sub_agents.capture.agent._load_transcript",
        return_value="We discussed the roadmap.",
    ):
        result = fetch_transcript("meeting-1")

    assert result == "We discussed the roadmap."


def test_fetch_transcript_passes_meeting_id_through_to_loader():
    with patch(
        "app.sub_agents.agenda.sub_agents.capture.agent._load_transcript",
        return_value="text",
    ) as mock_loader:
        fetch_transcript("meeting-42")

    mock_loader.assert_called_once_with("meeting-42")


def test_build_fetch_transcript_tool_returns_the_given_text_without_calling_loader():
    """Real-world finding: without an explicit transcript, Capture's
    fetch_transcript has no real backing store and the whole pipeline
    silently produces zero agenda items (confirmed live). Callers that
    already have real content (the /ui demo dashboard's sample
    transcripts) must be able to supply it directly."""
    with patch(
        "app.sub_agents.agenda.sub_agents.capture.agent._load_transcript",
    ) as mock_loader:
        tool = _build_fetch_transcript_tool("We discussed the roadmap.")
        result = tool("meeting-1")

    assert result == "We discussed the roadmap."
    mock_loader.assert_not_called()


def test_build_fetch_transcript_tool_falls_back_to_loader_when_none_given():
    with patch(
        "app.sub_agents.agenda.sub_agents.capture.agent._load_transcript",
        return_value="loaded text",
    ):
        tool = _build_fetch_transcript_tool(None)
        result = tool("meeting-1")

    assert result == "loaded text"


def test_capture_output_accepts_transcript_source():
    output = CaptureOutput(transcript_text="hello", source="transcript")

    assert output.transcript_text == "hello"
    assert output.source == "transcript"


def test_capture_output_accepts_prompted_source():
    output = CaptureOutput(transcript_text="hello", source="prompted")

    assert output.source == "prompted"


def test_capture_output_rejects_invalid_source():
    try:
        CaptureOutput(transcript_text="hello", source="made_up")
        raised = False
    except Exception:
        raised = True

    assert raised is True


def test_capture_agent_is_wired_with_fetch_transcript_tool():
    assert capture_agent.name == "agenda_capture"
    assert fetch_transcript in capture_agent.tools


def test_capture_agent_uses_structured_output():
    assert capture_agent.output_schema is CaptureOutput
    assert capture_agent.output_key == "capture_result"


def test_capture_agent_runs_at_temperature_zero():
    config = capture_agent.generate_content_config
    assert config.temperature == 0


def test_prompt_id_is_stable():
    assert PROMPT_ID == "agenda_capture.v1"
