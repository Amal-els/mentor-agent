"""ADK CaptureOutputAgent — first step of the post-meeting SequentialAgent
(design spec §4.4a). Never fabricates meeting content: falls back to
asking the user directly when fetch_transcript finds nothing."""

from pathlib import Path
from typing import Literal

from google.adk.agents import Agent
from google.genai import types as genai_types
from pydantic import BaseModel

MODEL = "gemini-3.5-flash-lite"
PROMPT_ID = "agenda_capture.v1"
_PROMPT_PATH = Path(__file__).resolve().parents[4] / "prompts" / "agenda_capture.v1.md"


def _load_transcript(meeting_id: str) -> str | None:
    """Real transcript lookup — placeholder for wherever this codebase's
    meeting-transcript source actually lives (out of scope for this plan
    per spec §10: this task wires the tool contract, not a new transcript
    ingestion pipeline). Returns None when no transcript exists."""
    return None


def fetch_transcript(meeting_id: str) -> str | None:
    """Fetches the transcript for a meeting, if one exists.

    Args:
        meeting_id: The calendar event id for the 1-on-1.

    Returns:
        The transcript text, or None if no transcript exists yet.
    """
    return _load_transcript(meeting_id)


def _build_fetch_transcript_tool(transcript_text: str | None):
    """Factory variant of fetch_transcript, for callers that already have
    real transcript content in hand for this specific call (currently:
    an explicit transcript_text on the meeting_end trigger payload — see
    run_post_meeting_flow). _load_transcript has no real backing store
    (see its own docstring — out of scope for this plan), so without
    this every real run's Capture step gets nothing and falls back to
    "ask the user directly," which can't actually work in a headless
    run (confirmed live: produces zero agenda items). Attached to a
    per-call clone of capture_agent, the same way _build_items_json_
    callback attaches a per-call before_agent_callback to phrase_agent's
    clone — never shared across calls."""

    def fetch_transcript(meeting_id: str) -> str | None:
        """Fetches the transcript for a meeting, if one exists.

        Args:
            meeting_id: The calendar event id for the 1-on-1.

        Returns:
            The transcript text, or None if no transcript exists yet.
        """
        if transcript_text is not None:
            return transcript_text
        return _load_transcript(meeting_id)

    return fetch_transcript


class CaptureOutput(BaseModel):
    transcript_text: str
    source: Literal["transcript", "prompted"]


capture_agent = Agent(
    name="agenda_capture",
    model=MODEL,
    instruction=_PROMPT_PATH.read_text(encoding="utf-8"),
    tools=[fetch_transcript],
    output_schema=CaptureOutput,
    output_key="capture_result",
    generate_content_config=genai_types.GenerateContentConfig(temperature=0),
)
