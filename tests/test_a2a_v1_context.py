"""contextId and multi-turn SendMessage on the A2A v1.0 path (#276)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from cabal.config.identity import CabalConfig, IdentityConfig
from cabal.core.a2a.codes import CODE_BUDGET_EXHAUSTED, CabalReason, annotate
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.auth.jwt import mint
from cabal.core.auth.keys import load_active_signing_key
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import open_session
from cabal.core.models import Conversation, Persona


async def _token(app: FastAPI, sub: str) -> str:
    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=app.state.master_key)
    assert stored is not None
    return mint(signing_key=stored, sub=sub, aud="cabal-core", scope=["colleague"])


async def _send(
    client: AsyncClient, token: str, text: str, *, agent: str = "echo", **message: Any
) -> Response:
    msg = {"messageId": f"m-{text}", "role": "ROLE_USER", "parts": [{"text": text}], **message}
    return await client.post(
        f"/agents/{agent}/a2a",
        json={"jsonrpc": "2.0", "id": 7, "method": "SendMessage", "params": {"message": msg}},
        headers={"A2A-Version": "1.0", "Authorization": f"Bearer {token}"},
    )


def _task(r: Response) -> dict[str, Any]:
    body = r.json()
    assert "result" in body, body
    return body["result"]["task"]


def _code(r: Response) -> int:
    assert r.status_code == 200, r.text
    return r.json()["error"]["code"]


class _Recorder:
    """Stands in for a persona (echo by default): records the history each
    dispatch receives, and returns it plus the new message, as a persona does."""

    def __init__(self, name: str = "echo") -> None:
        self.name = name
        self.seen: list[list[str]] = []

    async def handle_task(self, task: Task) -> Task:
        def text(m: Message) -> str:
            return m.parts[0].text  # type: ignore[union-attr]

        self.seen.append([text(m) for m in task.history])
        incoming = task.status.message
        assert incoming is not None
        return Task(
            id=task.id,
            contextId=task.context_id,
            status=TaskStatus(
                state="completed",
                message=Message(role="agent", parts=[TextPart(text=f"re: {text(incoming)}")]),
            ),
            history=[*task.history, incoming],
            metadata=task.metadata,
        )


# ---- contextId ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_generated_context_id_comes_back_and_is_stored(
    app: FastAPI, client: AsyncClient
) -> None:
    token = await _token(app, "agent:alice")
    task = _task(await _send(client, token, "hi"))
    assert task["contextId"]
    async with app.state.sessionmaker() as s:
        row = await s.get(Conversation, task["id"])
    assert row is not None and row.result_json["contextId"] == task["contextId"]


@pytest.mark.asyncio
async def test_a_client_context_id_is_kept_even_when_the_persona_drops_it(
    app: FastAPI, client: AsyncClient
) -> None:
    """The stock EchoAgent returns no contextId; the router restores it."""
    token = await _token(app, "agent:alice")
    task = _task(await _send(client, token, "hi", contextId="ctx-mine"))
    assert task["contextId"] == "ctx-mine"
    async with app.state.sessionmaker() as s:
        row = await s.get(Conversation, task["id"])
    assert row is not None and row.result_json["contextId"] == "ctx-mine"


# ---- context replay -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_new_task_in_a_context_sees_the_conversation_so_far(
    app: FastAPI, client: AsyncClient
) -> None:
    recorder = _Recorder()
    app.state.dispatch_router.register_local(recorder)
    token = await _token(app, "agent:alice")
    for text in ("one", "two", "three"):
        _task(await _send(client, token, text, contextId="ctx-1"))
    assert recorder.seen == [
        [],
        ["one", "re: one"],
        ["one", "re: one", "two", "re: two"],
    ]


@pytest.mark.asyncio
async def test_replay_stays_with_the_caller_and_the_context(
    app: FastAPI, client: AsyncClient
) -> None:
    recorder = _Recorder()
    app.state.dispatch_router.register_local(recorder)
    alice = await _token(app, "agent:alice")
    bob = await _token(app, "agent:bob")
    _task(await _send(client, alice, "alice-1", contextId="ctx-shared"))
    _task(await _send(client, bob, "bob-1", contextId="ctx-shared"))
    _task(await _send(client, alice, "other-ctx", contextId="ctx-other"))
    _task(await _send(client, alice, "alice-2", contextId="ctx-shared"))
    # bob's first task saw nothing of alice's; alice's second sees only hers.
    assert recorder.seen[1] == []
    assert recorder.seen[3] == ["alice-1", "re: alice-1"]


@pytest.mark.asyncio
async def test_replay_stays_with_the_persona(app: FastAPI, client: AsyncClient) -> None:
    async with app.state.sessionmaker() as s:
        s.add(Persona(name="second", config_json={}))
        await s.flush()
        await bulletin_store.register_agent(
            s,
            name="second",
            persona_name="second",
            role="singleton",
            endpoint="local://second",
            capabilities={"skills": []},
            safety_tier="moderate",
            agent_card_json={"name": "second"},
            agent_card_kid="kid-second",
        )
        await s.commit()
    echo, second = _Recorder(), _Recorder("second")
    app.state.dispatch_router.register_local(echo)
    app.state.dispatch_router.register_local(second)
    token = await _token(app, "agent:alice")
    _task(await _send(client, token, "to-echo", contextId="ctx-1"))
    _task(await _send(client, token, "to-second", agent="second", contextId="ctx-1"))
    assert second.seen == [[]]


class _Refuser:
    """Stands in for echo and ends the turn as cabal does for a budget or
    policy refusal: `annotate` keeps the earlier history and drops the incoming
    message."""

    name = "echo"

    async def handle_task(self, task: Task) -> Task:
        return annotate(task, CabalReason(code=CODE_BUDGET_EXHAUSTED, message="budget spent"))


@pytest.mark.asyncio
async def test_a_turn_cabal_ended_is_replayed_with_the_callers_message(
    app: FastAPI, client: AsyncClient
) -> None:
    recorder = _Recorder()
    app.state.dispatch_router.register_local(recorder)
    token = await _token(app, "agent:alice")
    _task(await _send(client, token, "one", contextId="ctx-1"))
    app.state.dispatch_router.register_local(_Refuser())
    refused = _task(await _send(client, token, "two", contextId="ctx-1"))
    assert refused["status"]["state"] == "TASK_STATE_FAILED"
    app.state.dispatch_router.register_local(recorder)
    _task(await _send(client, token, "three", contextId="ctx-1"))
    assert recorder.seen[-1] == ["one", "re: one", "two", "budget spent"]


@pytest.mark.asyncio
async def test_a_repeated_message_is_kept_when_cabal_ends_the_turn(
    app: FastAPI, client: AsyncClient
) -> None:
    """The same text and messageId twice in one conversation: the second,
    ended by cabal, is still stored, so the next turn replays both."""
    recorder = _Recorder()
    app.state.dispatch_router.register_local(recorder)
    token = await _token(app, "agent:alice")
    _task(await _send(client, token, "ok", contextId="ctx-1"))
    app.state.dispatch_router.register_local(_Refuser())
    _task(await _send(client, token, "ok", contextId="ctx-1"))
    app.state.dispatch_router.register_local(recorder)
    _task(await _send(client, token, "next", contextId="ctx-1"))
    assert recorder.seen[-1] == ["ok", "re: ok", "ok", "budget spent"]


@pytest.mark.asyncio
async def test_a_task_without_a_client_context_replays_nothing(
    app: FastAPI, client: AsyncClient
) -> None:
    recorder = _Recorder()
    app.state.dispatch_router.register_local(recorder)
    token = await _token(app, "agent:alice")
    _task(await _send(client, token, "one"))
    _task(await _send(client, token, "two"))
    assert recorder.seen == [[], []]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit, expected", [(2, ["two", "re: two"]), (0, [])])
async def test_replay_keeps_the_newest_configured_messages(
    app: FastAPI, client: AsyncClient, monkeypatch, limit, expected
) -> None:
    recorder = _Recorder()
    app.state.dispatch_router.register_local(recorder)
    monkeypatch.setattr(
        app.state, "identity", IdentityConfig(cabal=CabalConfig(context_replay_messages=limit))
    )
    token = await _token(app, "agent:alice")
    for text in ("one", "two", "three"):
        _task(await _send(client, token, text, contextId="ctx-1"))
    assert recorder.seen[2] == expected


# ---- a message that names a task -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_task_id_that_is_unknown_or_not_yours_is_not_found(
    app: FastAPI, client: AsyncClient
) -> None:
    alice = await _token(app, "agent:alice")
    bob = await _token(app, "agent:bob")
    task = _task(await _send(client, alice, "hi"))
    assert _code(await _send(client, alice, "again", taskId="no-such-task")) == -32001
    assert _code(await _send(client, bob, "again", taskId=task["id"])) == -32001


@pytest.mark.asyncio
async def test_a_mismatched_context_id_is_refused(app: FastAPI, client: AsyncClient) -> None:
    token = await _token(app, "agent:alice")
    task = _task(await _send(client, token, "hi", contextId="ctx-a"))
    r = await _send(client, token, "again", taskId=task["id"], contextId="ctx-b")
    assert _code(r) == -32602
    assert "contextId" in r.json()["error"]["message"]


@pytest.mark.asyncio
async def test_a_finished_task_takes_no_new_turn(app: FastAPI, client: AsyncClient) -> None:
    token = await _token(app, "agent:alice")
    task = _task(await _send(client, token, "hi", contextId="ctx-a"))
    r = await _send(client, token, "again", taskId=task["id"], contextId="ctx-a")
    assert _code(r) == -32004
    assert "completed; send a new message in its contextId" in r.json()["error"]["message"]


@pytest.mark.asyncio
async def test_a_paused_task_with_nothing_stored_takes_no_answer(app: FastAPI, client: AsyncClient) -> None:
    async with app.state.sessionmaker() as s:
        echo = await bulletin_store.get_active(s, "echo")
        assert echo is not None
        s.add(
            Conversation(
                task_id="t-paused",
                target_agent="echo",
                target_generation=echo.generation,
                caller_agent="agent:alice",
                state="input-required",
            )
        )
        await s.commit()
    token = await _token(app, "agent:alice")
    r = await _send(client, token, "answer", taskId="t-paused")
    assert _code(r) == -32004
    assert "no stored state to continue" in r.json()["error"]["message"]


@pytest.mark.asyncio
async def test_a_running_task_takes_no_answer(app: FastAPI, client: AsyncClient) -> None:
    async with app.state.sessionmaker() as s:
        echo = await bulletin_store.get_active(s, "echo")
        assert echo is not None
        s.add(
            Conversation(
                task_id="t-working",
                target_agent="echo",
                target_generation=echo.generation,
                caller_agent="agent:alice",
                state="working",
            )
        )
        await s.commit()
    token = await _token(app, "agent:alice")
    r = await _send(client, token, "answer", taskId="t-working")
    assert _code(r) == -32004
    assert "the task is working; only a task in input-required takes an answer" in r.json()["error"]["message"]
