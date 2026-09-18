from app.identity.types import (
    MatchCandidate,
    RawReference,
    Resolved,
    Unattributed,
    Unconfirmed,
)


def test_raw_reference_is_frozen_and_typed():
    ref = RawReference(
        source="slack",
        external_id="U123",
        handle="sbenali",
        email=None,
        display_name="Sarah Ben Youssef",
    )
    assert ref.source == "slack"
    assert ref.email is None


def test_resolved_is_frozen_and_typed():
    r = Resolved(person_id="p1", tier=1, confidence="verified")
    assert r.person_id == "p1"
    assert r.confidence == "verified"


def test_unconfirmed_carries_top_candidate_and_margin():
    top = MatchCandidate(person_id="p2", tier=4, score=0.8)
    u = Unconfirmed(reference_key="slack:handle:sbenali", top=top, margin=0.2)
    assert u.top.score == 0.8
    assert u.margin == 0.2


def test_unattributed_carries_raw_handle():
    u = Unattributed(reference_key="slack:handle:unknown", raw_handle="@unknown")
    assert u.raw_handle == "@unknown"
