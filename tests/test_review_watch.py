"""The review watch loop: a labelled PR gets a run (cabal issue 399, step 4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI

from cabal.config.identity import ReviewConfig, ReviewRepoConfig
from cabal.core import reviews as store
from cabal.core.db import open_session
from cabal.core.review_engine import OpenPull, Review, ReviewEngine, TimelineEvent, label_trail

SHA = "4cc6d008c35e6f21f8fb58140c17350bfa22ca1d"
SHA2 = "14347dc0000000000000000000000000000000ab"
REPO = "tenishai/techne-next"
LABEL = "ready-for-review"
T0, T1, T2 = "2026-11-13T10:00:00Z", "2026-11-13T11:00:00Z", "2026-11-13T12:00:00Z"


class WatchGitHub:
    def __init__(self, pulls: dict[str, list[OpenPull]], fail: set[str] | None = None) -> None:
        self.pulls = pulls
        self.fail = fail or set()
        self.removed: list[tuple[str, int, str]] = []
        self.listed: list[str] = []
        self.label = LABEL
        self.roles: dict[str, str] = {}  # login -> role; default admin
        self.timelines: dict[int, list[TimelineEvent]] = {}  # pr -> events; default below
        self.comments: list[tuple[str, int, str]] = []
        self.timeline_error: Exception | None = None
        self.remove_error: Exception | None = None
        self.conflicts: dict[int, bool | None] = {}  # pr -> mergeable; default True

    async def timeline(self, repo: str, pr: int) -> list[TimelineEvent]:
        if self.timeline_error is not None:
            raise self.timeline_error
        if pr in self.timelines:
            return self.timelines[pr]
        # By default: the head was committed, then dimartin labelled it.
        sha = next(p.sha for p in self.pulls[repo] if p.number == pr)
        return [TimelineEvent("committed", T0, sha=sha), TimelineEvent("labeled", T1, "dimartin", self.label)]

    async def role(self, repo: str, login: str) -> str:
        return self.roles.get(login, "admin")

    async def mergeable(self, repo: str, pr: int) -> bool | None:
        return self.conflicts.get(pr, True)

    async def comment(self, repo: str, pr: int, body: str) -> None:
        self.comments.append((repo, pr, body))

    async def head_sha(self, repo: str, pr: int) -> str:
        return next(p.sha for p in self.pulls[repo] if p.number == pr)

    async def reviews(self, repo: str, pr: int) -> list[Review]:
        return []

    async def open_pulls(self, repo: str) -> list[OpenPull]:
        self.listed.append(repo)
        if repo in self.fail:
            raise RuntimeError("GitHub said no")
        return list(self.pulls.get(repo, []))

    async def remove_label(self, repo: str, pr: int, label: str) -> None:
        if self.remove_error is not None:
            raise self.remove_error
        self.removed.append((repo, pr, label))
        # The PR loses the label, as GitHub would.
        self.pulls[repo] = [
            OpenPull(p.number, p.sha, p.draft, p.labels - {label}, p.fork) if p.number == pr else p
            for p in self.pulls[repo]
        ]


def _pull(
    n: int, sha: str = SHA, *, labels: tuple[str, ...] = (LABEL,), draft: bool = False, fork: bool = False
) -> OpenPull:
    return OpenPull(number=n, sha=sha, draft=draft, labels=frozenset(labels), fork=fork)


def _engine(app: FastAPI, gh: WatchGitHub, **cfg: Any) -> ReviewEngine:
    cfg.setdefault("repos", {REPO: ReviewRepoConfig()})
    return ReviewEngine(app_state=app.state, config=ReviewConfig(enabled=True, **cfg), github=gh)


async def _runs(app: FastAPI) -> list[dict[str, Any]]:
    async with open_session(app.state.sessionmaker) as session:
        return [store.to_dict(r) for r in await store.list_runs(session)]


async def test_a_labelled_pr_gets_a_run_at_its_head_and_loses_the_label(app: FastAPI) -> None:
    gh = WatchGitHub({REPO: [_pull(139), _pull(140, SHA2, labels=("bug",)), _pull(141, draft=True)]})
    engine = _engine(app, gh)
    assert await engine.watch() == 1
    runs = await _runs(app)
    assert [(r["pr"], r["sha"], r["trigger"], r["requested_by"]) for r in runs] == [
        (139, SHA, "label", "label:dimartin")
    ]
    assert runs[0]["seats"] == ["temperance", "prudence"]
    assert gh.removed == [(REPO, 139, LABEL)]  # not the unlabelled one, not the draft


async def test_relabelling_the_same_head_makes_no_second_run(app: FastAPI) -> None:
    gh = WatchGitHub({REPO: [_pull(139)]})
    engine = _engine(app, gh)
    assert await engine.watch() == 1
    gh.pulls[REPO] = [_pull(139)]  # someone puts the label back on the same head
    assert await engine.watch() == 0
    assert len(await _runs(app)) == 1
    assert gh.removed == [(REPO, 139, LABEL), (REPO, 139, LABEL)]


async def test_a_new_head_with_the_label_is_a_new_run(app: FastAPI) -> None:
    gh = WatchGitHub({REPO: [_pull(139)]})
    engine = _engine(app, gh)
    await engine.watch()
    gh.pulls[REPO] = [_pull(139, SHA2)]
    assert await engine.watch() == 1
    assert sorted(r["sha"] for r in await _runs(app)) == sorted([SHA, SHA2])


async def test_per_repo_seats_and_an_unwatched_repo(app: FastAPI) -> None:
    gh = WatchGitHub({REPO: [_pull(139)], "tenishai/cabal": [_pull(5)], "tenishai/kobo": [_pull(9)]})
    engine = _engine(
        app, gh,
        repos={REPO: ReviewRepoConfig(), "tenishai/cabal": ReviewRepoConfig(seats=["prudence"])},
    )
    assert await engine.watch() == 2
    seats = {(r["repo"], r["pr"]): r["seats"] for r in await _runs(app)}
    assert seats == {(REPO, 139): ["temperance", "prudence"], ("tenishai/cabal", 5): ["prudence"]}
    assert "tenishai/kobo" not in gh.listed


async def test_one_repo_failing_does_not_stop_the_others(app: FastAPI) -> None:
    gh = WatchGitHub({"tenishai/cabal": [_pull(5)]}, fail={REPO})
    engine = _engine(app, gh, repos={REPO: ReviewRepoConfig(), "tenishai/cabal": ReviewRepoConfig()})
    assert await engine.watch() == 1
    assert [r["repo"] for r in await _runs(app)] == ["tenishai/cabal"]


async def test_a_custom_label(app: FastAPI) -> None:
    gh = WatchGitHub({REPO: [_pull(139), _pull(140, SHA2, labels=("fates",))]})
    gh.label = "fates"
    engine = _engine(app, gh, label="fates")
    assert await engine.watch() == 1
    assert [r["pr"] for r in await _runs(app)] == [140]
    assert gh.removed == [(REPO, 140, "fates")]


async def test_tick_watches_on_its_own_interval(app: FastAPI) -> None:
    gh = WatchGitHub({REPO: []})
    engine = _engine(app, gh, watch_poll_seconds=300, prepare_command=["true"])
    t0 = datetime.now(UTC)
    await engine.tick(now=t0)
    await engine.tick(now=t0 + timedelta(seconds=60))
    assert gh.listed == [REPO]
    await engine.tick(now=t0 + timedelta(seconds=301))
    assert gh.listed == [REPO, REPO]


async def test_no_repos_means_no_watching(app: FastAPI) -> None:
    gh = WatchGitHub({})
    engine = _engine(app, gh, repos={})
    await engine.tick()
    assert gh.listed == []


async def test_the_rest_client_reads_pulls_and_removes_the_label() -> None:
    import httpx

    from cabal.core.review_engine import GitHubREST

    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.raw_path.decode(), request.headers.get("authorization")))
        if request.method == "GET" and request.url.path == "/repos/o/r/pulls":
            return httpx.Response(200, json=[
                {"number": 3, "head": {"sha": SHA, "repo": {"full_name": "o/r"}},
                 "base": {"repo": {"full_name": "o/r"}}, "draft": False, "labels": [{"name": "needs review"}]},
                {"number": 4, "head": {"sha": SHA2, "repo": {"full_name": "x/r"}},
                 "base": {"repo": {"full_name": "o/r"}}, "draft": True, "labels": []},
            ])
        if request.method == "DELETE":
            return httpx.Response(404 if "/4/" in request.url.path else 200, json={})
        return httpx.Response(500)

    gh = GitHubREST("https://api.example/", "t0k", transport=httpx.MockTransport(handler))
    try:
        pulls = await gh.open_pulls("o/r")
        assert pulls == [
            OpenPull(3, SHA, False, frozenset({"needs review"}), fork=False),
            OpenPull(4, SHA2, True, frozenset(), fork=True),
        ]
        await gh.remove_label("o/r", 3, "needs review")
        await gh.remove_label("o/r", 4, "needs review")  # already gone: not an error
    finally:
        await gh.aclose()
    assert seen[1] == ("DELETE", "/repos/o/r/issues/3/labels/needs%20review", "Bearer t0k")
    assert seen[0][0] == "GET" and "state=open" in seen[0][1]


async def test_the_rest_client_pages_through_open_pulls() -> None:
    import httpx

    from cabal.core.review_engine import GitHubREST

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        count = 100 if page == 1 else 2
        return httpx.Response(200, json=[
            {"number": page * 1000 + i, "head": {"sha": SHA}, "draft": False, "labels": []} for i in range(count)
        ])

    gh = GitHubREST("https://api.example/", None, transport=httpx.MockTransport(handler))
    try:
        pulls = await gh.open_pulls("o/r")
    finally:
        await gh.aclose()
    assert len(pulls) == 102 and pulls[-1].number == 2001


# ---- who labelled it, and which head (issue 431) ---------------------------------


async def test_a_fork_pr_is_refused_with_a_comment_and_loses_the_label(app: FastAPI) -> None:
    gh = WatchGitHub({REPO: [_pull(139, fork=True)]})
    assert await _engine(app, gh).watch() == 0
    assert await _runs(app) == []
    assert gh.removed == [(REPO, 139, LABEL)]
    [(_, pr, body)] = gh.comments
    assert pr == 139 and "comes from a fork" in body and SHA[:12] in body and f"`{LABEL}`" in body


async def test_a_repo_that_allows_forks_reviews_them(app: FastAPI) -> None:
    gh = WatchGitHub({REPO: [_pull(139, fork=True)]})
    assert await _engine(app, gh, repos={REPO: ReviewRepoConfig(allow_forks=True)}).watch() == 1
    assert gh.comments == []


def test_forks_and_the_merge_gate_cannot_both_be_on() -> None:
    import pydantic
    import pytest

    with pytest.raises(pydantic.ValidationError, match="never merged"):
        ReviewRepoConfig(allow_forks=True, merge=True)


async def test_a_triage_label_is_refused_by_default(app: FastAPI) -> None:
    gh = WatchGitHub({REPO: [_pull(139)]})
    gh.roles["dimartin"] = "triage"
    assert await _engine(app, gh).watch() == 0
    assert "dimartin has triage access, and a label here needs write" in gh.comments[0][2]
    assert gh.removed == [(REPO, 139, LABEL)]


async def test_label_min_role_can_let_triage_in(app: FastAPI) -> None:
    gh = WatchGitHub({REPO: [_pull(139)]})
    gh.roles["dimartin"] = "triage"
    assert await _engine(app, gh, label_min_role="triage").watch() == 1


async def test_the_merge_gate_needs_write_whatever_the_floor(app: FastAPI) -> None:
    gh = WatchGitHub({REPO: [_pull(139)]})
    gh.roles["dimartin"] = "triage"
    engine = _engine(app, gh, label_min_role="triage", repos={REPO: ReviewRepoConfig(merge=True)})
    assert await engine.watch() == 0
    assert "needs write" in gh.comments[0][2]


async def test_an_outsider_reads_as_none(app: FastAPI) -> None:
    gh = WatchGitHub({REPO: [_pull(139)]})
    gh.roles["dimartin"] = "none"
    assert await _engine(app, gh).watch() == 0
    assert "has none access" in gh.comments[0][2]
    assert "its account needs write on the repo" in gh.comments[0][2]


async def test_a_triage_refusal_doesnt_blame_the_token(app: FastAPI) -> None:
    gh = WatchGitHub({REPO: [_pull(139)]})
    gh.roles["dimartin"] = "triage"
    assert await _engine(app, gh).watch() == 0
    assert "unable to read roles" not in gh.comments[0][2]


async def test_a_conflicting_pr_is_refused(app: FastAPI) -> None:
    gh = WatchGitHub({REPO: [_pull(139)]})
    gh.conflicts[139] = False
    assert await _engine(app, gh).watch() == 0
    assert "conflicts with its base branch" in gh.comments[0][2]
    assert gh.removed == [(REPO, 139, LABEL)]


async def test_a_pr_github_hasnt_checked_yet_is_queued(app: FastAPI) -> None:
    gh = WatchGitHub({REPO: [_pull(139)]})
    gh.conflicts[139] = None  # still computing: the merge gate is the backstop
    assert await _engine(app, gh).watch() == 1


async def test_a_head_committed_after_the_label_is_refused(app: FastAPI) -> None:
    gh = WatchGitHub({REPO: [_pull(139, SHA2)]})
    gh.timelines[139] = [
        TimelineEvent("committed", T0, sha=SHA),
        TimelineEvent("labeled", T1, "dimartin", LABEL),
        TimelineEvent("committed", T2, sha=SHA2),
    ]
    assert await _engine(app, gh).watch() == 0
    assert "committed after the label; relabel" in gh.comments[0][2]


async def test_a_force_push_after_the_label_is_refused(app: FastAPI) -> None:
    gh = WatchGitHub({REPO: [_pull(139)]})
    gh.timelines[139] = [
        TimelineEvent("committed", T0, sha=SHA),
        TimelineEvent("labeled", T1, "dimartin", LABEL),
        TimelineEvent("head_ref_force_pushed", T2, "someone"),
    ]
    assert await _engine(app, gh).watch() == 0
    assert "force-pushed after the label" in gh.comments[0][2]


async def test_an_unreadable_timeline_leaves_the_label_for_the_next_poll(app: FastAPI) -> None:
    gh = WatchGitHub({REPO: [_pull(139)]})
    gh.timeline_error = RuntimeError("GitHub said no")
    assert await _engine(app, gh).watch() == 0
    assert gh.removed == [] and gh.comments == [] and await _runs(app) == []


async def test_a_label_that_wont_come_off_gets_no_comment(app: FastAPI) -> None:
    gh = WatchGitHub({REPO: [_pull(139, fork=True)]})
    gh.remove_error = RuntimeError("no")
    assert await _engine(app, gh).watch() == 0
    assert gh.comments == []


def test_label_trail_reads_the_last_labelling() -> None:
    events = [
        TimelineEvent("labeled", T0, "early", LABEL),
        TimelineEvent("committed", T1, sha=SHA),
        TimelineEvent("unlabeled", T1, "early", LABEL),
        TimelineEvent("labeled", T1, "other", "bug"),
        TimelineEvent("labeled", T2, "late", LABEL),
    ]
    assert label_trail(events, LABEL, SHA) == ("late", None)
    # Labelled before the head was committed: the head came after the label.
    assert label_trail(events[:1] + events[1:2], LABEL, SHA)[1] is not None


def test_label_trail_refusals() -> None:
    assert label_trail([TimelineEvent("committed", T0, sha=SHA)], LABEL, SHA) == (
        None, f"no `labeled` event for {LABEL} in the PR's timeline"
    )
    actor, why = label_trail([TimelineEvent("labeled", T1, "dimartin", LABEL)], LABEL, SHA)
    assert actor == "dimartin" and why == f"the head {SHA[:12]} isn't in the PR's timeline"
    _, why = label_trail(
        [TimelineEvent("committed", T0, sha=SHA), TimelineEvent("labeled", "", "dimartin", LABEL)], LABEL, SHA
    )
    assert why == "the label's event has no actor or time"
    _, why = label_trail([TimelineEvent("committed", "garbage", sha=SHA), TimelineEvent("labeled", T1, "d", LABEL)],
                         LABEL, SHA)
    assert why is not None and "committed after the label" in why
    # Committed at the label's own second still counts as before it.
    assert label_trail(
        [TimelineEvent("committed", T1, sha=SHA), TimelineEvent("labeled", T1, "d", LABEL)], LABEL, SHA
    ) == ("d", None)


async def test_the_rest_client_reads_the_timeline_role_and_comments() -> None:
    import httpx

    from cabal.core.review_engine import GitHubREST

    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/o/r/issues/3/timeline":
            return httpx.Response(200, json=[
                {"event": "committed", "sha": SHA, "committer": {"date": T0}},
                {"event": "labeled", "created_at": T1, "actor": {"login": "dimartin"}, "label": {"name": LABEL}},
                {"event": "reviewed", "submitted_at": T2},
            ])
        if path == "/repos/o/r/collaborators/dimartin/permission":
            return httpx.Response(200, json={"permission": "write", "role_name": "maintain"})
        if path == "/repos/o/r/collaborators/custom/permission":
            return httpx.Response(200, json={"permission": "read", "role_name": "auditor"})
        if path == "/repos/o/r/collaborators/stranger/permission":
            return httpx.Response(404, json={})
        if request.method == "POST" and path == "/repos/o/r/issues/3/comments":
            import json

            posted.append(json.loads(request.content))
            return httpx.Response(201, json={})
        return httpx.Response(500)

    gh = GitHubREST("https://api.example/", "t0k", transport=httpx.MockTransport(handler))
    try:
        assert await gh.timeline("o/r", 3) == [
            TimelineEvent("committed", T0, sha=SHA),
            TimelineEvent("labeled", T1, "dimartin", LABEL),
            TimelineEvent("reviewed", ""),
        ]
        assert await gh.role("o/r", "dimartin") == "maintain"
        assert await gh.role("o/r", "custom") == "read"
        assert await gh.role("o/r", "stranger") == "none"
        await gh.comment("o/r", 3, "hello")
    finally:
        await gh.aclose()
    assert posted == [{"body": "hello"}]


async def test_the_rest_client_calls_a_pr_without_a_head_repo_a_fork() -> None:
    import httpx

    from cabal.core.review_engine import GitHubREST

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"number": 5, "head": {"sha": SHA, "repo": None}, "base": {"repo": {"full_name": "o/r"}}, "labels": []}
        ])

    gh = GitHubREST("https://api.example/", None, transport=httpx.MockTransport(handler))
    try:
        assert [p.fork for p in await gh.open_pulls("o/r")] == [True]
    finally:
        await gh.aclose()
