"""The merge gate: an approved run is merged once CI is green (cabal issue 399, step 5)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from cabal.config.identity import ReviewConfig, ReviewRepoConfig
from cabal.core import reviews as store
from cabal.core.db import open_session
from cabal.core.review_engine import CheckRun, OpenPull, Review, ReviewEngine, ci_state
from tests.test_review_engine import _drive, _prepare

SHA = "4cc6d008c35e6f21f8fb58140c17350bfa22ca1d"
MERGED = "d5ad1be5a266e20379e042805372b8e2f090e830"
REPO = "tenishai/techne-next"
GREEN = ([CheckRun("unit", "completed", "success"), CheckRun("docker", "completed", "skipped")], [("dco", "success")])


def _error(status: int, message: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("PUT", "https://api.github.com/x")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(status, request=request, json={"message": message})
    )


class MergeGitHub:
    def __init__(self, ci: Any = GREEN, *, approve: bool = True) -> None:
        self.ci_result = ci
        self.merge_error: Exception | None = None
        self.ci_reads = 0
        self.merges: list[tuple[str, int, str, str]] = []
        self.review_list = [Review("prudence-tenishai", "APPROVED", SHA, 7, "2026-11-13T12:00:00Z")] if approve else []

    async def head_sha(self, repo: str, pr: int) -> str:
        return SHA

    async def reviews(self, repo: str, pr: int) -> list[Review]:
        return list(self.review_list)

    async def open_pulls(self, repo: str) -> list[OpenPull]:
        return []

    async def remove_label(self, repo: str, pr: int, label: str) -> None:
        return None

    async def ci(self, repo: str, sha: str) -> tuple[list[CheckRun], list[tuple[str, str]]]:
        self.ci_reads += 1
        if isinstance(self.ci_result, Exception):
            raise self.ci_result
        return self.ci_result

    async def merge(self, repo: str, pr: int, sha: str, method: str) -> str:
        if self.merge_error is not None:
            raise self.merge_error
        self.merges.append((repo, pr, sha, method))
        return MERGED


def _engine(app: FastAPI, gh: MergeGitHub, repo: ReviewRepoConfig | None = None, **cfg: Any) -> ReviewEngine:
    cfg.setdefault("seat_kinds", {"echo": "github_review"})
    cfg.setdefault("review_logins", {"echo": "prudence-tenishai"})
    cfg.setdefault("prepare_command", _prepare("review it"))
    cfg.setdefault("repos", {REPO: repo if repo is not None else ReviewRepoConfig(merge=True)})
    return ReviewEngine(app_state=app.state, config=ReviewConfig(enabled=True, **cfg), github=gh)


async def _queue(app: FastAPI, seats: list[str] | None = None) -> str:
    async with open_session(app.state.sessionmaker) as session:
        run, _ = await store.create_run(session, repo=REPO, pr=139, sha=SHA, seats=seats or ["echo"])
        return run.id


async def _merging(app: FastAPI) -> str:
    """A run already approved and handed to the gate."""
    run_id = await _queue(app)
    async with open_session(app.state.sessionmaker) as session:
        run = await store.get_run(session, run_id)
        assert run is not None
        await store.set_state(session, run_id, expected_version=run.version, state="merging")
    return run_id


async def _run(app: FastAPI, run_id: str) -> dict[str, Any]:
    async with open_session(app.state.sessionmaker) as session:
        run = await store.get_run(session, run_id)
        assert run is not None
        return store.to_dict(run)


# ---- the CI rule ----------------------------------------------------------------------


def test_ci_state() -> None:
    assert ci_state(*GREEN) == ("success", "")
    assert ci_state([], []) == ("pending", "no checks yet")
    assert ci_state([CheckRun("unit", "in_progress", None)], [("dco", "success")]) == ("pending", "unit")
    assert ci_state([], [("dco", "pending")]) == ("pending", "dco")
    assert ci_state([CheckRun("unit", "completed", "failure"), CheckRun("x", "queued", None)], []) == (
        "failure",
        "unit",
    )
    assert ci_state([CheckRun("unit", "completed", "cancelled")], []) == ("failure", "unit")
    assert ci_state([], [("dco", "error")]) == ("failure", "dco")


# ---- into the gate -------------------------------------------------------------------


async def test_an_approved_run_is_merged_at_its_sha(app: FastAPI) -> None:
    gh = MergeGitHub()
    engine = _engine(app, gh)
    run_id = await _queue(app)
    body = await _drive(app, engine, run_id, ticks=10)  # CI is green: the approving tick merges
    assert body["state"] == "merged" and body["reason"] == f"merged as {MERGED[:12]}"
    assert gh.merges == [(REPO, 139, SHA, "squash")]


async def test_merge_off_leaves_the_run_approved(app: FastAPI) -> None:
    gh = MergeGitHub()
    engine = _engine(app, gh, ReviewRepoConfig())
    body = await _drive(app, engine, await _queue(app), ticks=10)
    await engine.tick()
    assert (await _run(app, body["id"]))["state"] == "approved"
    assert gh.merges == [] and gh.ci_reads == 0


async def test_an_unlisted_repo_is_never_merged(app: FastAPI) -> None:
    gh = MergeGitHub()
    engine = _engine(app, gh, repos={})
    body = await _drive(app, engine, await _queue(app), ticks=10)
    assert body["state"] == "approved" and gh.merges == []


async def test_a_prepass_only_run_is_never_merged(app: FastAPI) -> None:
    gh = MergeGitHub()
    engine = _engine(app, gh, seat_kinds={"echo": "prepass"}, prepare_command=_prepare("x\n## Verdict\n0 wrong"))
    body = await _drive(app, engine, await _queue(app), ticks=10)
    assert body["state"] == "approved"
    await engine.tick()
    assert gh.merges == []


# ---- the gate ------------------------------------------------------------------------


async def test_pending_ci_waits_then_gives_up(app: FastAPI) -> None:
    gh = MergeGitHub(([CheckRun("unit", "in_progress", None)], []))
    engine = _engine(app, gh, ci_timeout_s=600)
    run_id = await _merging(app)
    t0 = datetime.now(UTC)
    await engine.tick(now=t0 + timedelta(seconds=60))
    assert (await _run(app, run_id))["state"] == "merging"
    await engine.tick(now=t0 + timedelta(seconds=700))
    body = await _run(app, run_id)
    assert body["state"] == "unmerged" and body["reason"] == "CI not green within 600s: unit"
    assert gh.merges == []


async def test_green_after_waiting_merges(app: FastAPI) -> None:
    gh = MergeGitHub(([], []))
    engine = _engine(app, gh)
    run_id = await _merging(app)
    await engine.tick()
    assert (await _run(app, run_id))["state"] == "merging"
    gh.ci_result = GREEN
    await engine.tick()
    assert (await _run(app, run_id))["state"] == "merged"


async def test_failed_ci_ends_unmerged(app: FastAPI) -> None:
    gh = MergeGitHub(([CheckRun("unit (3.12)", "completed", "failure")], [("dco", "success")]))
    engine = _engine(app, gh)
    run_id = await _merging(app)
    await engine.tick()
    body = await _run(app, run_id)
    assert body["state"] == "unmerged" and body["reason"] == f"CI failed at {SHA[:12]}: unit (3.12)"
    assert gh.merges == []


async def test_a_refused_merge_ends_unmerged_with_githubs_reason(app: FastAPI) -> None:
    gh = MergeGitHub()
    gh.merge_error = _error(409, "Head branch was modified. Review and try the merge again.")
    engine = _engine(app, gh)
    run_id = await _merging(app)
    await engine.tick()
    body = await _run(app, run_id)
    assert body["state"] == "unmerged"
    assert body["reason"] == "GitHub refused the merge: HTTP 409 Head branch was modified. Review and try the merge again."


@pytest.mark.parametrize("where", ["ci", "merge"])
async def test_an_outage_is_waited_out_within_the_timeout(app: FastAPI, where: str) -> None:
    gh = MergeGitHub()
    if where == "ci":
        gh.ci_result = _error(502)
    else:
        gh.merge_error = _error(502)
    engine = _engine(app, gh, ci_timeout_s=600)
    run_id = await _merging(app)
    t0 = datetime.now(UTC)
    await engine.tick(now=t0 + timedelta(seconds=60))
    assert (await _run(app, run_id))["state"] == "merging"
    await engine.tick(now=t0 + timedelta(seconds=700))
    body = await _run(app, run_id)
    assert body["state"] == "unmerged" and "for 7" in body["reason"]


async def test_a_403_reading_ci_ends_unmerged_at_once(app: FastAPI) -> None:
    gh = MergeGitHub(_error(403))
    engine = _engine(app, gh)
    run_id = await _merging(app)
    await engine.tick()
    body = await _run(app, run_id)
    assert body["state"] == "unmerged" and body["reason"] == "CI unreadable: HTTP 403"


async def test_ci_none_merges_without_reading_ci(app: FastAPI) -> None:
    gh = MergeGitHub()
    engine = _engine(app, gh, ReviewRepoConfig(merge=True, ci="none", merge_method="rebase"))
    run_id = await _merging(app)
    await engine.tick()
    assert (await _run(app, run_id))["state"] == "merged"
    assert gh.ci_reads == 0 and gh.merges == [(REPO, 139, SHA, "rebase")]


async def test_merge_turned_off_while_waiting_ends_unmerged(app: FastAPI) -> None:
    gh = MergeGitHub()
    engine = _engine(app, gh, ReviewRepoConfig(merge=False))
    run_id = await _merging(app)
    await engine.tick()
    body = await _run(app, run_id)
    assert body["state"] == "unmerged" and body["reason"].startswith("merge turned off")
    assert gh.merges == []


async def test_a_merging_run_can_be_canceled(app: FastAPI) -> None:
    gh = MergeGitHub()
    engine = _engine(app, gh)
    run_id = await _merging(app)
    async with open_session(app.state.sessionmaker) as session:
        run = await store.get_run(session, run_id)
        assert run is not None
        assert await store.cancel_run(session, run, reason="hold it") == []
    await engine.tick()
    assert (await _run(app, run_id))["state"] == "canceled"
    assert gh.merges == [] and gh.ci_reads == 0


async def test_the_gate_notifies(app: FastAPI) -> None:
    gh = MergeGitHub()
    engine = _engine(app, gh, notify_command=[sys.executable, "-c", "import sys; sys.stdin.read()"])
    seen: list[list[str]] = []
    real = engine._run_command

    async def recording(argv: list[str], **kw: Any) -> tuple[int, str, str]:
        seen.append(argv)
        return await real(argv, **kw)

    engine._run_command = recording
    await _merging(app)
    await engine.tick()
    assert seen == [[sys.executable, "-c", "import sys; sys.stdin.read()"]]


# ---- the REST client ---------------------------------------------------------------------


async def test_the_rest_client_reads_ci_and_merges_pinned() -> None:
    import json

    from cabal.core.review_engine import GitHubREST

    seen: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        if request.url.path.endswith("/check-runs"):
            page = int(request.url.params["page"])
            runs = [{"name": f"c{page}-{i}", "status": "completed", "conclusion": "success"} for i in range(100)]
            return httpx.Response(200, json={"check_runs": runs if page == 1 else runs[:1]})
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"state": "success", "statuses": [{"context": "dco", "state": "success"}]})
        if request.method == "PUT":
            return httpx.Response(200, json={"sha": MERGED, "merged": True})
        return httpx.Response(500)

    gh = GitHubREST("https://api.example/", "t0k", transport=httpx.MockTransport(handler))
    try:
        runs, statuses = await gh.ci("o/r", SHA)
        assert len(runs) == 101 and statuses == [("dco", "success")]
        assert await gh.merge("o/r", 5, SHA, "squash") == MERGED
    finally:
        await gh.aclose()
    assert seen[-1] == ("PUT", "/repos/o/r/pulls/5/merge", {"sha": SHA, "merge_method": "squash"})


async def _cancel(app: FastAPI, run_id: str) -> None:
    async with open_session(app.state.sessionmaker) as session:
        run = await store.get_run(session, run_id)
        assert run is not None
        await store.cancel_run(session, run, reason="hold it")


class CancelingGitHub(MergeGitHub):
    """Cancels the run in the middle of one GitHub call."""

    def __init__(self, app: FastAPI, during: str, **kw: Any) -> None:
        super().__init__(**kw)
        self.app, self.during, self.run_id = app, during, ""

    async def ci(self, repo: str, sha: str) -> tuple[list[CheckRun], list[tuple[str, str]]]:
        if self.during == "ci":
            await _cancel(self.app, self.run_id)
        return await super().ci(repo, sha)

    async def merge(self, repo: str, pr: int, sha: str, method: str) -> str:
        if self.during == "merge":
            await _cancel(self.app, self.run_id)
        return await super().merge(repo, pr, sha, method)


async def test_a_cancel_while_ci_is_read_stops_the_merge(app: FastAPI) -> None:
    gh = CancelingGitHub(app, "ci")
    engine = _engine(app, gh)
    gh.run_id = await _merging(app)
    await engine.tick()
    assert (await _run(app, gh.run_id))["state"] == "canceled"
    assert gh.merges == []


async def test_a_cancel_while_ci_fails_stays_canceled(app: FastAPI) -> None:
    gh = CancelingGitHub(app, "ci", ci=([CheckRun("unit", "completed", "failure")], []))
    engine = _engine(app, gh)
    gh.run_id = await _merging(app)
    await engine.tick()
    assert (await _run(app, gh.run_id))["state"] == "canceled"


async def test_a_cancel_during_the_merge_call_still_records_merged(app: FastAPI) -> None:
    gh = CancelingGitHub(app, "merge")
    engine = _engine(app, gh)
    gh.run_id = await _merging(app)
    await engine.tick()
    body = await _run(app, gh.run_id)
    assert body["state"] == "merged" and gh.merges == [(REPO, 139, SHA, "squash")]
