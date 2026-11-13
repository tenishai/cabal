"""Review runs: one review of one pull-request head (cabal issue 399).

A run is keyed by (repo, pr, sha), so a head is reviewed at most once: asking
again for the same head returns the run already there. Its steps are the seats
in order, e.g. Temperance's pre-pass, then Prudence. This module only records
runs; the engine that claims and drives them is a later step.

Every write moves `version`. A writer passes the version it read and the update
is refused if the row moved since (`StaleRun`), the same conditional update
`scheduler.py` uses to claim a scheduled task.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cabal.core.models import ReviewRun, ReviewStep

log = structlog.get_logger(__name__)

DEFAULT_SEATS: tuple[str, ...] = ("temperance", "prudence")
RUN_STATES = frozenset(
    {"queued", "running", "approved", "changes", "stopped", "failed", "merged", "canceled"}
)
# A run in one of these can still be canceled; the rest have ended.
OPEN_STATES = frozenset({"queued", "running"})
TRIGGERS = frozenset({"cli", "label"})

_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SEAT = re.compile(r"^[A-Za-z0-9_.-]+$")


class InvalidRun(ValueError):
    """The request can't name a review run: a bad repo, pr, sha or seat list."""


class StaleRun(RuntimeError):
    """The run moved since the caller read it; re-read and decide again."""


def validate(repo: str, pr: int, sha: str, seats: list[str]) -> None:
    if not _REPO.match(repo):
        raise InvalidRun(f"repo must be owner/name, not {repo!r}")
    if pr < 1:
        raise InvalidRun(f"pr must be a positive number, not {pr}")
    # The full sha: a short one could name a different commit later, and the
    # run's whole point is pinning the exact head it reviewed.
    if not _SHA.match(sha):
        raise InvalidRun("sha must be the full 40-character lowercase commit id")
    if not seats:
        raise InvalidRun("seats must name at least one reviewer")
    if len(set(seats)) != len(seats):
        raise InvalidRun("a seat may appear only once in a run")
    for seat in seats:
        if not _SEAT.match(seat):
            raise InvalidRun(f"seat {seat!r} is not an agent name")


async def get_run(session: AsyncSession, run_id: str) -> ReviewRun | None:
    result = await session.execute(
        select(ReviewRun).where(ReviewRun.id == run_id).options(selectinload(ReviewRun.steps))
    )
    return result.scalar_one_or_none()


async def find_run(session: AsyncSession, repo: str, pr: int, sha: str) -> ReviewRun | None:
    result = await session.execute(
        select(ReviewRun)
        .where(ReviewRun.repo == repo, ReviewRun.pr == pr, ReviewRun.sha == sha)
        .options(selectinload(ReviewRun.steps))
    )
    return result.scalar_one_or_none()


async def create_run(
    session: AsyncSession,
    *,
    repo: str,
    pr: int,
    sha: str,
    seats: list[str] | None = None,
    requested_by: str | None = None,
    trigger: str = "cli",
) -> tuple[ReviewRun, bool]:
    """Queue a review of `repo#pr` at `sha`. Returns (run, created): asking
    for a head that already has a run returns that run and False, whatever
    state it is in. A head reviewed once is not reviewed again; a new head
    (the author's fix) is a new run."""
    seats = list(seats) if seats else list(DEFAULT_SEATS)
    validate(repo, pr, sha, seats)
    if trigger not in TRIGGERS:
        raise InvalidRun(f"trigger must be one of {sorted(TRIGGERS)}")
    existing = await find_run(session, repo, pr, sha)
    if existing is not None:
        return existing, False
    run = ReviewRun(
        id=str(uuid.uuid4()),
        repo=repo,
        pr=pr,
        sha=sha,
        round=1,
        seats=seats,
        state="queued",
        requested_by=requested_by,
        trigger=trigger,
        version=1,
        updated_at=datetime.now(UTC),
        steps=[ReviewStep(idx=i, seat=seat, state="pending") for i, seat in enumerate(seats)],
    )
    try:
        # A savepoint, so losing the race below undoes only this insert and
        # not whatever else the caller's transaction holds.
        async with session.begin_nested():
            session.add(run)
    except IntegrityError:
        # Another writer queued the same head between our read and insert.
        raced = await find_run(session, repo, pr, sha)
        if raced is None:
            raise
        return raced, False
    log.info("review.queued", run_id=run.id, repo=repo, pr=pr, sha=sha, seats=seats)
    return run, True


async def list_runs(
    session: AsyncSession, *, state: str | None = None, limit: int = 100
) -> list[ReviewRun]:
    """Runs, newest first, optionally only those in `state`."""
    if state is not None and state not in RUN_STATES:
        raise InvalidRun(f"state must be one of {sorted(RUN_STATES)}")
    query = select(ReviewRun).options(selectinload(ReviewRun.steps))
    if state is not None:
        query = query.where(ReviewRun.state == state)
    query = query.order_by(ReviewRun.created_at.desc(), ReviewRun.id).limit(limit)
    return list((await session.execute(query)).scalars())


async def set_state(
    session: AsyncSession,
    run_id: str,
    *,
    expected_version: int,
    state: str,
    reason: str | None = None,
) -> int:
    """Move a run to `state` if it is still at `expected_version`. Returns the
    new version; raises StaleRun if another writer moved it first."""
    if state not in RUN_STATES:
        raise InvalidRun(f"state must be one of {sorted(RUN_STATES)}")
    result = await session.execute(
        update(ReviewRun)
        .where(ReviewRun.id == run_id, ReviewRun.version == expected_version)
        .values(
            state=state,
            reason=reason,
            version=expected_version + 1,
            updated_at=datetime.now(UTC),
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise StaleRun(f"review run {run_id} moved since version {expected_version}")
    log.info("review.state", run_id=run_id, state=state, reason=reason)
    return expected_version + 1


async def cancel_run(session: AsyncSession, run: ReviewRun, *, reason: str) -> int:
    """Cancel a run that hasn't ended. A seat already dispatched keeps going;
    stopping its task is the engine's job (a later step)."""
    if run.state not in OPEN_STATES:
        raise InvalidRun(f"run {run.id} has already ended ({run.state})")
    return await set_state(
        session, run.id, expected_version=run.version, state="canceled", reason=reason
    )


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def to_dict(run: ReviewRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "repo": run.repo,
        "pr": run.pr,
        "sha": run.sha,
        "round": run.round,
        "prev_verdict": run.prev_verdict,
        "seats": run.seats,
        "state": run.state,
        "reason": run.reason,
        "requested_by": run.requested_by,
        "trigger": run.trigger,
        "version": run.version,
        "created_at": _iso(run.created_at),
        "updated_at": _iso(run.updated_at),
        "steps": [
            {
                "idx": s.idx,
                "seat": s.seat,
                "sha": s.sha,
                "task_id": s.task_id,
                "workspace": s.workspace,
                "state": s.state,
                "outcome": s.outcome,
                "started_at": _iso(s.started_at),
                "finished_at": _iso(s.finished_at),
            }
            for s in run.steps
        ],
    }
