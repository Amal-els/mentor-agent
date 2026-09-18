"""Shared loaders for app/fixtures/pulse/{ranker,critic}/* — not a test
module itself (no test_ prefix), imported by unit and live ranker/critic
tests alike."""

from pathlib import Path

import yaml

from app.salience.pulse.types import ScoredItem

RANKER_DIR = Path(__file__).resolve().parents[1] / "app" / "fixtures" / "pulse" / "ranker"
CRITIC_DIR = Path(__file__).resolve().parents[1] / "app" / "fixtures" / "pulse" / "critic"


def load_ranker_fixture(name: str) -> dict:
    return yaml.safe_load((RANKER_DIR / f"{name}.yaml").read_text(encoding="utf-8"))


def scored_items_from_shortlist(shortlist: list[dict]) -> list[ScoredItem]:
    return [
        ScoredItem(
            item_id=entry["item_id"],
            # work_item, not event — none of app/fixtures/pulse/ranker/*
            # specify item_type at all (irrelevant to what they actually
            # test: sort order/tie-breaking), and "event" specifically
            # now means "never wins a Focus slot" (fallback_rank's own
            # exclusion), which would silently empty every one of these
            # fixtures' results.
            item_type=entry.get("item_type", "work_item"),
            score=entry["score"],
            score_terms=entry["score_terms"],
            candidate_focus=entry.get("candidate_focus", False),
            series_id=entry.get("series_id"),
        )
        for entry in shortlist
    ]


def load_critic_scenario(name: str) -> tuple[dict, dict, dict]:
    scenario_dir = CRITIC_DIR / name
    context = yaml.safe_load(
        (scenario_dir / "context.yaml").read_text(encoding="utf-8")
    )
    draft = yaml.safe_load(
        (scenario_dir / "draft_card.yaml").read_text(encoding="utf-8")
    )
    expected = yaml.safe_load(
        (scenario_dir / "expected_revision.yaml").read_text(encoding="utf-8")
    )
    return draft, context, expected
