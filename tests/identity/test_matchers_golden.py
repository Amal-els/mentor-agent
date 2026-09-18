import datetime

import yaml

from app.identity import matchers
from app.identity.models import Person
from app.identity.roster import RosterSnapshot
from app.identity.types import RawReference


def _load_golden():
    with open("tests/identity/golden.yaml") as f:
        return yaml.safe_load(f)


def _build_roster(golden) -> RosterSnapshot:
    people = [
        Person(
            id=p["id"],
            owner_user_id="golden-set-owner",
            canonical_name=p["canonical_name"],
            primary_email=p.get("primary_email"),
            is_self=False,
            is_active=p.get("is_active", True),
            created_at=datetime.datetime.now(datetime.UTC),
        )
        for p in golden["roster"]
    ]
    return RosterSnapshot(
        people=people, not_same_as=set(), relationships={}, roster_version=1
    )


def _resolve_via_matchers(ref: RawReference, roster: RosterSnapshot):
    """Runs the full tier ladder + gates, mirroring what resolve.py will do,
    without touching the DB — this is what makes the golden set DB-free."""
    for matcher in (
        matchers.match_idp,
        matchers.match_primary_email,
        matchers.match_alias_email,
    ):
        hit = matcher(ref, roster)
        if hit is not None:
            return ("resolved", hit.person_id, "verified")

    candidates: list = []
    exact_name = matchers.match_exact_name(ref, roster)
    if exact_name is not None:
        candidates.append(exact_name)
    candidates.extend(matchers.match_handle_heuristic(ref, roster))
    candidates.extend(matchers.match_fuzzy_name(ref, roster))
    candidates = matchers.apply_context_priors(candidates, ref, roster)
    candidates.sort(key=lambda c: c.score, reverse=True)

    if not candidates:
        return ("unattributed", None, None)

    top = candidates[0]
    second_score = candidates[1].score if len(candidates) > 1 else 0.0
    margin = top.score - second_score
    outcome = matchers.gate(top.score, margin, roster_size=len(roster.people))

    if outcome == "auto_link":
        return ("resolved", top.person_id, "inferred")
    if outcome == "ask":
        return ("unconfirmed", top.person_id, None)
    return ("unattributed", None, None)


def test_golden_set_precision_and_false_merge_gate():
    golden = _load_golden()
    roster = _build_roster(golden)

    total_resolved = 0
    correct_resolved = 0
    false_merges = 0

    for case in golden["cases"]:
        ref = RawReference(
            source=case["reference"]["source"],
            external_id=case["reference"].get("external_id"),
            handle=case["reference"].get("handle"),
            email=case["reference"].get("email"),
            display_name=case["reference"].get("display_name"),
        )
        kind, person_id, _confidence = _resolve_via_matchers(ref, roster)
        expect = case["expect"]

        assert (
            kind == expect["kind"]
        ), f"{case['name']}: got {kind}, want {expect['kind']}"

        if kind == "resolved":
            total_resolved += 1
            if person_id == expect.get("person_id"):
                correct_resolved += 1
            else:
                false_merges += 1
        elif kind == "unconfirmed" and "top_person_id" in expect:
            assert person_id == expect["top_person_id"], case["name"]

    assert false_merges == 0
    if total_resolved:
        assert correct_resolved / total_resolved >= 0.99
