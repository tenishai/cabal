"""LiteLLM client wrapper.

Wraps `litellm.acompletion` with:
    - circuit breaker (§6.11)
    - hook fires (litellm_pre / litellm_post)
    - structured error mapping (litellm exceptions → CabalLLMError variants)

The wrapper is THE only place in Cabal that calls litellm directly;
personas dispatch through it via `LiteLLMClient`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import structlog

from cabal.core.hooks.registry import HookName, HookRegistry
from cabal.core.llm.circuit_breaker import CircuitBreaker, CircuitOpenError

log = structlog.get_logger(__name__)


class CabalLLMError(RuntimeError):
    """Base for all LiteLLM-related failures the wrapper surfaces."""


class LLMRateLimitedError(CabalLLMError):
    """Provider returned 429."""


class LLMUnavailableError(CabalLLMError):
    """Provider returned 5xx, the proxy was unreachable, or the breaker is OPEN."""


class LLMInvalidRequestError(CabalLLMError):
    """Provider returned 4xx other than 429 (bad request, invalid model, etc.)."""


# litellm doesn't export a single base for these; we sniff by class name to
# avoid hard-importing every variant.
RATE_LIMIT_CLASSES = {"RateLimitError"}
UNAVAILABLE_CLASSES = {
    "APIConnectionError",
    "APIError",
    "ServiceUnavailableError",
    "InternalServerError",
    "Timeout",
    "APITimeoutError",
}
INVALID_REQUEST_CLASSES = {
    "BadRequestError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "UnprocessableEntityError",
}


def _classify(exc: Exception) -> type[CabalLLMError]:
    name = type(exc).__name__
    if name in RATE_LIMIT_CLASSES:
        return LLMRateLimitedError
    if name in UNAVAILABLE_CLASSES:
        return LLMUnavailableError
    if name in INVALID_REQUEST_CLASSES:
        return LLMInvalidRequestError
    return CabalLLMError


@dataclass
class LiteLLMClient:
    """Single shared instance per Cabal Core process.

    `acompletion_fn` is injected so tests don't pull in network deps. The
    daemon wires it to `litellm.acompletion`.
    """

    acompletion_fn: Callable[..., Any]
    hook_registry: HookRegistry
    api_base: str | None = None
    api_key: str | None = None
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)

    async def acompletion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        agent: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Call litellm.acompletion under the breaker, firing pre/post hooks."""
        pre_payload = {
            "agent": agent,
            "model": model,
            "messages": messages,
            "trace_id": trace_id,
            "span_id": span_id,
            "kwargs": kwargs,
        }
        await self.hook_registry.fire(HookName.LITELLM_PRE, pre_payload)

        try:
            self.breaker.before_call()
        except CircuitOpenError as exc:
            log.warning("litellm.circuit_open", agent=agent, model=model)
            await self.hook_registry.fire(
                HookName.LITELLM_POST,
                {**pre_payload, "outcome": "circuit_open", "error": str(exc)},
            )
            raise LLMUnavailableError(str(exc)) from exc

        merged_kwargs = dict(kwargs)
        if self.api_base is not None:
            merged_kwargs.setdefault("api_base", self.api_base)
        if self.api_key is not None:
            merged_kwargs.setdefault("api_key", self.api_key)

        try:
            response = await self.acompletion_fn(
                model=model, messages=messages, **merged_kwargs
            )
        except Exception as exc:  # noqa: BLE001
            self.breaker.on_failure()
            cabal_class = _classify(exc)
            log.warning(
                "litellm.call_failed",
                agent=agent,
                model=model,
                error_type=type(exc).__name__,
                breaker_state=self.breaker.state,
            )
            await self.hook_registry.fire(
                HookName.LITELLM_POST,
                {**pre_payload, "outcome": "error", "error_type": type(exc).__name__},
            )
            raise cabal_class(str(exc)) from exc

        self.breaker.on_success()
        await self.hook_registry.fire(
            HookName.LITELLM_POST,
            {**pre_payload, "outcome": "success", "response": response},
        )
        return response
