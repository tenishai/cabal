"""The review engine (cabal issue 399, step 3)."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from cabal.config.identity import ReviewConfig
from cabal.core import reviews as store
from cabal.core.db import open_session
from cabal.core.models import Conversation
from cabal.core.review_engine import (
    Review,
    ReviewEngine,
    parse_prepass,
    prior_rounds,
    review_at,
)

SHA = "4cc6d008c35e6f21f8fb58140c17350bfa22ca1d"
OTHER = "14347dc0000000000000000000000000000000ab"
REPO = "tenishai/techne-next"


# ---- parsing ------------------------------------------------------------------------


def test_parse_prepass_reads_the_verdict_counts() -> None:
    assert parse_prepass("intro\n## Verdict\n12 verified, 2 risky, 0 wrong\n## RISKY\n9 wrong") == {
        "wrong": 0,
        "risky": 2,
    }
    assert parse_prepass("## Verdict\n1 WRONG, 0 risky") == {"wrong": 1, "risky": 0}


@pytest.mark.parametrize("text", ["", "no verdict here, 0 wrong", "## Verdict\nall good", "## Verdict\n## X\n0 wrong"])
def test_a_missing_verdict_or_count_is_none_never_zero(text: str) -> None:
    assert parse_prepass(text) is None


def _r(state: str, sha: str, rid: int, at: str, login: str = "prudence-tenishai") -> Review:
    return Review(login=login, state=state, commit_id=sha, review_id=rid, submitted_at=at)


def test_review_at_takes_her_latest_verdict_at_the_sha() -> None:
    reviews = [
        _r("CHANGES_REQUESTED", SHA, 1, "2026-11-13T10:00:00Z"),
        _r("APPROVED", SHA, 2, "2026-11-13T11:00:00Z"),
        _r("COMMENTED", SHA, 3, "2026-11-13T12:00:00Z"),
        _r("APPROVED", SHA, 4, "2026-11-13T13:00:00Z", login="someone-else"),
        _r("APPROVED", OTHER, 5, "2026-11-13T14:00:00Z"),
    ]
    found = review_at(reviews, "prudence-tenishai", SHA)
    assert found is not None and found.review_id == 2
    assert review_at(reviews, "prudence-tenishai", "f" * 40) is None


def test_prior_rounds_counts_earlier_heads() -> None:
    assert prior_rounds([], "prudence-tenishai", SHA) == (1, None)
    reviews = [
        _r("CHANGES_REQUESTED", "a" * 40, 1, "2026-11-12T10:00:00Z"),
        _r("CHANGES_REQUESTED", "a" * 40, 2, "2026-11-12T11:00:00Z"),
        _r("CHANGES_REQUESTED", OTHER, 3, "2026-11-13T10:00:00Z"),
        _r("APPROVED", SHA, 4, "2026-11-13T12:00:00Z"),
    ]
    round_, prev = prior_rounds(reviews, "prudence-tenishai", SHA)
    assert round_ == 3
    assert prev == {"state": "CHANGES_REQUESTED", "sha": OTHER, "review_id": 3}


# ---- the engine against the test app's echo agent ------------------------------------


class FakeGitHub:
    def __init__(self, head: str = SHA, reviews: list[Review] | None = None) -> None:
        self.head = head
        self.review_list = reviews or []
        self.head_calls = 0

    async def head_sha(self, repo: str, pr: int) -> str:
        self.head_calls += 1
        return self.head

    async def reviews(self, repo: str, pr: int) -> list[Review]:
        return list(self.review_list)


def _prepare(brief: str, workspace: str = "/tmp/ws-1") -> list[str]:
    payload = json.dumps({"workspace": workspace, "brief": brief, "metadata": {"daedalus_workspace": "ws-1"}})
    return [sys.executable, "-c", f"import sys; print('progress'); print({payload!r})"]


def _engine(app: FastAPI, gh: FakeGitHub, **cfg: Any) -> tuple[ReviewEngine, list[list[str]]]:
    calls: list[list[str]] = []
    from cabal.core.review_engine import run_command

    async def recording(argv: list[str], **kw: Any) -> tuple[int, str, str]:
        calls.append(argv)
        return await run_command(argv, **kw)

    cfg.setdefault("seat_kinds", {"echo": "prepass"})
    config = ReviewConfig(enabled=True, **cfg)
    return ReviewEngine(app_state=app.state, config=config, github=gh, commands=recording), calls


async def _queue(app: FastAPI, seats: list[str] | None = None) -> str:
    async with open_session(app.state.sessionmaker) as session:
        run, _ = await store.create_run(session, repo=REPO, pr=139, sha=SHA, seats=seats or ["echo"])
        return run.id


async def _run(app: FastAPI, run_id: str) -> dict[str, Any]:
    async with open_session(app.state.sessionmaker) as session:
        run = await store.get_run(session, run_id)
        assert run is not None
        return store.to_dict(run)


async def _until_task_ends(app: FastAPI, task_id: str) -> None:
    for _ in range(250):
        async with app.state.sessionmaker() as s:
            row = await s.get(Conversation, task_id)
        if row is not None and row.state not in ("submitted", "working"):
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"task {task_id} never ended")


async def _drive(app: FastAPI, engine: ReviewEngine, run_id: str, ticks: int = 6) -> dict[str, Any]:
    """Tick until the run ends, letting each dispatched seat finish in between."""
    body = await _run(app, run_id)
    for _ in range(ticks):
        await engine.tick()
        body = await _run(app, run_id)
        if body["state"] not in ("queued", "running"):
            return body
        for step in body["steps"]:
            if step["state"] == "running" and step["task_id"]:
                await _until_task_ends(app, step["task_id"])
    return body


async def test_a_clean_prepass_approves_a_one_seat_run(app: FastAPI) -> None:
    gh = FakeGitHub()
    engine, calls = _engine(
        app, gh, prepare_command=_prepare("x\n## Verdict\n4 verified, 1 risky, 0 wrong"),
        cleanup_command=["true", "{workspace}"],
    )
    run_id = await _queue(app)
    body = await _drive(app, engine, run_id)
    assert body["state"] == "approved", body
    step = body["steps"][0]
    assert step["state"] == "passed" and step["sha"] == SHA and step["workspace"] == "/tmp/ws-1"
    assert step["outcome"]["wrong"] == 0 and step["outcome"]["risky"] == 1
    assert ["true", "/tmp/ws-1"] in calls  # the checkout was cleaned up
    async with app.state.sessionmaker() as s:
        row = await s.get(Conversation, step["task_id"])
    assert row is not None and row.target_agent == "echo"
    assert (row.result_json or {}).get("metadata", {}).get("daedalus_workspace") == "ws-1"


async def test_a_wrong_item_stops_the_run_and_skips_the_rest(app: FastAPI) -> None:
    engine, _ = _engine(app, FakeGitHub(), prepare_command=_prepare("x\n## Verdict\n2 wrong, 0 risky"))
    run_id = await _queue(app, ["echo", "prudence"])
    body = await _drive(app, engine, run_id)
    assert body["state"] == "stopped" and body["reason"] == "echo found 2 wrong"
    assert [s["state"] for s in body["steps"]] == ["stopped", "skipped"]


async def test_a_moved_head_stops_before_anything_is_dispatched(app: FastAPI) -> None:
    engine, calls = _engine(app, FakeGitHub(head=OTHER), prepare_command=_prepare("x"))
    run_id = await _queue(app)
    body = await _drive(app, engine, run_id)
    assert body["state"] == "stopped" and body["reason"].startswith("head moved to 14347dc")
    assert body["steps"][0]["task_id"] is None
    assert calls == []


async def test_a_reply_without_a_verdict_is_retried_once_then_fails(app: FastAPI) -> None:
    engine, calls = _engine(app, FakeGitHub(), prepare_command=_prepare("looks fine to me"))
    run_id = await _queue(app)
    body = await _drive(app, engine, run_id, ticks=10)
    assert body["state"] == "failed" and body["reason"] == "echo: no verdict"
    assert body["steps"][0]["outcome"]["attempts"] == 2
    assert len(calls) == 2  # prepared twice


async def test_a_github_review_seat_reads_her_review_at_the_sha(app: FastAPI) -> None:
    gh = FakeGitHub(reviews=[_r("APPROVED", SHA, 77, "2026-11-13T14:50:24Z")])
    engine, _ = _engine(
        app, gh, prepare_command=_prepare("review it"),
        seat_kinds={"echo": "github_review"}, review_logins={"echo": "prudence-tenishai"},
    )
    run_id = await _queue(app)
    body = await _drive(app, engine, run_id)
    assert body["state"] == "approved"
    assert body["steps"][0]["outcome"]["verdict"] == "APPROVED"
    assert body["steps"][0]["outcome"]["review_id"] == 77


async def test_changes_requested_ends_the_run_as_changes(app: FastAPI) -> None:
    gh = FakeGitHub(reviews=[_r("CHANGES_REQUESTED", SHA, 78, "2026-11-13T14:50:24Z")])
    engine, _ = _engine(
        app, gh, prepare_command=_prepare("review it"),
        seat_kinds={"echo": "github_review"}, review_logins={"echo": "prudence-tenishai"},
    )
    body = await _drive(app, engine, await _queue(app))
    assert body["state"] == "changes"


async def test_round_two_brief_names_the_previous_verdict(app: FastAPI) -> None:
    gh = FakeGitHub(reviews=[_r("CHANGES_REQUESTED", OTHER, 70, "2026-11-12T10:00:00Z")])
    seen: list[str] = []

    async def capture(argv: list[str], **kw: Any) -> tuple[int, str, str]:
        notes = argv[-1]
        seen.append(Path(notes).read_text() if notes else "")  # noqa: ASYNC240
        return 0, json.dumps({"workspace": "w", "brief": "b"}), ""

    engine = ReviewEngine(
        app_state=app.state,
        config=ReviewConfig(
            enabled=True, prepare_command=["prep", "{notes}"],
            seat_kinds={"echo": "github_review"}, review_logins={"echo": "prudence-tenishai"},
        ),
        github=gh, commands=capture,
    )
    run_id = await _queue(app)
    await engine.tick()
    assert "round 2" in seen[0] and "CHANGES_REQUESTED at 14347dc00000" in seen[0]
    body = await _run(app, run_id)
    assert body["round"] == 2 and body["prev_verdict"]["review_id"] == 70


async def test_no_review_at_the_sha_is_a_failure_not_a_pass(app: FastAPI) -> None:
    gh = FakeGitHub(reviews=[_r("APPROVED", OTHER, 5, "2026-11-12T10:00:00Z")])
    engine, _ = _engine(
        app, gh, prepare_command=_prepare("review it"),
        seat_kinds={"echo": "github_review"}, review_logins={"echo": "prudence-tenishai"},
    )
    body = await _drive(app, engine, await _queue(app), ticks=10)
    assert body["state"] == "failed" and "no review by prudence-tenishai at 4cc6d008c35e" in body["reason"]


async def test_a_prepare_failure_fails_the_run_after_one_retry(app: FastAPI) -> None:
    engine, _ = _engine(app, FakeGitHub(), prepare_command=[sys.executable, "-c", "import sys; sys.exit(3)"])
    body = await _drive(app, engine, await _queue(app))
    assert body["state"] == "failed" and body["reason"].startswith("echo: prepare exited 3")
    assert body["steps"][0]["task_id"] is None


async def test_max_running_holds_the_rest_queued(app: FastAPI) -> None:
    engine, _ = _engine(app, FakeGitHub(), prepare_command=_prepare("x\n## Verdict\n0 wrong"), max_running=1)
    first = await _queue(app)
    async with open_session(app.state.sessionmaker) as session:
        second, _ = await store.create_run(session, repo=REPO, pr=140, sha=SHA, seats=["echo"])
        second_id = second.id
    await engine.tick()
    states = sorted([(await _run(app, first))["state"], (await _run(app, second_id))["state"]])
    assert states == ["queued", "running"]


async def test_a_cancel_wins_over_the_engine(app: FastAPI) -> None:
    engine, _ = _engine(app, FakeGitHub(), prepare_command=_prepare("x\n## Verdict\n0 wrong"))
    run_id = await _queue(app)
    await engine.tick()
    body = await _run(app, run_id)
    await _until_task_ends(app, body["steps"][0]["task_id"])
    async with open_session(app.state.sessionmaker) as session:
        run = await store.get_run(session, run_id)
        assert run is not None
        await store.cancel_run(session, run, reason="by hand")
    await engine.tick()
    assert (await _run(app, run_id))["state"] == "canceled"


async def test_a_stuck_seat_times_out(app: FastAPI) -> None:
    from sqlalchemy import update

    engine, _ = _engine(app, FakeGitHub(), prepare_command=_prepare("x"), step_timeout_s=1)
    run_id = await _queue(app)
    await engine.tick()
    task_id = (await _run(app, run_id))["steps"][0]["task_id"]
    await _until_task_ends(app, task_id)
    async with open_session(app.state.sessionmaker) as session:  # pin it as still working
        await session.execute(update(Conversation).where(Conversation.task_id == task_id).values(state="working"))
    await engine.tick(now=datetime.now(UTC) + timedelta(seconds=5))
    step = (await _run(app, run_id))["steps"][0]
    assert step["state"] == "pending"  # retried once
    assert step["outcome"]["last_error"].startswith("no outcome within 1s")


async def test_the_engine_is_off_unless_enabled(app: FastAPI) -> None:
    assert app.state.review_engine is None
