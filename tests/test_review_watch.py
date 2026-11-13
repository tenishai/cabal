"""The review watch loop: a labelled PR gets a run (cabal issue 399, step 4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI

from cabal.config.identity import ReviewConfig, ReviewRepoConfig
from cabal.core import reviews as store
from cabal.core.db import open_session
from cabal.core.review_engine import OpenPull, Review, ReviewEngine

SHA = "4cc6d008c35e6f21f8fb58140c17350bfa22ca1d"
SHA2 = "14347dc0000000000000000000000000000000ab"
REPO = "tenishai/techne-next"
LABEL = "ready-for-review"


class WatchGitHub:
    def __init__(self, pulls: dict[str, list[OpenPull]], fail: set[str] | None = None) -> None:
        self.pulls = pulls
        self.fail = fail or set()
        self.removed: list[tuple[str, int, str]] = []
        self.listed: list[str] = []

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
        self.removed.append((repo, pr, label))
        # The PR loses the label, as GitHub would.
        self.pulls[repo] = [
            OpenPull(p.number, p.sha, p.draft, p.labels - {label}) if p.number == pr else p
            for p in self.pulls[repo]
        ]


def _pull(n: int, sha: str = SHA, *, labels: tuple[str, ...] = (LABEL,), draft: bool = False) -> OpenPull:
    return OpenPull(number=n, sha=sha, draft=draft, labels=frozenset(labels))


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
        (139, SHA, "label", f"label:{LABEL}")
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
                {"number": 3, "head": {"sha": SHA}, "draft": False, "labels": [{"name": "needs review"}]},
                {"number": 4, "head": {"sha": SHA2}, "draft": True, "labels": []},
            ])
        if request.method == "DELETE":
            return httpx.Response(404 if "/4/" in request.url.path else 200, json={})
        return httpx.Response(500)

    gh = GitHubREST("https://api.example/", "t0k", transport=httpx.MockTransport(handler))
    try:
        pulls = await gh.open_pulls("o/r")
        assert pulls == [
            OpenPull(3, SHA, False, frozenset({"needs review"})),
            OpenPull(4, SHA2, True, frozenset()),
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
