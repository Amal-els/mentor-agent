import logging

from app.pipeline.pulse import enforce_ranker_output
from app.sub_agents.pulse.sub_agents.ranker.agent import (
    MAX_FOCUS,
    PROMPT_ID,
    RankerResult,
    fallback_rank,
)
from app.salience.pulse.types import ScoredItem


def _items(n, item_type="work_item"):
    # work_item, not event, by default — these tests exercise closed-set/
    # truncation/dedup logic unrelated to item type; "event" specifically
    # would now trip enforce_ranker_output's own event-in-Focus rejection
    # (events never win a Focus slot — see that function's own docstring)
    # and silently empty every result via fallback_rank's matching filter.
    # Tests that actually want to exercise that rejection pass item_type=
    # "event" explicitly.
    return [
        ScoredItem(
            item_id=f"i{k}",
            item_type=item_type,
            score=float(n - k),
            score_terms={},
            candidate_focus=False,
        )
        for k in range(n)
    ]


def test_valid_result_passes_through_unchanged():
    shortlist = _items(5)
    result = RankerResult(
        ordered_item_ids=["i0", "i1"],
        rationale={"i0": "a", "i1": "b"},
        prompt_id=PROMPT_ID,
    )

    checked, fell_back = enforce_ranker_output(shortlist, result)

    assert checked.ordered_item_ids == ["i0", "i1"]
    assert fell_back is False


def test_more_than_three_ids_are_hard_truncated():
    shortlist = _items(5)
    result = RankerResult(
        ordered_item_ids=["i0", "i1", "i2", "i3"],
        rationale={"i0": "a", "i1": "b", "i2": "c", "i3": "d"},
        prompt_id=PROMPT_ID,
    )

    checked, fell_back = enforce_ranker_output(shortlist, result)

    assert len(checked.ordered_item_ids) == MAX_FOCUS
    assert checked.ordered_item_ids == ["i0", "i1", "i2"]
    assert set(checked.rationale) == {"i0", "i1", "i2"}
    assert fell_back is False


def test_unknown_id_triggers_full_fallback_to_l5_score_order():
    shortlist = _items(5)
    result = RankerResult(
        ordered_item_ids=["i0", "bogus-id"],
        rationale={"i0": "a", "bogus-id": "invented"},
        prompt_id=PROMPT_ID,
    )

    checked, fell_back = enforce_ranker_output(shortlist, result)

    assert fell_back is True
    assert checked.ordered_item_ids == fallback_rank(shortlist).ordered_item_ids


def test_unknown_id_is_logged(caplog):
    shortlist = _items(5)
    result = RankerResult(
        ordered_item_ids=["bogus-id"],
        rationale={"bogus-id": "invented"},
        prompt_id=PROMPT_ID,
    )

    with caplog.at_level(logging.WARNING):
        enforce_ranker_output(shortlist, result)

    assert any("bogus-id" in record.message for record in caplog.records)


def test_event_picked_for_focus_triggers_full_fallback_to_l5_score_order():
    # work items 0-4 plus one real event, scored higher than all of them —
    # a ranker picking it for Focus must be rejected the same way an
    # unknown id is, not merely discouraged: every event already shows in
    # Today's meeting load regardless of ranking, so one in Focus too is
    # never correct output.
    shortlist = _items(5) + [
        ScoredItem(
            item_id="evt-high-score",
            item_type="event",
            score=999.0,
            score_terms={},
            candidate_focus=False,
        )
    ]
    result = RankerResult(
        ordered_item_ids=["evt-high-score", "i0"],
        rationale={"evt-high-score": "important meeting", "i0": "a"},
        prompt_id=PROMPT_ID,
    )

    checked, fell_back = enforce_ranker_output(shortlist, result)

    assert fell_back is True
    assert "evt-high-score" not in checked.ordered_item_ids
    assert checked.ordered_item_ids == fallback_rank(shortlist).ordered_item_ids


def test_event_picked_for_focus_is_logged(caplog):
    shortlist = [
        ScoredItem(
            item_id="evt-1", item_type="event", score=5.0, score_terms={}, candidate_focus=False
        )
    ]
    result = RankerResult(
        ordered_item_ids=["evt-1"], rationale={"evt-1": "meeting"}, prompt_id=PROMPT_ID
    )

    with caplog.at_level(logging.WARNING):
        enforce_ranker_output(shortlist, result)

    assert any("evt-1" in record.message for record in caplog.records)


def test_missing_rationale_for_a_valid_id_triggers_full_fallback():
    shortlist = _items(5)
    result = RankerResult(
        ordered_item_ids=["i0", "i1"],
        rationale={"i0": "a"},
        prompt_id=PROMPT_ID,
    )

    checked, fell_back = enforce_ranker_output(shortlist, result)

    assert fell_back is True
    assert checked.ordered_item_ids == fallback_rank(shortlist).ordered_item_ids


def test_missing_rationale_is_logged(caplog):
    shortlist = _items(5)
    result = RankerResult(
        ordered_item_ids=["i0", "i1"],
        rationale={"i0": "a"},
        prompt_id=PROMPT_ID,
    )

    with caplog.at_level(logging.WARNING):
        enforce_ranker_output(shortlist, result)

    assert any("i1" in record.message for record in caplog.records)


def test_duplicate_ids_are_deduped_preserving_first_occurrence():
    shortlist = _items(5)
    result = RankerResult(
        ordered_item_ids=["i0", "i1", "i0"],
        rationale={"i0": "a", "i1": "b"},
        prompt_id=PROMPT_ID,
    )

    checked, fell_back = enforce_ranker_output(shortlist, result)

    assert checked.ordered_item_ids == ["i0", "i1"]
    assert fell_back is False


def test_every_surviving_item_keeps_its_source_item_id():
    shortlist = _items(5)
    result = RankerResult(
        ordered_item_ids=["i2", "i0"],
        rationale={"i2": "x", "i0": "y"},
        prompt_id=PROMPT_ID,
    )

    checked, _ = enforce_ranker_output(shortlist, result)

    shortlist_ids = {item.item_id for item in shortlist}
    assert all(item_id in shortlist_ids for item_id in checked.ordered_item_ids)
