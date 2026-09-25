"""Revoking every token a subject holds (cabal#200): the room re-mints an agent's
colleague pass on every re-wire and never took the old one back, so each
rewire left one more live 365-day credential. `POST /admin/token/revoke`
records "tokens for `sub` issued before T are invalid"; every verification
site on the dispatch path and the scope gate refuses such a token from then
on, and a token minted after T passes.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from cabal.core.app import create_app
from cabal.core.auth import revocations as revocations_module
from cabal.core.auth.jwt import mint, verify
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.auth.revocations import RevocationList
from cabal.core.db import open_session
from cabal.core.models import BootstrapCode

SUB = "agent:room-service"


async def _operator_token(app: FastAPI, client: AsyncClient) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(select(BootstrapCode).where(BootstrapCode.used_at.is_(None)))
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return r.json()["access_token"]


async def _colleague_token(app: FastAPI, *, sub: str = SUB, now: int | None = None) -> str:
    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=app.state.master_key)
    assert stored is not None
    return mint(signing_key=stored, sub=sub, aud="cabal-core", scope=["colleague"], now=now)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _task(task_id: str) -> dict:
    return {
        "target": "echo",
        "task": {
            "id": task_id,
            "status": {
                "state": "submitted",
                "message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]},
            },
        },
    }


def _earlier() -> int:
    """An issue time comfortably before "now" and comfortably inside the
    default TTL, so the token is valid on its own terms and only the
    revocation can refuse it."""
    return int(time.time()) - 120


async def _revoke(app, client, sub: str, *, op: str | None = None, **extra) -> object:
    # The bootstrap code is single-use, so a test that revokes twice passes
    # the operator token it already holds.
    op = op or await _operator_token(app, client)
    return await client.post("/admin/token/revoke", json={"sub": sub, **extra}, headers=_bearer(op))


@pytest.mark.asyncio
async def test_a_token_issued_before_the_revocation_is_refused_on_dispatch(
    app: FastAPI, client: AsyncClient
) -> None:
    old = await _colleague_token(app, now=_earlier())
    ok = await client.post("/a2a/tasks", json=_task("t-before"), headers=_bearer(old))
    assert ok.status_code == 200, ok.text

    r = await _revoke(app, client, SUB)
    assert r.status_code == 200
    assert r.json()["sub"] == SUB
    assert isinstance(r.json()["not_before"], int)

    refused = await client.post("/a2a/tasks", json=_task("t-after"), headers=_bearer(old))
    assert refused.status_code == 401
    assert "revoked" in refused.text


@pytest.mark.asyncio
async def test_a_token_issued_after_the_revocation_passes(
    app: FastAPI, client: AsyncClient
) -> None:
    r = await _revoke(app, client, SUB, not_before=1_500_000_000)
    assert r.status_code == 200
    fresh = await _colleague_token(app)  # iat is now, well after the cutoff
    resp = await client.post("/a2a/tasks", json=_task("t-fresh"), headers=_bearer(fresh))
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_the_scope_gate_refuses_a_revoked_token_too(
    app: FastAPI, client: AsyncClient
) -> None:
    old = await _colleague_token(app, now=_earlier())
    assert (await client.get("/directory", headers=_bearer(old))).status_code == 200
    await _revoke(app, client, SUB)
    r = await client.get("/directory", headers=_bearer(old))
    assert r.status_code == 401
    assert "revoked" in r.text


@pytest.mark.asyncio
async def test_json_rpc_refuses_a_revoked_token(app: FastAPI, client: AsyncClient) -> None:
    old = await _colleague_token(app, now=_earlier())
    await _revoke(app, client, SUB)
    envelope = {
        "jsonrpc": "2.0",
        "id": "revoked-rpc-1",
        "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}},
    }
    r = await client.post("/agents/echo", json=envelope, headers=_bearer(old))
    # JSON-RPC answers 200 and carries the refusal in the envelope's error.
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["error"] is not None, body
    assert "revoked" in body["error"]["message"]


@pytest.mark.asyncio
async def test_other_subjects_are_untouched(app: FastAPI, client: AsyncClient) -> None:
    other = await _colleague_token(app, sub="agent:other", now=_earlier())
    await _revoke(app, client, SUB)
    resp = await client.post("/a2a/tasks", json=_task("t-other"), headers=_bearer(other))
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_a_revocation_survives_a_reload(app: FastAPI, client: AsyncClient) -> None:
    await _revoke(app, client, SUB, not_before=1_500_000_000)
    reloaded = await RevocationList.load(app.state.sessionmaker)
    assert reloaded.refuses({"sub": SUB, "iat": 1_400_000_000}) is not None
    assert reloaded.refuses({"sub": SUB, "iat": 1_600_000_000}) is None
    assert reloaded.refuses({"sub": "agent:other", "iat": 0}) is None


@pytest.mark.asyncio
async def test_revoking_again_only_moves_the_cutoff_forward(
    app: FastAPI, client: AsyncClient
) -> None:
    op = await _operator_token(app, client)
    first = await _revoke(app, client, SUB, op=op, not_before=1_500_000_000)
    second = await _revoke(app, client, SUB, op=op, not_before=1_400_000_000)
    assert first.json()["not_before"] == 1_500_000_000
    assert second.json()["not_before"] == 1_500_000_000


@pytest.mark.asyncio
async def test_a_colleague_cannot_revoke(app: FastAPI, client: AsyncClient) -> None:
    token = await _colleague_token(app)
    r = await client.post("/admin/token/revoke", json={"sub": "agent:x"}, headers=_bearer(token))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_revoke_needs_a_subject(app: FastAPI, client: AsyncClient) -> None:
    op = await _operator_token(app, client)
    r = await client.post("/admin/token/revoke", json={}, headers=_bearer(op))
    assert r.status_code == 422


# ---- PR #221 review: the exchange route, the same-second race, and the
# guarantees that had no test --------------------------------------------


@pytest.mark.asyncio
async def test_a_revoked_pass_cannot_buy_a_gateway_token(
    app: FastAPI, client: AsyncClient
) -> None:
    """`/auth/liminal-token` is a verification site too. Without the check a
    revoked pass exchanges for a fresh aud=liminal token whose iat is after
    the cutoff, which nothing downstream can refuse."""
    old = await _colleague_token(app, now=_earlier())
    ok = await client.post("/auth/liminal-token", headers=_bearer(old))
    assert ok.status_code == 200, ok.text

    await _revoke(app, client, SUB)
    refused = await client.post("/auth/liminal-token", headers=_bearer(old))
    assert refused.status_code == 401, refused.text
    assert "revoked" in refused.text


@pytest.mark.asyncio
async def test_json_rpc_tasks_get_refuses_a_revoked_token(
    app: FastAPI, client: AsyncClient
) -> None:
    """tasks/get verifies the token on its own path, not through the
    dispatch door, so it needs (and has) its own check."""
    old = await _colleague_token(app, now=_earlier())
    send = {
        "jsonrpc": "2.0",
        "id": "send-1",
        "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}},
    }
    sent = await client.post("/agents/echo", json=send, headers=_bearer(old))
    task_id = sent.json()["result"]["id"]
    get = {"jsonrpc": "2.0", "id": "get-1", "method": "tasks/get", "params": {"id": task_id}}
    before = await client.post("/agents/echo", json=get, headers=_bearer(old))
    assert before.json().get("result", {}).get("id") == task_id, before.text

    await _revoke(app, client, SUB)
    after = (await client.post("/agents/echo", json=get, headers=_bearer(old))).json()
    assert after.get("error") is not None, after
    assert "revoked" in after["error"]["message"]


def test_a_token_issued_at_the_cutoff_passes() -> None:
    """The boundary the room depends on: `iat == not_before` passes, one
    second earlier is refused."""
    revocations = RevocationList({SUB: 1_700_000_000})
    assert revocations.refuses({"sub": SUB, "iat": 1_700_000_000}) is None
    assert revocations.refuses({"sub": SUB, "iat": 1_699_999_999}) is not None


class _FrozenClock:
    """time.time() that stands still until the revocation wait sleeps, so
    "the same second" is a fact of the test, not a race against the clock."""

    def __init__(self, start: float) -> None:
        self.now = start
        self.slept = 0.0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.now += seconds


async def _mint_service(client: AsyncClient, op: str, sub: str = SUB):
    return await client.post(
        "/admin/token/mint-service",
        json={"sub": sub, "scope": ["colleague"], "aud": "cabal-core", "ttl_days": 365},
        headers=_bearer(op),
    )


@pytest.mark.asyncio
async def test_mint_revoke_mint_in_one_second_leaves_only_the_new_pass(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kobo's re-wire retry: pass A minted, then revoke, then pass B, all in
    one wall-clock second. A must be refused and B accepted."""
    clock = _FrozenClock(float(int(time.time())) + 0.25)
    monkeypatch.setattr(time, "time", clock.time)
    monkeypatch.setattr(revocations_module, "_sleep", clock.sleep)
    second = int(clock.now)

    op = await _operator_token(app, client)
    minted_a = await _mint_service(client, op)
    assert minted_a.status_code == 200, minted_a.text
    a = minted_a.json()["access_token"]
    assert (await client.post("/a2a/tasks", json=_task("t-a"), headers=_bearer(a))).status_code == 200

    revoked = await _revoke(app, client, SUB, op=op)
    assert revoked.json()["not_before"] == second + 1

    minted_b = await _mint_service(client, op)
    assert minted_b.status_code == 200, minted_b.text
    b = minted_b.json()["access_token"]
    assert 0 < clock.slept <= 1.0  # the mint waited out the revoked second
    b_iat = verify(b, key_set=app.state.jwks_provider())["iat"]
    assert b_iat == second + 1

    old = await client.post("/a2a/tasks", json=_task("t-a2"), headers=_bearer(a))
    assert old.status_code == 401, old.text
    new = await client.post("/a2a/tasks", json=_task("t-b"), headers=_bearer(b))
    assert new.status_code == 200, new.text


@pytest.mark.asyncio
async def test_minting_for_a_subject_revoked_into_the_future_is_refused(
    app: FastAPI, client: AsyncClient
) -> None:
    op = await _operator_token(app, client)
    await _revoke(app, client, SUB, op=op, not_before=int(time.time()) + 3600)
    r = await _mint_service(client, op)
    assert r.status_code == 409, r.text
    # Other subjects mint as before.
    assert (await _mint_service(client, op, sub="agent:other")).status_code == 200


@pytest.mark.asyncio
async def test_a_revocation_survives_a_daemon_restart(
    app: FastAPI, client: AsyncClient
) -> None:
    """A second app over the same database (what a restart is) still refuses
    the revoked pass: the lifespan loads the table, it does not start empty."""
    old = await _colleague_token(app, now=_earlier())
    await _revoke(app, client, SUB)

    db_url = os.environ["CABAL_DB_URL"].replace("sqlite://", "sqlite+aiosqlite://", 1)
    restarted = create_app(agent="all", config_path="identity.toml", db_url=db_url)
    async with restarted.router.lifespan_context(restarted):
        from httpx import ASGITransport

        async with AsyncClient(
            transport=ASGITransport(app=restarted), base_url="http://test"
        ) as c:
            r = await c.post("/a2a/tasks", json=_task("t-restart"), headers=_bearer(old))
    assert r.status_code == 401, r.text
    assert "revoked" in r.text


class _PrimaryKeySession:
    """Stands in for an AsyncSession over a table keyed by `sub`. `get`
    yields to the loop before answering, the way a real query does, so two
    unserialized revokes interleave every time instead of only when the
    driver's timing happens to line up (a real SQLite session did not
    reproduce it reliably)."""

    def __init__(self, table: dict) -> None:
        self._table = table
        self._pending: list = []

    async def get(self, _model, sub: str):
        found = self._table.get(sub)  # read when the query runs...
        for _ in range(3):
            await asyncio.sleep(0)  # ...answered after the loop moves on
        return found

    def add(self, row) -> None:
        self._pending.append(row)

    async def commit(self) -> None:
        for row in self._pending:
            if row.sub in self._table:
                raise RuntimeError(f"UNIQUE constraint failed: token_revocations.sub={row.sub}")
            self._table[row.sub] = row
        self._pending.clear()


@pytest.mark.asyncio
async def test_two_first_revocations_of_one_subject_at_once_both_succeed() -> None:
    """Revoke and destroy racing for a subject with no row yet must not both
    INSERT; the second would hit the primary key and answer 500."""
    revocations = RevocationList()
    table: dict = {}
    results = await asyncio.gather(
        revocations.revoke(_PrimaryKeySession(table), "agent:racing", 1_600_000_000),
        revocations.revoke(_PrimaryKeySession(table), "agent:racing", 1_600_000_001),
    )
    assert results == [1_600_000_000, 1_600_000_001]
    assert revocations.cutoff("agent:racing") == 1_600_000_001
    assert table["agent:racing"].not_before == 1_600_000_001
