"""Owner-scoped schema for identity resolution (design spec §5, §11.2). Every
table carries owner_user_id, indexed first. Postgres-specific types (JSONB,
TIMESTAMP WITH TIME ZONE) per spec §11.1 — this schema targets Postgres; SQLite
remains valid only for the single-user local install path."""

import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Person(Base):
    __tablename__ = "persons"
    __table_args__ = (
        Index("ix_person_owner", "owner_user_id"),
        Index(
            "uq_person_owner_primary_email",
            "owner_user_id",
            "primary_email",
            unique=True,
            sqlite_where=text("primary_email IS NOT NULL"),
            postgresql_where=text("primary_email IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    canonical_name: Mapped[str] = mapped_column(String)
    primary_email: Mapped[str | None] = mapped_column(String, nullable=True)
    is_self: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    roster_source: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class Identity(Base):
    __tablename__ = "identities"
    __table_args__ = (
        Index("ix_identity_owner", "owner_user_id"),
        UniqueConstraint(
            "owner_user_id", "reference_key", name="uq_identity_owner_reference_key"
        ),
        Index(
            "uq_identity_owner_source_external_id",
            "owner_user_id",
            "source",
            "external_id",
            unique=True,
            sqlite_where=text("external_id IS NOT NULL"),
            postgresql_where=text("external_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    person_id: Mapped[str] = mapped_column(String, ForeignKey("persons.id"))
    source: Mapped[str] = mapped_column(
        String
    )  # "calendar" | "slack" | "linear" | "jira"
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reference_key: Mapped[str] = mapped_column(String)
    key_version: Mapped[int] = mapped_column(Integer)
    tier: Mapped[int] = mapped_column(Integer)
    confidence: Mapped[str] = mapped_column(String)  # "verified" | "inferred"
    verified_by: Mapped[str] = mapped_column(String)  # "auto" | "user_confirmed"
    handle: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)
    first_seen: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class UnresolvedReference(Base):
    __tablename__ = "unresolved_references"
    __table_args__ = (
        Index("ix_unresolved_reference_owner", "owner_user_id"),
        UniqueConstraint(
            "owner_user_id", "reference_key", name="uq_unresolved_owner_reference_key"
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    reference_key: Mapped[str] = mapped_column(String)
    key_version: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    handle: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    candidates: Mapped[list] = mapped_column(JSONB, default=list)
    best_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=0)
    distinct_day_count: Mapped[int] = mapped_column(Integer, default=0)
    last_seen_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    ask_count: Mapped[int] = mapped_column(Integer, default=0)
    scored_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    roster_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, default="pending")
    first_seen: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class NotSameAs(Base):
    __tablename__ = "not_same_as"
    __table_args__ = (
        Index(
            "ix_not_same_as_owner_key_person",
            "owner_user_id",
            "reference_key",
            "person_id",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    reference_key: Mapped[str] = mapped_column(String)
    person_id: Mapped[str] = mapped_column(String, ForeignKey("persons.id"))
    rejected_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(String, default="user")


class PendingConfirmation(Base):
    __tablename__ = "pending_confirmations"
    __table_args__ = (
        Index("ix_pending_confirmation_owner", "owner_user_id"),
        Index(
            "uq_pending_confirmation_owner_key_candidate",
            "owner_user_id",
            "reference_key",
            "candidate_person_id",
            unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    reference_key: Mapped[str] = mapped_column(
        String, index=True
    )  # not an FK, see spec §5
    candidate_person_id: Mapped[str] = mapped_column(String, ForeignKey("persons.id"))
    candidate_score: Mapped[float] = mapped_column(Float)
    surface: Mapped[str] = mapped_column(String)  # "card" | "friday_batch"
    card_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    asked_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class MergeLog(Base):
    __tablename__ = "merge_log"
    __table_args__ = (Index("ix_merge_log_owner", "owner_user_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    person_id: Mapped[str] = mapped_column(String, ForeignKey("persons.id"))
    reference_key: Mapped[str] = mapped_column(String, index=True)
    action: Mapped[str] = mapped_column(String)
    prev_person_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[str | None] = mapped_column(String, nullable=True)
    actor: Mapped[str] = mapped_column(String)  # "system" | "user"
    reason: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class PersonRelationship(Base):
    __tablename__ = "person_relationships"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "person_id_a",
            "person_id_b",
            name="uq_person_relationship_owner_pair",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    person_id_a: Mapped[str] = mapped_column(String, ForeignKey("persons.id"))
    person_id_b: Mapped[str] = mapped_column(String, ForeignKey("persons.id"))
    co_meeting_count: Mapped[int] = mapped_column(Integer, default=0)
    shared_project_count: Mapped[int] = mapped_column(Integer, default=0)
    last_contact_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    computed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class RosterVersion(Base):
    """One row PER OWNER (owner_user_id is the primary key) — not a CHECK(id=1)
    singleton. A single global counter would let one user's roster change
    invalidate every other user's cached UnresolvedReference scores."""

    __tablename__ = "roster_version"

    owner_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, default=0)
