import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import Suppression
from app.salience.pulse.config import BUDGET_PUSH_MAX_ITEMS
from app.salience.pulse.gate import apply_gate
from app.salience.pulse.types import PreGateContext, Removal, ScoredItem

NOW = datetime.datetime(2026, 8, 7, 7, 0, tzinfo=datetime.UTC)
CLOCK = FrozenClock(at=NOW)


def _item(item_id, score=5.0, series_id=None):
    return ScoredItem(
        item_id=item_id,
        item_type="event",
        score=score,
        score_terms={},
        candidate_focus=False,
        series_id=series_id,
    )


def _pre_gate(shortlist, owner_user_id):
    return PreGateContext(
        owner_user_id=owner_user_id,
        owner_tz="UTC",
        window_date=NOW.date(),
        window_reason="today",
        shortlist=shortlist,
        day_events=[],
        owed=[],
        degraded_sources=[],
    )


def _suppression(owner_scope, **overrides):
    defaults = {
        "id": str(uuid.uuid4()),
        "scope": "instance",
        "target_ref": "x",
        "reason": "test",
        "created_at": NOW,
        "expires_at": NOW + datetime.timedelta(days=7),
        "created_by": "user",
    }
    defaults.update(overrides)
    row = Suppression(**defaults)
    owner_scope.add(row)
    owner_scope.commit()
    return row


def test_pull_bypasses_suppression_and_budget(make_scope):
    scope = make_scope()
    items = [_item(f"i{n}") for n in range(BUDGET_PUSH_MAX_ITEMS + 2)]
    _suppression(scope, scope="instance", target_ref="i0")
    pre_gate = _pre_gate(items, scope.owner_user_id)

    survivors, removals = apply_gate(scope, CLOCK, pre_gate, "pull")

    assert survivors == items
    assert removals == []


def test_cron_removes_series_suppressed_item(make_scope):
    scope = make_scope()
    items = [_item("evt-1", series_id=None), _item("evt-2", series_id="series:standup")]
    _suppression(scope, scope="series", target_ref="series:standup")
    pre_gate = _pre_gate(items, scope.owner_user_id)

    survivors, removals = apply_gate(scope, CLOCK, pre_gate, "cron")

    assert [i.item_id for i in survivors] == ["evt-1"]
    assert removals == [
        Removal(item_id="evt-2", reason="series_suppression:series:standup")
    ]


def test_cron_applies_budget_after_suppression(make_scope):
    scope = make_scope()
    items = [_item(f"i{n}", score=10.0 - n) for n in range(BUDGET_PUSH_MAX_ITEMS + 2)]
    pre_gate = _pre_gate(items, scope.owner_user_id)

    survivors, removals = apply_gate(scope, CLOCK, pre_gate, "cron")

    assert len(survivors) == BUDGET_PUSH_MAX_ITEMS
    assert [i.item_id for i in survivors] == [
        f"i{n}" for n in range(BUDGET_PUSH_MAX_ITEMS)
    ]
    budget_removals = [r for r in removals if r.reason == "budget"]
    assert len(budget_removals) == 2
    assert {r.item_id for r in budget_removals} == {
        f"i{BUDGET_PUSH_MAX_ITEMS}",
        f"i{BUDGET_PUSH_MAX_ITEMS + 1}",
    }


def test_narrower_suppression_beats_broader_in_reason(make_scope):
    scope = make_scope()
    items = [_item("evt-1")]
    _suppression(scope, scope="global", target_ref="global")
    _suppression(scope, scope="instance", target_ref="evt-1")
    pre_gate = _pre_gate(items, scope.owner_user_id)

    survivors, removals = apply_gate(scope, CLOCK, pre_gate, "cron")

    assert survivors == []
    assert len(removals) == 1
    assert removals[0].reason == "instance_suppression:evt-1"


def test_expired_suppression_does_not_apply(make_scope):
    scope = make_scope()
    items = [_item("evt-1")]
    _suppression(
        scope,
        scope="instance",
        target_ref="evt-1",
        expires_at=NOW - datetime.timedelta(days=1),
    )
    pre_gate = _pre_gate(items, scope.owner_user_id)

    survivors, removals = apply_gate(scope, CLOCK, pre_gate, "cron")

    assert [i.item_id for i in survivors] == ["evt-1"]
    assert removals == []


def test_gate_accountability_pull_minus_cron_equals_removals(make_scope):
    scope = make_scope()
    items = [_item("evt-1"), _item("evt-2", series_id="series:standup")]
    _suppression(scope, scope="series", target_ref="series:standup")
    pre_gate = _pre_gate(items, scope.owner_user_id)

    cron_survivors, removals = apply_gate(scope, CLOCK, pre_gate, "cron")
    pull_survivors, pull_removals = apply_gate(scope, CLOCK, pre_gate, "pull")

    cron_ids = {i.item_id for i in cron_survivors}
    pull_ids = {i.item_id for i in pull_survivors}
    removal_ids = {r.item_id for r in removals}

    assert pull_ids - cron_ids == removal_ids
    assert cron_ids - pull_ids == set()
    assert pull_removals == []
