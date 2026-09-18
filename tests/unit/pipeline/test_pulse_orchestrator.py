import datetime
import logging
import uuid

from app.core.clock import FrozenClock
from app.core.models import Message, WorkItem
from app.core.scope import OwnerScope
from app.identity.models import Person
from app.ingest.fixture_source import (
    FixtureCalendarClient,
    FixtureLinearClient,
    FixtureSlackClient,
    load_day_fixture,
)
from app.ingest.seed import seed_fixture
from app.pipeline import pulse as pulse_module
from app.pipeline.pulse import (
    _fetch_detail,
    build_degradation_line,
    context_hash,
    run_pulse,
)
from app.salience.pulse.types import ScoredItem
from app.sub_agents.pulse.agent import PulseAgentResult
from app.sub_agents.pulse.sub_agents.ranker.agent import RankerOutput
from app.sub_agents.pulse.sub_agents.writer.agent import WriterDraftItem, WriterOutput

CLOCK = FrozenClock(at=datetime.datetime(2026, 8, 7, 7, 0, tzinfo=datetime.UTC))


def _clients(fixture_name):
    fixture = load_day_fixture(fixture_name)
    return [
        FixtureCalendarClient(fixture),
        FixtureSlackClient(fixture),
        FixtureLinearClient(fixture),
    ]


def test_build_degradation_line_is_none_when_healthy():
    assert build_degradation_line([]) is None


def test_build_degradation_line_names_every_unhealthy_source():
    line = build_degradation_line(["calendar", "linear"])
    assert line is not None
    assert "calendar" in line
    assert "linear" in line


def test_fetch_detail_combines_ticket_key_and_owner_for_a_work_item(
    pg_session, make_scope
):
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()),
        canonical_name="Sarah Ben Youssef",
        is_active=True,
        created_at=datetime.datetime.now(datetime.UTC),
    )
    scope.add(person)
    scope.commit()
    row = WorkItem(
        id=str(uuid.uuid4()),
        actor_reference_key="linear:sarah@acme.com",
        resolved_person_id=person.id,
        source="linear",
        external_id="MENT-214",
        title="Fix flaky roster test",
    )
    scope.add(row)
    scope.commit()

    detail = _fetch_detail(
        scope,
        ScoredItem(
            item_id=row.id,
            item_type="work_item",
            score=1.0,
            score_terms={},
            candidate_focus=True,
        ),
    )

    assert detail == "MENT-214 · Sarah Ben Youssef"


def test_fetch_detail_falls_back_to_just_the_ticket_key_with_no_resolved_owner(
    pg_session, make_scope
):
    scope = make_scope()
    row = WorkItem(
        id=str(uuid.uuid4()),
        actor_reference_key="linear:unknown@acme.com",
        source="linear",
        external_id="MENT-9",
        title="Something",
    )
    scope.add(row)
    scope.commit()

    detail = _fetch_detail(
        scope,
        ScoredItem(
            item_id=row.id,
            item_type="work_item",
            score=1.0,
            score_terms={},
            candidate_focus=True,
        ),
    )

    assert detail == "MENT-9"


def test_fetch_detail_shows_the_sender_for_a_message(pg_session, make_scope):
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()),
        canonical_name="Marc Cohen",
        is_active=True,
        created_at=datetime.datetime.now(datetime.UTC),
    )
    scope.add(person)
    scope.commit()
    row = Message(
        id=str(uuid.uuid4()),
        actor_reference_key="slack:U1",
        resolved_person_id=person.id,
        source="slack",
        external_id="1.1",
        body_ref="slack://C1/1.1",
    )
    scope.add(row)
    scope.commit()

    detail = _fetch_detail(
        scope,
        ScoredItem(
            item_id=row.id,
            item_type="message",
            score=1.0,
            score_terms={},
            candidate_focus=True,
        ),
    )

    assert detail == "From Marc Cohen"


def test_fetch_detail_is_none_for_events(pg_session, make_scope):
    from app.core.models import Event

    scope = make_scope()
    row = Event(
        id=str(uuid.uuid4()),
        actor_reference_key="calendar:sarah@acme.com",
        source="calendar",
        external_id="evt-1",
        title="1:1 with Sarah",
        status="confirmed",
    )
    scope.add(row)
    scope.commit()

    detail = _fetch_detail(
        scope,
        ScoredItem(
            item_id=row.id,
            item_type="event",
            score=1.0,
            score_terms={},
            candidate_focus=True,
        ),
    )

    assert detail is None


def test_context_hash_is_stable_for_same_shortlist():
    h1 = context_hash([{"item_id": "a", "score": 1.0}])
    h2 = context_hash([{"item_id": "a", "score": 1.0}])
    h3 = context_hash([{"item_id": "b", "score": 1.0}])

    assert h1 == h2
    assert h1 != h3


def test_run_pulse_normal_day_produces_one_to_three_items(pg_session):
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)

    result = run_pulse(
        scope,
        CLOCK,
        _clients("normal_day"),
        trigger="pull",
        requested_at=CLOCK.now(),
    )

    assert 1 <= len(result.items) <= 3
    assert all(item.why_now for item in result.items)
    assert result.ranker_prompt_id == "pulse_ranker.v1"
    assert result.writer_prompt_id == "pulse_writer.v1"
    assert result.critic_prompt_id == "pulse_critic.v1"
    # the critic is now a real LLM judge with no credentials in this
    # sandboxed unit-test process (tests/conftest.py forces creds_available()
    # False) — there's nothing to attempt, so it's deliberately skipped, not
    # a failure. tests/live/test_critic_smoke.py proves the real judge path.
    assert result.critic_skipped is True


def test_run_pulse_items_match_ranker_order_exactly(pg_session):
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)

    result = run_pulse(
        scope,
        CLOCK,
        _clients("normal_day"),
        trigger="pull",
        requested_at=CLOCK.now(),
    )

    assert [i.item_id for i in result.items] == result.ordered_item_ids


def test_run_pulse_degraded_source_sets_degradation_line(pg_session):
    counts = seed_fixture(pg_session, "degraded_source")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)

    result = run_pulse(
        scope,
        CLOCK,
        _clients("degraded_source"),
        trigger="pull",
        requested_at=CLOCK.now(),
    )

    assert result.degradation_line is not None
    assert "calendar" in result.degradation_line
    assert "linear" in result.degradation_line


def test_run_pulse_clear_day_ships_with_no_focus_items(pg_session):
    counts = seed_fixture(pg_session, "clear_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)

    result = run_pulse(
        scope,
        CLOCK,
        _clients("clear_day"),
        trigger="pull",
        requested_at=CLOCK.now(),
    )

    assert result.items == []
    assert len(result.context.owed) == 1


def test_run_pulse_reports_the_llm_path_as_unused_when_creds_are_forced_off(
    pg_session,
):
    """The gap this closes: a green suite was previously compatible with the
    LLM path being silently dead forever, because the fallback and "creds
    just aren't configured" cases were indistinguishable. This test's
    process may well have real credentials configured (tests/conftest.py's
    autouse fixture forces creds_available() to False regardless, so every
    unit test — this one included — exercises the deterministic fallback
    path deliberately, keeping the suite network-free). The symmetric claim
    — that credentials being present means the LLM path was actually used,
    not silently abandoned — is tests/live/test_llm_smoke.py's job; only a
    real key can prove that."""
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)

    result = run_pulse(
        scope,
        CLOCK,
        _clients("normal_day"),
        trigger="pull",
        requested_at=CLOCK.now(),
    )

    assert result.creds_present is False
    assert result.ranker_used_llm is False
    assert result.writer_used_llm is False


def test_run_pulse_logs_a_revision_as_ids_and_reasons_only(
    pg_session, monkeypatch, caplog
):
    """Confirms the revision log — the future eval dataset — actually fires
    and carries only ids + reasons, never item content (title/why_now).
    Ranker+writer+critic's actual retry behavior lives inside pulse_agent
    (app/sub_agents/pulse/agent.py) and is covered there — this test is
    pulse.py's own logging/bookkeeping around whatever PulseAgentResult
    comes back, so run_pulse_agent itself is faked with a canned result."""
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)

    monkeypatch.setattr(pulse_module, "creds_available", lambda: True)

    def fake_run_pulse_agent(shortlist, item_titles, critic_context, degradation_line):
        item_id = shortlist[0].item_id
        return PulseAgentResult(
            ranker=RankerOutput(ordered_item_ids=[item_id], rationale={item_id: "x"}),
            writer=WriterOutput(
                items=[
                    WriterDraftItem(
                        item_id=item_id, title="T", why_now="W", action="A"
                    )
                ]
            ),
            critic_revised=True,
            critic_reasons=[(item_id, "forced-for-test: skipped a blocker")],
        )

    monkeypatch.setattr(pulse_module, "run_pulse_agent", fake_run_pulse_agent)

    with caplog.at_level(logging.INFO):
        result = run_pulse(
            scope,
            CLOCK,
            _clients("normal_day"),
            trigger="pull",
            requested_at=CLOCK.now(),
        )

    assert result.critic_revised is True
    assert len(result.critic_reasons) == 1
    flagged_id, _reason = result.critic_reasons[0]

    revise_records = [
        r for r in caplog.records if r.message.startswith("critic_revise owner=")
    ]
    assert len(revise_records) == 1
    logged_message = revise_records[0].message
    assert flagged_id in logged_message
    assert "forced-for-test" in logged_message
    # ids + reason only — no item content (titles) leaked into the log
    for item in result.items:
        assert item.title not in logged_message
