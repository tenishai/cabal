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
    UniqueConstraint,
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
    # Full Task body persisted on dispatch success — serves tasks/get, and a
    # future tasks/resubscribe, without replaying the event log.
    result_json: Mapped[dict | None] = mapped_column(JSON)
    # cabal#2 — points at a `task_checkpoints` row when a persona's
    # tool-use loop has saved partial state. Cleared when the Task
    # completes. Most rows are NULL (checkpointing is opt-in).
    checkpoint_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("task_checkpoints.task_id", ondelete="SET NULL")
    )
    # cabal issue 406 — a dispatch asked to start later waits in state
    # `scheduled` until `not_before`. `schedule_json` holds the caller's
    # claims (sub, persona, scope, aud, iat, exp), never a token: the start
    # re-checks revocation against them and mints a fresh dispatch token.
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    schedule_json: Mapped[dict | None] = mapped_column(JSON)
    # cabal issue 423 — a task sent to a group (`[groups]` in identity.toml).
    # While it waits, `target_agent` is the member it was addressed through;
    # the Scheduler moves it to the first idle member when it starts the task.
    target_group: Mapped[str | None] = mapped_column(String)
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
        Index("idx_conversations_schedule", "state", "not_before"),
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


# ---------- dispatch_pairs ---------------------------------------------------


class DispatchPair(Base):
    """Flat caller -> target allow pair for agent-to-agent dispatch.

    Room-owned policy (kobo decides who may call whom); cabal just stores
    the pairs and enforces them in the one dispatch re-mint policy
    (`routes/_dispatch_auth.dispatch_token`). An EMPTY table means
    every dispatch is allowed -- see CABAL-DECISIONS.md D-018.
    """

    __tablename__ = "dispatch_pairs"

    caller: Mapped[str] = mapped_column(String, primary_key=True)
    target: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_utcnow_default()
    )


# ---------- bootstrap_codes --------------------------------------------------


class BootstrapCode(Base):
    """One-shot codes printed to compose logs for first-touch operator login."""

    __tablename__ = "bootstrap_codes"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    persona_name: Mapped[str | None] = mapped_column(String)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TokenRevocation(Base):
    """Every token a subject holds, revoked at once: tokens for `sub` whose
    `iat` is before `not_before` are refused by every verification site on
    the dispatch path and by the scope gate (cabal, 2026-09-24). One row per
    subject; a second revocation only moves the cutoff forward. Service
    tokens are otherwise untracked (no per-token store, no jti), so this is
    the one revocation story for a credential that has leaked or been
    replaced: the room re-mints an agent's colleague pass on every re-wire
    and takes the earlier ones back through here."""

    __tablename__ = "token_revocations"

    sub: Mapped[str] = mapped_column(String, primary_key=True)
    not_before: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )



# ---------- review_runs / review_steps (cabal issue 399) -------------------


class ReviewRun(Base):
    """One review of one pull request head: (repo, pr, sha) is unique, so a
    head is reviewed at most once. `seats` is the ordered list of reviewers
    (e.g. ["temperance", "prudence"]); `steps` holds one row per seat.

    `version` is bumped on every write. A writer that read version N updates
    `WHERE version = N`, so two writers can't both move the same run (the
    conditional-update claim `scheduler.py` uses for scheduled tasks).
    """

    __tablename__ = "review_runs"
    __table_args__ = (
        UniqueConstraint("repo", "pr", "sha", name="uq_review_runs_head"),
        Index("ix_review_runs_state", "state"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    repo: Mapped[str] = mapped_column(String, nullable=False)
    pr: Mapped[int] = mapped_column(Integer, nullable=False)
    sha: Mapped[str] = mapped_column(String, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    prev_verdict: Mapped[dict | None] = mapped_column(JSON)
    seats: Mapped[list] = mapped_column(JSON, nullable=False)
    # queued, running, approved, changes, stopped, failed, merged, canceled
    state: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    reason: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str | None] = mapped_column(String)
    trigger: Mapped[str] = mapped_column(String, nullable=False, default="cli")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_utcnow_default()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    steps: Mapped[list[ReviewStep]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="ReviewStep.idx"
    )


class ReviewStep(Base):
    """One seat's part in a review run. `sha`, `task_id`, `workspace` and
    `outcome` are left empty here; the review engine (a later step of issue
    399) fills them when it starts and collects the seat: `sha` with the head
    it read before dispatching, `outcome` with the seat's result as the engine
    read it (Temperance's wrong/risky counts, Prudence's verdict and review id),
    never the seat's own exit code."""

    __tablename__ = "review_steps"

    run_id: Mapped[str] = mapped_column(
        String, ForeignKey("review_runs.id", ondelete="CASCADE"), primary_key=True
    )
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    seat: Mapped[str] = mapped_column(String, nullable=False)
    sha: Mapped[str | None] = mapped_column(String)
    task_id: Mapped[str | None] = mapped_column(String)
    workspace: Mapped[str | None] = mapped_column(String)
    # pending, running, passed, stopped, failed, skipped
    state: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    outcome: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[ReviewRun] = relationship(back_populates="steps")


# ---------- meter_spend -------------------------------------------------------


class MeterSpend(Base):
    """What the token meter measured for one task on a metered agent (cabal
    issue 394, part 2). `kind` is `task` for calls made while the agent ran
    exactly one task, `unattributed` while it ran none, and `ambiguous` while it
    ran several; those two keep one row per agent with `task_id` empty.
    `reported` is the agent's own usage report, when it sends one, kept next to
    the measured numbers so the two can be compared.
    """

    __tablename__ = "meter_spend"
    __table_args__ = (
        UniqueConstraint("agent", "kind", "task_id", name="uq_meter_spend_task"),
        Index("ix_meter_spend_root", "root_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agent: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    task_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    root_id: Mapped[str | None] = mapped_column(String)
    calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    refused_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reported: Mapped[dict | None] = mapped_column(JSON)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
