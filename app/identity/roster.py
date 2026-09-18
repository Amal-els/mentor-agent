"""The only DB read matchers see (design spec §6). One atomic snapshot so the
stamped roster_version always matches the data scored against it. Every read
goes through OwnerScope (spec §11.3) — no function here accepts a bare
Session."""

from dataclasses import dataclass

from app.core.scope import OwnerScope
from app.identity.models import NotSameAs, Person, PersonRelationship, RosterVersion


@dataclass(frozen=True)
class RosterSnapshot:
    people: list[Person]
    not_same_as: set[tuple[str, str]]
    relationships: dict[tuple[str, str], PersonRelationship]
    roster_version: int


def load_snapshot(scope: OwnerScope) -> RosterSnapshot:
    version_row = scope.session.get(RosterVersion, scope.owner_user_id)
    roster_version = version_row.version if version_row else 0

    people = list(scope.session.execute(scope.query(Person)).scalars().all())

    not_same_as = {
        (row.reference_key, row.person_id)
        for row in scope.session.execute(scope.query(NotSameAs)).scalars().all()
    }

    relationships = {
        (row.person_id_a, row.person_id_b): row
        for row in scope.session.execute(scope.query(PersonRelationship))
        .scalars()
        .all()
    }

    return RosterSnapshot(
        people=people,
        not_same_as=not_same_as,
        relationships=relationships,
        roster_version=roster_version,
    )
