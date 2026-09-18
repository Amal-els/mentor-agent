"""OwnerScope: the per-request owner_user_id + session carrier and the only
sanctioned way to query or write an owned model (design spec §11.3). No
function in app/identity/ may accept a bare Session — this object is what
they take instead. Enforced at CI time by a scope-leak test that greps for
bare session.query(...)/select(...) on owned models outside this module's
helpers."""

from dataclasses import dataclass

from sqlalchemy import Select, select
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class OwnerScope:
    owner_user_id: str
    session: Session

    def query(self, model) -> Select:
        return select(model).where(model.owner_user_id == self.owner_user_id)

    def add(self, instance) -> None:
        existing = getattr(instance, "owner_user_id", None)
        if existing is not None and existing != self.owner_user_id:
            raise ValueError(
                f"instance owner_user_id={existing!r} does not match "
                f"scope owner_user_id={self.owner_user_id!r}"
            )
        instance.owner_user_id = self.owner_user_id
        self.session.add(instance)

    def commit(self) -> None:
        self.session.commit()
