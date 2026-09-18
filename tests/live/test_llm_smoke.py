"""One-shot live smoke test for the ranker/writer LLM path. Deselected by
default (pyproject.toml's `addopts = "-m 'not live'"`) — run explicitly with
`uv run pytest -m live` after setting GOOGLE_API_KEY or GEMINI_API_KEY.

This does NOT assert fixture-exact output — models drift, wording varies
run to run. It asserts only the invariants the pipeline actually depends on:
closed set, count <= 3, why_now present, no fact outside score_terms. Until
this has passed at least once against a real key, treat
app/prompts/pulse_ranker.v1.md and pulse_writer.v1.md as unreviewed
code — every other test in this repo only exercises the deterministic
fallback path, never the prompts themselves."""

import datetime

import pytest

from app.core.clock import FrozenClock
from app.core.llm import creds_available
from app.core.scope import OwnerScope
from app.ingest.fixture_source import (
    FixtureCalendarClient,
    FixtureLinearClient,
    FixtureSlackClient,
    load_day_fixture,
)
from app.ingest.seed import seed_fixture
from app.pipeline.pulse import run_pulse
from app.sub_agents.pulse.sub_agents.ranker.agent import MAX_FOCUS, llm_rank
from app.sub_agents.pulse.sub_agents.writer.agent import WriterInputItem, llm_write
from tests.fixture_loading import load_ranker_fixture, scored_items_from_shortlist

pytestmark = pytest.mark.live

_SKIP_REASON = "no LLM credentials configured (GOOGLE_API_KEY/GEMINI_API_KEY)"


@pytest.mark.skipif(not creds_available(), reason=_SKIP_REASON)
def test_live_ranker_respects_closed_set_and_count():
    fixture = load_ranker_fixture("normal_day")
    shortlist = scored_items_from_shortlist(fixture["shortlist"])
    valid_ids = {item.item_id for item in shortlist}

    result = llm_rank(shortlist, context=None)

    assert 1 <= len(result.ordered_item_ids) <= MAX_FOCUS
    assert set(result.ordered_item_ids) <= valid_ids
    assert len(result.ordered_item_ids) == len(set(result.ordered_item_ids))
    for item_id in result.ordered_item_ids:
        assert result.rationale.get(item_id), f"no rationale for {item_id}"


@pytest.mark.skipif(not creds_available(), reason=_SKIP_REASON)
def test_live_writer_why_now_traces_only_to_score_terms():
    fixture = load_ranker_fixture("normal_day")
    shortlist = scored_items_from_shortlist(fixture["shortlist"])
    by_id = {item.item_id: item for item in shortlist}

    ranker_result = llm_rank(shortlist, context=None)
    chosen_ids = ranker_result.ordered_item_ids

    titles = {
        "evt-1": "1:1 with Sarah",
        "evt-3": "Design review",
        "MENT-214": "Fix flaky roster test",
        "evt-2": "Team standup",
        "MENT-201": "Write eval dataset for critic",
    }
    writer_inputs = [
        WriterInputItem(
            item_id=item_id,
            title=titles.get(item_id, item_id),
            score_terms=by_id[item_id].score_terms,
            rationale=ranker_result.rationale.get(item_id, ""),
        )
        for item_id in chosen_ids
    ]

    drafted = llm_write(writer_inputs)

    assert [d.item_id for d in drafted] == chosen_ids
    for item in drafted:
        assert item.why_now.strip(), f"{item.item_id} has an empty why_now"
        assert item.action.strip()

        terms = by_id[item.item_id].score_terms
        allowed_name = terms.get("person_waiting")
        for other_id, other_item in by_id.items():
            if other_id == item.item_id:
                continue
            other_name = other_item.score_terms.get("person_waiting")
            if other_name and other_name != allowed_name:
                # crude but effective: the wrong item's name must not leak in
                assert other_name.split()[0] not in item.why_now, (
                    f"{item.item_id}'s why_now names {other_name!r}, which "
                    "belongs to a different item — no provenance for that claim"
                )


@pytest.mark.skipif(not creds_available(), reason=_SKIP_REASON)
def test_live_run_pulse_actually_uses_the_llm_path(pg_session):
    """The symmetric claim to test_pulse_orchestrator.py's unit test (which
    forces creds_available() False and asserts the fallback fires): with a
    real key, run_pulse() must actually take the LLM path, not silently fall
    back to the deterministic path while still reporting creds_present=True.
    That exact gap — a fallback indistinguishable from "never attempted" —
    is what this whole live-test setup exists to close.

    Does NOT assert ranker_used_llm/writer_used_llm are unconditionally
    True: a real run surfaced the ranker occasionally mistyping one
    character of a 36-char UUID item_id when echoing it back, which
    enforce_ranker_output()'s closed-set check correctly caught and fell
    back on — the safety net working as designed, not a bug. What this
    asserts instead: the LLM path was attempted (not skipped because
    "unconfigured"), and the result is valid either way. If the writer
    fell back too, or the critic ever needed to revise, that's still
    correct behavior, not a test failure."""
    fixture = load_day_fixture("normal_day")
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = FrozenClock(at=datetime.datetime.fromisoformat(fixture["now"]))
    source_clients = [
        FixtureCalendarClient(fixture),
        FixtureSlackClient(fixture),
        FixtureLinearClient(fixture),
    ]

    result = run_pulse(
        scope, clock, source_clients, trigger="pull", requested_at=clock.now()
    )

    assert result.creds_present is True
    # the LLM path was genuinely attempted — never silently skipped despite
    # credentials being present (the gap this test exists to close)
    assert result.writer_used_llm is True or result.ranker_fell_back is True
    assert 1 <= len(result.items) <= 3
    assert all(item.why_now.strip() for item in result.items)
    assert all(item.item_id for item in result.items)
