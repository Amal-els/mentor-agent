"""ADK orchestrator for the pulse ritual's L6 composition step: ranker ->
writer -> critic, wrapped in a real LoopAgent (docs/plans/morning-pulse.md
M4, updated design — see docs/plans/morning-pulse.md's note on the
ranker/critic loop rebuild for why). NOTE: LoopAgent is deprecated
upstream in favor of ADK 2.0's graph-based Workflow API, which cannot yet
be used as an LlmAgent sub-agent — LoopAgent is still the correct tool for
this today; revisit if/when Workflow gains that capability.

Unlike the SequentialAgent this replaced, critic_agent is a real sub_agent
here, not a standalone call from app/pipeline/pulse.py — it judges both
the ranking (against the priority-signals table, given every candidate's
score_terms) and the writing (grounding), tagging each reason with
scope: "ranking" | "writing" (pulse_critic.v1.md). On "revise", writer_agent
always re-runs with that critique in hand; ranker_agent only re-runs if at
least one reason is scope="ranking" — see _MaybeRunRanker below. A
writing-only revise skipping the ranker isn't just an efficiency win: it
avoids feeding ranker_agent feedback that was never about its own output,
which could otherwise make it second-guess a selection/order that was
never in question (undermining its own "temperature 0, same input, same
output" contract — the input would change even though nothing it did was
wrong). Capped at one retry (max_iterations=2) — the second pass ships
regardless of its own verdict, same "one revise loop" precedent the old
design used. What stays unconditionally code, never trusted to any agent:
enforce_ranker_output's closed-set/hard-cut check and the writer output/
order assertion (app/pipeline/pulse.py), applied to whatever this loop's
final iteration produced."""

import json
from dataclasses import dataclass

from google.adk.agents import BaseAgent, LoopAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.events.event_actions import EventActions

from app.core.adk_runner import run_agent_sync
from app.salience.pulse.types import ScoredItem
from app.sub_agents.pulse.sub_agents.critic.agent import critic_agent
from app.sub_agents.pulse.sub_agents.ranker.agent import RankerOutput, ranker_agent
from app.sub_agents.pulse.sub_agents.writer.agent import WriterOutput, writer_agent


class _MaybeRunRanker(BaseAgent):
    """Wraps ranker_agent as its one sub_agent (the ADK-idiomatic
    conditional-router shape) and skips actually invoking it — no LLM call
    at all, not just a discarded one — when state says the last critique
    had no scope="ranking" reason. ranker_result simply carries forward
    unchanged in state when skipped; writer_agent still re-runs against it
    with the fresh critique, since a writing-only revise means the
    selection/order was fine but the prose wasn't."""

    async def _run_async_impl(self, ctx: InvocationContext):
        if not ctx.session.state.get("rerun_ranker", True):
            return  # no events — ranker never runs this iteration
        async for event in self.sub_agents[0].run_async(ctx):
            yield event


class _PulseGateStep(BaseAgent):
    """The non-LLM half of each loop iteration: reads critic_agent's
    verdict from state and either escalates (stop the loop) or stashes the
    critique for ranker_agent/writer_agent's next attempt via
    previous_critique_json, plus whether that critique warrants re-running
    the ranker at all (rerun_ranker — read by _MaybeRunRanker above).
    Iteration 0 revise -> one more pass; iteration 1 always escalates
    regardless of that pass's own verdict — the second critic call is
    still made (observability/logging) but never acted on, matching the
    old code-orchestrated critic loop's exact behavior."""

    async def _run_async_impl(self, ctx: InvocationContext):
        state = ctx.session.state
        iteration = state.get("pulse_loop_iteration", 0)
        critic_result = state["critic_result"]

        state_delta: dict = {"pulse_loop_iteration": iteration + 1}
        should_escalate = True

        if iteration == 0:
            if critic_result["verdict"] == "revise":
                reasons = critic_result["reasons"]
                state_delta["previous_critique_json"] = json.dumps(reasons)
                state_delta["pulse_revised"] = True
                # No reasons on a revise shouldn't happen, but if it did,
                # default to rerunning the ranker — safer than silently
                # skipping it with nothing to justify that.
                state_delta["rerun_ranker"] = (
                    any(r.get("scope") == "ranking" for r in reasons)
                    if reasons
                    else True
                )
                should_escalate = False  # let the loop run once more
            else:
                state_delta["pulse_revised"] = False
        # iteration >= 1: always escalate — the second verdict is already
        # in state (critic_agent's own output_key) but this step
        # deliberately does nothing further with it.

        yield Event(
            author=self.name,
            actions=EventActions(escalate=should_escalate, state_delta=state_delta),
        )


pulse_agent = LoopAgent(
    name="pulse_agent",
    sub_agents=[
        _MaybeRunRanker(name="pulse_ranker_step", sub_agents=[ranker_agent]),
        writer_agent,
        critic_agent,
        _PulseGateStep(name="pulse_gate"),
    ],
    max_iterations=2,
)


@dataclass(frozen=True)
class PulseAgentResult:
    ranker: RankerOutput
    writer: WriterOutput
    critic_revised: bool
    critic_reasons: list[tuple[str, str]]


def run_pulse_agent(
    shortlist: list[ScoredItem],
    item_titles: dict[str, str],
    critic_context: dict,
    degradation_line: str | None,
) -> PulseAgentResult:
    """Runs the whole ranker/writer/critic loop in one Runner invocation.
    critic_context is the same shape app/pipeline/pulse.py's
    _critic_context_view already builds (shortlist score_terms +
    degraded_sources) — static across iterations, seeded once here rather
    than rebuilt per pass, since the *input* candidates never change
    mid-loop, only the ranking/writing of them does. Raises on any
    failure — the caller decides whether to attempt this at all and
    catches failures itself, same contract the old run_pulse_pipeline had."""
    shortlist_view = [
        {
            "item_id": item.item_id,
            "score": item.score,
            "score_terms": item.score_terms,
        }
        for item in shortlist
    ]
    score_terms_by_id = {item.item_id: item.score_terms for item in shortlist}

    state = run_agent_sync(
        pulse_agent,
        {
            "ranker_input_json": json.dumps(shortlist_view),
            "item_titles_json": json.dumps(item_titles),
            "item_score_terms_json": json.dumps(score_terms_by_id),
            "critic_context_json": json.dumps(critic_context),
            "degradation_line": degradation_line,
            "previous_critique_json": "[]",
            "pulse_loop_iteration": 0,
            "rerun_ranker": True,  # always run on the first pass
        },
    )
    ranker_result = RankerOutput.model_validate(state["ranker_result"])
    writer_result = WriterOutput.model_validate(state["writer_result"])
    critic_revised = state.get("pulse_revised", False)
    critic_reasons = (
        [
            (r["item_id"], r["reason"])
            for r in json.loads(state.get("previous_critique_json", "[]"))
        ]
        if critic_revised
        else []
    )

    return PulseAgentResult(
        ranker=ranker_result,
        writer=writer_result,
        critic_revised=critic_revised,
        critic_reasons=critic_reasons,
    )
