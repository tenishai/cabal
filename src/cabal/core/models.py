"""SQLAlchemy ORM models.

Schema matches cabal-handoff.md §5.1. Composite primary key on agents
(name, generation) is what enables Primus generation rotation (§6.10): two
rows for the same name can coexist in `draining` and `active` status.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base; all tables register against `Base.metadata`."""

    type_annotation_map = {dict: JSON, list: JSON}


def _utcnow_default() -> Any:
    """server_default for `CURRENT_TIMESTAMP` (SQLite stores as UTC ISO)."""
    return text("CURRENT_TIMESTAMP")


# ---------- personas ---------------------------------------------------------


class Persona(Base):
    __tablename__ = "personas"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_utcnow_default()
    )

    agents: Mapped[list[Agent]] = relationship(back_populates="persona", cascade="all")


# ---------- agents -----------------------------------------------------------


class Agent(Base):
    """Bulletin-board row. Composite PK enables coexisting generations."""

    __tablename__ = "agents"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, primary_key=True, server_default=text("1"))
    persona_name: Mapped[str] = mapped_column(String, ForeignKey("personas.name"), nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    endpoint: Mapped[str] = mapped_column(String, nullable=False)
    capabilities_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    safety_tier: Mapped[str] = mapped_column(String, nullable=False)
    agent_card_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    agent_card_kid: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'active'"))
    drain_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Federation (cabal#45): "member" = an agent this cabal owns (local or its
    # own container); "federated" = a sovereign peer this cabal is a client of.
    # federation_audience is the sovereign service id callers mint into `aud`.
    kind: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'member'"))
    # cabal#142 — presence payload for kind='session' rows: what this live
    # session is working on right now (repo / branch / one-line task). NULL on
    # every persona row and on any session that has not heartbeated yet.
    work_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    federation_audience: Mapped[str | None] = mapped_column(String)

    persona: Mapped[Persona] = relationship(back_populates="agents")

    __table_args__ = (
        # Partial index so per-name lookup of the active generation is O(1).
        Index(
            "idx_agents_name_active",
            "name",
            sqlite_where=text("status = 'active'"),
        ),
        Index("idx_agents_status", "status"),
    )


# ---------- conversations ----------------------------------------------------


class Conversation(Base):
    """A2A Task lifecycle row.

    `target_agent` + `target_generation` together FK to `agents` so the
    broker can route returns to the *exact* generation that dispatched a
    subordinate, even when that generation is `draining` (§6.10).
    """

    __tablename__ = "conversations"

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    target_agent: Mapped[str] = mapped_column(String, nullable=False)
    target_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    caller_agent: Mapped[str | None] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, nullable=False)
    cabal_escalated_from: Mapped[str | None] = mapped_column(
        String, ForeignKey("conversations.task_id")
    )
    # Full Task body persisted on dispatch success — enables tasks/get and
    # tasks/resubscribe (A2A v1.0) without replaying the event log.
    result_json: Mapped[dict | None] = mapped_column(JSON)
    # cabal#2 — points at a `task_checkpoints` row when a persona's
    # tool-use loop has saved partial state. Cleared when the Task
    # completes. Most rows are NULL (checkpointing is opt-in).
    checkpoint_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("task_checkpoints.task_id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_utcnow_default()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=_utcnow_default(),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["target_agent", "target_generation"],
            ["agents.name", "agents.generation"],
        ),
        Index(
            "idx_conversations_escalation",
            "cabal_escalated_from",
            sqlite_where=text("cabal_escalated_from IS NOT NULL"),
        ),
        Index("idx_conversations_state", "state"),
    )


# ---------- events -----------------------------------------------------------


class Event(Base):
    """Append-only audit log. Lifecycle hooks + broker write here."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(String)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_utcnow_default()
    )

    __table_args__ = (
        Index("idx_events_task", "task_id"),
        Index("idx_events_created", "created_at"),
        Index("idx_events_type", "event_type"),
    )


# ---------- task_checkpoints ------------------------------------------------


class TaskCheckpoint(Base):
    """cabal#2 — in-flight tool-use loop snapshot, keyed by task_id.

    Latest-write-wins (upsert on save). Dropped when the Task completes.
    NULLs in Conversation.checkpoint_id are the common case; rows here
    only exist for personas with `[personas.NAME.checkpointing] enabled`
    that crashed mid-loop, or are still in flight.
    """

    __tablename__ = "task_checkpoints"

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    messages_json: Mapped[list] = mapped_column(JSON, nullable=False)
    task_json: Mapped[dict | None] = mapped_column(JSON)
    persona_state_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_utcnow_default()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=_utcnow_default(),
        onupdate=lambda: datetime.now(UTC),
    )


class DelegationBudget(Base):
    """cabal#174 — the delegation budget counter, made to survive a restart.

    CONVERSATION KEYS ONLY, and the clock is why. `DelegationBudgetGuardrail`
    keys on `task.context_id` when present and falls back to the caller's
    persona name otherwise, and it reads `time.monotonic()` directly, which
    resets on restart. (No `_now` seam: that attribute belongs to
    `DoomLoopGuardrail`, and an earlier version of this docstring cited it
    here -- right substance, wrong class, #175 r1 nit 2.) A caller key carries
    a `window_start` on that clock, so restoring one after a restart would
    mean restoring a count against a window whose origin no longer exists. A
    conversation key has no TTL and no window by deliberate design -- the
    count is its whole state -- so it is the class that can be persisted
    soundly.

    SCOPED, NOT COMPLETE (#175 r1 Important). A **threaded** resumed task
    falls into this class. An un-threaded one does not: `context_id` defaults
    to `None`, the guard falls back to a windowed caller key, and that is
    never written here -- so a caller-keyed budget is still zeroed by every
    restart, at a cost of one extra window of a rolling rate limit. See
    `cabal.core.budget_store` for the full accounting.

    COUNT IS MONOTONIC HERE even though the in-memory counter is not. Memory
    keeps an LRU (`_BUDGET_MAXSIZE`) whose eviction is a known refill path;
    the upsert takes `max(stored, incoming)` so an eviction cannot lower the
    durable record and re-open the hole this table closes.
    """

    __tablename__ = "delegation_budgets"

    session_key: Mapped[str] = mapped_column(String, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=_utcnow_default(),
        onupdate=lambda: datetime.now(UTC),
    )


# ---------- secrets ----------------------------------------------------------


class Secret(Base):
    """Encrypted credential store. Real encryption lands with auth/keys."""

    __tablename__ = "secrets"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    value_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    purpose: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_utcnow_default()
    )


# ---------- signing_keys -----------------------------------------------------


class SigningKey(Base):
    """RS256 keypairs used to sign AgentCards and JWTs."""

    __tablename__ = "signing_keys"

    kid: Mapped[str] = mapped_column(String, primary_key=True)
    public_jwk_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    private_key_pem_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_utcnow_default()
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------- bootstrap_codes --------------------------------------------------


class BootstrapCode(Base):
    """One-shot codes printed to compose logs for first-touch operator login."""

    __tablename__ = "bootstrap_codes"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    persona_name: Mapped[str | None] = mapped_column(String)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
