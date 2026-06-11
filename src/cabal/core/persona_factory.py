"""Build + register persona LocalHandlers from identity.toml.

Used in two places:
  - app lifespan (initial registration at startup)
  - POST /admin/reload/identity (hot reload without daemon restart)

Keeping the logic in one function means the reload path can't drift from
the startup path — same overrides, same subordinate construction, same
naming.
"""

from __future__ import annotations

from typing import Any

import structlog

from cabal.agents.personas import PERSONA_REGISTRY
from cabal.agents.personas.rook import RookPrimusPersona
from cabal.agents.subordinate import Subordinate
from cabal.config.identity import IdentityConfig
from cabal.core.broker.router import DispatchRouter
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient
from cabal.core.memory.session_tracker import SessionTracker
from cabal.core.memory.store import MemoryStore

log = structlog.get_logger(__name__)


def register_personas_from_identity(
    *,
    dispatch_router: DispatchRouter,
    identity: IdentityConfig,
    litellm_client: LiteLLMClient,
    memory_store: MemoryStore,
    session_tracker: SessionTracker,
    hook_registry: HookRegistry,
) -> list[str]:
    """Build + register LocalHandlers for every persona in PERSONA_REGISTRY
    that's also declared in identity.toml. Returns the list of registered
    agent names (with -primus suffix for primus roles)."""
    registered: list[str] = []
    for persona_name, persona_cfg in identity.personas.items():
        factory = PERSONA_REGISTRY.get(persona_name)
        if factory is None:
            log.info(
                "personas.no_handler_registered",
                persona=persona_name,
                msg="declared in identity.toml but not in PERSONA_REGISTRY",
            )
            continue

        if persona_name == "rook":
            sub_instances: dict[str, Subordinate] = {}
            for role, sub_cfg in persona_cfg.subordinates.items():
                sub_instances[role] = Subordinate(
                    role=role,
                    llm_client=litellm_client,
                    model=sub_cfg.model,
                    max_tokens=sub_cfg.max_tokens,
                    memory_store=memory_store,
                    session_tracker=session_tracker,
                )
            if "implementer" not in sub_instances:
                log.warning(
                    "personas.rook_missing_implementer",
                    msg="rook requires personas.rook.subordinates.implementer; skipping handler",
                )
                continue
            handler: Any = RookPrimusPersona(
                litellm_client,
                subordinates=sub_instances,
                hook_registry=hook_registry,
                model=persona_cfg.model_override,
                max_tokens=persona_cfg.max_tokens_override or 8192,
            )
            handler.memory_store = memory_store
            handler.session_tracker = session_tracker
        else:
            handler = factory(
                litellm_client,
                memory_store=memory_store,
                session_tracker=session_tracker,
                model=persona_cfg.model_override,
                max_tokens=persona_cfg.max_tokens_override or 8192,
            )

        # Apply remaining overrides uniformly.
        if persona_cfg.system_prompt_override:
            handler.system_prompt = persona_cfg.system_prompt_override
            log.info(
                "personas.system_prompt_overridden", persona=persona_name
            )
        if persona_cfg.commit_to_memory is not None:
            handler.commit_to_memory = persona_cfg.commit_to_memory

        agent_name = (
            f"{persona_name}-primus"
            if persona_cfg.role == "primus"
            else persona_name
        )
        handler.name = agent_name
        dispatch_router.register_local(handler)
        registered.append(agent_name)
        log.info(
            "personas.handler_registered",
            persona=persona_name,
            agent=agent_name,
            model=handler.model,
        )
    return registered
