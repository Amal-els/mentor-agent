"""Schema for the shared, pair-scoped rolling 1-on-1 agenda (design spec
§5). Every table here is queried through PairScope (app/agenda/scope.py),
never a bare Session — Accomplishment is the one exception, owner-scoped
like Commitment since it's a durable per-user ledger, not agenda state."""

import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Pair(Base):
    """The report's manager history. At most one row per report_user_id
    has ended_at IS NULL — the current manager. Created/transitioned only
    by app.ingest.notion_pair_sync.sync_notion_pair_edge, never through
    PairScope, since a scope object requires a Pair to already exist."""

    __tablename__ = "pairs"
    __table_args__ = (
        Index("ix_pair_report", "report_user_id"),
        Index(
            "uq_pair_one_active_per_report",
            "report_user_id",
            unique=True,
            sqlite_where=text("ended_at IS NULL"),
            postgresql_where=text("ended_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    report_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    manager_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AgendaItem(Base):
    __tablename__ = "agenda_items"
    __table_args__ = (
        Index("ix_agenda_item_report", "report_user_id"),
        # Scoped by visibility (not just report_user_id/source_link) so a
        # non-visible existing row (e.g. report_only) never blocks a new,
        # visible row from being written for the same source_link — see
        # migration c556b944a1bb for the full rationale. Still exactly one
        # open row per (report, source_link, visibility) bucket.
        Index(
            "uq_agenda_item_report_source_link_visibility",
            "report_user_id",
            "source_link",
            "visibility",
            unique=True,
            sqlite_where=text("source_link IS NOT NULL AND status != 'resolved'"),
            postgresql_where=text("source_link IS NOT NULL AND status != 'resolved'"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # PairScope's query anchor (spec §3.2) — survives manager transitions.
    report_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    # Provenance only: which manager-era this item was created under.
    pair_id: Mapped[str] = mapped_column(String, ForeignKey("pairs.id"))
    text: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(
        String
    )  # jira | slack | accomplishment_ledger | commitment_ledger | manual | meeting_synthesis
    source_link: Mapped[str | None] = mapped_column(String, nullable=True)
    visibility: Mapped[str] = mapped_column(
        String, default="shared"
    )  # shared | manager_only | report_only
    status: Mapped[str] = mapped_column(
        String, default="open"
    )  # open | resolved | pending_consent
    surfaced_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    created_by_role: Mapped[str] = mapped_column(String)  # manager | report


class AgendaItemHistory(Base):
    """One row per mutation — never a JSON blob mutated in place (same
    reasoning MergeLog gave identity resolution)."""

    __tablename__ = "agenda_item_history"
    __table_args__ = (Index("ix_agenda_item_history_item", "agenda_item_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agenda_item_id: Mapped[str] = mapped_column(String, ForeignKey("agenda_items.id"))
    version: Mapped[int] = mapped_column(Integer)
    changed_by_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    changed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    diff: Mapped[dict] = mapped_column(JSONB, default=dict)


class Accomplishment(Base):
    """The minimal durable target append_ledger_item's kind=accomplishment
    branch needs (spec §5.4). Owner-scoped like Commitment (app/core/
    models.py) — F5's own plan may extend this later."""

    __tablename__ = "accomplishments"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    description: Mapped[str] = mapped_column(String)
    source_reference_key: Mapped[str] = mapped_column(String)
    occurred_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    # Populated lazily by synthesize_friday_review the first time this row
    # is read with a null value (design spec §4/§6) — no backfill
    # migration, no categorization pipeline (F5 isn't built yet). Closed
    # set enforced in code (app.sub_agents.friday_review.sub_agents.
    # synthesize.agent.SKILL_CATEGORIES), stored as free String — matches
    # this file's own AgendaItem.source convention rather than
    # introducing this codebase's first real DB-level enum.
    skill_category: Mapped[str | None] = mapped_column(String, nullable=True)
    # A real relationship to the Goal/OKR this accomplishment actually
    # moved — was previously only ever narrative text (Friday review's own
    # moved_goal_title, a free string on that ritual's synthesized card,
    # never persisted back onto the row it's describing). Nullable: not
    # every accomplishment ties to a tracked OKR (app/agenda/store.py's
    # append_ledger_item never sets this — the meeting-synthesis LLM tool
    # has no goal-selection step yet), and the manual "add to ledger" UI
    # (app/triggers/agenda_router.py's add_accomplishment_webhook) makes
    # it optional too, same reasoning.
    goal_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("goals.id"), nullable=True, index=True
    )
