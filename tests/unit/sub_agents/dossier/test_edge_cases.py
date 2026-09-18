"""Task 12: golden-fixture edge case tests for spec §6's edge-case table.

Each fixture under tests/fixtures/dossier/*.json documents an example
SynthesizeOutput-shaped LLM response (the exact dict synthesize_dossier
reads back out of session state via OUTPUT_KEY). Tests load the fixture
with json.load and drive it through the REAL pipeline
(gather_dossier_context -> synthesize_dossier), patching only
app.core.adk_runner.run_agent_sync so no live LLM call is made — the same
pattern Task 6/8 established (see tests/unit/sub_agents/dossier/
sub_agents/synthesize/test_agent.py and tests/unit/sub_agents/dossier/
test_agent.py)."""

import datetime
import json
import uuid
from pathlib import Path

from app.agenda.models import Pair
from app.agenda.scope import resolve_pair_scope
from app.agenda.store import append_agenda_item
from app.core.clock import FrozenClock
from app.core.models import User
from app.core.scope import OwnerScope
from app.delivery.cards import build_dossier_card
from app.identity import config as identity_config
from app.identity.models import Identity, Person
from app.identity.normalize import build_reference_key
from app.ingest.live_source import _adapt_calendar_event
from app.sub_agents.dossier.sub_agents.gather.agent import gather_dossier_context
from app.sub_agents.dossier.sub_agents.synthesize.agent import (
    OUTPUT_KEY,
    synthesize_dossier,
)


def _seed_identity(
    scope: OwnerScope,
    person_id: str,
    person_name: str,
    external_id: str,
    now: datetime.datetime,
) -> None:
    """Pre-seeds a real Person + Identity so resolve() returns Resolved
    (via _resolve's own Identity.reference_key cache hit) for an attendee
    dict built as {"source": "calendar", "external_id": external_id, ...}
    — the real event->Pair matching (_resolve_pair_scope_for_attendees)
    needs a real person_id on the resolved attendee, not Unattributed."""
    scope.add(Person(id=person_id, canonical_name=person_name, created_at=now))
    scope.commit()
    scope.add(
        Identity(
            id=str(uuid.uuid4()),
            person_id=person_id,
            source="calendar",
            external_id=external_id,
            reference_key=build_reference_key("calendar", external_id, None, None),
            key_version=identity_config.KEY_VERSION,
            tier=1,
            confidence="verified",
            verified_by="user_confirmed",
            handle=None,
            email=None,
            display_name=person_name,
            provenance={},
            first_seen=now,
            last_seen=now,
        )
    )
    scope.commit()

NOW = datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC)

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "dossier"


def _load_fixture(name: str) -> dict:
    with (FIXTURES_DIR / name).open(encoding="utf-8") as fh:
        return json.load(fh)


class _EmptyClient:
    def fetch(self, window, owner_user_id):
        return []


def _fake_run_agent_sync_returning(fixture: dict):
    def _fake(agent, initial_state, kickoff_text="Begin."):
        assert "context_json" in initial_state
        return {OUTPUT_KEY: fixture}

    return _fake


def test_no_agenda_no_history_produces_nothing_to_prep(pg_session, monkeypatch):
    # spec §6 "nothing to prep": no agenda carryover, no signals, and an
    # LLM response with empty talking_points/promised_and_not_delivered ->
    # DossierCard.nothing_to_prep must be True. Exercised through the real
    # gather_dossier_context -> synthesize_dossier pipeline (not a direct
    # DossierCard construction) so the flag's actual computation
    # (len(talking_points) == 0 and not promised_and_not_delivered) is
    # what's under test, not just its dataclass field.
    fixture = _load_fixture("nothing_to_prep.json")
    monkeypatch.setattr(
        "app.core.adk_runner.run_agent_sync", _fake_run_agent_sync_returning(fixture)
    )

    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)

    event = {
        "external_id": "evt-empty",
        "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
        "is_recurring": False,
        "attendees": [],
    }
    context = gather_dossier_context(
        event, scope, clock, {"slack": _EmptyClient(), "linear": _EmptyClient()}
    )
    assert context.agenda_carryover is None

    card = synthesize_dossier(context, llm_agent=object())

    assert card.talking_points == []
    assert card.nothing_to_prep is True


def test_meeting_in_4_minutes_still_qualifies_as_t_minus_15_window():
    from app.triggers.dossier.dossier_scheduler import _is_t_minus_15

    starts_at = (NOW + datetime.timedelta(minutes=4)).isoformat()
    assert _is_t_minus_15(starts_at, NOW) is True


def test_short_version_flag_flows_through_synthesize_and_card_render(
    pg_session, monkeypatch
):
    # spec §6 "short version": the LLM may flag short_version=true (e.g.
    # low signal volume). synthesize_dossier must pass the flag through
    # unchanged onto DossierCard, and build_dossier_card (Task 7) must not
    # crash or drop the rest of the card's content when rendering it.
    fixture = _load_fixture("short_version.json")
    monkeypatch.setattr(
        "app.core.adk_runner.run_agent_sync", _fake_run_agent_sync_returning(fixture)
    )

    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)

    event = {
        "external_id": "evt-short",
        "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
        "is_recurring": False,
        "attendees": [],
    }
    context = gather_dossier_context(
        event, scope, clock, {"slack": _EmptyClient(), "linear": _EmptyClient()}
    )

    card = synthesize_dossier(context, llm_agent=object())

    assert card.short_version is True
    assert card.who == fixture["who"]
    assert [p.text for p in card.talking_points] == [
        fixture["talking_points"][0]["text"]
    ]

    blocks = build_dossier_card(card)
    assert isinstance(blocks, list) and len(blocks) > 0
    # NOTE (real gap, not worked around here): build_dossier_card does not
    # currently branch on card.short_version at all -- it renders the same
    # blocks regardless of the flag's value. The flag is plumbed all the
    # way from the LLM output through synthesize_dossier onto DossierCard,
    # but nothing downstream (Task 7's delivery renderer, Task 9's pull
    # path, Task 10's UI payload) reads it. This test only asserts current
    # behavior (flows through, doesn't crash) -- it is not this task's job
    # to add short-version-specific rendering to Task 7.


def test_external_attendee_with_no_history_produces_sensible_card(
    pg_session, monkeypatch
):
    # spec §6 "external attendee, no history": an external calendar
    # attendee with no prior Slack/Linear signal. dossier_synthesize.md
    # instructs the model to still populate why_now/suggested_opener for
    # external or high-stakes attendees even with empty talking_points.
    # This exercises the real attendee-resolution path (Unattributed, via
    # app.identity.resolve) rather than hand-building a ResolvedAttendee.
    fixture = _load_fixture("external_no_history.json")
    monkeypatch.setattr(
        "app.core.adk_runner.run_agent_sync", _fake_run_agent_sync_returning(fixture)
    )

    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)

    event = {
        "external_id": "evt-external",
        "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
        "is_recurring": False,
        "attendees": [
            {
                "source": "calendar",
                "external_id": "jordan@prospect.example.com",
                "email": "jordan@prospect.example.com",
                "display_name": "Jordan Prospect",
            }
        ],
    }
    context = gather_dossier_context(
        event, scope, clock, {"slack": _EmptyClient(), "linear": _EmptyClient()}
    )
    assert len(context.resolved_attendees) == 1
    assert context.agenda_carryover is None

    card = synthesize_dossier(context, llm_agent=object())

    assert card.talking_points == []
    assert card.why_now == fixture["why_now"]
    assert card.suggested_opener == fixture["suggested_opener"]
    # No sourced talking points and no promised-and-not-delivered items ->
    # still counts as "nothing to prep" (there is nothing concrete to
    # discuss), even though a suggested_opener is present -- the opener is
    # a conversational aid, not a prep item, and nothing_to_prep's
    # definition (synthesize/agent.py) only looks at talking_points and
    # promised_and_not_delivered.
    assert card.nothing_to_prep is True

    blocks = build_dossier_card(card)
    assert isinstance(blocks, list) and len(blocks) > 0


def test_agenda_carryover_overrides_llm_talking_points_end_to_end(
    pg_session, monkeypatch
):
    # spec §6 "agenda carryover": for a recurring 1:1, rolling agenda items
    # already on file must be reused VERBATIM instead of whatever the LLM
    # invents -- this is Task 5 (gather) and Task 6 (synthesize)'s cross-
    # task contract. The fake run_agent_sync deliberately returns DIFFERENT
    # talking points than the seeded agenda item, so a passing assertion
    # here proves the override actually beats the LLM's output rather than
    # merely that carryover data exists.
    fixture = _load_fixture("agenda_carryover.json")
    monkeypatch.setattr(
        "app.core.adk_runner.run_agent_sync", _fake_run_agent_sync_returning(fixture)
    )

    report = User(id=str(uuid.uuid4()), created_at=NOW)
    manager = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add_all([report, manager])
    pg_session.commit()

    pair = Pair(
        id=str(uuid.uuid4()),
        report_user_id=report.id,
        manager_user_id=manager.id,
        started_at=NOW,
        ended_at=None,
    )
    pg_session.add(pair)
    pg_session.commit()

    clock = FrozenClock(at=NOW)
    # get_current_pair (used both by resolve_pair_scope below and by
    # _resolve_pair_scope_for_attendees, the real event->Pair matching
    # gather_dossier_context now uses) needs a real Pair row to exist.
    # Seed a real AgendaItem via the real store function (not a hand-built
    # row) so the write path matches production.
    self_scope = resolve_pair_scope(pg_session, report.id, report.id)
    assert self_scope is not None
    seeded_item = append_agenda_item(
        self_scope,
        {
            "text": "Reuse this verbatim from the rolling agenda",
            "source": "manual",
            "source_link": "https://notion.so/real-agenda-item",
            "visibility": "shared",
            "created_by_user_id": report.id,
            "created_by_role": "report",
        },
        clock,
    )

    scope = OwnerScope(owner_user_id=report.id, session=pg_session)
    # The real event->Pair mapping (gather/agent.py's
    # _resolve_pair_scope_for_attendees) needs an attendee that actually
    # resolves to the manager, not an empty attendee list.
    _seed_identity(scope, manager.id, "Manager", "mgr-ext-id", NOW)
    event = {
        "external_id": "evt-carryover",
        "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
        "is_recurring": True,
        "attendees": [
            {"source": "calendar", "external_id": "mgr-ext-id", "display_name": "Manager"}
        ],
    }
    context = gather_dossier_context(
        event, scope, clock, {"slack": _EmptyClient(), "linear": _EmptyClient()}
    )
    assert context.agenda_carryover is not None
    assert [item.text for item in context.agenda_carryover] == [seeded_item.text]

    card = synthesize_dossier(context, llm_agent=object())

    # The LLM fixture's talking point text must NOT appear -- only the
    # seeded agenda item's text survives.
    fixture_texts = {tp["text"] for tp in fixture["talking_points"]}
    card_texts = [p.text for p in card.talking_points]
    assert card_texts == [seeded_item.text]
    assert not (set(card_texts) & fixture_texts)


def test_agenda_carryover_populated_from_a_real_adapted_calendar_event(pg_session):
    # Regression test for finding 1 (whole-branch review): every OTHER
    # dossier test builds a synthetic {"is_recurring": True, ...} event
    # dict by hand -- that shape never comes out of production.
    # _adapt_calendar_event (app/ingest/live_source.py) is what actually
    # produces a real event dict, and it emits "series_id" (from Google's
    # own recurringEventId), never "is_recurring" at all. This test drives
    # a REAL adapter-shaped event (recurringEventId set, no is_recurring
    # key anywhere) through the real gather_dossier_context and proves
    # agenda_carryover still gets populated -- proving the fix actually
    # works against production's real shape, not just the synthetic shape
    # that was masking the bug in every other test.
    report = User(id=str(uuid.uuid4()), created_at=NOW)
    manager = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add_all([report, manager])
    pg_session.commit()

    pair = Pair(
        id=str(uuid.uuid4()),
        report_user_id=report.id,
        manager_user_id=manager.id,
        started_at=NOW,
        ended_at=None,
    )
    pg_session.add(pair)
    pg_session.commit()

    clock = FrozenClock(at=NOW)
    self_scope = resolve_pair_scope(pg_session, report.id, report.id)
    assert self_scope is not None
    seeded_item = append_agenda_item(
        self_scope,
        {
            "text": "Carried over via a real adapter-shaped event",
            "source": "manual",
            "source_link": "https://notion.so/real-agenda-item-2",
            "visibility": "shared",
            "created_by_user_id": report.id,
            "created_by_role": "report",
        },
        clock,
    )

    raw_calendar_event = {
        "id": "evt-real-adapted",
        "summary": "Weekly 1:1",
        "start": {"dateTime": (NOW + datetime.timedelta(minutes=15)).isoformat()},
        "end": {"dateTime": (NOW + datetime.timedelta(minutes=45)).isoformat()},
        "status": "confirmed",
        "organizer": {"email": "manager@example.com"},
        # The real signal a live Google Calendar MCP response sets for a
        # recurring instance -- _adapt_calendar_event maps this to
        # "series_id", never to an "is_recurring" boolean.
        "recurringEventId": "series-weekly-1-1",
    }
    event = _adapt_calendar_event(raw_calendar_event)
    assert "is_recurring" not in event
    assert event["series_id"] == "series-weekly-1-1"
    # _adapt_calendar_event doesn't itself map attendees at all (a separate,
    # real gap — out of scope here); set it directly so this test can still
    # exercise the real event->Pair matching gather_dossier_context now does.
    event["attendees"] = [
        {"source": "calendar", "external_id": "mgr-ext-id-2", "display_name": "Manager"}
    ]

    scope = OwnerScope(owner_user_id=report.id, session=pg_session)
    _seed_identity(scope, manager.id, "Manager", "mgr-ext-id-2", NOW)
    context = gather_dossier_context(
        event, scope, clock, {"slack": _EmptyClient(), "linear": _EmptyClient()}
    )

    assert context.agenda_carryover is not None
    assert [item.text for item in context.agenda_carryover] == [seeded_item.text]
