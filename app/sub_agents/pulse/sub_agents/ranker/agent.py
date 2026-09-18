"""ADK ranker sub-agent — pulse_ranker.v1 (docs/plans/morning-pulse.md M4).
A real LlmAgent (structured output, temperature 0), replacing the previous
plain google.genai call. Runs standalone (run_ranker_agent, for isolated/
live testing) or as the first step of pulse_pipeline
(app/sub_agents/pulse/agent.py's SequentialAgent). Either way, its raw
output is never trusted as-is — app/pipeline/pulse.py's enforce_ranker_output
(closed set, hard cut, no dupes) still runs in code afterward; "a rule
check is strictly more reliable than a model's judgment for facts already
in structured data" (this file's docstring precedent, not overturned by
turning ranker itself into a real agent).

fallback_rank/rank/RankerResult below are the deterministic, non-LLM side
of the ranker — not something the agent calls, but what app/pipeline/
pulse.py falls back to when the agent isn't attempted or fails. Kept in
this file (not app/pipeline/) because both halves are "the ranker," and a
reader asking "what does ranking do" shouldn't have to look in two
places."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from google.adk.agents import Agent
from google.genai import types as genai_types
from pydantic import BaseModel, Field

from app.core.adk_runner import run_agent_sync
from app.salience.pulse.types import ScoredItem

MODEL = "gemini-3.5-flash-lite"
PROMPT_ID = "pulse_ranker.v1"
MAX_FOCUS = 3
_PROMPT_PATH = Path(__file__).resolve().parents[4] / "prompts" / "pulse_ranker.v1.md"


class RankerOutput(BaseModel):
    ordered_item_ids: list[str] = Field(
        description="1 to 3 item_ids, most important first, every one drawn "
        "from the input list."
    )
    rationale: dict[str, str] = Field(
        description="One short line per id in ordered_item_ids, built only "
        "from that item's score_terms."
    )


def _default_previous_critique(callback_context) -> None:
    """Standalone callers (run_ranker_agent) never seed previous_critique_json
    — only pulse_agent's loop does, on a retry — so default it to "no
    feedback yet" rather than let the {previous_critique_json} template
    reference fail on a first/only attempt."""
    callback_context.state.setdefault("previous_critique_json", "[]")


ranker_agent = Agent(
    name="pulse_ranker",
    model=MODEL,
    instruction=_PROMPT_PATH.read_text(encoding="utf-8")
    + "\n\nInput (JSON list of candidates):\n{ranker_input_json}",
    before_agent_callback=_default_previous_critique,
    output_schema=RankerOutput,
    output_key="ranker_result",
    generate_content_config=genai_types.GenerateContentConfig(temperature=0),
)


def _shortlist_view(shortlist: list[ScoredItem]) -> list[dict]:
    return [
        {"item_id": item.item_id, "score": item.score, "score_terms": item.score_terms}
        for item in shortlist
    ]


def run_ranker_agent(shortlist: list[ScoredItem]) -> RankerOutput:
    """Raises on any failure — the caller (llm_rank below, or
    app/sub_agents/pulse/agent.py's pipeline) decides whether to attempt
    this at all and catches failures itself."""
    state = run_agent_sync(
        ranker_agent, {"ranker_input_json": json.dumps(_shortlist_view(shortlist))}
    )
    return RankerOutput.model_validate(state["ranker_result"])


@dataclass(frozen=True)
class RankerResult:
    ordered_item_ids: list[str]
    rationale: dict[str, str]
    prompt_id: str


def _rationale_for(item: ScoredItem) -> str:
    terms = item.score_terms
    bits = []
    if terms.get("overdue"):
        bits.append("overdue")
    elif terms.get("due_today"):
        bits.append("due today")
    person = terms.get("person_waiting")
    if person:
        bits.append(f"{person} waiting")
    return ", ".join(bits) if bits else "lower priority"


def fallback_rank(shortlist: list[ScoredItem]) -> RankerResult:
    """Deterministic score-order + cut to 3, no LLM. This is both the L5
    score-order fallback (pipeline step 6, "ranker fails") and what the
    ranker fixtures (app/fixtures/pulse/ranker/*) test directly. Python's
    sort is stable, so equal scores keep their original shortlist order —
    ties are broken deterministically, never at random.

    Events are never candidates here — every event, focus or not, already
    shows in the card's own Today's meeting load section (sourced
    independently from context.day_events, not from this shortlist), so a
    meeting winning a Focus slot would just be the same information
    twice. This is also what app.pipeline.pulse.enforce_ranker_output
    falls back to when the LLM ranker picks an event id anyway, so this
    exclusion is the actual, unconditional guarantee — not just a
    default a caller could accidentally bypass."""
    candidates = [item for item in shortlist if item.item_type != "event"]
    ordered = sorted(candidates, key=lambda item: item.score, reverse=True)[:MAX_FOCUS]
    return RankerResult(
        ordered_item_ids=[item.item_id for item in ordered],
        rationale={item.item_id: _rationale_for(item) for item in ordered},
        prompt_id=PROMPT_ID,
    )


def rank(
    shortlist: list[ScoredItem],
    context=None,
    model_call: Callable[[list[ScoredItem], object], RankerResult] | None = None,
) -> RankerResult:
    """Attempts the LLM ranker (temperature 0, prompts/pulse_ranker.v1.md via
    model_call) and falls back to fallback_rank on any failure — missing API
    key, malformed response, network error, anything. The fallback firing is
    never silent: app/pipeline/pulse.py is responsible for logging it."""
    if model_call is not None:
        try:
            return model_call(shortlist, context)
        except Exception:
            pass
    return fallback_rank(shortlist)


def llm_rank(shortlist: list[ScoredItem], context=None) -> RankerResult:
    """The real LLM path (temperature 0), via run_ranker_agent above. Raises
    on any failure — the caller (app/pipeline/pulse.py) decides whether to
    attempt this at all (only when app.core.llm.creds_available()) and
    catches failures itself, so a failed call is distinguishable from
    "never attempted"."""
    result = run_ranker_agent(shortlist)
    return RankerResult(
        ordered_item_ids=list(result.ordered_item_ids),
        rationale=dict(result.rationale),
        prompt_id=PROMPT_ID,
    )
