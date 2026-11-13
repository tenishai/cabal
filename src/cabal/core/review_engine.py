"""The review engine: takes queued review runs through their seats (cabal issue 399).

A run (core/reviews.py) is one review of one pull-request head. Each tick, the
engine:

1. moves every running run one step on: a pending seat is started, a running
   seat whose task has ended is collected, one past `step_timeout_s` is failed;
2. claims queued runs while fewer than `max_running` are running, with the
   `queued -> running` conditional update, and starts their first seat.

Starting a seat: read the PR head, and stop the run with "head moved" if it is
no longer the run's sha. Run `prepare_command` for the seat's checkout, brief and
metadata, then dispatch detached to the seat as `dispatch_sub` with operator
scope, through the same dispatch-token policy as any caller.

Collecting a seat depends on its kind (`seat_kinds`):
- "prepass": the seat's reply must carry `## Verdict` with an "N wrong" count.
  Any WRONG stops the run. No verdict is a failure, never a pass.
- "github_review": the outcome is the review `review_logins[seat]` posted on the
  PR with `commit_id` equal to the run's sha. APPROVED passes, CHANGES_REQUESTED
  ends the run as `changes`; no such review is a failure. The seat's reply and
  exit code are not read.

A seat that fails (dispatch error, no verdict, timeout) is retried once, then
fails the run. A GitHub read the step needs that fails with a 4xx (not 429) is
such a failure; an outage (5xx, 429, network) is waited out for at most
`step_timeout_s`, then is one too. A timed-out seat's task is aborted before
the retry. When a run ends, `cleanup_command` and `notify_command` run.

The watch loop (step 4) runs in the same tick, every `watch_poll_seconds`: in each
repo under `[review.repos]`, an open, non-draft PR carrying `label` (default
`ready-for-review`) gets a run at its head, and the label is removed.

Declared background work, in the shape cabal issue 260 asks for:
    what: one asyncio loop, every `review.poll_seconds`
    started by: the app lifespan, after the Scheduler, only if `review.enabled`
    reason: review a PR head without a session launching it
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx
import structlog
from sqlalchemy import func, select, update

from cabal.config.identity import ReviewConfig
from cabal.core.a2a.inbound import inbound_metadata
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.db import open_session
from cabal.core.models import Conversation, ReviewRun, ReviewStep
from cabal.core.reviews import InvalidRun, create_run, get_run

log = structlog.get_logger(__name__)

#: A conversation in one of these hasn't ended.
_LIVE_TASK = frozenset({"submitted", "working", "scheduled"})
_VERDICT = re.compile(r"^##\s*Verdict\b", re.MULTILINE)
_COUNT = re.compile(r"(\d+)\s+(wrong|risky)\b", re.IGNORECASE)
MAX_ATTEMPTS = 2


# ---- GitHub -------------------------------------------------------------------


@dataclass(frozen=True)
class Review:
    login: str
    state: str
    commit_id: str
    review_id: int
    submitted_at: str


@dataclass(frozen=True)
class OpenPull:
    number: int
    sha: str
    draft: bool
    labels: frozenset[str]


class GitHub(Protocol):
    async def head_sha(self, repo: str, pr: int) -> str: ...
    async def reviews(self, repo: str, pr: int) -> list[Review]: ...
    async def open_pulls(self, repo: str) -> list[OpenPull]: ...
    async def remove_label(self, repo: str, pr: int, label: str) -> None: ...


class GitHubREST:
    """The two reads the engine needs, over GitHub's REST API."""

    def __init__(self, api: str, token: str | None, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=api.rstrip("/"), headers=headers, timeout=30.0, transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def head_sha(self, repo: str, pr: int) -> str:
        resp = await self._client.get(f"/repos/{repo}/pulls/{pr}")
        resp.raise_for_status()
        return str(resp.json()["head"]["sha"])

    async def open_pulls(self, repo: str) -> list[OpenPull]:
        out: list[OpenPull] = []
        page = 1
        while True:
            resp = await self._client.get(
                f"/repos/{repo}/pulls", params={"state": "open", "per_page": 100, "page": page}
            )
            resp.raise_for_status()
            batch = resp.json()
            out += [
                OpenPull(
                    number=int(p["number"]),
                    sha=str(p["head"]["sha"]),
                    draft=bool(p.get("draft")),
                    labels=frozenset(str(lb.get("name", "")) for lb in p.get("labels") or []),
                )
                for p in batch
            ]
            if len(batch) < 100:
                return out
            page += 1

    async def remove_label(self, repo: str, pr: int, label: str) -> None:
        resp = await self._client.delete(f"/repos/{repo}/issues/{pr}/labels/{quote(label, safe='')}")
        if resp.status_code != 404:  # already gone is fine
            resp.raise_for_status()

    async def reviews(self, repo: str, pr: int) -> list[Review]:
        out: list[Review] = []
        page = 1
        while True:
            resp = await self._client.get(
                f"/repos/{repo}/pulls/{pr}/reviews", params={"per_page": 100, "page": page}
            )
            resp.raise_for_status()
            batch = resp.json()
            out += [
                Review(
                    login=(r.get("user") or {}).get("login", ""),
                    state=r.get("state", ""),
                    commit_id=r.get("commit_id") or "",
                    review_id=int(r["id"]),
                    submitted_at=r.get("submitted_at") or "",
                )
                for r in batch
            ]
            if len(batch) < 100:
                return out
            page += 1


# ---- outcome parsing ------------------------------------------------------------


def parse_prepass(text: str) -> dict[str, int] | None:
    """The counts from a pre-pass reply's `## Verdict` section, or None if it has
    none. A verdict with no "N wrong" count reads as malformed (None): a missing
    count must never pass as zero."""
    match = _VERDICT.search(text)
    if match is None:
        return None
    section = text[match.end():]
    nxt = re.search(r"^##\s", section, re.MULTILINE)
    if nxt is not None:
        section = section[: nxt.start()]
    counts = {kind.lower(): int(n) for n, kind in _COUNT.findall(section)}
    if "wrong" not in counts:
        return None
    return {"wrong": counts["wrong"], "risky": counts.get("risky", 0)}


def review_at(reviews: Sequence[Review], login: str, sha: str) -> Review | None:
    """The latest verdict `login` left at `sha` (APPROVED or CHANGES_REQUESTED)."""
    mine = [
        r for r in reviews
        if r.login == login and r.commit_id == sha and r.state in ("APPROVED", "CHANGES_REQUESTED")
    ]
    return max(mine, key=lambda r: (r.submitted_at, r.review_id)) if mine else None


def prior_rounds(reviews: Sequence[Review], login: str, sha: str) -> tuple[int, dict[str, Any] | None]:
    """(round, previous verdict) for a review at `sha`: 1 + the heads `login`
    gave a verdict on before, and the latest of those."""
    earlier = [
        r for r in reviews
        if r.login == login and r.commit_id != sha and r.state in ("APPROVED", "CHANGES_REQUESTED")
    ]
    if not earlier:
        return 1, None
    last = max(earlier, key=lambda r: (r.submitted_at, r.review_id))
    heads = {r.commit_id for r in earlier}
    return 1 + len(heads), {"state": last.state, "sha": last.commit_id, "review_id": last.review_id}


def _reply_text(row: Conversation) -> str:
    result = row.result_json or {}
    message = (result.get("status") or {}).get("message") or {}
    parts = message.get("parts") or []
    return "\n".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))


def _aware(when: datetime | None) -> datetime | None:
    if when is None:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


# ---- commands -------------------------------------------------------------------


async def run_command(argv: list[str], *, stdin: bytes | None = None, limit_s: float = 900.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=limit_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"timed out after {limit_s:.0f}s"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _fill(argv: list[str], **values: Any) -> list[str]:
    """Replace each `{name}` in `argv` with its value. Not str.format: any other
    brace in an argument (a JSON snippet, a python -c body) stays as it is."""
    out = []
    for arg in argv:
        for name, value in values.items():
            arg = arg.replace("{" + name + "}", str(value))
        out.append(arg)
    return out


# ---- the engine -------------------------------------------------------------------


class StepError(Exception):
    """A seat couldn't be started or read; retried once, then fails the run."""


class ReviewEngine:
    def __init__(
        self,
        *,
        app_state: Any,
        config: ReviewConfig,
        github: GitHub,
        commands: Any = run_command,
    ) -> None:
        self._state = app_state
        self._cfg = config
        self._gh = github
        self._run_command = commands
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_watch: datetime | None = None

    # -- lifecycle

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="cabal-review-engine")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        await self._task
        self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 -- one bad tick must not end the loop
                log.warning("review.tick_failed", error=repr(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._cfg.poll_seconds)
            except TimeoutError:
                pass

    # -- one tick

    async def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        if self._cfg.repos and (
            self._last_watch is None
            or (now - self._last_watch).total_seconds() >= self._cfg.watch_poll_seconds
        ):
            self._last_watch = now
            await self.watch()
        for run_id in await self._run_ids("running"):
            await self._advance(run_id, now)
        while await self._count("running") < self._cfg.max_running:
            claimed = await self._claim_next()
            if claimed is None:
                break
            await self._advance(claimed, now)

    # -- the watch loop (step 4)

    async def watch(self) -> int:
        """Queue a run for each open, non-draft PR carrying the label in a watched
        repo, at its head, then take the label off. Returns how many runs it
        created. A head that already has a run gets none (a head is reviewed once),
        and its label still comes off: the label asks for a review of the head it
        was put on, and that one exists."""
        label = self._cfg.label
        created = 0
        for repo, repo_cfg in self._cfg.repos.items():
            try:
                pulls = await self._gh.open_pulls(repo)
            except Exception as exc:  # noqa: BLE001 -- one repo failing must not stop the rest
                log.warning("review.watch_unreadable", repo=repo, error=repr(exc))
                continue
            for pull in pulls:
                if label not in pull.labels or pull.draft:
                    continue
                seats = list(repo_cfg.seats or self._cfg.seats_default)
                try:
                    async with open_session(self._state.sessionmaker) as session:
                        run, new = await create_run(
                            session, repo=repo, pr=pull.number, sha=pull.sha, seats=seats,
                            requested_by=f"label:{label}", trigger="label",
                        )
                        run_id, state = run.id, run.state
                except InvalidRun as exc:
                    log.warning("review.watch_refused", repo=repo, pr=pull.number, error=str(exc))
                    continue
                created += int(new)
                log.info(
                    "review.watch_queued" if new else "review.watch_known",
                    repo=repo, pr=pull.number, sha=pull.sha, run_id=run_id, state=state,
                )
                try:
                    await self._gh.remove_label(repo, pull.number, label)
                except Exception as exc:  # noqa: BLE001 -- the run exists; a stuck label only repeats "known"
                    log.warning("review.label_not_removed", repo=repo, pr=pull.number, error=repr(exc))
        return created

    async def _run_ids(self, state: str) -> list[str]:
        async with open_session(self._state.sessionmaker) as session:
            rows = await session.execute(
                select(ReviewRun.id).where(ReviewRun.state == state).order_by(ReviewRun.created_at)
            )
            return list(rows.scalars())

    async def _count(self, state: str) -> int:
        async with open_session(self._state.sessionmaker) as session:
            return int(
                (
                    await session.execute(
                        select(func.count()).select_from(ReviewRun).where(ReviewRun.state == state)
                    )
                ).scalar_one()
            )

    async def _claim_next(self) -> str | None:
        """The oldest queued run, moved to `running` by this call, or None."""
        async with open_session(self._state.sessionmaker) as session:
            candidates = (
                await session.execute(
                    select(ReviewRun.id, ReviewRun.version)
                    .where(ReviewRun.state == "queued")
                    .order_by(ReviewRun.created_at, ReviewRun.id)
                    .limit(8)
                )
            ).all()
            for run_id, version in candidates:
                claimed = await session.execute(
                    update(ReviewRun)
                    .where(ReviewRun.id == run_id, ReviewRun.version == version, ReviewRun.state == "queued")
                    .values(state="running", version=version + 1, updated_at=datetime.now(UTC))
                )
                if claimed.rowcount == 1:  # type: ignore[attr-defined]
                    log.info("review.claimed", run_id=run_id)
                    return str(run_id)
        return None

    async def _advance(self, run_id: str, now: datetime) -> None:
        async with open_session(self._state.sessionmaker) as session:
            run = await get_run(session, run_id)
            if run is None or run.state != "running":
                return
            step = next((s for s in run.steps if s.state in ("pending", "running")), None)
            snapshot = _Snapshot.of(run, step)
        if snapshot.step is None:
            await self._finish(snapshot, "approved", None)
            return
        if snapshot.step.state == "pending":
            await self._start_step(snapshot, now)
        else:
            await self._check_step(snapshot, now)

    # -- starting a seat

    async def _start_step(self, snap: _Snapshot, now: datetime) -> None:
        step = snap.step
        assert step is not None
        if self._state.dispatch_router.running.draining:
            return  # new work waits out a drain (issue 405), like a scheduled task
        try:
            head = await self._gh.head_sha(snap.repo, snap.pr)
        except Exception as exc:  # noqa: BLE001 -- sorted by _github_unreadable
            await self._github_unreadable(snap, exc, now, "head")
            return
        if head != snap.sha:
            await self._end_step(snap, "stopped", {"head": head})
            await self._finish(snap, "stopped", f"head moved to {head[:12]}")
            return
        notes = ""
        if self._kind(step.seat) == "github_review":
            notes = await self._round_notes(snap, step.seat)
        prepass = await self._prepass_text(snap) if self._kind(step.seat) == "github_review" else ""
        try:
            prepared = await self._prepare(snap, step.seat, notes, prepass)
            task_id = await self._dispatch(step.seat, prepared["brief"], prepared.get("metadata") or {})
        except StepError as exc:
            await self._step_failed(snap, str(exc), workspace=None)
            return
        workspace = prepared.get("workspace")
        # Only a step still pending becomes running. A cancel, or any end of the
        # run, that landed while prepare_command ran has already marked it
        # skipped (cancel_run and _finish both do), and must not be overwritten.
        async with open_session(self._state.sessionmaker) as session:
            started = await session.execute(
                update(ReviewStep)
                .where(
                    ReviewStep.run_id == snap.run_id,
                    ReviewStep.idx == step.idx,
                    ReviewStep.state == "pending",
                )
                .values(
                    state="running",
                    sha=head,
                    task_id=task_id,
                    workspace=workspace,
                    started_at=datetime.now(UTC),
                    outcome=_without_since({**(step.outcome or {}), "attempts": step.attempts + 1}),
                )
            )
        if started.rowcount != 1:  # type: ignore[attr-defined]
            self.abort_task(task_id, "review run ended while the seat was being prepared")
            await self._cleanup(workspace)
            log.info("review.seat_dropped", run_id=snap.run_id, seat=step.seat, task_id=task_id)
            return
        log.info("review.seat_started", run_id=snap.run_id, seat=step.seat, task_id=task_id)

    async def _round_notes(self, snap: _Snapshot, seat: str) -> str:
        login = self._cfg.review_logins.get(seat)
        if not login:
            return ""
        try:
            reviews = await self._gh.reviews(snap.repo, snap.pr)
        except Exception as exc:  # noqa: BLE001 -- notes are a courtesy, not a gate
            log.warning("review.reviews_unreadable", run_id=snap.run_id, error=repr(exc))
            return ""
        round_, prev = prior_rounds(reviews, login, snap.sha)
        async with open_session(self._state.sessionmaker) as session:
            await session.execute(
                update(ReviewRun).where(ReviewRun.id == snap.run_id).values(round=round_, prev_verdict=prev)
            )
        if prev is None:
            return ""
        return (
            f"This is round {round_}. Your round {round_ - 1} verdict was {prev['state']} at "
            f"{prev['sha'][:12]}; check that each item you raised then was addressed."
        )

    async def _prepass_text(self, snap: _Snapshot) -> str:
        """The reply of the latest earlier pre-pass seat that passed, for the next
        seat's brief (Prudence reads Temperance's report). Empty if there was none."""
        for seat, task_id in reversed(snap.passed):
            if self._kind(seat) != "prepass":
                continue
            async with open_session(self._state.sessionmaker) as session:
                row = await session.get(Conversation, task_id)
            return _reply_text(row) if row is not None else ""
        return ""

    async def _prepare(self, snap: _Snapshot, seat: str, notes: str, prepass: str = "") -> dict[str, Any]:
        if not self._cfg.prepare_command:
            raise StepError("review.prepare_command is not set")
        files: dict[str, str] = {}
        for name, text in (("notes", notes), ("prepass", prepass)):
            if text:
                with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
                    fh.write(text)
                files[name] = fh.name
        try:
            argv = _fill(
                self._cfg.prepare_command, seat=seat, repo=snap.repo, pr=snap.pr, sha=snap.sha,
                notes=files.get("notes", ""), prepass=files.get("prepass", ""),
            )
            rc, out, err = await self._run_command(argv)
        finally:
            for path in files.values():
                os.unlink(path)
        if rc != 0:
            raise StepError(f"prepare exited {rc}: {err.strip()[-300:]}")
        lines = [ln for ln in out.splitlines() if ln.strip()]
        try:
            prepared = json.loads(lines[-1]) if lines else None
        except json.JSONDecodeError:
            prepared = None
        if not isinstance(prepared, dict) or not isinstance(prepared.get("brief"), str):
            raise StepError("prepare printed no JSON object with a brief on its last line")
        return prepared

    async def _dispatch(self, seat: str, brief: str, metadata: dict[str, Any]) -> str:
        from cabal.core.broker.router import DispatchError, DrainingError
        from cabal.core.routes._dispatch_auth import DispatchRefusal, review_dispatch_token

        token, claims = await review_dispatch_token(self._state, seat, self._cfg.dispatch_sub)
        if isinstance(token, DispatchRefusal):
            raise StepError(f"dispatch refused: {token.message}")
        detached = self._state.detached
        if not detached.try_reserve():
            raise StepError("too many tasks are running detached")
        message = Message(role="user", parts=[TextPart(text=brief)])
        task = Task(
            **{
                "id": str(uuid.uuid4()),
                "contextId": str(uuid.uuid4()),
                "status": TaskStatus(state="submitted", message=message),
                "metadata": inbound_metadata(metadata),
            }
        )
        try:
            started, run = await self._state.dispatch_router.start_detached(
                target_agent=seat, target_generation=None, task=task, caller_jwt=token, caller_claims=claims,
            )
        except (DrainingError, DispatchError) as exc:
            detached.release()
            raise StepError(f"dispatch failed: {exc}") from exc
        except BaseException:
            detached.release()
            raise
        detached.spawn(started.id, run)
        return str(started.id)

    # -- collecting a seat

    async def _check_step(self, snap: _Snapshot, now: datetime) -> None:
        step = snap.step
        assert step is not None
        async with open_session(self._state.sessionmaker) as session:
            row = await session.get(Conversation, step.task_id) if step.task_id else None
        if row is not None and row.state in _LIVE_TASK:
            started = _aware(step.started_at)
            if started is not None and (now - started).total_seconds() > self._cfg.step_timeout_s:
                # Stop the seat first, so the retry doesn't run beside it (two
                # reviews posted on one PR).
                self.abort_task(step.task_id, "review step timed out")
                await self._step_failed(snap, f"no outcome within {self._cfg.step_timeout_s:.0f}s", step.workspace)
            return
        kind = self._kind(step.seat)
        if kind == "prepass":
            counts = parse_prepass(_reply_text(row)) if row is not None and row.state == "completed" else None
            if counts is None:
                why = "no verdict" if row is None or row.state == "completed" else f"task {row.state}"
                await self._step_failed(snap, why, step.workspace)
                return
            if counts["wrong"] > 0:
                await self._end_step(snap, "stopped", counts)
                await self._finish(snap, "stopped", f"{step.seat} found {counts['wrong']} wrong")
                return
            await self._end_step(snap, "passed", counts)
            await self._advance_after(snap)
            return
        login = self._cfg.review_logins.get(step.seat, "")
        try:
            reviews = await self._gh.reviews(snap.repo, snap.pr)
        except Exception as exc:  # noqa: BLE001 -- sorted by _github_unreadable
            await self._github_unreadable(snap, exc, now, "reviews")
            return
        found = review_at(reviews, login, snap.sha) if login else None
        if found is None:
            await self._step_failed(snap, f"no review by {login or '?'} at {snap.sha[:12]}", step.workspace)
            return
        outcome = {"verdict": found.state, "review_id": found.review_id}
        if found.state == "CHANGES_REQUESTED":
            await self._end_step(snap, "passed", outcome)
            await self._finish(snap, "changes", None)
            return
        await self._end_step(snap, "passed", outcome)
        await self._advance_after(snap)

    async def _advance_after(self, snap: _Snapshot) -> None:
        assert snap.step is not None
        if snap.step.idx == snap.last_idx:
            await self._finish(snap, "approved", None)

    # -- writes

    async def _end_step(self, snap: _Snapshot, state: str, outcome: dict[str, Any] | None) -> None:
        step = snap.step
        assert step is not None
        async with open_session(self._state.sessionmaker) as session:
            await session.execute(
                update(ReviewStep)
                .where(ReviewStep.run_id == snap.run_id, ReviewStep.idx == step.idx)
                .values(
                    state=state,
                    outcome=_without_since({**(step.outcome or {}), **(outcome or {})}),
                    finished_at=datetime.now(UTC),
                )
            )
        await self._cleanup(step.workspace)
        log.info("review.seat_ended", run_id=snap.run_id, seat=step.seat, state=state, outcome=outcome)

    async def _step_failed(self, snap: _Snapshot, why: str, workspace: str | None) -> None:
        """Retry the seat once (back to pending), then fail the run."""
        step = snap.step
        assert step is not None
        attempts = step.attempts + (0 if step.state == "running" else 1)
        await self._cleanup(workspace)
        if attempts < MAX_ATTEMPTS:
            async with open_session(self._state.sessionmaker) as session:
                await session.execute(
                    update(ReviewStep)
                    .where(ReviewStep.run_id == snap.run_id, ReviewStep.idx == step.idx)
                    .values(
                        state="pending", task_id=None, workspace=None, started_at=None,
                        outcome=_without_since({**(step.outcome or {}), "attempts": attempts, "last_error": why}),
                    )
                )
            log.warning("review.seat_retry", run_id=snap.run_id, seat=step.seat, reason=why)
            return
        async with open_session(self._state.sessionmaker) as session:
            await session.execute(
                update(ReviewStep)
                .where(ReviewStep.run_id == snap.run_id, ReviewStep.idx == step.idx)
                .values(
                    state="failed", finished_at=datetime.now(UTC),
                    outcome=_without_since({**(step.outcome or {}), "attempts": attempts, "last_error": why}),
                )
            )
        await self._finish(snap, "failed", f"{step.seat}: {why}")

    async def _finish(self, snap: _Snapshot, state: str, reason: str | None) -> None:
        """End the run, if nobody moved it since this tick read it (a cancel wins)."""
        async with open_session(self._state.sessionmaker) as session:
            done = await session.execute(
                update(ReviewRun)
                .where(ReviewRun.id == snap.run_id, ReviewRun.state == "running")
                .values(state=state, reason=reason, version=ReviewRun.version + 1, updated_at=datetime.now(UTC))
            )
            if done.rowcount != 1:  # type: ignore[attr-defined]
                return
            await session.execute(
                update(ReviewStep)
                .where(ReviewStep.run_id == snap.run_id, ReviewStep.state == "pending")
                .values(state="skipped")
            )
        log.info("review.ended", run_id=snap.run_id, repo=snap.repo, pr=snap.pr, sha=snap.sha, state=state, reason=reason)
        await self._notify(snap.run_id)

    async def _cleanup(self, workspace: str | None) -> None:
        if not workspace or not self._cfg.cleanup_command:
            return
        rc, _out, err = await self._run_command(_fill(self._cfg.cleanup_command, workspace=workspace), limit_s=120.0)
        if rc != 0:
            log.warning("review.cleanup_failed", workspace=workspace, rc=rc, error=err.strip()[-300:])

    async def _notify(self, run_id: str) -> None:
        if not self._cfg.notify_command:
            return
        from cabal.core.reviews import to_dict

        async with open_session(self._state.sessionmaker) as session:
            run = await get_run(session, run_id)
            body = json.dumps(to_dict(run)).encode() if run is not None else b"{}"
        rc, _out, err = await self._run_command(self._cfg.notify_command, stdin=body, limit_s=60.0)
        if rc != 0:
            log.warning("review.notify_failed", run_id=run_id, rc=rc, error=err.strip()[-300:])

    async def _github_unreadable(self, snap: _Snapshot, exc: Exception, now: datetime, what: str) -> None:
        """A GitHub read the step needs failed. A 4xx other than 429 won't go away
        (a wrong PR number, a private repo and no token): the step fails, retried
        once like any failure. Anything else (5xx, 429, network) is an outage:
        try again next tick, but no longer than `step_timeout_s` from the first
        such error, so a run never sits in `running` holding a slot forever."""
        step = snap.step
        assert step is not None
        workspace = step.workspace if step.state == "running" else None
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        if status is not None and 400 <= status < 500 and status != 429:
            await self._step_failed(snap, f"GitHub {what}: HTTP {status}", workspace)
            return
        outcome = dict(step.outcome or {})
        since = outcome.get("github_error_since")
        if since is None:
            outcome["github_error_since"] = now.isoformat()
            outcome["last_error"] = f"GitHub {what}: {exc!r}"[:300]
            async with open_session(self._state.sessionmaker) as session:
                await session.execute(
                    update(ReviewStep)
                    .where(ReviewStep.run_id == snap.run_id, ReviewStep.idx == step.idx)
                    .values(outcome=outcome)
                )
            log.warning("review.github_unreadable", run_id=snap.run_id, what=what, error=repr(exc))
            return
        waited = (now - datetime.fromisoformat(since)).total_seconds()
        if waited > self._cfg.step_timeout_s:
            await self._step_failed(snap, f"GitHub {what} unreadable for {waited:.0f}s: {exc!r}"[:300], workspace)
            return
        log.warning("review.github_unreadable", run_id=snap.run_id, what=what, waited_s=round(waited), error=repr(exc))

    def abort_task(self, task_id: str | None, reason: str) -> int:
        """Signal a seat's running task to stop, through the abort graph (the
        same path as POST /admin/tasks/{id}/abort). 0 if it isn't running."""
        if not task_id:
            return 0
        signalled = int(self._state.dispatch_router.abort_coordinator.abort(task_id, reason=reason))
        if signalled:
            log.info("review.seat_aborted", task_id=task_id, reason=reason)
        return signalled

    def _kind(self, seat: str) -> str:
        return self._cfg.seat_kinds.get(seat, "prepass")


def _without_since(outcome: dict[str, Any]) -> dict[str, Any]:
    outcome.pop("github_error_since", None)
    return outcome


@dataclass(frozen=True)
class _Step:
    idx: int
    seat: str
    state: str
    task_id: str | None
    workspace: str | None
    started_at: datetime | None
    outcome: dict[str, Any] | None

    @property
    def attempts(self) -> int:
        return int((self.outcome or {}).get("attempts", 0))


@dataclass(frozen=True)
class _Snapshot:
    """What one tick read of a run, so no ORM row outlives its session."""

    run_id: str
    repo: str
    pr: int
    sha: str
    last_idx: int
    step: _Step | None
    # (seat, task_id) of each earlier step that passed, in order.
    passed: tuple[tuple[str, str], ...] = ()

    @classmethod
    def of(cls, run: ReviewRun, step: ReviewStep | None) -> _Snapshot:
        return cls(
            run_id=run.id,
            repo=run.repo,
            pr=run.pr,
            sha=run.sha,
            last_idx=max((s.idx for s in run.steps), default=-1),
            passed=tuple(
                (s.seat, s.task_id) for s in run.steps
                if s.state == "passed" and s.task_id and (step is None or s.idx < step.idx)
            ),
            step=None
            if step is None
            else _Step(
                idx=step.idx, seat=step.seat, state=step.state, task_id=step.task_id,
                workspace=step.workspace, started_at=step.started_at, outcome=step.outcome,
            ),
        )
