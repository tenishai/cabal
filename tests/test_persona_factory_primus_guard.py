"""Regression fence for the role="primus" class-mismatch guard (cabal#68).

`role` is a free-form field, so a persona can be declared `role = "primus"`
while its PERSONA_REGISTRY class is a singleton. Before PR #67 that crashed
daemon startup with a bare TypeError on the primus-only constructor kwargs
(`subordinates`, `hook_registry`). The factory now skips it with
`personas.primus_class_mismatch`; this pins that through the real factory.
"""

from __future__ import annotations

from typing import Any

import structlog

from cabal.config.identity import IdentityConfig, PersonaConfig, SubordinateConfig
from cabal.core.abort import AbortCoordinator
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient
from cabal.core.memory.session_tracker import NoOpSessionTracker
from cabal.core.memory.store import NoOpMemoryStore
from cabal.core.persona_factory import register_personas_from_identity


def _client() -> LiteLLMClient:
    async def fn(model: str, messages: list, **kwargs: Any):
        return {"choices": [{"message": {"content": "ok"}}]}

    return LiteLLMClient(acompletion_fn=fn, hook_registry=HookRegistry())


class _StubRouter:
    """Only the members the factory touches."""

    def __init__(self) -> None:
        self.abort_coordinator = AbortCoordinator()
        self.registered: list[Any] = []

    def input_guardrail_of(self, kind: type) -> Any:
        return None

    def register_local(self, handler: Any) -> None:
        self.registered.append(handler)

    def register_capabilities(self, agent_name: str, caps: list[str]) -> None:
        pass


def test_singleton_class_declared_primus_is_skipped_not_fatal() -> None:
    # `librarian` maps to a singleton LLMPersona; `mason` is a real primus and
    # must still register, proving the skip costs only the bad persona.
    sub = SubordinateConfig(model="m")
    identity = IdentityConfig(
        personas={
            "librarian": PersonaConfig(role="primus", subordinates={"implementer": sub}),
            "mason": PersonaConfig(role="primus", subordinates={"implementer": sub}),
        }
    )
    router = _StubRouter()

    with structlog.testing.capture_logs() as logs:
        registered = register_personas_from_identity(
            dispatch_router=router,  # type: ignore[arg-type]
            identity=identity,
            litellm_client=_client(),
            memory_store=NoOpMemoryStore(),
            session_tracker=NoOpSessionTracker(),
            hook_registry=HookRegistry(),
        )

    assert registered == ["mason-primus"]
    assert [h.name for h in router.registered] == ["mason-primus"]
    mismatch = [e for e in logs if e.get("event") == "personas.primus_class_mismatch"]
    assert len(mismatch) == 1, [e.get("event") for e in logs]
    assert mismatch[0]["persona"] == "librarian"
    assert mismatch[0]["log_level"] == "warning"
