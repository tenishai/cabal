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

import httpx
import structlog

from cabal.agents.personas import PERSONA_REGISTRY
from cabal.agents.primus import PrimusPersona
from cabal.agents.subordinate import Subordinate
from cabal.config.identity import IdentityConfig
from cabal.core.auth.keys import StoredKey
from cabal.core.broker.router import DispatchRouter
from cabal.core.checkpoint import CheckpointStore
from cabal.core.guardrails_builtin import DelegationBudgetGuardrail
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
    mcp_http_client: httpx.AsyncClient | None = None,
    signing_key: StoredKey | None = None,
    checkpoint_store: CheckpointStore | None = None,
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

        if persona_cfg.role == "primus":
            # A role="primus" persona must map to a PrimusPersona subclass (the
            # primus constructor signature). A mis-declared singleton (free-form
            # role field) would otherwise crash daemon startup with a bare
            # TypeError on the subordinates/hook_registry kwargs — skip loudly
            # instead, mirroring the missing-implementer handling below.
            primus_cls = factory  # resolved + non-None checked above
            if not (isinstance(primus_cls, type) and issubclass(primus_cls, PrimusPersona)):
                log.warning(
                    "personas.primus_class_mismatch",
                    persona=persona_name,
                    msg=(
                        f"{persona_name!r} declared role='primus' but its registry "
                        f"class {primus_cls.__name__} is not a PrimusPersona; "
                        "skipping handler"
                    ),
                )
                continue
            sub_instances: dict[str, Subordinate] = {}
            for role, sub_cfg in persona_cfg.subordinates.items():
                sub = Subordinate(
                    primus=persona_name,
                    role=role,
                    llm_client=litellm_client,
                    model=sub_cfg.model,
                    max_tokens=sub_cfg.max_tokens,
                    memory_store=memory_store,
                    session_tracker=session_tracker,
                )
                # cabal#11 — subordinates share the router's AbortGraph so a
                # parent abort reaches their in-flight LLM calls.
                sub.abort_coordinator = dispatch_router.abort_coordinator
                # Inherit the primus's provider kwargs unless this subordinate
                # states its own. A model-family switch is a property of the
                # whole persona tree, so making the operator repeat it per
                # subordinate would mostly produce half-configured trees.
                sub.extra_kwargs = dict(
                    sub_cfg.extra_kwargs
                    if sub_cfg.extra_kwargs is not None
                    else persona_cfg.extra_kwargs
                )
                sub_instances[role] = sub
            if "implementer" not in sub_instances:
                log.warning(
                    "personas.primus_missing_implementer",
                    persona=persona_name,
                    msg=(
                        f"{persona_name!r} primus requires "
                        "personas.<name>.subordinates.implementer; skipping handler"
                    ),
                )
                continue
            # SKIP THE PERSONA, NEVER THE PROCESS (cabal#156 r3 Important 2).
            # Its two neighbours above log and `continue` precisely so one bad
            # persona cannot cost the rest of the file, and the breadth ceiling
            # added in #156 is the first OPERATOR-TRIGGERABLE exception on this
            # path -- the pre-existing "requires a non-reviewer subordinate"
            # raise is unreachable from here, because the implementer check
            # above already continued. Unwrapped, a misdeclared breadth took
            # down daemon startup and left every later persona unregistered,
            # and `POST /admin/reload/identity` 500'd with state.identity
            # already swapped and the handler set half rebuilt.
            try:
                handler: Any = primus_cls(
                    litellm_client,
                    subordinates=sub_instances,
                    hook_registry=hook_registry,
                    model=persona_cfg.model_override,
                    max_tokens=persona_cfg.max_tokens_override or 8192,
                    # cabal#158 — the ROUTER'S budget instance, not a new one,
                    # so a conversation's delegations count once whether they go
                    # through the router or through a fan-out leg. None when the
                    # operator disabled the budget; the primus then behaves as
                    # before.
                    delegation_budget=dispatch_router.input_guardrail_of(
                        DelegationBudgetGuardrail
                    ),
                )
            except ValueError as exc:
                log.error(
                    "personas.primus_config_rejected",
                    persona=persona_name,
                    msg=f"{persona_name!r} skipping handler: {exc}",
                )
                continue
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

        # cabal#11 — every handler shares the router's AbortGraph coordinator
        # so operator/budget aborts reach its dispatch loop.
        handler.abort_coordinator = dispatch_router.abort_coordinator

        # Apply remaining overrides uniformly.
        if persona_cfg.system_prompt_override:
            handler.system_prompt = persona_cfg.system_prompt_override
            log.info("personas.system_prompt_overridden", persona=persona_name)
        if persona_cfg.commit_to_memory is not None:
            handler.commit_to_memory = persona_cfg.commit_to_memory
        if persona_cfg.extra_kwargs:
            handler.extra_kwargs = dict(persona_cfg.extra_kwargs)
            log.info(
                "personas.extra_kwargs_applied",
                persona=persona_name,
                keys=sorted(persona_cfg.extra_kwargs),
            )

        # Auto-augment system prompt with edit-format instructions so
        # the operator doesn't have to paste them into identity.toml.
        # Append AFTER any system_prompt_override so the override stays
        # the operator's voice and the format rules are added below it.
        if persona_cfg.edit_format == "search-replace":
            from cabal.cli.edit_format import SEARCH_REPLACE_INSTRUCTIONS

            handler.system_prompt = handler.system_prompt + SEARCH_REPLACE_INSTRUCTIONS
            log.info(
                "personas.edit_format_instructions_appended",
                persona=persona_name,
                edit_format=persona_cfg.edit_format,
            )

        # cabal#2 — swap the LLMPersona's no-op CheckpointStore for the
        # real one when the persona opts in. PrimusPersona subclasses are
        # excluded for now (multi-stage pipeline checkpointing is a follow-up;
        # see cabal/core/checkpoint.py module docstring).
        if (
            persona_cfg.checkpointing.enabled
            and checkpoint_store is not None
            and persona_cfg.role != "primus"
        ):
            handler.checkpoint_store = checkpoint_store
            log.info("personas.checkpointing_enabled", persona=persona_name)
        elif persona_cfg.checkpointing.enabled and persona_cfg.role == "primus":
            log.warning(
                "personas.checkpointing_unsupported",
                persona=persona_name,
                msg=(
                    "PrimusPersona checkpointing is not yet implemented; "
                    "the [personas.<name>.checkpointing] enabled flag is ignored."
                ),
            )

        agent_name = f"{persona_name}-primus" if persona_cfg.role == "primus" else persona_name
        handler.name = agent_name

        # MCP tool surface: wire iff [liminal].url is set AND this persona
        # opted in via [personas.NAME.mcp].enabled. Without both, the
        # handler keeps tool_proxy=None and the tool-use loop is skipped.
        if (
            identity.liminal.url
            and persona_cfg.mcp.enabled
            and signing_key is not None
            and mcp_http_client is not None
        ):
            from cabal.core.auth.jwt import mint as _mint_jwt
            from cabal.core.tools.mcp_proxy import build_proxy

            _agent = agent_name  # capture for the closure
            _persona = persona_name
            _tool_extras_dict = (
                dict(persona_cfg.mcp.tool_extras) if persona_cfg.mcp.tool_extras else {}
            )
            # First-class persona_cfg.mcp.allowed_handlers folds into the
            # tool_extras dict here. Explicit None means "unrestricted"
            # and is omitted; an explicit list (including the empty list)
            # is forwarded so liminal can refuse everything if the
            # operator wants that.
            if persona_cfg.mcp.allowed_handlers is not None:
                _tool_extras_dict["liminal.allowed_handlers"] = list(
                    persona_cfg.mcp.allowed_handlers
                )
            _tool_extras = _tool_extras_dict if _tool_extras_dict else None

            def _token_provider(
                _key=signing_key,
                _ag=_agent,
                _per=_persona,
                _extras=_tool_extras,
                _ttl=identity.liminal.token_ttl_seconds,
                _aud=identity.liminal.jwt_aud,
            ) -> str:
                return _mint_jwt(
                    signing_key=_key,
                    sub=_ag,
                    aud=_aud,
                    persona=_per,
                    scope=["dispatch"],
                    tool_extras=_extras,
                    ttl_seconds=_ttl,
                )

            proxy = build_proxy(
                base_url=identity.liminal.url,
                persona=persona_name,
                agent_name=agent_name,
                token_provider=_token_provider,
                http_client=mcp_http_client,
            )
            handler.tool_proxy = proxy
            if persona_cfg.mcp.max_steps is not None:
                handler.tool_use_max_steps = persona_cfg.mcp.max_steps
            log.info(
                "personas.mcp_wired",
                persona=persona_name,
                liminal_url=identity.liminal.url,
            )
        elif persona_cfg.mcp.enabled:
            # The persona asked for a tool surface and did not get one. Without
            # this branch the miss is silent: `_Open` is extra="allow", so an
            # identity.toml still carrying the old misspelled `[limenal]`
            # section is absorbed as an unknown key, `[liminal].url` falls back
            # to None, and every persona comes up toolless with nothing in the
            # logs. Report which precondition was missing.
            log.warning(
                "personas.mcp_skipped",
                persona=persona_name,
                liminal_url_set=bool(identity.liminal.url),
                signing_key_set=signing_key is not None,
                http_client_set=mcp_http_client is not None,
            )

        dispatch_router.register_local(handler)
        # Register capability tags so cabal#3 late-binding dispatch can
        # reach this persona via required_capabilities. Empty list is
        # fine — agent just isn't reachable by capability.
        dispatch_router.register_capabilities(agent_name, list(persona_cfg.capabilities))
        registered.append(agent_name)
        log.info(
            "personas.handler_registered",
            persona=persona_name,
            agent=agent_name,
            model=handler.model,
            capabilities=list(persona_cfg.capabilities),
        )
    return registered
