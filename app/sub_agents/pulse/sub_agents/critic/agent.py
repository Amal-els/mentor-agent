"""ADK critic sub-agent — pulse_critic.v1 (docs/plans/morning-pulse.md M4).
A real LLM-as-judge, fully replacing the previous rule-based evaluate()
(deliberate choice, not a stopgap — see docs/plans/morning-pulse.md's note
on this rebuild for why). pulse_critic.v1.md already specified every check
an LLM judge needs to make (provenance, focus count, why_now presence,
invented names, closed set, degradation line, ranking-vs-priority-signals);
this agent's instruction is that file's content verbatim.

Originally a different model than ranker_agent/writer_agent (gemini-3.6-flash
vs their gemini-2.5-flash) — deliberate: a judge model from the same
family/tier as the model it's judging is a well-known source of
self-evaluation bias, and the whole point of this loop is catching what
the ranker/writer themselves would rubber-stamp. gemini-2.5-flash was
retired for new Gemini API keys, so ranker_agent/writer_agent were moved
to gemini-3.6-flash too (matching critic_agent) — the anti-bias separation
this rationale describes is temporarily not in effect; pick a genuinely
different model/tier for one side if that matters again.

Real sub_agent of app/sub_agents/pulse/agent.py's pulse_agent (a LoopAgent
with ranker_agent, writer_agent, critic_agent, and a code-only gate step) —
_prepare_critic_input below is that loop's before_agent_callback, building
critic_draft_json fresh from writer_result each iteration (an ADK agent can
be both a sub_agent and independently Runner-invoked — ranker_agent/
writer_agent already prove this pattern, see run_ranker_agent/
run_writer_agent). critic_context_json stays static across iterations
(the *input* shortlist never changes mid-loop) so the caller seeds it once,
not per-iteration.

run_critic_agent below is the standalone/test entry point (tests/live/
test_critic_smoke.py's caller) — see app/sub_agents/pulse/sub_agents/
ranker/agent.py's docstring for why it lives alongside the agent rather
than in app/pipeline/. Unlike ranker/writer there is no rule-based
fallback here — a critic failure was always "ship as-is, log it," never a
deterministic retry."""

import json
from pathlib import Path
from typing import Literal

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.genai import types as genai_types
from pydantic import BaseModel, Field

from app.core.adk_runner import run_agent_sync
from app.sub_agents.pulse.sub_agents.writer.agent import PROMPT_ID as WRITER_PROMPT_ID

MODEL = "gemini-3.7-flash"
PROMPT_ID = "pulse_critic.v1"
MAX_FOCUS = 3
_PROMPT_PATH = Path(__file__).resolve().parents[4] / "prompts" / "pulse_critic.v1.md"


class CriticReason(BaseModel):
    item_id: str  # "" for whole-draft issues (focus count, degradation line)
    reason: str
    # Which agent owns fixing this — decides whether pulse_agent's loop
    # re-invokes ranker_agent on retry at all (app/sub_agents/pulse/
    # agent.py's _MaybeRunRanker) or only writer_agent. "ranking": omitted/
    # misordered candidate, wrong focus count, or an id outside the
    # shortlist. "writing": prose grounding (why_now/action) or a missing
    # degradation line.
    scope: Literal["ranking", "writing"]


class CriticOutput(BaseModel):
    verdict: Literal["pass", "revise"]
    reasons: list[CriticReason] = Field(default_factory=list)


def _prepare_critic_input(callback_context: CallbackContext) -> None:
    """Loop-mode only (pulse_agent) — standalone callers (run_critic_agent
    below) already seed critic_draft_json directly and never populate
    writer_result, so this is a no-op for them. Checking writer_result's
    presence rather than "critic_draft_json already built" matters inside
    the loop specifically: state persists across LoopAgent iterations, so
    the latter would find iteration 1's leftover draft on iteration 2 and
    evaluate stale prose instead of the writer's fresh retry (same failure
    mode writer_agent's own callback has to guard against)."""
    state = callback_context.state
    if "writer_result" not in state:
        return
    writer_result = state["writer_result"]
    draft = {
        "prompt_id": WRITER_PROMPT_ID,
        "temperature": 0,
        "degradation_line": state.get("degradation_line"),
        "items": writer_result["items"],
    }
    state["critic_draft_json"] = json.dumps(draft)


critic_agent = Agent(
    name="pulse_critic",
    model=MODEL,
    instruction=_PROMPT_PATH.read_text(encoding="utf-8")
    + "\n\nDraft to evaluate:\n{critic_draft_json}"
    + "\n\nContext it was written from:\n{critic_context_json}",
    before_agent_callback=_prepare_critic_input,
    output_schema=CriticOutput,
    output_key="critic_result",
    generate_content_config=genai_types.GenerateContentConfig(temperature=0),
)


def run_critic_agent(draft: dict, context: dict) -> CriticOutput:
    """draft/context: the same plain dicts app/pipeline/pulse.py's
    _critic_draft_view/_critic_context_view already build. Raises on any
    failure — the caller (evaluate() below) decides how to handle it."""
    state = run_agent_sync(
        critic_agent,
        {
            "critic_draft_json": json.dumps(draft),
            "critic_context_json": json.dumps(context),
        },
    )
    return CriticOutput.model_validate(state["critic_result"])
