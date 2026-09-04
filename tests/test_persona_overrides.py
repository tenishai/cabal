"""Phase X: identity.toml persona overrides flow into the registered handler."""

from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from cabal.core.app import create_app
from cabal.core.models import BootstrapCode


def _write_identity(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "identity.toml"
    p.write_text(body)
    return p


@pytest_asyncio.fixture
async def stub_acompletion_captures():
    """A stub LLM that captures the model + system prompt for assertions."""
    captures: list[dict[str, Any]] = []

    async def fn(model: str, messages: list[dict[str, Any]], **kwargs):
        captures.append(
            {
                "model": model,
                "system": next(
                    (m["content"] for m in messages if m["role"] == "system"), ""
                ),
                "max_tokens": kwargs.get("max_tokens"),
                "chat_template_kwargs": kwargs.get("chat_template_kwargs"),
            }
        )
        return {"choices": [{"message": {"content": "ok"}}]}

    return fn, captures


async def _build_app(tmp_path: Path, identity_body: str, acompletion_fn):
    db_file = Path(os.environ["CABAL_DB_URL"].replace("sqlite:///", ""))
    identity_path = _write_identity(tmp_path, identity_body)
    fastapi_app = create_app(
        config_path=str(identity_path),
        db_url=f"sqlite+aiosqlite:///{db_file}",
        acompletion_fn=acompletion_fn,
    )
    return fastapi_app


async def _bootstrap_token(app, client) -> str:
    async with app.state.sessionmaker() as s:
        row = (
            await s.execute(select(BootstrapCode).where(BootstrapCode.used_at.is_(None)))
        ).scalar_one()
    r = await client.post("/auth/bootstrap", json={"code": row.code})
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_system_prompt_override_reaches_llm(
    tmp_path, stub_acompletion_captures
) -> None:
    fn, captures = stub_acompletion_captures
    identity = dedent(
        """
        [personas.archivist]
        role = "singleton"
        safety_tier = "moderate"
        system_prompt_override = "OPERATOR CUSTOM PROMPT for archivist."
        """
    )
    fastapi_app = await _build_app(tmp_path, identity, fn)
    async with fastapi_app.router.lifespan_context(fastapi_app):
        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            token = await _bootstrap_token(fastapi_app, client)
            body = {
                "target": "archivist",
                "task": {
                    "id": "t-prompt",
                    "status": {
                        "state": "submitted",
                        "message": {
                            "role": "user",
                            "parts": [{"kind": "text", "text": "anything"}],
                        },
                    },
                },
            }
            r = await client.post(
                "/a2a/tasks",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200

    assert any(
        "OPERATOR CUSTOM PROMPT for archivist." in cap["system"] for cap in captures
    )


@pytest.mark.asyncio
async def test_model_override_changes_target_model(
    tmp_path, stub_acompletion_captures
) -> None:
    fn, captures = stub_acompletion_captures
    identity = dedent(
        """
        [personas.architect]
        role = "singleton"
        safety_tier = "moderate"
        model_override = "anthropic/claude-haiku-4-5"
        """
    )
    fastapi_app = await _build_app(tmp_path, identity, fn)
    async with fastapi_app.router.lifespan_context(fastapi_app):
        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            token = await _bootstrap_token(fastapi_app, client)
            body = {
                "target": "architect",
                "task": {
                    "id": "t-model",
                    "status": {
                        "state": "submitted",
                        "message": {
                            "role": "user",
                            "parts": [{"kind": "text", "text": "design"}],
                        },
                    },
                },
            }
            r = await client.post(
                "/a2a/tasks",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, r.text

    assert any(cap["model"] == "anthropic/claude-haiku-4-5" for cap in captures)


@pytest.mark.asyncio
async def test_max_tokens_override_reaches_llm(
    tmp_path, stub_acompletion_captures
) -> None:
    fn, captures = stub_acompletion_captures
    identity = dedent(
        """
        [personas.librarian]
        role = "singleton"
        safety_tier = "moderate"
        max_tokens_override = 2048
        """
    )
    fastapi_app = await _build_app(tmp_path, identity, fn)
    async with fastapi_app.router.lifespan_context(fastapi_app):
        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            token = await _bootstrap_token(fastapi_app, client)
            body = {
                "target": "librarian",
                "task": {
                    "id": "t-mt",
                    "status": {
                        "state": "submitted",
                        "message": {
                            "role": "user",
                            "parts": [{"kind": "text", "text": "hi"}],
                        },
                    },
                },
            }
            r = await client.post(
                "/a2a/tasks",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, r.text

    assert any(cap["max_tokens"] == 2048 for cap in captures)


@pytest.mark.asyncio
async def test_commit_to_memory_override_can_disable_archivist(
    tmp_path, stub_acompletion_captures
) -> None:
    """Archivist default is True; operator can flip it off via identity.toml."""
    fn, _ = stub_acompletion_captures
    identity = dedent(
        """
        [personas.archivist]
        role = "singleton"
        safety_tier = "moderate"
        commit_to_memory = false
        """
    )
    fastapi_app = await _build_app(tmp_path, identity, fn)
    async with fastapi_app.router.lifespan_context(fastapi_app):
        # Inspect the registered handler directly
        router = fastapi_app.state.dispatch_router
        handler = router._local["archivist"]  # type: ignore[attr-defined]
        assert handler.commit_to_memory is False


@pytest.mark.asyncio
async def test_no_overrides_keeps_defaults(tmp_path, stub_acompletion_captures) -> None:
    """Identity declaring just a persona uses persona-class defaults."""
    fn, _ = stub_acompletion_captures
    identity = dedent(
        """
        [personas.archivist]
        role = "singleton"
        safety_tier = "moderate"
        """
    )
    fastapi_app = await _build_app(tmp_path, identity, fn)
    async with fastapi_app.router.lifespan_context(fastapi_app):
        router = fastapi_app.state.dispatch_router
        handler = router._local["archivist"]  # type: ignore[attr-defined]
        # Archivist defaults: commit_to_memory=True, sonnet model
        assert handler.commit_to_memory is True
        assert "sonnet" in handler.model.lower()


@pytest.mark.asyncio
async def test_extra_kwargs_from_identity_reach_the_provider_call(
    tmp_path, stub_acompletion_captures
) -> None:
    """identity.toml -> factory -> handler -> litellm, end to end.

    The unit tests in test_persona_extra_kwargs.py set `extra_kwargs` on the
    objects directly, so they pass even if the FACTORY never reads the config.
    This is the test that fails when that wiring is removed.
    """
    fn, captures = stub_acompletion_captures
    identity = dedent(
        """
        [personas.archivist]
        role = "singleton"
        safety_tier = "moderate"
        extra_kwargs = { chat_template_kwargs = { enable_thinking = false } }
        """
    )
    fastapi_app = await _build_app(tmp_path, identity, fn)
    async with fastapi_app.router.lifespan_context(fastapi_app):
        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            token = await _bootstrap_token(fastapi_app, client)
            r = await client.post(
                "/a2a/tasks",
                json={
                    "target": "archivist",
                    "task": {
                        "id": "t-kwargs",
                        "status": {
                            "state": "submitted",
                            "message": {
                                "role": "user",
                                "parts": [{"kind": "text", "text": "anything"}],
                            },
                        },
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200

    assert captures, "expected an LLM call"
    assert all(
        c["chat_template_kwargs"] == {"enable_thinking": False} for c in captures
    ), captures


@pytest.mark.asyncio
async def test_subordinates_inherit_the_primus_extra_kwargs(
    tmp_path, stub_acompletion_captures
) -> None:
    """One line in the persona block covers the whole tree.

    A model-family switch is a property of the tree, not of one hop; requiring
    it per subordinate would mostly produce half-configured trees where the
    primus answers and its implementer hangs.
    """
    fn, captures = stub_acompletion_captures
    identity = dedent(
        """
        [personas.wright]
        role = "primus"
        safety_tier = "moderate"
        extra_kwargs = { chat_template_kwargs = { enable_thinking = false } }

        [personas.wright.subordinates.implementer]
        model = "openai/some-local-model"
        """
    )
    fastapi_app = await _build_app(tmp_path, identity, fn)
    async with fastapi_app.router.lifespan_context(fastapi_app):
        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            token = await _bootstrap_token(fastapi_app, client)
            r = await client.post(
                "/a2a/tasks",
                json={
                    "target": "wright-primus",
                    "task": {
                        "id": "t-inherit",
                        "status": {
                            "state": "submitted",
                            "message": {
                                "role": "user",
                                "parts": [{"kind": "text", "text": "anything"}],
                            },
                        },
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200

    # plan + implementer + aggregate
    assert len(captures) == 3, [c["system"][:40] for c in captures]
    dropped = [
        c["system"].splitlines()[0]
        for c in captures
        if c["chat_template_kwargs"] != {"enable_thinking": False}
    ]
    assert not dropped, f"hops that did not inherit: {dropped}"


@pytest.mark.asyncio
async def test_a_subordinate_can_opt_out_with_an_empty_dict(
    tmp_path, stub_acompletion_captures
) -> None:
    """`extra_kwargs = {}` sends nothing on that hop — documented, now pinned.

    identity.toml.example promotes this to operator-facing behaviour, but
    nothing held the factory to it: changing the `is not None` check to a
    truthiness test silently converts the opt-out into an inherit, and every
    other test stays green.
    """
    fn, captures = stub_acompletion_captures
    identity = dedent(
        """
        [personas.wright]
        role = "primus"
        safety_tier = "moderate"
        extra_kwargs = { chat_template_kwargs = { enable_thinking = false } }

        [personas.wright.subordinates.implementer]
        model = "openai/some-local-model"
        extra_kwargs = {}
        """
    )
    fastapi_app = await _build_app(tmp_path, identity, fn)
    async with fastapi_app.router.lifespan_context(fastapi_app):
        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            token = await _bootstrap_token(fastapi_app, client)
            r = await client.post(
                "/a2a/tasks",
                json={
                    "target": "wright-primus",
                    "task": {
                        "id": "t-optout",
                        "status": {
                            "state": "submitted",
                            "message": {
                                "role": "user",
                                "parts": [{"kind": "text", "text": "anything"}],
                            },
                        },
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200

    # Discriminate on MODEL, not on the word "subordinate" in the prompt: the
    # primus PLAN prompt says "You DELEGATE to subordinates", so a substring
    # test keys the plan hop as a subordinate and a collapsing dict then hides
    # it by last-wins. Caught in review — the assertion landed on the right
    # capture purely by ordering, and adding a reviewer would have moved it.
    subordinate_hops = [c for c in captures if c["model"] == "openai/some-local-model"]
    primus_hops = [c for c in captures if c["model"] != "openai/some-local-model"]

    assert len(subordinate_hops) == 1, captures
    assert len(primus_hops) == 2, "expected the plan and aggregate hops"
    assert subordinate_hops[0]["chat_template_kwargs"] is None, "opt-out ignored"
    # BOTH primus hops, which the previous discriminator never checked.
    for hop in primus_hops:
        assert hop["chat_template_kwargs"] == {"enable_thinking": False}
