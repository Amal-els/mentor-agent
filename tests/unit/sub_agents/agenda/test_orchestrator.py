import asyncio
import datetime
from unittest.mock import patch

import pytest
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agenda.scope import resolve_pair_scope
from app.core.clock import FrozenClock
from app.core.scope import OwnerScope
from app.sub_agents.agenda.agent import RollingAgendaOrchestrator

NOW = datetime.datetime(2026, 8, 14, tzinfo=datetime.UTC)


def _run(agent, initial_state):
    async def _go():
        session_service = InMemorySessionService()
        await session_service.create_session(
            app_name="test", user_id="u", session_id="s", state=initial_state
        )
        runner = Runner(agent=agent, app_name="test", session_service=session_service)
        async for _event in runner.run_async(
            user_id="u",
            session_id="s",
            new_message=types.Content(
                role="user", parts=[types.Part.from_text(text="go")]
            ),
        ):
            pass
        session = await session_service.get_session(
            app_name="test", user_id="u", session_id="s"
        )
        return dict(session.state)

    return asyncio.run(_go())


def test_manual_note_trigger_writes_a_note(db_session, make_pair):
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)
    orchestrator = RollingAgendaOrchestrator(
        name="agenda_orchestrator",
        pair_scope=pair_scope,
        owner_scope=owner_scope,
        clock=FrozenClock(at=NOW),
    )

    _run(
        orchestrator,
        {
            "trigger_type": "manual_note",
            "acting_user_id": pair.report_user_id,
            "text": "ask about the roadmap",
            "visibility": "shared",
        },
    )

    from app.agenda.models import AgendaItem

    rows = (
        db_session.query(AgendaItem).filter_by(report_user_id=pair.report_user_id).all()
    )
    assert len(rows) == 1
    assert rows[0].text == "ask about the roadmap"


def test_ledger_event_trigger_routes_correctly(db_session, make_pair):
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)
    orchestrator = RollingAgendaOrchestrator(
        name="agenda_orchestrator",
        pair_scope=pair_scope,
        owner_scope=owner_scope,
        clock=FrozenClock(at=NOW),
    )

    _run(
        orchestrator,
        {
            "trigger_type": "ledger_event",
            "kind": "jira_blocker",
            "text": "unblock the CI flake",
            "source": "jira",
            "source_link": "JIRA-123",
            "visibility": "shared",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
    )

    from app.agenda.models import AgendaItem

    rows = (
        db_session.query(AgendaItem).filter_by(report_user_id=pair.report_user_id).all()
    )
    assert len(rows) == 1
    assert rows[0].source == "jira"


def test_ledger_event_commitment_kind_writes_through_the_ledger_not_the_agenda_directly(
    db_session, make_pair
):
    """Guards the kind-based split inside the ledger_event branch itself:
    kind="commitment" must go through append_ledger_item (durable
    Commitment row first, agenda mirror second, per store.py's
    append_ledger_item docstring) rather than append_agenda_item
    directly. test_ledger_event_trigger_routes_correctly above only
    exercises kind="jira_blocker" (the append_agenda_item side of the
    split) — this covers the other side, so a bug that always calls
    append_agenda_item regardless of kind wouldn't slip past both tests."""
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)
    orchestrator = RollingAgendaOrchestrator(
        name="agenda_orchestrator",
        pair_scope=pair_scope,
        owner_scope=owner_scope,
        clock=FrozenClock(at=NOW),
    )

    _run(
        orchestrator,
        {
            "trigger_type": "ledger_event",
            "kind": "commitment",
            "text": "ship the roadmap doc",
            "source": "manual",
            "visibility": "shared",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
    )

    from app.agenda.models import AgendaItem
    from app.core.models import Commitment

    commitments = (
        db_session.query(Commitment)
        .filter_by(source_reference_key=f"agenda:{pair.report_user_id}")
        .all()
    )
    assert len(commitments) == 1
    assert commitments[0].description == "ship the roadmap doc"

    rows = (
        db_session.query(AgendaItem).filter_by(report_user_id=pair.report_user_id).all()
    )
    assert len(rows) == 1
    assert rows[0].source == "commitment_ledger"
    assert rows[0].source_link == commitments[0].id


def test_ledger_event_accomplishment_kind_without_occurred_at_raises_clear_valueerror(
    db_session, make_pair
):
    """kind="accomplishment" requires occurred_at (store.py's
    _parse_occurred_at has no default for it, unlike due_at on the
    commitment path). Without a boundary check here, a trigger payload
    that omits occurred_at would fall all the way through to
    datetime.fromisoformat(None) inside store.py and blow up with an
    opaque TypeError. The orchestrator should catch this at the same
    layer it already validates trigger_type itself, and raise a clear
    ValueError instead."""
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)
    orchestrator = RollingAgendaOrchestrator(
        name="agenda_orchestrator",
        pair_scope=pair_scope,
        owner_scope=owner_scope,
        clock=FrozenClock(at=NOW),
    )

    with pytest.raises(ValueError, match="occurred_at"):
        _run(
            orchestrator,
            {
                "trigger_type": "ledger_event",
                "kind": "accomplishment",
                "text": "shipped the roadmap doc",
                "source": "manual",
                "visibility": "shared",
                "created_by_user_id": pair.report_user_id,
                "created_by_role": "report",
            },
        )


def test_ledger_event_accomplishment_kind_writes_through_the_ledger_not_the_agenda_directly(
    db_session, make_pair
):
    """Mirrors the commitment-kind test above, but for the accomplishment
    path: kind="accomplishment" with a valid occurred_at must go through
    append_ledger_item (durable Accomplishment row first, agenda mirror
    second) rather than append_agenda_item directly."""
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)
    orchestrator = RollingAgendaOrchestrator(
        name="agenda_orchestrator",
        pair_scope=pair_scope,
        owner_scope=owner_scope,
        clock=FrozenClock(at=NOW),
    )

    _run(
        orchestrator,
        {
            "trigger_type": "ledger_event",
            "kind": "accomplishment",
            "text": "shipped the roadmap doc",
            "source": "manual",
            "visibility": "shared",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
            "occurred_at": NOW.isoformat(),
        },
    )

    from app.agenda.models import Accomplishment, AgendaItem

    accomplishments = (
        db_session.query(Accomplishment)
        .filter_by(source_reference_key=f"agenda:{pair.report_user_id}")
        .all()
    )
    assert len(accomplishments) == 1
    assert accomplishments[0].description == "shipped the roadmap doc"

    rows = (
        db_session.query(AgendaItem).filter_by(report_user_id=pair.report_user_id).all()
    )
    assert len(rows) == 1
    assert rows[0].source == "accomplishment_ledger"
    assert rows[0].source_link == accomplishments[0].id


def test_meeting_end_trigger_delegates_to_run_post_meeting_flow(db_session, make_pair):
    """meeting_end must call run_post_meeting_flow with the meeting_id
    pulled from state plus the orchestrator's own pair_scope/owner_scope/
    clock, and the returned payload must round-trip through ADK's session
    (via the Event/EventActions state_delta) into agenda_payload — not
    just be produced by the mocked call."""
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)
    clock = FrozenClock(at=NOW)
    orchestrator = RollingAgendaOrchestrator(
        name="agenda_orchestrator",
        pair_scope=pair_scope,
        owner_scope=owner_scope,
        clock=clock,
    )

    fake_payload = {"components": [], "meeting_id": "meeting-42"}

    with patch(
        "app.sub_agents.agenda.agent.run_post_meeting_flow", return_value=fake_payload
    ) as mock_flow:
        final_state = _run(
            orchestrator,
            {
                "trigger_type": "meeting_end",
                "meeting_id": "meeting-42",
            },
        )

    mock_flow.assert_called_once_with(
        "meeting-42", pair_scope, owner_scope, clock, transcript_text=None
    )
    assert final_state["agenda_payload"] == fake_payload


def test_meeting_end_trigger_forwards_an_explicit_transcript_text(
    db_session, make_pair
):
    """The /ui demo dashboard (and any future real trigger source) needs
    a way to hand Capture real content directly, since fetch_transcript
    has no real backing store on its own — confirmed live that without
    this, a real meeting_end run produces zero agenda items."""
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)
    clock = FrozenClock(at=NOW)
    orchestrator = RollingAgendaOrchestrator(
        name="agenda_orchestrator",
        pair_scope=pair_scope,
        owner_scope=owner_scope,
        clock=clock,
    )

    with patch(
        "app.sub_agents.agenda.agent.run_post_meeting_flow",
        return_value={"components": []},
    ) as mock_flow:
        _run(
            orchestrator,
            {
                "trigger_type": "meeting_end",
                "meeting_id": "meeting-42",
                "transcript_text": "We agreed to ship on Friday.",
            },
        )

    mock_flow.assert_called_once_with(
        "meeting-42",
        pair_scope,
        owner_scope,
        clock,
        transcript_text="We agreed to ship on Friday.",
    )
