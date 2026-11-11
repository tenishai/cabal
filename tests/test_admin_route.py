"""/admin/agents tests."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from cabal.core.models import BootstrapCode


async def _peek_bootstrap_code(app: FastAPI) -> str:
    async with app.state.sessionmaker() as s:
        result = await s.execute(select(BootstrapCode).where(BootstrapCode.used_at.is_(None)))
        return result.scalar_one().code


@pytest.mark.asyncio
async def test_admin_agents_requires_operator_scope(app: FastAPI, client: AsyncClient) -> None:
    # Build a non-operator token
    from cabal.core.auth.jwt import mint
    from cabal.core.auth.keys import load_active_signing_key
    from cabal.core.db import open_session

    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=app.state.master_key)
    assert stored is not None
    persona_token = mint(
        signing_key=stored,
        sub="persona:archivist",
        aud="cabal-core",
        scope=["dispatch"],
    )
    resp = await client.get("/admin/agents", headers={"Authorization": f"Bearer {persona_token}"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_agents_lists_echo_for_operator(app: FastAPI, client: AsyncClient) -> None:
    code = await _peek_bootstrap_code(app)
    r = await client.post("/auth/bootstrap", json={"code": code})
    token = r.json()["access_token"]

    resp = await client.get("/admin/agents", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    names = {a["name"] for a in body["agents"]}
    assert "echo" in names
    echo = next(a for a in body["agents"] if a["name"] == "echo")
    assert echo["status"] == "active"
    assert echo["role"] == "builtin"


@pytest.mark.asyncio
async def test_admin_agents_unauthenticated_rejected(client: AsyncClient) -> None:
    resp = await client.get("/admin/agents")
    assert resp.status_code == 401


# ---- cabal#2: POST /admin/tasks/{id}/resume -------------------------------


async def _operator_token(app: FastAPI, client: AsyncClient) -> str:
    code = await _peek_bootstrap_code(app)
    r = await client.post("/auth/bootstrap", json={"code": code})
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_admin_resume_404_for_unknown_task(app: FastAPI, client: AsyncClient) -> None:
    token = await _operator_token(app, client)
    resp = await client.post(
        "/admin/tasks/does-not-exist/resume",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_admin_resume_requires_operator_scope(app: FastAPI, client: AsyncClient) -> None:
    from cabal.core.auth.jwt import mint
    from cabal.core.auth.keys import load_active_signing_key
    from cabal.core.db import open_session

    async with open_session(app.state.sessionmaker) as session:
        stored = await load_active_signing_key(session, master_key=app.state.master_key)
    assert stored is not None
    persona_token = mint(
        signing_key=stored,
        sub="persona:archivist",
        aud="cabal-core",
        scope=["dispatch"],
    )
    resp = await client.post(
        "/admin/tasks/some-task/resume",
        headers={"Authorization": f"Bearer {persona_token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_resume_replays_from_checkpoint(app: FastAPI, client: AsyncClient) -> None:
    """End-to-end: seed a checkpoint + crashed Conversation row, register
    a fake LocalHandler that succeeds on resume, then POST resume."""
    from cabal.agents.llm_persona import LLMPersona
    from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
    from cabal.core.checkpoint import Checkpoint
    from cabal.core.hooks.registry import HookRegistry
    from cabal.core.llm.litellm_client import LiteLLMClient
    from cabal.core.models import Agent

    token = await _operator_token(app, client)

    # 1. Register a fake bulletin row + LocalHandler the resume endpoint
    #    can dispatch to. Bookkeeper.on_dispatch_start requires an Agent
    #    row to FK against.
    from sqlalchemy import select as _select

    from cabal.core.db import open_session

    saved_task = Task(
        id="resume-me",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text="resume me")]),
        ),
        metadata={},
    )
    # Save the checkpoint FIRST so the Conversation.checkpoint_id FK
    # can point at it.
    await app.state.checkpoint_store.save(
        Checkpoint(
            task_id="resume-me",
            step_index=0,
            messages=[
                {"role": "system", "content": "you are echo"},
                {"role": "user", "content": "resume me"},
            ],
            task_json=saved_task.model_dump(by_alias=True, exclude_none=True),
            persona_state={"session_id": "s-1"},
        )
    )
    async with open_session(app.state.sessionmaker) as session:
        existing_echo = (
            await session.execute(_select(Agent).where(Agent.name == "echo", Agent.generation == 1))
        ).scalar_one()
        from cabal.core.models import Conversation

        conv = Conversation(
            task_id="resume-me",
            target_agent="echo",
            target_generation=existing_echo.generation,
            caller_agent=None,
            state="working",
            checkpoint_id="resume-me",
        )
        session.add(conv)

    # 3. Register a fresh LLMPersona-as-LocalHandler for `echo` that
    #    completes on resume. The existing echo handler doesn't write
    #    checkpoints; swap in one that does for this test.
    async def healthy_llm(model, messages, **kwargs):
        return {"choices": [{"message": {"content": "resumed cleanly"}}]}

    resumed_persona = LLMPersona(
        name="echo",
        model="x",
        llm_client=LiteLLMClient(acompletion_fn=healthy_llm, hook_registry=HookRegistry()),
        checkpoint_store=app.state.checkpoint_store,
    )
    app.state.dispatch_router.register_local(resumed_persona)

    # 4. POST resume.
    resp = await client.post(
        "/admin/tasks/resume-me/resume",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] == "resume-me"
    assert body["state"] == "completed"
    text_parts = body["result"]["status"]["message"]["parts"]
    assert any("resumed cleanly" in p.get("text", "") for p in text_parts)

    # 5. Checkpoint dropped on completion.
    assert await app.state.checkpoint_store.load("resume-me") is None


@pytest.mark.asyncio
async def test_admin_resume_409_while_the_task_is_still_running(app: FastAPI, client: AsyncClient) -> None:
    """A checkpointing persona has a row and a checkpoint mid-run; resuming it
    then would run it twice and unlist the first (PR 407 round 3)."""
    from datetime import UTC, datetime

    from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
    from cabal.core.checkpoint import Checkpoint
    from cabal.core.db import open_session
    from cabal.core.models import Conversation
    from cabal.core.running import RunningTask

    token = await _operator_token(app, client)
    saved = Task(
        id="busy-1",
        status=TaskStatus(state="working", message=Message(role="user", parts=[TextPart(text="x")])),
        metadata={},
    )
    await app.state.checkpoint_store.save(
        Checkpoint(
            task_id="busy-1",
            step_index=0,
            messages=[{"role": "user", "content": "x"}],
            task_json=saved.model_dump(by_alias=True, exclude_none=True),
            persona_state={},
        )
    )
    async with open_session(app.state.sessionmaker) as session:
        session.add(
            Conversation(
                task_id="busy-1", target_agent="echo", target_generation=1, caller_agent=None,
                state="working", checkpoint_id="busy-1",
            )
        )
    running = app.state.dispatch_router.running
    entry = RunningTask(task_id="busy-1", target_agent="echo", caller_agent=None, started_at=datetime.now(UTC))
    running.add(entry)
    resp = await client.post("/admin/tasks/busy-1/resume", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 409, resp.text
    assert "still running" in resp.text
    assert running.get("busy-1") is entry
