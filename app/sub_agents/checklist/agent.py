"""checklist_response.v1 (app/prompts/checklist_response.v1.md) — the short
supportive line shown when someone checks an item off the /ui morning-pulse
checklist (app/salience/checklist.py). A real LlmAgent, run through the same
sync bridge every other single-shot pulse sub-agent uses (run_agent_sync);
generate_supportive_response is the one entrypoint callers (app/triggers/
checklist_router.py) use — it never raises, falling back to a deterministic
line on any failure (missing credentials, network, malformed output) so a
check-off always gets a response, LLM or not."""

import concurrent.futures
import logging
from pathlib import Path

from google.adk.agents import Agent
from google.genai import types as genai_types
from pydantic import BaseModel, Field

from app.core.adk_runner import run_agent_sync

MODEL = "gemini-3.5-flash-lite"
PROMPT_ID = "checklist_response.v1"
_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "checklist_response.v1.md"

# A checklist check-off is a click, not a page load — it has to feel
# instant. Found live: an occasional slow model round trip left the
# checkbox spinning for several seconds with no bound at all, and the
# rest of the request (build_checklist's own DB round trips) already
# costs 1-2s against a remote Postgres on its own — this budget has to
# stay small enough that the two together don't compound into something
# that reads as broken. Capped here rather than left to whatever the
# SDK's own default is, so a slow call falls back to the deterministic
# line quickly instead of making the click feel stuck.
RESPONSE_TIMEOUT_SECONDS = 2.0

logger = logging.getLogger(__name__)


class ChecklistResponseOutput(BaseModel):
    message: str = Field(description="One short, warm sentence naming the item just finished.")


checklist_response_agent = Agent(
    name="checklist_response",
    model=MODEL,
    instruction=_PROMPT_PATH.read_text(encoding="utf-8")
    + "\n\nJust completed: {item_title}\n"
    "Today's progress: {done_count}/{total_count} items done.",
    output_schema=ChecklistResponseOutput,
    output_key="checklist_response_result",
    generate_content_config=genai_types.GenerateContentConfig(temperature=0.4),
)

# Rotated by done_count, not random — same input (same item, same count)
# always produces the same fallback line within one process lifetime,
# matching every other fallback in this codebase's "deterministic unless
# the LLM path is genuinely available" posture.
_FALLBACK_TEMPLATES = [
    'Done — "{title}" is off your list. {done}/{total} today.',
    'Nice, "{title}" is checked off. That\'s {done} of {total} so far today.',
    'Cleared: "{title}". {done}/{total} today — keep going.',
    'Marked "{title}" done. {done}/{total} today.',
]


def _fallback_response(item_title: str, done_count: int, total_count: int) -> str:
    if total_count > 0 and done_count >= total_count:
        return f'Done — "{item_title}" was the last one. Your checklist is clear for today.'
    template = _FALLBACK_TEMPLATES[done_count % len(_FALLBACK_TEMPLATES)]
    return template.format(title=item_title, done=done_count, total=total_count)


def _call_llm(item_title: str, done_count: int, total_count: int) -> str:
    state = run_agent_sync(
        checklist_response_agent,
        {
            "item_title": item_title,
            "done_count": done_count,
            "total_count": total_count,
        },
    )
    result = ChecklistResponseOutput.model_validate(state["checklist_response_result"])
    message = result.message.strip()
    if not message:
        raise ValueError("empty message from checklist_response_agent")
    return message


def generate_supportive_response(
    item_title: str, done_count: int, total_count: int
) -> str:
    """Never raises, and never blocks past RESPONSE_TIMEOUT_SECONDS —
    callers get a response either way, real LLM copy when credentials/
    network cooperate and answer quickly, the deterministic line above
    otherwise. item_title may be empty (a row whose source went missing)
    — the prompt and fallback both still produce a valid, if generic,
    sentence."""
    # Not a `with ThreadPoolExecutor(...) as executor:` block deliberately
    # — that shuts down with wait=True on exit, which blocks until the
    # submitted call actually finishes even after future.result() has
    # already timed out, defeating the timeout entirely for a genuinely
    # slow call. shutdown(wait=False) lets this function return the
    # fallback immediately; the abandoned thread finishes on its own and
    # its result is simply discarded.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(_call_llm, item_title, done_count, total_count)
        return future.result(timeout=RESPONSE_TIMEOUT_SECONDS)
    except Exception:
        logger.exception(
            "checklist_response: LLM call failed or timed out, using "
            "deterministic fallback"
        )
        return _fallback_response(item_title, done_count, total_count)
    finally:
        executor.shutdown(wait=False)
