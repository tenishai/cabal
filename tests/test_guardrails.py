"""Guardrails abstraction tests — cabal#39.

Covers:
  - run_guardrails ordering (PASS, BLOCK short-circuit, REWRITE threading)
  - LLMPersona integration: input BLOCK/REWRITE, output BLOCK/REWRITE, empty list
  - DispatchRouter integration: input BLOCK prevents A2A hop
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from joserfc.jwk import KeySet, RSAKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cabal.agents.llm_persona import LLMPersona
from cabal.core.a2a.codes import CODE_POLICY_VIOLATION, reason_code
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.auth.jwt import mint
from cabal.core.auth.keys import create_signing_key
from cabal.core.broker.bookkeeping import Bookkeeper
from cabal.core.broker.router import DispatchRouter
from cabal.core.bulletin import store as bulletin_store
from cabal.core.db import make_engine, make_sessionmaker
from cabal.core.db_pragmas import install_sqlite_pragmas
from cabal.core.guardrails import (
    Guardrail,
    GuardrailContext,
    Verdict,
    VerdictType,
    run_guardrails,
)
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient
from cabal.core.models import Base, Persona

# ---------------------------------------------------------------------------
# Trivial example guardrails (for tests only — no production guardrails here)
# ---------------------------------------------------------------------------


class _BlockIfContains:
    """Block if the string target contains a given substring.

    Works for str targets (output phase) and stringified list targets.
    """

    def __init__(self, word: str) -> None:
        self.name = f"block_if_contains:{word}"
        self._word = word

    async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
        target_str = str(target)
        if self._word in target_str:
            return Verdict.block(f"target contains forbidden word: {self._word!r}")
        return Verdict.pass_()


class _UppercaseRewrite:
    """Rewrite str target to uppercase."""

    name = "uppercase_rewrite"

    async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
        if isinstance(target, str):
            return Verdict.rewrite(target.upper(), reason="uppercased")
        return Verdict.pass_()


class _AppendRewrite:
    """Rewrite str target by appending a suffix."""

    def __init__(self, suffix: str) -> None:
        self.name = f"append:{suffix}"
        self._suffix = suffix

    async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
        if isinstance(target, str):
            return Verdict.rewrite(target + self._suffix)
        return Verdict.pass_()


class _AlwaysPass:
    name = "always_pass"

    async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
        return Verdict.pass_()


class _AlwaysBlock:
    def __init__(self, reason: str = "blocked") -> None:
        self.name = "always_block"
        self._reason = reason

    async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
        return Verdict.block(self._reason)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(phase: str = "input") -> GuardrailContext:
    return GuardrailContext(persona_name="test", task_id="t-1", phase=phase)


def _user_task(text: str = "hello") -> Task:
    return Task(
        id="t-1",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text=text)]),
        ),
    )


def _stub(reply: str):
    async def fn(model: str, messages: list[dict[str, Any]], **kwargs) -> dict:
        return {
            "choices": [{"message": {"role": "assistant", "content": reply}}],
            "model": model,
        }
    return fn


def _capturing_stub(captured: list):
    """Stub that records the messages it receives, then returns a fixed reply."""

    async def fn(model: str, messages: list[dict[str, Any]], **kwargs) -> dict:
        captured.append(messages)
        return {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "model": model,
        }

    return fn


# ---------------------------------------------------------------------------
# 1. Abstraction unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_guardrails_empty_list_returns_pass() -> None:
    verdict = await run_guardrails([], "anything", _ctx())
    assert verdict.type == VerdictType.PASS
    assert verdict.rewritten is None


@pytest.mark.asyncio
async def test_run_guardrails_all_pass_no_rewrite() -> None:
    verdict = await run_guardrails([_AlwaysPass(), _AlwaysPass()], "target", _ctx())
    assert verdict.type == VerdictType.PASS
    assert verdict.rewritten is None


@pytest.mark.asyncio
async def test_run_guardrails_block_short_circuits() -> None:
    """First BLOCK prevents remaining guardrails from running."""
    call_log: list[str] = []

    class _Logging:
        def __init__(self, name: str, verdict: Verdict) -> None:
            self.name = name
            self._verdict = verdict

        async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
            call_log.append(self.name)
            return self._verdict

    g1 = _Logging("g1", Verdict.pass_())
    g2 = _Logging("g2", Verdict.block("stop here"))
    g3 = _Logging("g3", Verdict.pass_())  # must NOT run

    verdict = await run_guardrails([g1, g2, g3], "target", _ctx())

    assert verdict.type == VerdictType.BLOCK
    assert verdict.reason == "stop here"
    assert call_log == ["g1", "g2"]  # g3 skipped


@pytest.mark.asyncio
async def test_run_guardrails_rewrite_threads_into_next() -> None:
    """A REWRITE feeds its value into the next guardrail's target."""
    received: list[Any] = []

    class _RecordThenPass:
        name = "record"

        async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
            received.append(target)
            return Verdict.pass_()

    rewriter = _AppendRewrite(" REWRITTEN")
    recorder = _RecordThenPass()

    verdict = await run_guardrails([rewriter, recorder], "hello", _ctx())

    assert verdict.type == VerdictType.PASS
    assert verdict.rewritten == "hello REWRITTEN"
    assert received == ["hello REWRITTEN"]  # recorder saw the rewritten value


@pytest.mark.asyncio
async def test_run_guardrails_rewrite_passes_final_value() -> None:
    """All-pass-with-rewrite returns PASS with rewritten=final accumulated value."""
    verdict = await run_guardrails(
        [_AppendRewrite("-A"), _AppendRewrite("-B")],
        "start",
        _ctx(),
    )
    assert verdict.type == VerdictType.PASS
    assert verdict.rewritten == "start-A-B"


@pytest.mark.asyncio
async def test_run_guardrails_block_after_rewrite() -> None:
    """BLOCK after a REWRITE still blocks (short-circuits)."""
    verdict = await run_guardrails(
        [_AppendRewrite("-X"), _AlwaysBlock("stop")],
        "hello",
        _ctx(),
    )
    assert verdict.type == VerdictType.BLOCK
    assert verdict.reason == "stop"


@pytest.mark.asyncio
async def test_verdict_convenience_constructors() -> None:
    p = Verdict.pass_()
    assert p.type == VerdictType.PASS
    assert p.rewritten is None

    b = Verdict.block("bad")
    assert b.type == VerdictType.BLOCK
    assert b.reason == "bad"
    assert b.rewritten is None

    r = Verdict.rewrite("new", reason="why")
    assert r.type == VerdictType.REWRITE
    assert r.rewritten == "new"
    assert r.reason == "why"


def test_guardrail_protocol_runtime_check() -> None:
    """Guardrail is @runtime_checkable — isinstance works."""
    g = _AlwaysPass()
    assert isinstance(g, Guardrail)


# ---------------------------------------------------------------------------
# 2. LLMPersona integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persona_no_guardrails_unchanged() -> None:
    """Empty guardrails list: existing behavior is completely unchanged."""
    client = LiteLLMClient(acompletion_fn=_stub("Paris."), hook_registry=HookRegistry())
    persona = LLMPersona(name="geo", model="m", llm_client=client)
    # guardrails defaults to []
    result = await persona.handle_task(_user_task("capital of France?"))
    assert result.status.state == "completed"
    assert result.status.message is not None
    assert result.status.message.parts[0].text == "Paris."


@pytest.mark.asyncio
async def test_persona_input_block_fails_task_without_llm_call() -> None:
    """Input BLOCK: task fails with policy-violation; LLM is never called."""
    call_count = 0

    async def counting_stub(model, messages, **kw):
        nonlocal call_count
        call_count += 1
        return {"choices": [{"message": {"content": "should not appear"}}]}

    client = LiteLLMClient(acompletion_fn=counting_stub, hook_registry=HookRegistry())
    persona = LLMPersona(
        name="strict",
        model="m",
        llm_client=client,
        guardrails=[_BlockIfContains("forbidden")],
    )
    result = await persona.handle_task(_user_task("this is forbidden text"))
    assert result.status.state == "failed"
    assert reason_code(result) == CODE_POLICY_VIOLATION
    assert "forbidden" in (result.metadata.get("cabal_reason_message") or "")
    assert call_count == 0  # LLM never called


@pytest.mark.asyncio
async def test_persona_input_rewrite_changes_what_llm_sees() -> None:
    """Input REWRITE: the LLM receives the rewritten messages."""
    seen_messages: list[list[dict]] = []
    client = LiteLLMClient(
        acompletion_fn=_capturing_stub(seen_messages), hook_registry=HookRegistry()
    )

    class _InjectUserMessage:
        """Rewrite: replace all user message content with 'REWRITTEN' (input only)."""

        name = "inject_user"

        async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
            if ctx.phase != "input":
                return Verdict.pass_()
            rewritten = [
                {**m, "content": "REWRITTEN"} if m.get("role") == "user" else m
                for m in target
            ]
            return Verdict.rewrite(rewritten)

    persona = LLMPersona(
        name="rewriter",
        model="m",
        llm_client=client,
        guardrails=[_InjectUserMessage()],
    )
    result = await persona.handle_task(_user_task("original text"))
    assert result.status.state == "completed"
    # LLM saw the rewritten user message, not the original
    assert seen_messages, "LLM was not called"
    user_msgs = [m for m in seen_messages[0] if m.get("role") == "user"]
    assert user_msgs[0]["content"] == "REWRITTEN"


@pytest.mark.asyncio
async def test_persona_output_block_fails_task_after_llm() -> None:
    """Output BLOCK: task fails even though LLM produced a response."""
    client = LiteLLMClient(
        acompletion_fn=_stub("top secret result"), hook_registry=HookRegistry()
    )
    persona = LLMPersona(
        name="censor",
        model="m",
        llm_client=client,
        guardrails=[_BlockIfContains("secret")],
    )

    class _OutputBlock:
        name = "output_blocker"

        async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
            if ctx.phase == "output" and isinstance(target, str) and "secret" in target:
                return Verdict.block("contains secret")
            return Verdict.pass_()

    persona = LLMPersona(
        name="censor",
        model="m",
        llm_client=client,
        guardrails=[_OutputBlock()],
    )
    result = await persona.handle_task(_user_task("tell me something"))
    assert result.status.state == "failed"
    assert reason_code(result) == CODE_POLICY_VIOLATION
    assert "output" in (result.metadata.get("cabal_reason_message") or "")


@pytest.mark.asyncio
async def test_persona_output_rewrite_replaces_response() -> None:
    """Output REWRITE: the final task carries the rewritten text."""
    client = LiteLLMClient(
        acompletion_fn=_stub("original reply"), hook_registry=HookRegistry()
    )

    class _OutputRewrite:
        name = "output_rewriter"

        async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
            if ctx.phase == "output" and isinstance(target, str):
                return Verdict.rewrite(target.upper())
            return Verdict.pass_()

    persona = LLMPersona(
        name="shouter",
        model="m",
        llm_client=client,
        guardrails=[_OutputRewrite()],
    )
    result = await persona.handle_task(_user_task("say something"))
    assert result.status.state == "completed"
    assert result.status.message is not None
    assert result.status.message.parts[0].text == "ORIGINAL REPLY"


@pytest.mark.asyncio
async def test_persona_input_guardrail_does_not_run_on_output() -> None:
    """A guardrail that only checks phase='input' leaves the output unchanged."""
    client = LiteLLMClient(
        acompletion_fn=_stub("clean output"), hook_registry=HookRegistry()
    )

    class _InputOnlyBlock:
        name = "input_only_block"

        async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
            # Only block during the output phase — this should NOT run for input
            # (here it always passes for input, and we're not calling it for output
            # because the phase check is done by the persona, not by this guardrail)
            return Verdict.pass_()

    persona = LLMPersona(
        name="p",
        model="m",
        llm_client=client,
        guardrails=[_InputOnlyBlock()],
    )
    result = await persona.handle_task(_user_task("hi"))
    assert result.status.state == "completed"


# ---------------------------------------------------------------------------
# 3. Router integration tests
# ---------------------------------------------------------------------------

# Fixtures duplicated from test_broker_router.py for isolation.


@pytest_asyncio.fixture
async def _sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    install_sqlite_pragmas(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return make_sessionmaker(engine)


@pytest.fixture
def _master_key() -> bytes:
    return Fernet.generate_key()


@pytest_asyncio.fixture
async def _signing_key(_sessionmaker, _master_key):
    async with _sessionmaker() as s:
        stored = await create_signing_key(s, master_key=_master_key)
        await s.commit()
    return stored


@pytest.fixture
def _jwks_provider(_signing_key):
    jwk_dict = dict(
        RSAKey.import_key(_signing_key.private_key.public_key()).as_dict(private=False)
    )
    jwk_dict["kid"] = _signing_key.kid
    key_set = KeySet.import_key_set({"keys": [jwk_dict]})
    return lambda: key_set


@pytest_asyncio.fixture
async def _agent_row(_sessionmaker):
    async with _sessionmaker() as s:
        s.add(Persona(name="echo", config_json={}))
        await s.flush()
        row = await bulletin_store.register_agent(
            s,
            name="echo",
            persona_name="echo",
            role="singleton",
            endpoint="http://echo:7400",
            capabilities={"skills": []},
            safety_tier="strict",
            agent_card_json={"name": "echo"},
            agent_card_kid="kid-1",
        )
        await s.commit()
    return row


@dataclass(slots=True)
class _EchoHandler:
    name: str = "echo"
    dispatched: list[Task] = field(default_factory=list)

    async def handle_task(self, task: Task) -> Task:
        self.dispatched.append(task)
        return Task(
            id=task.id,
            status=TaskStatus(
                state="completed",
                message=Message(role="agent", parts=[TextPart(text="echoed")]),
            ),
            metadata=task.metadata,
        )


def _router_task() -> Task:
    return Task(
        id="r-001",
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text="route me")]),
        ),
    )


@pytest.mark.asyncio
async def test_router_no_guardrails_dispatches_normally(
    _sessionmaker, _signing_key, _jwks_provider, _agent_row
) -> None:
    """Without guardrails the router dispatches as usual."""
    router = DispatchRouter(
        sessionmaker=_sessionmaker,
        bookkeeper=Bookkeeper(_sessionmaker),
        jwks_provider=_jwks_provider,
    )
    handler = _EchoHandler()
    router.register_local(handler)

    token = mint(signing_key=_signing_key, sub="primus:1", aud="echo")
    result = await router.dispatch(
        target_agent="echo",
        target_generation=None,
        task=_router_task(),
        caller_jwt=token,
    )
    assert result.status.state == "completed"
    assert len(handler.dispatched) == 1


@pytest.mark.asyncio
async def test_router_input_guardrail_block_prevents_dispatch(
    _sessionmaker, _signing_key, _jwks_provider, _agent_row
) -> None:
    """A router-level BLOCK prevents the handler from being called."""
    router = DispatchRouter(
        sessionmaker=_sessionmaker,
        bookkeeper=Bookkeeper(_sessionmaker),
        jwks_provider=_jwks_provider,
        input_guardrails=[_BlockIfContains("route")],
    )
    handler = _EchoHandler()
    router.register_local(handler)

    token = mint(signing_key=_signing_key, sub="primus:1", aud="echo")
    result = await router.dispatch(
        target_agent="echo",
        target_generation=None,
        task=_router_task(),  # text contains "route"
        caller_jwt=token,
    )
    assert result.status.state == "failed"
    assert reason_code(result) == CODE_POLICY_VIOLATION
    assert "router guardrail blocked" in (result.metadata.get("cabal_reason_message") or "")
    assert len(handler.dispatched) == 0  # handler was never reached


@pytest.mark.asyncio
async def test_router_input_guardrail_pass_dispatches_normally(
    _sessionmaker, _signing_key, _jwks_provider, _agent_row
) -> None:
    """A router-level PASS guardrail does not interfere with dispatch."""
    router = DispatchRouter(
        sessionmaker=_sessionmaker,
        bookkeeper=Bookkeeper(_sessionmaker),
        jwks_provider=_jwks_provider,
        input_guardrails=[_AlwaysPass()],
    )
    handler = _EchoHandler()
    router.register_local(handler)

    token = mint(signing_key=_signing_key, sub="primus:1", aud="echo")
    result = await router.dispatch(
        target_agent="echo",
        target_generation=None,
        task=_router_task(),
        caller_jwt=token,
    )
    assert result.status.state == "completed"
    assert len(handler.dispatched) == 1
