from app.identity.types import Unattributed
from app.sub_agents.dossier.sub_agents.gather.agent import DossierContext, ResolvedAttendee
from app.sub_agents.dossier.sub_agents.synthesize.agent import (
    OUTPUT_KEY,
    TalkingPoint,
    drop_unsourced_talking_points,
    synthesize_dossier,
)


def _make_context(
    agenda_carryover=None, open_commitments=None, blocked_work_items=None
) -> DossierContext:
    return DossierContext(
        event={"external_id": "evt-1", "title": "Sync"},
        resolved_attendees=[
            ResolvedAttendee(
                raw={"display_name": "Sam"},
                resolution=Unattributed(reference_key="k1", raw_handle="sam"),
            )
        ],
        agenda_carryover=agenda_carryover,
        raw_signals={},
        open_commitments=open_commitments or [],
        blocked_work_items=blocked_work_items or [],
    )


def test_synthesize_dossier_reads_result_from_session_state(monkeypatch):
    # synthesize_dossier must consume run_agent_sync's real return shape —
    # the full final session state, not a pre-parsed dossier dict — by
    # reading state[OUTPUT_KEY]. This stubs run_agent_sync to prove that
    # wiring without a live LLM call (Task 10 owns golden-fixture LLM
    # tests; this only exercises the pure orchestration around the call).
    def fake_run_agent_sync(agent, initial_state, kickoff_text="Begin."):
        assert "context_json" in initial_state
        return {
            OUTPUT_KEY: {
                "who": ["Sam External"],
                "why_now": "Renewal next week.",
                "talking_points": [
                    {"text": "Ship the migration", "source_link": "https://linear.app/x/1"},
                    {"text": "Unfounded guess", "source_link": None},
                ],
                "promised_and_not_delivered": [],
                "suggested_opener": None,
                "short_version": False,
            }
        }

    monkeypatch.setattr(
        "app.core.adk_runner.run_agent_sync", fake_run_agent_sync
    )

    card = synthesize_dossier(_make_context(), llm_agent=object())

    assert card.who == ["Sam External"]
    assert [p.text for p in card.talking_points] == ["Ship the migration"]
    assert card.nothing_to_prep is False


def test_synthesize_dossier_prefers_agenda_carryover_over_model_points(monkeypatch):
    class _FakeAgendaItem:
        def __init__(self, text, source_link):
            self.id = "item-1"
            self.text = text
            self.source = "manual"
            self.source_link = source_link
            self.status = "open"
            self.surfaced_count = 0

    def fake_run_agent_sync(agent, initial_state, kickoff_text="Begin."):
        return {
            OUTPUT_KEY: {
                "who": ["Sam External"],
                "why_now": "Recurring 1:1.",
                "talking_points": [
                    {"text": "Model-invented point", "source_link": "https://example.com"},
                ],
                "promised_and_not_delivered": [],
                "suggested_opener": None,
                "short_version": False,
            }
        }

    monkeypatch.setattr(
        "app.core.adk_runner.run_agent_sync", fake_run_agent_sync
    )

    carryover = [_FakeAgendaItem("Reuse this verbatim", "https://notion.so/y")]
    card = synthesize_dossier(_make_context(agenda_carryover=carryover), llm_agent=object())

    assert [p.text for p in card.talking_points] == ["Reuse this verbatim"]


def test_synthesize_dossier_keeps_agenda_carryover_with_a_non_url_source_link(
    monkeypatch,
):
    # Regression, found live: AgendaItem.source_link is overloaded
    # (app/agenda/store.py's append_ledger_item sets it to the mirrored
    # Commitment/Accomplishment row's own internal id for ledger-sourced
    # items, not a clickable URL). The http(s)-only provenance check must
    # only ever apply to FRESH LLM-synthesized points, never to trusted
    # agenda carryover — applying it unconditionally silently dropped
    # every commitment-sourced carryover item, collapsing a real agenda
    # into an empty "nothing to prep" card.
    class _FakeAgendaItem:
        def __init__(self, text, source_link):
            self.id = "item-1"
            self.text = text
            self.source = "commitment_ledger"
            self.source_link = source_link
            self.status = "open"
            self.surfaced_count = 0

    def fake_run_agent_sync(agent, initial_state, kickoff_text="Begin."):
        return {
            OUTPUT_KEY: {
                "who": ["Sam External"],
                "why_now": "Recurring 1:1.",
                "talking_points": [],
                "promised_and_not_delivered": [],
                "suggested_opener": None,
                "short_version": False,
            }
        }

    monkeypatch.setattr("app.core.adk_runner.run_agent_sync", fake_run_agent_sync)

    carryover = [
        _FakeAgendaItem(
            "Bob will finalize migration tests by Thursday",
            "3311b5bc-97c2-49a2-9f75-5b8303477024",
        )
    ]
    card = synthesize_dossier(_make_context(agenda_carryover=carryover), llm_agent=object())

    assert [p.text for p in card.talking_points] == [
        "Bob will finalize migration tests by Thursday"
    ]
    assert card.nothing_to_prep is False


def test_synthesize_dossier_flags_nothing_to_prep_when_all_unsourced(monkeypatch):
    def fake_run_agent_sync(agent, initial_state, kickoff_text="Begin."):
        return {
            OUTPUT_KEY: {
                "who": ["Sam External"],
                "why_now": "Nothing new.",
                "talking_points": [{"text": "Unfounded guess", "source_link": None}],
                "promised_and_not_delivered": [],
                "suggested_opener": None,
                "short_version": False,
            }
        }

    monkeypatch.setattr(
        "app.core.adk_runner.run_agent_sync", fake_run_agent_sync
    )

    card = synthesize_dossier(_make_context(), llm_agent=object())

    assert card.talking_points == []
    assert card.nothing_to_prep is True


def test_synthesize_dossier_prefers_real_commitments_over_model_guess(monkeypatch):
    def fake_run_agent_sync(agent, initial_state, kickoff_text="Begin."):
        return {
            OUTPUT_KEY: {
                "who": ["Sam External"],
                "why_now": "Renewal next week.",
                "talking_points": [],
                "promised_and_not_delivered": ["Model's ungrounded guess"],
                "suggested_opener": None,
                "short_version": False,
            }
        }

    monkeypatch.setattr("app.core.adk_runner.run_agent_sync", fake_run_agent_sync)

    open_commitments = [
        {
            "id": "c1",
            "description": "Send the updated timeline",
            "promised_to_person_id": "person-sam",
            "promised_to_name": "Sam External",
            "promised_at": "2026-08-01T00:00:00+00:00",
            "due_at": "2026-08-10T00:00:00+00:00",
            "overdue": True,
        }
    ]
    card = synthesize_dossier(
        _make_context(open_commitments=open_commitments), llm_agent=object()
    )

    assert card.promised_and_not_delivered == [
        "Send the updated timeline (with Sam External) — overdue"
    ]


def test_synthesize_dossier_formats_blocked_work_items_deterministically(monkeypatch):
    def fake_run_agent_sync(agent, initial_state, kickoff_text="Begin."):
        return {
            OUTPUT_KEY: {
                "who": ["Sam External"],
                "why_now": "Renewal next week.",
                "talking_points": [],
                "promised_and_not_delivered": [],
                "suggested_opener": None,
                "short_version": False,
            }
        }

    monkeypatch.setattr("app.core.adk_runner.run_agent_sync", fake_run_agent_sync)

    blocked_work_items = [
        {
            "id": "wi-1",
            "external_id": "MENT-214",
            "title": "Fix flaky roster test",
            "url": "https://linear.app/MENT-214",
            "person_id": "person-sam",
            "person_name": "Sam External",
            "days_stale": 3,
        }
    ]
    card = synthesize_dossier(
        _make_context(blocked_work_items=blocked_work_items), llm_agent=object()
    )

    assert card.blockers == [
        "MENT-214 — Fix flaky roster test (blocked — Sam External), 3d"
    ]
    # a blocker alone is still something to prep for — this must not
    # collapse to "nothing to prep" just because talking_points and
    # promised_and_not_delivered are both empty.
    assert card.nothing_to_prep is False


def test_synthesize_dossier_nothing_to_prep_requires_no_blockers_too(monkeypatch):
    def fake_run_agent_sync(agent, initial_state, kickoff_text="Begin."):
        return {
            OUTPUT_KEY: {
                "who": ["Sam External"],
                "why_now": "Renewal next week.",
                "talking_points": [],
                "promised_and_not_delivered": [],
                "suggested_opener": None,
                "short_version": False,
            }
        }

    monkeypatch.setattr("app.core.adk_runner.run_agent_sync", fake_run_agent_sync)

    card = synthesize_dossier(_make_context(), llm_agent=object())

    assert card.blockers == []
    assert card.nothing_to_prep is True


def test_drops_talking_point_whose_source_link_is_not_a_real_url():
    # Caught live: the model sometimes copies a Commitment row's raw "id"
    # field (a bare UUID, not a URL) into source_link when it phrases an
    # open_commitments item as a talking point instead of leaving it to
    # section 4. Truthiness alone let this through before.
    points = [
        TalkingPoint(text="Real point", source_link="https://linear.app/x/1"),
        TalkingPoint(
            text="Bob will finalize migration tests by Thursday",
            source_link="3311b5bc-97c2-49a2-9f75-5b8303477024",
        ),
    ]
    kept = drop_unsourced_talking_points(points)
    assert [p.text for p in kept] == ["Real point"]


def test_drops_talking_points_with_no_source_link():
    points = [
        TalkingPoint(text="Ship the migration", source_link="https://linear.app/x/1"),
        TalkingPoint(text="Unfounded guess", source_link=None),
        TalkingPoint(text="Renewal date", source_link="https://notion.so/y"),
    ]
    kept = drop_unsourced_talking_points(points)
    assert [p.text for p in kept] == ["Ship the migration", "Renewal date"]


def test_all_dropped_when_none_sourced():
    points = [TalkingPoint(text="Guess one", source_link=None)]
    assert drop_unsourced_talking_points(points) == []
