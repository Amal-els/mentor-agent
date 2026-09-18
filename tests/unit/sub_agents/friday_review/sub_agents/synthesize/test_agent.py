import datetime

from app.sub_agents.friday_review.sub_agents.gather.agent import (
    AgendaSummary,
    FridayReviewContext,
    SkillDistribution,
    WinEvidence,
)
from app.sub_agents.friday_review.sub_agents.synthesize.agent import (
    WinOutput,
    build_proposed_ledger_items,
    drop_unsourced_wins,
    format_okr_progress,
    is_quiet_week,
)


def test_drops_wins_with_no_matching_evidence():
    context_wins = [
        WinEvidence(
            description="Shipped X", source_reference_key="wi-1", source_link="https://x/1",
            kind="work_item", already_logged=False,
        )
    ]
    llm_wins = [
        WinOutput(source_reference_key="wi-1", phrased_text="Shipped the migration."),
        WinOutput(source_reference_key="hallucinated", phrased_text="Made something up."),
    ]
    kept = drop_unsourced_wins(llm_wins, context_wins)
    assert [w.source_reference_key for w in kept] == ["wi-1"]
    assert kept[0].source_link == "https://x/1"


def test_build_proposed_ledger_items_excludes_already_logged():
    from app.sub_agents.friday_review.sub_agents.synthesize.agent import ScoredWin

    wins = [
        ScoredWin(
            text="Shipped X", source_link="https://x/1", source_reference_key="wi-1",
            moved_goal_title=None, already_logged=False, skill_category="technical_execution",
        ),
        ScoredWin(
            text="Already on the ledger", source_link=None, source_reference_key="acc-1",
            moved_goal_title=None, already_logged=True, skill_category=None,
        ),
    ]
    proposed = build_proposed_ledger_items(wins)
    assert len(proposed) == 1
    assert proposed[0]["source_reference_key"] == "wi-1"
    assert proposed[0]["skill_category"] == "technical_execution"


def test_quiet_week_when_nothing_at_all():
    context = FridayReviewContext(
        week_start=datetime.date(2026, 8, 24), wins=[], slipped=[],
        agenda=AgendaSummary(), okr_progress=[], career_goal=None,
        daily_pulse_patterns=[], skill_distribution=SkillDistribution(counts={}),
        identity_batch=[],
    )
    assert is_quiet_week(context, kept_wins=[]) is True


def test_not_quiet_week_when_okr_progress_exists():
    from app.sub_agents.friday_review.sub_agents.gather.agent import OkrProgress

    context = FridayReviewContext(
        week_start=datetime.date(2026, 8, 24), wins=[], slipped=[],
        agenda=AgendaSummary(), okr_progress=[
            OkrProgress(title="Ship it", goal_type="key_result", progress=0.5,
                        current_value=5, target_value=10)
        ], career_goal=None, daily_pulse_patterns=[],
        skill_distribution=SkillDistribution(counts={}), identity_batch=[],
    )
    assert is_quiet_week(context, kept_wins=[]) is False


def test_format_okr_progress_is_deterministic_not_llm():
    from app.sub_agents.friday_review.sub_agents.gather.agent import OkrProgress

    lines = format_okr_progress(
        [OkrProgress(title="Ship it", goal_type="key_result", progress=0.6,
                      current_value=6, target_value=10)]
    )
    assert lines == ["Ship it: 60%"]
