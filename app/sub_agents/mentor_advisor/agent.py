"""On-demand mentor advice for the /ui dashboard (requested: "a popup
agent near the accomplishment ledger like a mentor that constantly
advises the user and gives them long term and short term tasks in order
to get aligned with career goals and short term objective, similar to
what lands in the friday reflection"). Deliberately NOT a scheduled
ritual with its own delivery table — it's a single on-demand LLM call,
triggered by the button click itself (app/triggers/mentor_advisor_
router.py), reusing app.sub_agents.friday_review.sub_agents.gather.agent.
gather_friday_review_context wholesale rather than re-deriving the same
goals/OKR-progress/skill-distribution/stuck-items context a second way —
that context is already exactly "everything real this person's situation
is grounded in," the same input Friday review itself synthesizes from."""

import json
from pathlib import Path

from google.adk.agents import Agent
from google.genai import types as genai_types
from pydantic import BaseModel

from app.sub_agents.friday_review.sub_agents.gather.agent import FridayReviewContext

MODEL = "gemini-3.5-flash-lite"
PROMPT_ID = "mentor_advisor.v1"
PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "mentor_advisor.v1.md"
OUTPUT_KEY = "mentor_advisor_result"


class MentorAdviceOutput(BaseModel):
    short_term_tasks: list[str] = []
    long_term_tasks: list[str] = []
    rationale: str | None = None


def build_mentor_advisor_agent() -> Agent:
    instruction = PROMPT_PATH.read_text(encoding="utf-8") + "\n\nContext:\n{context_json}"
    return Agent(
        name="mentor_advisor",
        model=MODEL,
        instruction=instruction,
        output_schema=MentorAdviceOutput,
        output_key=OUTPUT_KEY,
        generate_content_config=genai_types.GenerateContentConfig(temperature=0.2),
    )


def _context_to_state(context: FridayReviewContext) -> dict:
    import dataclasses

    return {
        "context_json": json.dumps(
            {
                "wins": [
                    {"description": w.description, "kind": w.kind}
                    for w in context.wins
                ],
                "okr_progress": [dataclasses.asdict(g) for g in context.okr_progress],
                "career_goal": (
                    dataclasses.asdict(context.career_goal) if context.career_goal else None
                ),
                "slipped": [dataclasses.asdict(s) for s in context.slipped],
                "skill_distribution": context.skill_distribution.counts,
                "agenda_stuck": [i.text for i in context.agenda.stuck],
            },
            default=str,
        )
    }


def generate_mentor_advice(context: FridayReviewContext, llm_agent) -> MentorAdviceOutput:
    from app.core.adk_runner import run_agent_sync

    state = run_agent_sync(
        llm_agent,
        _context_to_state(context),
        kickoff_text="Give this person mentor advice for right now.",
    )
    return MentorAdviceOutput.model_validate(state[OUTPUT_KEY])
