"""Review runs: the record and its HTTP surface (cabal issue 399, step 2)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from cabal.core import reviews as store
from cabal.core.db import open_session
from cabal.core.models import BootstrapCode

SHA = "4cc6d008c35e6f21f8fb58140c17350bfa22ca1d"
SHA2 = "14347dc0000000000000000000000000000000ab"


async def _operator(app: FastAPI, client: AsyncClient) -> dict[str, str]:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(select(BootstrapCode).where(BootstrapCode.used_at.is_(None)))
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_a_queued_run_has_its_seats_as_pending_steps(app: FastAPI, client: AsyncClient) -> None:
    auth = await _operator(app, client)
    r = await client.post(
        "/reviews", json={"repo": "tenishai/techne-next", "pr": 139, "sha": SHA}, headers=auth
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    run = body["run"]
    assert run["state"] == "queued" and run["version"] == 1 and run["round"] == 1
    assert run["seats"] == ["temperance", "prudence"]
    assert [(s["idx"], s["seat"], s["state"]) for s in run["steps"]] == [
        (0, "temperance", "pending"),
        (1, "prudence", "pending"),
    ]
    assert run["requested_by"]

    shown = await client.get(f"/reviews/{run['id']}", headers=auth)
    assert shown.json()["run"] == run


async def test_the_same_head_is_one_run_and_a_new_head_is_another(
    app: FastAPI, client: AsyncClient
) -> None:
    auth = await _operator(app, client)
    req = {"repo": "tenishai/techne-next", "pr": 139, "sha": SHA}
    first = (await client.post("/reviews", json=req, headers=auth)).json()
    again = (await client.post("/reviews", json={**req, "seats": ["prudence"]}, headers=auth)).json()
    assert again["created"] is False
    assert again["run"]["id"] == first["run"]["id"]
    assert again["run"]["seats"] == ["temperance", "prudence"]  # the first ask stands

    other = (await client.post("/reviews", json={**req, "sha": SHA2}, headers=auth)).json()
    assert other["created"] is True and other["run"]["id"] != first["run"]["id"]

    listed = (await client.get("/reviews", headers=auth)).json()["runs"]
    assert {r["id"] for r in listed} == {first["run"]["id"], other["run"]["id"]}


@pytest.mark.parametrize(
    "bad",
    [
        {"repo": "techne-next", "pr": 1, "sha": SHA},
        {"repo": "tenishai/techne-next", "pr": 0, "sha": SHA},
        {"repo": "tenishai/techne-next", "pr": 1, "sha": SHA[:12]},
        {"repo": "tenishai/techne-next", "pr": 1, "sha": SHA.upper()},
        {"repo": "tenishai/techne-next", "pr": 1, "sha": SHA, "seats": ["prudence", "prudence"]},
        {"repo": "tenishai/techne-next", "pr": 1, "sha": SHA, "seats": ["a b"]},
        {"repo": "tenishai/techne-next", "pr": 1, "sha": SHA, "trigger": "cron"},
    ],
)
async def test_a_request_that_cant_name_a_run_is_refused(
    app: FastAPI, client: AsyncClient, bad: dict
) -> None:
    auth = await _operator(app, client)
    r = await client.post("/reviews", json=bad, headers=auth)
    assert r.status_code == 422, r.text
    assert (await client.get("/reviews", headers=auth)).json()["runs"] == []


async def test_every_route_needs_the_operator(app: FastAPI, client: AsyncClient) -> None:
    req = {"repo": "tenishai/techne-next", "pr": 139, "sha": SHA}
    assert (await client.post("/reviews", json=req)).status_code == 401
    assert (await client.get("/reviews")).status_code == 401
    assert (await client.get("/reviews/x")).status_code == 401
    assert (await client.post("/reviews/x/cancel")).status_code == 401


async def test_cancel_ends_an_open_run_once(app: FastAPI, client: AsyncClient) -> None:
    auth = await _operator(app, client)
    run = (
        await client.post(
            "/reviews", json={"repo": "tenishai/techne-next", "pr": 139, "sha": SHA}, headers=auth
        )
    ).json()["run"]
    r = await client.post(f"/reviews/{run['id']}/cancel", json={"reason": "head moved"}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["run"]["state"] == "canceled"
    assert r.json()["run"]["reason"] == "head moved"
    assert r.json()["run"]["version"] == 2
    assert r.json()["aborted"] == 0  # no seat was running
    assert [s["state"] for s in r.json()["run"]["steps"]] == ["skipped", "skipped"]

    again = await client.post(f"/reviews/{run['id']}/cancel", headers=auth)
    assert again.status_code == 409
    assert (await client.post("/reviews/nope/cancel", headers=auth)).status_code == 404

    by_state = (await client.get("/reviews?state=canceled", headers=auth)).json()["runs"]
    assert [r["id"] for r in by_state] == [run["id"]]
    assert (await client.get("/reviews?state=bogus", headers=auth)).status_code == 422


async def test_a_write_from_a_stale_version_is_refused(app: FastAPI) -> None:
    async with open_session(app.state.sessionmaker) as session:
        run, _ = await store.create_run(session, repo="tenishai/cabal", pr=5, sha=SHA)
        run_id = run.id
    async with open_session(app.state.sessionmaker) as session:
        v2 = await store.set_state(session, run_id, expected_version=1, state="running")
    assert v2 == 2
    async with open_session(app.state.sessionmaker) as session:
        with pytest.raises(store.StaleRun):
            await store.set_state(session, run_id, expected_version=1, state="failed")
    async with open_session(app.state.sessionmaker) as session:
        fresh = await store.get_run(session, run_id)
        assert fresh is not None and fresh.state == "running" and fresh.version == 2


def test_migration_downgrades_and_upgrades_again(tmp_path, monkeypatch) -> None:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    from cabal.core.app import _migrations_dir

    db = tmp_path / "m.db"
    monkeypatch.setenv("CABAL_DB_URL", f"sqlite:///{db}")
    cfg = Config()
    cfg.set_main_option("script_location", str(_migrations_dir()))

    def tables() -> set[str]:
        return set(inspect(create_engine(f"sqlite:///{db}")).get_table_names())

    command.upgrade(cfg, "head")
    assert {"review_runs", "review_steps"} <= tables()
    command.downgrade(cfg, "f3b6c1d2e4a5")
    assert not ({"review_runs", "review_steps"} & tables())
    command.upgrade(cfg, "head")
    assert {"review_runs", "review_steps"} <= tables()


async def test_losing_the_insert_race_returns_the_winner_and_keeps_the_callers_work(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prudence, PR 424 nit: the savepoint is what keeps a lost race from rolling
    back the caller's other work. Without it this fails with PendingRollbackError."""
    from cabal.core.models import DispatchPair

    async with open_session(app.state.sessionmaker) as session:
        winner, _ = await store.create_run(session, repo="tenishai/cabal", pr=9, sha=SHA)
        winner_id = winner.id

    real_find = store.find_run
    misses = {"left": 1}

    async def find_once_missing(*args, **kwargs):  # type: ignore[no-untyped-def]
        if misses["left"]:
            misses["left"] -= 1
            return None  # as if the winner committed between our read and insert
        return await real_find(*args, **kwargs)

    monkeypatch.setattr(store, "find_run", find_once_missing)
    async with open_session(app.state.sessionmaker) as session:
        session.add(DispatchPair(caller="a", target="b"))  # unrelated work in the same transaction
        await session.flush()
        run, created = await store.create_run(session, repo="tenishai/cabal", pr=9, sha=SHA)
        assert created is False and run.id == winner_id
        assert [s.seat for s in run.steps] == ["temperance", "prudence"]
    async with app.state.sessionmaker() as s:
        assert (await s.get(DispatchPair, ("a", "b"))) is not None
