"""Layer 6 composition for F4: the ONLY LLM call in the whole ritual
(AGENT.md §2). Everything else in this module — okr formatting, quiet-
week detection, agenda/slipped rendering, proposed-ledger-item assembly —
is deterministic Python over FridayReviewContext, matching design spec
§4's "skill distribution and OKR progress are computed in gather, not
asked of the LLM" decision. The model's only jobs: phrase each win, map
it to a goal, write one adjustment, write the career narrative, and
classify a missing skill_category — see the prompt file itself."""

import dataclasses
import datetime
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.adk.agents import Agent
from google.genai import types as genai_types
from pydantic import BaseModel

from app.sub_agents.friday_review.sub_agents.gather.agent import (
    FridayReviewContext,
    OkrProgress,
    WinEvidence,
)

PROMPT_VERSION = "friday_review_synthesize@v1"
PROMPT_PATH = Path(__file__).resolve().parents[4] / "prompts" / "friday_review_synthesize.md"
MODEL = "gemini-3.5-flash-lite"
OUTPUT_KEY = "friday_review_synthesize_result"

SKILL_CATEGORIES = (
    "technical_execution",
    "cross_team_collab",
    "mentorship",
    "leadership_docs",
)


@dataclass(frozen=True)
class ScoredWin:
    text: str
    source_link: str | None
    source_reference_key: str
    moved_goal_title: str | None
    already_logged: bool
    skill_category: str | None = None
    # Carried straight through from WinEvidence.id — see that field's own
    # docstring for why the persistence step below keys off this instead
    # of source_reference_key.
    id: str | None = None


@dataclass(frozen=True)
class FridayReviewCard:
    week_start: datetime.date
    wins: list
    slipped_lines: list
    one_adjustment: str | None
    agenda_resolved_lines: list
    agenda_stuck_lines: list
    okr_progress_lines: list
    career_narrative: str | None
    daily_pulse_patterns: list
    skill_distribution_summary: str | None
    quiet_week: bool
    identity_asks_count: int
    # REAL CHANGE (requested: "in the friday reflection u don't mention
    # what needs to be worked onn for the next week") — broader than
    # one_adjustment above (which is only ever derived from `slipped`):
    # this can also flag a behind-pace OKR or a stuck agenda item.
    next_week_focus_lines: list = dataclasses.field(default_factory=list)


class WinOutput(BaseModel):
    source_reference_key: str
    phrased_text: str
    moved_goal_title: str | None = None


class SkillClassificationOutput(BaseModel):
    source_reference_key: str
    skill_category: str


class SynthesizeOutput(BaseModel):
    wins: list[WinOutput] = []
    one_adjustment: str | None = None
    career_narrative: str | None = None
    skill_classifications: list[SkillClassificationOutput] = []
    next_week_focus: list[str] = []


def build_synthesize_agent() -> Agent:
    """Factory for the real synthesis Agent, mirrors app.sub_agents.
    dossier.sub_agents.synthesize.agent.build_synthesize_agent's shape
    exactly, including the appended {context_json} placeholder ADK needs
    to actually substitute the seeded state key into the prompt."""
    instruction = (
        PROMPT_PATH.read_text(encoding="utf-8") + "\n\nContext:\n{context_json}"
    )
    return Agent(
        name="friday_review_synthesize",
        model=MODEL,
        instruction=instruction,
        output_schema=SynthesizeOutput,
        output_key=OUTPUT_KEY,
        generate_content_config=genai_types.GenerateContentConfig(temperature=0),
    )


def drop_unsourced_wins(
    llm_wins: list, context_wins: list
) -> list:
    """The provenance rule, enforced against real DB rows rather than
    trusted from the LLM's own text: an LLM-phrased win only survives if
    its source_reference_key matches a real WinEvidence gather actually
    produced. A hallucinated key (one not in context_wins) is dropped —
    mirrors app.sub_agents.dossier.sub_agents.synthesize.agent's
    drop_unsourced_talking_points, adapted to key off real evidence
    instead of a bare source_link presence check."""
    by_key = {w.source_reference_key: w for w in context_wins}
    kept: list[ScoredWin] = []
    for llm_win in llm_wins:
        evidence = by_key.get(llm_win.source_reference_key)
        if evidence is None:
            continue
        kept.append(
            ScoredWin(
                text=llm_win.phrased_text,
                source_link=evidence.source_link,
                source_reference_key=evidence.source_reference_key,
                moved_goal_title=llm_win.moved_goal_title,
                already_logged=evidence.already_logged,
                skill_category=evidence.existing_skill_category,
                id=evidence.id,
            )
        )
    return kept


def _apply_skill_classifications(
    wins: list, classifications: list
) -> list:
    by_key = {
        c.source_reference_key: c.skill_category
        for c in classifications
        if c.skill_category in SKILL_CATEGORIES
    }
    return [
        dataclasses.replace(w, skill_category=by_key[w.source_reference_key])
        if w.skill_category is None and w.source_reference_key in by_key
        else w
        for w in wins
    ]


def build_proposed_ledger_items(wins: list) -> list:
    """The "Confirm & log" checklist's contents (design spec §2's
    correction) — only evidence with no Accomplishment row yet, never
    wins already on the ledger. Written directly onto FridayReviewDelivery
    by deliver_friday_review; turned into real Accomplishment rows only by
    confirm_and_log_ledger_items, on explicit consent."""
    return [
        {
            "description": w.text,
            "source_reference_key": w.source_reference_key,
            "skill_category": w.skill_category,
        }
        for w in wins
        if not w.already_logged
    ]


def format_okr_progress(items: list) -> list:
    lines = []
    for item in items:
        if item.progress is not None:
            lines.append(f"{item.title}: {round(item.progress * 100)}%")
        elif item.current_value is not None and item.target_value:
            lines.append(f"{item.title}: {item.current_value}/{item.target_value}")
        else:
            lines.append(f"{item.title}: in progress")
    return lines


def _format_slipped(context: FridayReviewContext) -> list:
    return [s.description for s in context.slipped]


def format_skill_distribution_counts(counts: dict) -> str | None:
    """Public so app.sub_agents.friday_review.agent's post-classification
    persistence step can recompute this WITH this week's freshly
    classified wins folded in, not just gather's pre-classification
    8-week snapshot — see that module's own docstring for why the counts
    it passes here differ from context.skill_distribution.counts."""
    if not counts:
        return None
    total = sum(counts.values())
    parts = [f"{k}: {round(v / total * 100)}%" for k, v in sorted(counts.items())]
    return ", ".join(parts)


def _format_skill_distribution(context: FridayReviewContext) -> str | None:
    return format_skill_distribution_counts(context.skill_distribution.counts)


def is_quiet_week(context: FridayReviewContext, kept_wins: list) -> bool:
    return not (
        kept_wins
        or context.okr_progress
        or context.agenda.resolved_this_week
        or context.agenda.carried
        or context.slipped
    )


def _context_to_state(context: FridayReviewContext) -> dict:
    return {
        "context_json": json.dumps(
            {
                "wins": [dataclasses.asdict(w) for w in context.wins],
                "okr_progress": [dataclasses.asdict(g) for g in context.okr_progress],
                "career_goal": (
                    dataclasses.asdict(context.career_goal) if context.career_goal else None
                ),
                "slipped": [dataclasses.asdict(s) for s in context.slipped],
                "skill_distribution": context.skill_distribution.counts,
                # REAL BUG FOUND AND FIXED: agenda.stuck items are real
                # AgendaItem ORM rows, not dataclasses (dataclasses.asdict
                # would raise on them) — and this key was missing
                # entirely until the next_week_focus addition below
                # needed it. The prompt already instructs the model to
                # ground next_week_focus in stuck agenda items; without
                # this, that instruction referenced data the model could
                # never actually see.
                "agenda_stuck": [i.text for i in context.agenda.stuck],
            },
            default=str,
        )
    }


def synthesize_friday_review(context: FridayReviewContext, llm_agent) -> FridayReviewCard:
    from app.core.adk_runner import run_agent_sync

    state = run_agent_sync(
        llm_agent,
        _context_to_state(context),
        kickoff_text="Write the Friday reflection for this context.",
    )
    result = SynthesizeOutput.model_validate(state[OUTPUT_KEY])

    wins = drop_unsourced_wins(result.wins, context.wins)
    wins = _apply_skill_classifications(wins, result.skill_classifications)
    quiet = is_quiet_week(context, wins)

    return FridayReviewCard(
        week_start=context.week_start,
        wins=wins,
        slipped_lines=_format_slipped(context),
        one_adjustment=None if quiet else result.one_adjustment,
        agenda_resolved_lines=[i.text for i in context.agenda.resolved_this_week],
        agenda_stuck_lines=[i.text for i in context.agenda.stuck],
        okr_progress_lines=format_okr_progress(context.okr_progress),
        career_narrative=result.career_narrative if context.career_goal else None,
        daily_pulse_patterns=context.daily_pulse_patterns,
        skill_distribution_summary=_format_skill_distribution(context),
        quiet_week=quiet,
        identity_asks_count=len(context.identity_batch),
        next_week_focus_lines=[] if quiet else result.next_week_focus,
    )
