"""Direct unit tests for matchers.py helpers and the ambiguity guard on
match_alias_email. golden.yaml exercises the tier ladder end-to-end but does
not exercise the alias-email ambiguity scenario (two people whose derived
alias local-part collides) or edge cases of the prefix-ratio metric — this
module covers those directly."""

import datetime

from app.identity import config, matchers
from app.identity.models import Person
from app.identity.roster import RosterSnapshot
from app.identity.types import RawReference


def _person(person_id: str, canonical_name: str, primary_email: str | None) -> Person:
    return Person(
        id=person_id,
        owner_user_id="test-owner",
        canonical_name=canonical_name,
        primary_email=primary_email,
        is_self=False,
        is_active=True,
        created_at=datetime.datetime.now(datetime.UTC),
    )


def _roster(*people: Person) -> RosterSnapshot:
    return RosterSnapshot(
        people=list(people), not_same_as=set(), relationships={}, roster_version=1
    )


# ---------------------------------------------------------------------------
# _alias_local_part
# ---------------------------------------------------------------------------


def test_alias_local_part_derives_first_initial_dot_surname():
    assert matchers._alias_local_part("sarah.benyoussef@acme.com") == "s.benyoussef"


def test_alias_local_part_returns_none_without_a_dot_in_local_part():
    assert matchers._alias_local_part("sarah@acme.com") is None


def test_alias_local_part_returns_none_when_first_or_rest_is_empty():
    assert matchers._alias_local_part(".benyoussef@acme.com") is None
    assert matchers._alias_local_part("sarah.@acme.com") is None


# ---------------------------------------------------------------------------
# _prefix_ratio
# ---------------------------------------------------------------------------


def test_prefix_ratio_full_prefix_match_scores_high():
    # "jamie" is a complete leading match against "jamieext".
    ratio = matchers._prefix_ratio("jamie", "jamieext")
    assert ratio == 2 * 5 / (5 + 8)


def test_prefix_ratio_shared_tail_only_scores_low():
    # "sbenali" vs "sarahbenali" share only a one-character prefix ("s")
    # despite sharing the "benali" tail — must not score as a strong match.
    ratio = matchers._prefix_ratio("sbenali", "sarahbenali")
    assert ratio < 0.2


def test_prefix_ratio_no_shared_prefix_is_zero():
    assert matchers._prefix_ratio("totallyunrelated", "jamieext") == 0.0


def test_prefix_ratio_empty_strings_are_zero():
    assert matchers._prefix_ratio("", "jamie") == 0.0
    assert matchers._prefix_ratio("jamie", "") == 0.0
    assert matchers._prefix_ratio("", "") == 0.0


def test_prefix_ratio_identical_strings_is_one():
    assert matchers._prefix_ratio("jamie", "jamie") == 1.0


def test_prefix_ratio_known_recall_tradeoff_first_initial_surname():
    # Documented trade-off: "jsmith" against "John Smith" (name_stem
    # "johnsmith") shares only the leading "j" — this is a real missed
    # match versus a full-string ratio, deliberately accepted (see the
    # docstring on _prefix_ratio) because it is a recall loss, not a
    # false-merge risk.
    ratio = matchers._prefix_ratio("jsmith", "johnsmith")
    assert ratio < config.TIER4_HANDLE_HEURISTIC_FLOOR


# ---------------------------------------------------------------------------
# match_alias_email ambiguity guard
# ---------------------------------------------------------------------------


def test_match_alias_email_returns_none_when_two_people_derive_the_same_alias():
    # "Sarah Benyoussef" and "Sami Benyoussef" both derive to
    # "s.benyoussef" from their primary_email. Must not silently pick one.
    roster = _roster(
        _person("p1", "Sarah Benyoussef", "sarah.benyoussef@acme.com"),
        _person("p2", "Sami Benyoussef", "sami.benyoussef@acme.com"),
    )
    ref = RawReference(
        source="linear",
        external_id="L1",
        handle=None,
        email="s.benyoussef@acme.io",
        display_name=None,
    )
    assert matchers.match_alias_email(ref, roster) is None


def test_match_alias_email_matches_when_derivation_is_unambiguous():
    roster = _roster(
        _person("p1", "Sarah Benyoussef", "sarah.benyoussef@acme.com"),
        _person("p2", "Mohamed Amine", "mohamed.amine@acme.com"),
    )
    ref = RawReference(
        source="linear",
        external_id="L1",
        handle=None,
        email="s.benyoussef@acme.io",
        display_name=None,
    )
    hit = matchers.match_alias_email(ref, roster)
    assert hit is not None
    assert hit.person_id == "p1"
    assert hit.tier == 2
    assert hit.score == config.TIER2_ALIAS_EMAIL_SCORE


def test_match_alias_email_returns_none_for_unknown_domain():
    roster = _roster(_person("p1", "Sarah Benyoussef", "sarah.benyoussef@acme.com"))
    ref = RawReference(
        source="linear",
        external_id="L1",
        handle=None,
        email="s.benyoussef@example.com",
        display_name=None,
    )
    assert matchers.match_alias_email(ref, roster) is None
