"""PairScope: the per-request report_user_id + session carrier and the
only sanctioned way to query or write a pair-scoped model (design spec
§3.2). Mirrors app.core.scope.OwnerScope's shape and role exactly, with
one structural difference: membership (who may construct a PairScope at
all) is resolved dynamically against Pair, not passed in by the caller —
see resolve_pair_scope below. No function in app/agenda/ may accept a
bare Session — this object is what they take instead. Enforced at CI
time by a scope-leak test mirroring identity resolution's Task 15."""

from dataclasses import dataclass

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from app.agenda.models import Pair


@dataclass(frozen=True)
class PairScope:
    report_user_id: str
    acting_user_id: str
    session: Session

    def query(self, model) -> Select:
        return select(model).where(model.report_user_id == self.report_user_id)

    def add(self, instance) -> None:
        existing = getattr(instance, "report_user_id", None)
        if existing is not None and existing != self.report_user_id:
            raise ValueError(
                f"instance report_user_id={existing!r} does not match "
                f"scope report_user_id={self.report_user_id!r}"
            )
        instance.report_user_id = self.report_user_id
        self.session.add(instance)

    def commit(self) -> None:
        self.session.commit()


def get_current_pair(session: Session, report_user_id: str) -> Pair | None:
    """The one query for "what is report_user_id's CURRENT Pair right
    now" (ended_at IS NULL) — every caller that needs this (resolve_pair_
    scope below, and app.sub_agents.agenda.sub_agents.synthesize.agent's
    created_by_user_id resolution) goes through here so the definition of
    "current" can't drift between two independently-written copies of the
    same query."""
    return session.execute(
        select(Pair).where(
            Pair.report_user_id == report_user_id, Pair.ended_at.is_(None)
        )
    ).scalar_one_or_none()


def get_active_pairs_for(session: Session, user_id: str) -> list[Pair]:
    """Every active Pair (ended_at IS NULL) user_id belongs to, as EITHER
    the report or the manager. Exists because a single-tenant deployment's
    one configured "owner" account is not always the report side of a
    relationship — confirmed live, one real setup has the owner configured
    as a manager instead (app.triggers.slack.slack_socket_listener's
    _resolve_agenda_pair_contexts is the first caller, resolving which
    report_user_id(s) a Slack mention concerns without assuming which role
    the owner plays). A user can appear in more than one row here (a
    manager with several reports), so callers must handle a list, not a
    single Pair, unlike get_current_pair above."""
    return list(
        session.execute(
            select(Pair).where(
                Pair.ended_at.is_(None),
                or_(Pair.report_user_id == user_id, Pair.manager_user_id == user_id),
            )
        )
        .scalars()
        .all()
    )


def resolve_pair_scope(
    session: Session, report_user_id: str, acting_user_id: str
) -> PairScope | None:
    """Full cutoff on manager transition (spec §3.1): a former manager
    gets None here, the same as a total stranger — there is no reduced
    read-only tier. Only the report or the CURRENT (ended_at IS NULL)
    manager for this report_user_id resolve to a scope."""
    if acting_user_id == report_user_id:
        return PairScope(
            report_user_id=report_user_id,
            acting_user_id=acting_user_id,
            session=session,
        )

    current_pair = get_current_pair(session, report_user_id)
    if current_pair is not None and current_pair.manager_user_id == acting_user_id:
        return PairScope(
            report_user_id=report_user_id,
            acting_user_id=acting_user_id,
            session=session,
        )

    return None
