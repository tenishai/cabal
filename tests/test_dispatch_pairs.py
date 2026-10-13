"""Dispatch pairs: the switchboard checks who may call whom.

kobo (the room) owns the policy of which agent may dispatch to which
target; cabal just stores flat `(caller, target)` pairs and enforces
them in the one re-mint policy, `routes/_dispatch_auth.dispatch_token`,
which every door calls: `POST /a2a/tasks`, the JSON-RPC door
(`POST /agents/{name}` and `/agents/{name}/stream`) and the A2A v1.0 path
(`POST /agents/{name}/a2a`).

An EMPTY table means OPEN: before the room has ever synced a set of
pairs, every agent-to-agent dispatch is allowed. Enforcement begins the
instant the first (possibly empty-again) sync lands via
`PUT /admin/dispatch-pairs` (CABAL-DECISIONS.md D-018).

Covers:
    1. Enforcement matrix on POST /a2a/tasks: pair allowed, pair missing
       (403), empty table (open), operator caller (never gated), and a
       token lacking operator/colleague scope (refused by the scope gate
       before the pairs table is ever consulted).
    2. GET/PUT /admin/dispatch-pairs: auth required, atomic replace proven
       by an interleaved read, shape validation (422), and the sanity cap.
    3. The JSON-RPC door gives the same answer as the REST one for the
       same token, and empty-means-open holds for every caller shape.
    4. Refusals are recorded server-side (#315): one warning per refusal,
       an events row only when the caller's token verified, caller, target
       and reason bounded, a failed audit write still refuses, and syncing an empty
       table warns.

"""

from __future__ import annotations

import asyncio

import pytest
import structlog
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from cabal.core.auth.jwt import mint
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.db import open_session
from cabal.core.models import BootstrapCode, Event

TARGET = "echo"


async def _operator_token(app: FastAPI, client: AsyncClient) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(select(BootstrapCode).where(BootstrapCode.used_at.is_(None)))
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return r.json()["access_token"]


async def _mint_token(
    app: FastAPI, *, sub: str, aud: str = "cabal-core", scope: list[str] | None = None
) -> str:
    """Mint a token for these tests.

    The dispatch cases pass scope=["colleague"] deliberately. This branch was
    written before cabal#124 existed, when an "agent:<name>" sub alone reached
    the dispatch_pairs check; since #124 landed, scope decides whether a
    credential may ask for a re-mint at all and the pairs table decides which
    caller->target pairs are permitted. A scopeless agent token is refused by
    the scope gate and never reaches the policy these tests exercise -- which
    matches what the room actually issues.
    """
    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=app.state.master_key)
    assert stored is not None
    return mint(signing_key=stored, sub=sub, aud=aud, scope=scope or [])


def _task_body(target: str, task_id: str) -> dict:
    return {
        "target": target,
        "task": {
            "id": task_id,
            "status": {
                "state": "submitted",
                "message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]},
            },
        },
    }


async def _dispatch(client: AsyncClient, token: str, *, target: str = TARGET, task_id: str = "t-1"):
    return await client.post(
        "/a2a/tasks",
        json=_task_body(target, task_id),
        headers={"Authorization": f"Bearer {token}"},
    )


# ---- 1. enforcement matrix on POST /a2a/tasks --------------------------


@pytest.mark.asyncio
async def test_empty_table_is_open(app: FastAPI, client: AsyncClient) -> None:
    """No pairs have ever been synced: every agent may dispatch anywhere."""
    token = await _mint_token(app, sub="agent:room", scope=["colleague"])
    resp = await _dispatch(client, token, task_id="t-open")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_allowed_pair_dispatches(app: FastAPI, client: AsyncClient) -> None:
    from cabal.core import dispatch_pairs

    async with open_session(app.state.sessionmaker) as session:
        await dispatch_pairs.replace_all(session, [("agent:sizing-agent", TARGET)])

    token = await _mint_token(app, sub="agent:sizing-agent", scope=["colleague"])
    resp = await _dispatch(client, token, task_id="t-allowed")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_missing_pair_is_403_with_readable_body(app: FastAPI, client: AsyncClient) -> None:
    from cabal.core import dispatch_pairs

    # Non-empty table that does NOT mention agent:rogue -> rogue is denied.
    async with open_session(app.state.sessionmaker) as session:
        await dispatch_pairs.replace_all(session, [("agent:sizing-agent", TARGET)])

    token = await _mint_token(app, sub="agent:rogue", scope=["colleague"])
    resp = await _dispatch(client, token, task_id="t-missing")
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert "agent:rogue" in detail
    assert TARGET in detail
    assert "not a colleague" in detail
    assert "\u2014" not in detail  # no em dashes in user-facing copy


@pytest.mark.asyncio
async def test_operator_caller_never_gated(app: FastAPI, client: AsyncClient) -> None:
    """An operator token dispatches regardless of dispatch_pairs content."""
    from cabal.core import dispatch_pairs

    # Non-empty table that mentions nobody relevant.
    async with open_session(app.state.sessionmaker) as session:
        await dispatch_pairs.replace_all(session, [("agent:someone-else", "someone-else-target")])

    token = await _operator_token(app, client)
    resp = await _dispatch(client, token, task_id="t-operator")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_token_without_colleague_scope_never_reaches_pairs(
    app: FastAPI, client: AsyncClient
) -> None:
    """A token lacking operator/colleague scope is refused one gate earlier.

    Named for the SCOPE, not the sub shape: this test used to be called
    test_non_agent_service_sub_unaffected_by_pairs, which attributed the
    refusal to the sub not being "agent:"-shaped. That was true before
    cabal#124 and is not now -- see
    test_empty_table_is_open_for_any_sub_shape, where the same
    "persona:archivist" sub carrying colleague scope reaches the table and
    passes it. What keeps THIS token out is the scope gate.
    """
    from cabal.core import dispatch_pairs

    # Even with an EMPTY table -- which is open for EVERY caller shape,
    # not just agent: subs -- a token carrying neither operator nor
    # colleague scope is refused by the scope gate, one step earlier.
    token = await _mint_token(app, sub="persona:archivist", aud="archivist", scope=["dispatch"])
    resp = await _dispatch(client, token, task_id="t-service-1")
    assert resp.status_code == 403

    # And with a populated table that would otherwise gate a colleague,
    # this token's rejection reason is unchanged: still the scope gate,
    # never a pairs 403.
    async with open_session(app.state.sessionmaker) as session:
        await dispatch_pairs.replace_all(session, [("agent:sizing-agent", TARGET)])
    resp = await _dispatch(client, token, task_id="t-service-2")
    assert resp.status_code == 403
    assert "not a colleague" not in resp.json()["detail"]


# ---- 2. PUT/GET /admin/dispatch-pairs ----------------------------------


@pytest.mark.asyncio
async def test_get_dispatch_pairs_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/admin/dispatch-pairs")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_put_dispatch_pairs_requires_auth(client: AsyncClient) -> None:
    resp = await client.put("/admin/dispatch-pairs", json={"pairs": []})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_dispatch_pairs_requires_operator_scope(app: FastAPI, client: AsyncClient) -> None:
    token = await _mint_token(app, sub="persona:archivist", scope=["dispatch"])
    resp = await client.get("/admin/dispatch-pairs", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_put_dispatch_pairs_requires_operator_scope(app: FastAPI, client: AsyncClient) -> None:
    token = await _mint_token(app, sub="agent:room", scope=["dispatch"])
    resp = await client.put(
        "/admin/dispatch-pairs",
        json={"pairs": [["agent:a", "b"]]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_put_then_get_round_trips(app: FastAPI, client: AsyncClient) -> None:
    token = await _operator_token(app, client)
    put_resp = await client.put(
        "/admin/dispatch-pairs",
        json={"pairs": [["agent:a", "b"], ["agent:c", "d"]]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert put_resp.status_code == 200, put_resp.text

    get_resp = await client.get(
        "/admin/dispatch-pairs", headers={"Authorization": f"Bearer {token}"}
    )
    assert get_resp.status_code == 200, get_resp.text
    pairs = {tuple(p) for p in get_resp.json()["pairs"]}
    assert pairs == {("agent:a", "b"), ("agent:c", "d")}


@pytest.mark.asyncio
async def test_put_replaces_whole_set(app: FastAPI, client: AsyncClient) -> None:
    """A second PUT fully replaces the first -- no merge, no leftovers."""
    token = await _operator_token(app, client)
    await client.put(
        "/admin/dispatch-pairs",
        json={"pairs": [["agent:a", "b"]]},
        headers={"Authorization": f"Bearer {token}"},
    )
    resp = await client.put(
        "/admin/dispatch-pairs",
        json={"pairs": [["agent:x", "y"]]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    get_resp = await client.get(
        "/admin/dispatch-pairs", headers={"Authorization": f"Bearer {token}"}
    )
    pairs = {tuple(p) for p in get_resp.json()["pairs"]}
    assert pairs == {("agent:x", "y")}


@pytest.mark.asyncio
async def test_put_dedupes_duplicate_pair_in_body(app: FastAPI, client: AsyncClient) -> None:
    """A body containing the same (caller, target) pair twice is not a
    client error -- it's the same statement stated twice. The pair must
    be de-duplicated before insert (which would otherwise hit the
    composite primary key), and the response count reflects the
    de-duplicated total."""
    token = await _operator_token(app, client)
    resp = await client.put(
        "/admin/dispatch-pairs",
        json={"pairs": [["agent:a", "b"], ["agent:a", "b"]]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"count": 1}

    get_resp = await client.get(
        "/admin/dispatch-pairs", headers={"Authorization": f"Bearer {token}"}
    )
    pairs = {tuple(p) for p in get_resp.json()["pairs"]}
    assert pairs == {("agent:a", "b")}


@pytest.mark.asyncio
async def test_put_response_is_count_only(app: FastAPI, client: AsyncClient) -> None:
    """The PUT response is trimmed to {"count": N} -- the caller already
    has its list; GET remains the surface for inspecting what's stored."""
    token = await _operator_token(app, client)
    resp = await client.put(
        "/admin/dispatch-pairs",
        json={"pairs": [["agent:a", "b"], ["agent:c", "d"]]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"count": 2}


@pytest.mark.asyncio
async def test_put_integrity_error_becomes_422_with_readable_body(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt-and-braces: de-dupe should already prevent a composite-PK
    collision from reaching the database, but if one gets through anyway
    (e.g. a future bug in de-dupe, or some other constraint), the route
    must translate it into a 422 with a readable body instead of letting
    an unhandled IntegrityError surface as a 500."""
    from sqlalchemy.exc import IntegrityError

    from cabal.core import dispatch_pairs as dispatch_pairs_module

    async def _boom(session, pairs):
        raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

    # admin.py does `from cabal.core import dispatch_pairs` locally inside
    # each route function, so patching the module's attribute (rather than
    # any name bound in admin.py) is what the route actually calls.
    monkeypatch.setattr(dispatch_pairs_module, "replace_all", _boom)

    token = await _operator_token(app, client)
    resp = await client.put(
        "/admin/dispatch-pairs",
        json={"pairs": [["agent:a", "b"]]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail
    assert "—" not in detail  # no em dashes in user-facing copy


def test_dedupe_pairs_is_order_preserving() -> None:
    from cabal.core import dispatch_pairs

    result = dispatch_pairs.dedupe_pairs(
        [("agent:a", "b"), ("agent:c", "d"), ("agent:a", "b"), ("agent:e", "f")]
    )
    assert result == [("agent:a", "b"), ("agent:c", "d"), ("agent:e", "f")]


@pytest.mark.asyncio
async def test_put_rejects_malshaped_pairs(app: FastAPI, client: AsyncClient) -> None:
    token = await _operator_token(app, client)
    # A 3-element "pair" is not a (str, str) pair.
    resp = await client.put(
        "/admin/dispatch-pairs",
        json={"pairs": [["agent:a", "b", "extra"]]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_rejects_non_string_elements(app: FastAPI, client: AsyncClient) -> None:
    token = await _operator_token(app, client)
    resp = await client.put(
        "/admin/dispatch-pairs",
        json={"pairs": [["agent:a", 5]]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_rejects_over_cap(app: FastAPI, client: AsyncClient) -> None:
    from cabal.core import dispatch_pairs

    token = await _operator_token(app, client)
    too_many = [[f"agent:{i}", "t"] for i in range(dispatch_pairs.MAX_PAIRS + 1)]
    resp = await client.put(
        "/admin/dispatch-pairs",
        json={"pairs": too_many},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_replace_all_is_atomic_under_interleaved_read(app: FastAPI) -> None:
    """The write is DELETE-all + INSERT within a single uncommitted
    transaction. A concurrent reader on a separate session must never
    observe the dangerous half-state (old rows deleted, new rows not yet
    inserted) -- that momentary "empty" would be misread as OPEN by
    `is_allowed`. Proven here by literally interleaving a paused writer
    with a reader on another connection."""
    from cabal.core import dispatch_pairs

    # Seed an initial, non-empty set.
    async with open_session(app.state.sessionmaker) as session:
        await dispatch_pairs.replace_all(session, [("agent:old", "target-old")])

    writer_started = asyncio.Event()
    writer_may_commit = asyncio.Event()
    writer_session = app.state.sessionmaker()

    async def writer() -> None:
        await dispatch_pairs.replace_all(writer_session, [("agent:new", "target-new")])
        writer_started.set()
        await writer_may_commit.wait()
        await writer_session.commit()
        await writer_session.close()

    writer_task = asyncio.create_task(writer())
    await writer_started.wait()

    # The writer has deleted the old row and staged the new one, but has
    # NOT committed. A fresh reader session must still see the OLD,
    # fully-committed state -- never an empty table.
    async with open_session(app.state.sessionmaker) as session:
        pairs = await dispatch_pairs.list_all(session)
    assert pairs == [("agent:old", "target-old")], (
        "reader observed a half-written state; the replace is not atomic"
    )

    writer_may_commit.set()
    await writer_task

    async with open_session(app.state.sessionmaker) as session:
        pairs = await dispatch_pairs.list_all(session)
    assert pairs == [("agent:new", "target-new")]


# ---- 4. the same policy on the JSON-RPC door ---------------------------


async def _dispatch_jsonrpc(
    client: AsyncClient, token: str, *, target: str = TARGET, req_id: str = "rpc-1"
):
    return await client.post(
        f"/agents/{target}",
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "message/send",
            "params": {
                "message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )


async def _dispatch_v1(client: AsyncClient, token: str, *, target: str = TARGET, req_id: str = "v1-1"):
    return await client.post(
        f"/agents/{target}/a2a",
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "SendMessage",
            "params": {
                "message": {"messageId": "m-1", "role": "ROLE_USER", "parts": [{"text": "hi"}]}
            },
        },
        headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0"},
    )


@pytest.mark.asyncio
async def test_jsonrpc_honours_dispatch_pairs(app: FastAPI, client: AsyncClient) -> None:
    """A policy enforced on some doors is not enforced.

    The JSON-RPC door once had its own copy of the re-mint policy, and it
    re-minted without ever consulting the table -- so the same token refused
    by POST /a2a/tasks dispatched happily through POST /agents/{name}. Every
    door now calls `_dispatch_auth.dispatch_token`; this pins the same answer
    on each, the A2A v1.0 path included.
    """
    from cabal.core import dispatch_pairs

    async with open_session(app.state.sessionmaker) as session:
        await dispatch_pairs.replace_all(session, [("agent:sizing-agent", TARGET)])

    rogue = await _mint_token(app, sub="agent:rogue", scope=["colleague"])

    rest = await _dispatch(client, rogue, task_id="t-rogue-rest")
    assert rest.status_code == 403, rest.text

    rpc = await _dispatch_jsonrpc(client, rogue, req_id="rpc-rogue")
    assert rpc.status_code == 200, rpc.text
    body = rpc.json()
    assert body.get("error") is not None, f"JSON-RPC let a refused caller through: {body}"
    assert "not a colleague" in body["error"]["message"], body

    v1 = await _dispatch_v1(client, rogue, req_id="v1-rogue")
    assert v1.status_code == 403, v1.text
    assert "not a colleague" in v1.text


@pytest.mark.asyncio
async def test_missing_pair_names_the_remedy_on_every_door(app: FastAPI, client: AsyncClient) -> None:
    """#329: a refusal for a missing pair says how to fix it, in words and as
    a code a runtime can act on, on all three routes. `detail` stays a
    string: daedalus 0.5.0's `_refusal_detail` shows `str(body["detail"])`
    to the model."""
    from cabal.core import dispatch_pairs

    async with open_session(app.state.sessionmaker) as session:
        await dispatch_pairs.replace_all(session, [("agent:sizing-agent", TARGET)])
    rogue = await _mint_token(app, sub="agent:rogue", scope=["colleague"])
    sentence = (
        f"agent:rogue is not a colleague of {TARGET} yet: agent:rogue's owner can "
        f"invite {TARGET} as a colleague, and {TARGET}'s owner accepts."
    )

    rest = await _dispatch(client, rogue, task_id="t-remedy-rest")
    assert rest.status_code == 403
    assert rest.json() == {"detail": sentence, "remedy": "colleague_invite"}

    rpc = (await _dispatch_jsonrpc(client, rogue, req_id="rpc-remedy")).json()
    assert rpc["error"]["message"] == sentence
    assert rpc["error"]["data"] == {"remedy": "colleague_invite"}

    v1 = await _dispatch_v1(client, rogue, req_id="v1-remedy")
    assert v1.status_code == 403
    assert v1.json() == {"detail": sentence, "remedy": "colleague_invite"}


@pytest.mark.asyncio
async def test_a_refusal_without_a_remedy_carries_no_remedy_field(
    app: FastAPI, client: AsyncClient
) -> None:
    """Only the pair check names a remedy; a scope refusal's body is unchanged."""
    token = await _mint_token(app, sub="agent:rogue", scope=["dispatch"], aud="cabal-core")
    rest = await _dispatch(client, token, task_id="t-no-remedy")
    assert rest.status_code in (401, 403), rest.text
    assert set(rest.json()) == {"detail"}


@pytest.mark.asyncio
async def test_jsonrpc_allows_a_synced_pair(app: FastAPI, client: AsyncClient) -> None:
    """The shared policy must not be refusing everything either."""
    from cabal.core import dispatch_pairs

    async with open_session(app.state.sessionmaker) as session:
        await dispatch_pairs.replace_all(session, [("agent:sizing-agent", TARGET)])

    allowed = await _mint_token(app, sub="agent:sizing-agent", scope=["colleague"])
    rpc = await _dispatch_jsonrpc(client, allowed, req_id="rpc-allowed")
    assert rpc.status_code == 200, rpc.text
    assert rpc.json().get("error") is None, rpc.json()

    v1 = await _dispatch_v1(client, allowed, req_id="v1-allowed")
    assert v1.status_code == 200, v1.text
    assert v1.json()["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED", v1.json()


@pytest.mark.asyncio
async def test_empty_table_is_open_for_any_sub_shape(
    app: FastAPI, client: AsyncClient
) -> None:
    """Empty means open, and it has to mean open for every caller shape.

    An earlier draft refused any sub that was not "agent:<name>" outright,
    which broke the deploy-safety guarantee: with a never-synced table, main
    re-mints such a token and this refused it.
    """
    token = await _mint_token(app, sub="persona:archivist", scope=["colleague"])
    resp = await _dispatch(client, token, task_id="t-any-shape")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_allowed_targets_agrees_with_is_allowed(app: FastAPI) -> None:
    """`allowed_targets` (the directory's set form, cabal#321) must give the
    same answer as `is_allowed` per target, including None for an empty table."""
    from cabal.core import dispatch_pairs

    async with open_session(app.state.sessionmaker) as session:
        assert await dispatch_pairs.allowed_targets(session, "agent:a") is None
        await dispatch_pairs.replace_all(
            session, [("agent:a", "b"), ("agent:a", "c"), ("agent:x", "d")]
        )
        allowed = await dispatch_pairs.allowed_targets(session, "agent:a")
        assert allowed == {"b", "c"}
        for target in ("b", "c", "d"):
            assert (target in allowed) == await dispatch_pairs.is_allowed(
                session, caller="agent:a", target=target
            )
        assert await dispatch_pairs.allowed_targets(session, "agent:z") == set()


# ---- 4. refusals are recorded server-side (#315) ---------------------------


async def _refusal_events(app: FastAPI) -> list[dict]:
    async with open_session(app.state.sessionmaker) as session:
        rows = (
            await session.execute(select(Event).where(Event.event_type == "dispatch_refused"))
        ).scalars().all()
    return [row.payload_json for row in rows]


@pytest.mark.asyncio
async def test_missing_pair_is_logged_and_audited_once(app: FastAPI, client: AsyncClient) -> None:
    from cabal.core import dispatch_pairs

    async with open_session(app.state.sessionmaker) as session:
        await dispatch_pairs.replace_all(session, [("agent:sizing-agent", TARGET)])
    token = await _mint_token(app, sub="agent:rogue", scope=["colleague"])
    with structlog.testing.capture_logs() as logs:
        resp = await _dispatch(client, token, task_id="t-logged")
    assert resp.status_code == 403
    refused = [e for e in logs if e["event"] == "dispatch.refused"]
    assert len(refused) == 1, logs
    entry = refused[0]
    assert entry["log_level"] == "warning"
    assert (entry["caller"], entry["target"], entry["check"]) == ("agent:rogue", TARGET, "pair")
    assert all(token not in str(v) for v in entry.values())
    events = await _refusal_events(app)
    assert len(events) == 1
    assert events[0]["caller"] == "agent:rogue"
    assert events[0]["target"] == TARGET
    assert events[0]["check"] == "pair"
    assert "not a colleague" in events[0]["reason"]
    assert all(token not in str(v) for v in events[0].values())


@pytest.mark.asyncio
async def test_scope_refusal_names_the_scope_check(app: FastAPI, client: AsyncClient) -> None:
    token = await _mint_token(app, sub="agent:nobody", scope=[])
    with structlog.testing.capture_logs() as logs:
        resp = await _dispatch(client, token, task_id="t-scope")
    assert resp.status_code in (401, 403)
    refused = [e for e in logs if e["event"] == "dispatch.refused"]
    assert [e["check"] for e in refused] == ["scope"]
    assert [e["check"] for e in await _refusal_events(app)] == ["scope"]


@pytest.mark.asyncio
async def test_invalid_token_refusal_is_logged_but_never_written(
    app: FastAPI, client: AsyncClient
) -> None:
    """An unverified caller must not get a durable write: anyone who can reach
    the port can send a bad token (PR #316 review)."""
    with structlog.testing.capture_logs() as logs:
        resp = await _dispatch(client, "not-a-jwt", target="x" * 5000, task_id="t-bad")
    assert resp.status_code == 401
    refused = [e for e in logs if e["event"] == "dispatch.refused"]
    assert len(refused) == 1
    assert refused[0]["caller"] is None
    assert refused[0]["check"] == "token"
    assert len(refused[0]["target"]) < 200
    assert await _refusal_events(app) == []


@pytest.mark.asyncio
async def test_recorded_fields_are_bounded(app: FastAPI, client: AsyncClient) -> None:
    token = await _mint_token(app, sub="agent:nobody", scope=[])
    await _dispatch(client, token, target="y" * 5000, task_id="t-long")
    events = await _refusal_events(app)
    assert len(events) == 1
    assert len(events[0]["target"]) < 200 and events[0]["target"].endswith("[truncated]")
    assert len(events[0]["reason"]) < 200


@pytest.mark.asyncio
async def test_a_failed_audit_write_still_refuses(
    app: FastAPI, client: AsyncClient, monkeypatch
) -> None:
    from cabal.core import dispatch_pairs
    from cabal.core.routes import _dispatch_auth

    async with open_session(app.state.sessionmaker) as session:
        await dispatch_pairs.replace_all(session, [("agent:sizing-agent", TARGET)])
    token = await _mint_token(app, sub="agent:rogue", scope=["colleague"])

    real = _dispatch_auth.open_session
    calls = {"n": 0}

    def flaky(sessionmaker):
        # The policy's own session must work; only the audit write fails.
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("db gone")
        return real(sessionmaker)

    monkeypatch.setattr(_dispatch_auth, "open_session", flaky)
    with structlog.testing.capture_logs() as logs:
        resp = await _dispatch(client, token, task_id="t-flaky")
    assert resp.status_code == 403
    assert [e["event"] for e in logs if e["event"].startswith("dispatch.refused")] == [
        "dispatch.refused",
        "dispatch.refused_audit_failed",
    ]


@pytest.mark.asyncio
async def test_allowed_dispatch_records_no_refusal(app: FastAPI, client: AsyncClient) -> None:
    from cabal.core import dispatch_pairs

    async with open_session(app.state.sessionmaker) as session:
        await dispatch_pairs.replace_all(session, [("agent:sizing-agent", TARGET)])
    token = await _mint_token(app, sub="agent:sizing-agent", scope=["colleague"])
    with structlog.testing.capture_logs() as logs:
        resp = await _dispatch(client, token, task_id="t-ok")
    assert resp.status_code == 200, resp.text
    assert not [e for e in logs if e["event"] == "dispatch.refused"]
    assert await _refusal_events(app) == []


@pytest.mark.asyncio
async def test_jsonrpc_refusal_is_recorded_too(app: FastAPI, client: AsyncClient) -> None:
    from cabal.core import dispatch_pairs

    async with open_session(app.state.sessionmaker) as session:
        await dispatch_pairs.replace_all(session, [("agent:sizing-agent", TARGET)])
    token = await _mint_token(app, sub="agent:rogue", scope=["colleague"])
    await _dispatch_jsonrpc(client, token)
    assert [e["check"] for e in await _refusal_events(app)] == ["pair"]


@pytest.mark.asyncio
async def test_syncing_an_empty_pairs_table_warns(app: FastAPI, client: AsyncClient) -> None:
    op = await _operator_token(app, client)
    headers = {"Authorization": f"Bearer {op}"}
    with structlog.testing.capture_logs() as logs:
        r1 = await client.put(
            "/admin/dispatch-pairs", json={"pairs": [["agent:a", TARGET]]}, headers=headers
        )
        r2 = await client.put("/admin/dispatch-pairs", json={"pairs": []}, headers=headers)
    assert r1.status_code == 200 and r2.status_code == 200
    events = [(e["event"], e["log_level"], e["count"]) for e in logs if "dispatch_pairs" in e["event"]]
    assert events == [
        ("dispatch_pairs.replaced", "info", 1),
        ("dispatch_pairs.replaced_empty", "warning", 0),
    ]
