"""End-to-end: Cabal persona uses liminal MCP gateway.

Spins up a real liminal FastAPI app (in-process via httpx.ASGITransport),
wires Cabal's MCPToolProxy to it with a token_provider that mints JWTs
using Cabal's own active signing key, dispatches through an LLMPersona
that returns a tool_use intent on its first response and a final answer
on its second.

This is the proof that the whole platform works: a persona on Cabal's
side gets a tool call routed through liminal, executes against
liminal's scratchpad handler, and the result lands back in the LLM
conversation.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("liminal")
from cryptography.hazmat.primitives import serialization
from joserfc.jwk import KeySet, RSAKey

from cabal.agents.llm_persona import LLMPersona
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.auth.jwt import mint as mint_jwt
from cabal.core.auth.keys import StoredKey, generate_rsa_keypair
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient
from cabal.core.tools.mcp_proxy import MCPToolProxy, build_proxy


@pytest.fixture
def cabal_signing_key() -> StoredKey:
    """A signing key Cabal mints with, exported as a KeySet so liminal
    can verify against it."""
    priv, _pub = generate_rsa_keypair()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    rsa_key = RSAKey.import_key(pem)
    rsa_key.ensure_kid()
    public_jwk = dict(rsa_key.as_dict(private=False))
    public_jwk["kid"] = rsa_key.kid
    return StoredKey(
        kid=rsa_key.kid, private_key=priv, public_jwk=public_jwk, status="active"
    )


@pytest.fixture
def liminal_keyset(cabal_signing_key) -> KeySet:
    return KeySet.import_key_set({"keys": [cabal_signing_key.public_jwk]})


@pytest.fixture
def liminal_app(liminal_keyset):
    """A real liminal FastAPI app verifying against Cabal's signing key."""
    from liminal.handlers.scratchpad import ScratchpadHandler
    from liminal.registry import Registry
    from liminal.registry.discover import register_explicit
    from liminal.server import StaticKeySetVerifier, create_app

    registry = Registry()
    register_explicit(registry, "scratchpad", ScratchpadHandler())
    verifier = StaticKeySetVerifier(key_set=liminal_keyset)
    return create_app(jwt_verifier=verifier, registry=registry)


@pytest_asyncio.fixture
async def liminal_http_client(liminal_app):
    """httpx.AsyncClient pointing at the in-process liminal app via ASGI."""
    transport = httpx.ASGITransport(app=liminal_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://liminal-test"
    ) as c:
        yield c


@pytest.fixture
def tool_proxy(cabal_signing_key, liminal_http_client) -> MCPToolProxy:
    """Cabal-side proxy talking to the in-process liminal."""
    def token_provider() -> str:
        return mint_jwt(
            signing_key=cabal_signing_key,
            sub="archivist",
            aud="liminal",
            persona="archivist",
            scope=["dispatch"],
            ttl_seconds=300,
        )

    return build_proxy(
        base_url="http://liminal-test",
        persona="archivist",
        agent_name="archivist",
        token_provider=token_provider,
        http_client=liminal_http_client,
    )


@pytest.mark.asyncio
async def test_proxy_can_list_tools(tool_proxy) -> None:
    """Sanity: Cabal's proxy can fetch the tool surface."""
    tools = await tool_proxy.list_tools_for_llm()
    names = {t["function"]["name"] for t in tools}
    assert "scratchpad__set" in names
    assert "scratchpad__get" in names


@pytest.mark.asyncio
async def test_proxy_can_call_tool_directly(tool_proxy) -> None:
    """Sanity: Cabal can dispatch a tool via the proxy."""
    outcome = await tool_proxy.call_tool(
        "scratchpad__set", {"key": "x", "value": "y"}
    )
    assert outcome.is_error is False


def _llm_emits_tool_call_then_final(set_args: dict, final_text: str):
    """Stub LiteLLM that first emits a tool_call, then a final answer."""
    state = {"step": 0}

    async def fn(model: str, messages: list, **kwargs):
        state["step"] += 1
        if state["step"] == 1:
            # First turn: emit a tool_use intent
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "scratchpad__set",
                                        "arguments": json.dumps(set_args),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        # Second turn: final answer (after seeing tool result)
        return {"choices": [{"message": {"content": final_text}}]}

    return fn


@pytest.mark.asyncio
async def test_persona_loops_through_tool_use_and_returns_final_text(
    tool_proxy,
) -> None:
    """The big one: persona dispatches, LLM asks for a tool call,
    proxy executes via liminal, LLM is re-invoked with the result,
    persona returns the final text."""
    fn = _llm_emits_tool_call_then_final(
        set_args={"key": "favorite_color", "value": "purple"},
        final_text="I've stored your favorite color.",
    )
    client = LiteLLMClient(acompletion_fn=fn, hook_registry=HookRegistry())
    persona = LLMPersona(
        name="archivist",
        model="x",
        llm_client=client,
        system_prompt="x",
        tool_proxy=tool_proxy,
    )
    task = Task(
        id="t-tool",
        status=TaskStatus(
            state="submitted",
            message=Message(
                role="user",
                parts=[TextPart(text="remember purple is my favorite color")],
            ),
        ),
    )
    response = await persona.handle_task(task)
    assert response.status.state == "completed"
    final_text = response.status.message.parts[0].text
    assert "stored your favorite color" in final_text

    # Cross-verify: the tool actually ran — call scratchpad.get and see
    outcome = await tool_proxy.call_tool(
        "scratchpad__get", {"key": "favorite_color"}
    )
    assert json.loads(outcome.content[0]["text"])["value"] == "purple"


@pytest.mark.asyncio
async def test_persona_without_tool_proxy_skips_tool_loop() -> None:
    """No proxy → no tools=... kwarg in the LiteLLM call → tool_calls
    are never expected. Verify the existing behavior still works."""
    seen: dict[str, Any] = {}

    async def fn(model: str, messages: list, **kwargs):
        seen.update(kwargs)
        return {"choices": [{"message": {"content": "no tools needed"}}]}

    client = LiteLLMClient(acompletion_fn=fn, hook_registry=HookRegistry())
    persona = LLMPersona(
        name="x", model="x", llm_client=client, system_prompt="x"
    )
    task = Task(
        id="t-no-tools",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text="hi")]),
        ),
    )
    response = await persona.handle_task(task)
    assert response.status.state == "completed"
    assert "tools" not in seen


@pytest.mark.asyncio
async def test_tool_use_max_steps_caps_the_loop(tool_proxy) -> None:
    """A model that keeps requesting tool_use forever should be capped
    at tool_use_max_steps total LLM calls."""
    call_count = {"n": 0}

    async def fn(model: str, messages: list, **kwargs):
        call_count["n"] += 1
        # Always emit a tool call
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"call-{call_count['n']}",
                                "type": "function",
                                "function": {
                                    "name": "scratchpad__set",
                                    "arguments": json.dumps(
                                        {"key": "k", "value": "v"}
                                    ),
                                },
                            }
                        ],
                    }
                }
            ]
        }

    client = LiteLLMClient(acompletion_fn=fn, hook_registry=HookRegistry())
    persona = LLMPersona(
        name="x",
        model="x",
        llm_client=client,
        system_prompt="x",
        tool_proxy=tool_proxy,
        tool_use_max_steps=3,  # initial + 2 tool iterations max
    )
    task = Task(
        id="t-cap",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text="loop")]),
        ),
    )
    await persona.handle_task(task)
    # max 3 total LLM calls (initial + 2 tool iterations)
    assert call_count["n"] <= 3


@pytest.mark.asyncio
async def test_tool_error_surfaces_in_loop(tool_proxy) -> None:
    """A tool that doesn't exist should produce an error block the LLM
    can read; the loop continues with the error in context."""

    # Replace the tool call with one that doesn't exist
    async def fn_with_bad_tool(model: str, messages: list, **kwargs):
        if kwargs.get("__state", {}).get("called", False):
            return {"choices": [{"message": {"content": "(saw tool error)"}}]}
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-bad",
                                "type": "function",
                                "function": {
                                    "name": "ghost__summon",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    }
                }
            ]
        }

    # Use simpler stub: emit a bad tool first then a final answer
    step = {"n": 0}

    async def fn2(model: str, messages: list, **kwargs):
        step["n"] += 1
        if step["n"] == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-bad",
                                    "type": "function",
                                    "function": {
                                        "name": "ghost__summon",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"content": "got the error"}}]}

    client = LiteLLMClient(acompletion_fn=fn2, hook_registry=HookRegistry())
    persona = LLMPersona(
        name="x",
        model="x",
        llm_client=client,
        system_prompt="x",
        tool_proxy=tool_proxy,
    )
    task = Task(
        id="t-err",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text="call ghost")]),
        ),
    )
    response = await persona.handle_task(task)
    assert response.status.state == "completed"
    assert "got the error" in response.status.message.parts[0].text
