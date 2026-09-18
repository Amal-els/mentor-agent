import datetime
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base

class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime.datetime] = mapped_column()
    tz: Mapped[str] = mapped_column(String, default="UTC")
    pulse_fire_time_local: Mapped[datetime.time] = mapped_column(
        Time, default=datetime.time(8, 30)
    )
    late_cutoff_local: Mapped[datetime.time] = mapped_column(
        Time, default=datetime.time(21, 0)
    )
    friday_review_fire_time_local: Mapped[datetime.time] = mapped_column(
        Time, default=datetime.time(16, 0)
    )
    slack_user_id: Mapped[str | None] = mapped_column(
        String, unique=True, nullable=True
    )
    agenda_client_secret: Mapped[str | None] = mapped_column(
        String, unique=True, nullable=True
    )
   
    notion_owner_email: Mapped[str | None] = mapped_column(
        String, unique=True, nullable=True
    )
   
    notion_display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    
    setup_token: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    setup_token_expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    
    notion_person_id: Mapped[str | None] = mapped_column(
        String, unique=True, nullable=True
    )
   
    notion_access_revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    
    last_live_seed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class GoogleCredential(Base):
    __tablename__ = "google_credentials"

    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), primary_key=True
    )
    refresh_token_encrypted: Mapped[str] = mapped_column(String)
    
    scopes: Mapped[list] = mapped_column(JSONB, default=list)
    
    google_email: Mapped[str | None] = mapped_column(String, nullable=True)
    connected_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True)
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))

class _OwnedAttributedRecordMixin:
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), index=True
    )
    actor_reference_key: Mapped[str] = mapped_column(String, index=True)
    resolved_person_id: Mapped[str | None] = mapped_column(String, nullable=True)


class Event(_OwnedAttributedRecordMixin, Base):
    __tablename__ = "events"
    __table_args__ = (
        Index(
            "uq_event_owner_source_external_id",
            "owner_user_id",
            "source",
            "external_id",
            unique=True,
            sqlite_where=text("external_id IS NOT NULL"),
            postgresql_where=text("external_id IS NOT NULL"),
        ),
    )

    source: Mapped[str | None] = mapped_column(String, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    starts_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ends_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str | None] = mapped_column(String, nullable=True)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    series_id: Mapped[str | None] = mapped_column(String, nullable=True)
    attendees: Mapped[list] = mapped_column(JSONB, default=list)


class WorkItem(_OwnedAttributedRecordMixin, Base):
    __tablename__ = "work_items"
    __table_args__ = (
        Index(
            "uq_work_item_owner_source_external_id",
            "owner_user_id",
            "source",
            "external_id",
            unique=True,
            sqlite_where=text("external_id IS NOT NULL"),
            postgresql_where=text("external_id IS NOT NULL"),
        ),
    )

    source: Mapped[str | None] = mapped_column(String, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str | None] = mapped_column(String, nullable=True)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    due_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    blocks_others: Mapped[bool] = mapped_column(Boolean, default=False)


class Message(_OwnedAttributedRecordMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index(
            "uq_message_owner_source_external_id",
            "owner_user_id",
            "source",
            "external_id",
            unique=True,
            sqlite_where=text("external_id IS NOT NULL"),
            postgresql_where=text("external_id IS NOT NULL"),
        ),
    )

    source: Mapped[str | None] = mapped_column(String, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    channel: Mapped[str | None] = mapped_column(String, nullable=True)
    channel_name: Mapped[str | None] = mapped_column(String, nullable=True)
    sender_display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    sent_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    is_dm: Mapped[bool] = mapped_column(Boolean, default=False)
    body_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    action_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_reply: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    source_echo_of: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=None)

class GraphNode(Base):
    __tablename__ = "graph_nodes"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "node_type",
            "canonical_key",
            name="uq_graph_node_owner_type_key",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), nullable=False, index=True
    )
    node_type: Mapped[str] = mapped_column(String, nullable=False)
    canonical_key: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False, default="")
    properties: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

class GraphEdge(Base):
    __tablename__ = "graph_edges"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "from_node_id",
            "to_node_id",
            "edge_type",
            name="uq_graph_edge_owner_nodes_type",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), nullable=False, index=True
    )
    from_node_id: Mapped[str] = mapped_column(
        String, ForeignKey("graph_nodes.id"), nullable=False, index=True
    )
    to_node_id: Mapped[str] = mapped_column(
        String, ForeignKey("graph_nodes.id"), nullable=False, index=True
    )
    edge_type: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    status: Mapped[str] = mapped_column(String, nullable=False, default="confirmed")
    method: Mapped[str] = mapped_column(String, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    reviewed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class Goal(Base):
    __tablename__ = "goals"
    __table_args__ = (
        Index(
            "uq_goal_owner_source_external_id",
            "owner_user_id",
            "source",
            "external_id",
            unique=True,
            sqlite_where=text("external_id IS NOT NULL"),
            postgresql_where=text("external_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), index=True
    )
    title: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="active")
    external_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    goal_type: Mapped[str] = mapped_column(String, default="objective")
    parent_external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    quarter: Mapped[str | None] = mapped_column(String, nullable=True)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_value: Mapped[float | None] = mapped_column(Float, nullable=True)

class OneOnOneNote(Base):
    __tablename__ = "one_on_one_notes"
    __table_args__ = (
        Index(
            "uq_one_on_one_note_owner_source_external_id",
            "owner_user_id",
            "source",
            "external_id",
            unique=True,
            sqlite_where=text("external_id IS NOT NULL"),
            postgresql_where=text("external_id IS NOT NULL"),
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), index=True
    )
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    meeting_id: Mapped[str | None] = mapped_column(String, nullable=True)
    linked_key_result_external_ids: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))

class Commitment(Base):
    __tablename__ = "commitments"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), index=True
    )
    promised_to_person_id: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str] = mapped_column(String)
    source_reference_key: Mapped[str] = mapped_column(String)
    promised_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String, default="open")

class Suppression(Base):
    __tablename__ = "suppressions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), index=True
    )
    scope: Mapped[str] = mapped_column(String)
    target_ref: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String, default="user")


class Weight(Base):
    __tablename__ = "weights"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "key", name="uq_weight_owner_key"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), index=True
    )
    key: Mapped[str] = mapped_column(String)
    value: Mapped[float] = mapped_column(Float)
    n_signals: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))

class PulseDelivery(Base):
    __tablename__ = "pulse_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "ritual",
            "local_date",
            "trigger",
            name="uq_pulse_delivery_owner_ritual_date_trigger",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), index=True
    )
    ritual: Mapped[str] = mapped_column(String, default="pulse")
    local_date: Mapped[datetime.date] = mapped_column(Date)
    trigger: Mapped[str] = mapped_column(String)
    delivered_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    item_ids: Mapped[list] = mapped_column(JSONB, default=list)
    context_hash: Mapped[str] = mapped_column(String)
    prompt_version: Mapped[str] = mapped_column(String)
    card_json: Mapped[dict] = mapped_column(JSONB, default=dict)


class ChecklistCompletion(Base):
    __tablename__ = "checklist_completions"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "item_type",
            "source",
            "external_id",
            name="uq_checklist_completion_owner_item",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), index=True
    )
    item_type: Mapped[str] = mapped_column(String)  # "work_item" | "message"
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    ai_response: Mapped[str] = mapped_column(String, default="")

class DossierDelivery(Base):
    __tablename__ = "dossier_deliveries"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), nullable=False, index=True
    )
    event_external_id: Mapped[str] = mapped_column(String, nullable=False)
    sent_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    prompt_version: Mapped[str] = mapped_column(String, nullable=False)
    talking_points_source: Mapped[str] = mapped_column(String, nullable=False)
    card_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    dm_channel: Mapped[str | None] = mapped_column(String, nullable=True)
    feedback: Mapped[str] = mapped_column(String, nullable=False, default="none")
    feedback_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    who_summary: Mapped[str] = mapped_column(String, nullable=False, default="")
    why_now: Mapped[str] = mapped_column(String, nullable=False, default="")
    candidate_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "event_external_id", name="uq_dossier_delivery_owner_event"
        ),
    )

class FridayReviewDelivery(Base):
    __tablename__ = "friday_review_deliveries"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), nullable=False, index=True
    )
    week_start_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    trigger: Mapped[str] = mapped_column(String, nullable=False)  # "scheduled" | "pull"
    sent_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    skipped_reason: Mapped[str | None] = mapped_column(String, nullable=True)  # "on_leave" | None
    prompt_version: Mapped[str] = mapped_column(String, nullable=False)
    card_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    proposed_ledger_items: Mapped[list] = mapped_column(JSONB, default=list)
    ledger_confirmed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "week_start_date", "trigger",
            name="uq_friday_review_delivery_owner_week_trigger",
        ),
    )


class FeedbackEvent(Base):
    __tablename__ = "feedback_events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), index=True
    )
    pulse_delivery_id: Mapped[str] = mapped_column(
        String, ForeignKey("pulse_deliveries.id")
    )
    item_id: Mapped[str] = mapped_column(String)
    item_type: Mapped[str] = mapped_column(String)
    signal: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    consolidated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

class RawIngestRef(Base):
    __tablename__ = "raw_ingest_refs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), index=True
    )
    source: Mapped[str] = mapped_column(String)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    canonical_table: Mapped[str] = mapped_column(String)
    canonical_id: Mapped[str] = mapped_column(String)
    fetched_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    checksum: Mapped[str | None] = mapped_column(String, nullable=True)

class ProcessedTrigger(Base):
    __tablename__ = "processed_triggers"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "external_id",
            "kind",
            name="uq_processed_trigger_owner_external_kind",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), index=True
    )
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # "meeting_end" | "dossier_t15"
    processed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))