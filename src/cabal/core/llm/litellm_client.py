"""LiteLLM client wrapper — the default LLMClient implementation.

Wraps `litellm.acompletion` with:
    - circuit breaker (§6.11)
    - hook fires (litellm_pre / litellm_post)
    - structured error mapping (litellm exceptions → CabalLLMError variants)
    - budget reservation / refund (when wired)

Supports both deployment shapes via `api_base` / `api_key`:

    Library mode — provider env vars (ANTHROPIC_API_KEY etc.) handle auth,
    litellm dispatches directly to the provider. Use when running solo or
    when the daemon has direct provider credentials.

    Proxy mode — `api_base` points at a LiteLLM proxy server, `api_key`
    is the proxy's service token. Same litellm code path; proxy holds
    provider credentials, enforces budgets centrally, surfaces all
    LLM traffic for observability. Use in scale deployments.

Satisfies the LLMClient Protocol in `cabal.core.llm.protocol`. Future
non-LiteLLM clients implement the same Protocol; persona code does not
change between impls.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from cabal.core.abort import AbortCoordinator
from cabal.core.hooks.registry import HookName, HookRegistry
from cabal.core.llm.budget import BudgetExhaustedError, BudgetGuard
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
    budget: BudgetGuard | None = None
    # cabal#11 — when wired, a budget trip or open circuit aborts the task's
    # AbortGraph node, cancelling any still-running descendant work (e.g. Rook
    # subordinates) rather than letting them run on toward the same wall.
    abort_coordinator: AbortCoordinator | None = None

    def _abort_on_trip(self, task_id: str | None, reason: str) -> None:
        if self.abort_coordinator is not None and task_id is not None:
            self.abort_coordinator.abort(task_id, reason=reason)

    async def acompletion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        agent: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        task_id: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Call litellm.acompletion under the breaker, firing pre/post hooks.

        If `budget` is wired and `task_id` is provided, reserves a budget
        chunk before the call and refunds the unused portion after.
        BudgetExhaustedError is translated to LLMUnavailableError so
        callers don't have to special-case it.
        """
        pre_payload = {
            "agent": agent,
            "model": model,
            "messages": messages,
            "trace_id": trace_id,
            "span_id": span_id,
            "task_id": task_id,
            "kwargs": kwargs,
        }
        await self.hook_registry.fire(HookName.LITELLM_PRE, pre_payload)

        reserved_amount: int | None = None
        if self.budget is not None and task_id is not None:
            try:
                reserved_amount = self.budget.reserve(task_id)
            except BudgetExhaustedError as exc:
                log.warning("litellm.budget_exhausted", task_id=task_id, agent=agent)
                self._abort_on_trip(task_id, "budget exhausted")
                await self.hook_registry.fire(
                    HookName.LITELLM_POST,
                    {**pre_payload, "outcome": "budget_exhausted", "error": str(exc)},
                )
                raise LLMUnavailableError(str(exc)) from exc

        try:
            self.breaker.before_call()
        except CircuitOpenError as exc:
            if reserved_amount is not None and task_id is not None:
                self.budget.refund(task_id, used=0, reserved=reserved_amount)
            log.warning("litellm.circuit_open", agent=agent, model=model)
            self._abort_on_trip(task_id, "circuit open")
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
            if reserved_amount is not None and task_id is not None:
                # Failed call counts as zero used — refund the reservation.
                self.budget.refund(task_id, used=0, reserved=reserved_amount)
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
        used = _tokens_used(response)
        if reserved_amount is not None and task_id is not None:
            self.budget.refund(task_id, used=used, reserved=reserved_amount)
        await self.hook_registry.fire(
            HookName.LITELLM_POST,
            {**pre_payload, "outcome": "success", "response": response, "tokens_used": used},
        )
        return response

    async def astream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        agent: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        task_id: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Stream a completion as text deltas. Yields each non-empty chunk.

        Same pre/post hooks + breaker + budget machinery as acompletion(),
        but the call is made with `stream=True` and the underlying response
        is iterated. Errors are classified identically. Token usage isn't
        always available mid-stream — refund uses an approximate count
        derived from total text length when usage metadata is absent.
        """
        pre_payload = {
            "agent": agent,
            "model": model,
            "messages": messages,
            "trace_id": trace_id,
            "span_id": span_id,
            "task_id": task_id,
            "kwargs": kwargs,
            "stream": True,
        }
        await self.hook_registry.fire(HookName.LITELLM_PRE, pre_payload)

        reserved_amount: int | None = None
        if self.budget is not None and task_id is not None:
            try:
                reserved_amount = self.budget.reserve(task_id)
            except BudgetExhaustedError as exc:
                await self.hook_registry.fire(
                    HookName.LITELLM_POST,
                    {**pre_payload, "outcome": "budget_exhausted", "error": str(exc)},
                )
                raise LLMUnavailableError(str(exc)) from exc

        try:
            self.breaker.before_call()
        except CircuitOpenError as exc:
            if reserved_amount is not None and task_id is not None:
                self.budget.refund(task_id, used=0, reserved=reserved_amount)
            await self.hook_registry.fire(
                HookName.LITELLM_POST,
                {**pre_payload, "outcome": "circuit_open", "error": str(exc)},
            )
            raise LLMUnavailableError(str(exc)) from exc

        merged_kwargs = dict(kwargs)
        merged_kwargs["stream"] = True
        if self.api_base is not None:
            merged_kwargs.setdefault("api_base", self.api_base)
        if self.api_key is not None:
            merged_kwargs.setdefault("api_key", self.api_key)

        try:
            stream = await self.acompletion_fn(
                model=model, messages=messages, **merged_kwargs
            )
        except Exception as exc:  # noqa: BLE001
            self.breaker.on_failure()
            if reserved_amount is not None and task_id is not None:
                self.budget.refund(task_id, used=0, reserved=reserved_amount)
            cabal_class = _classify(exc)
            await self.hook_registry.fire(
                HookName.LITELLM_POST,
                {**pre_payload, "outcome": "error", "error_type": type(exc).__name__},
            )
            raise cabal_class(str(exc)) from exc

        full_text = ""
        try:
            async for chunk in stream:
                delta_text = _extract_delta(chunk)
                if delta_text:
                    full_text += delta_text
                    yield delta_text
        except Exception as exc:  # noqa: BLE001
            self.breaker.on_failure()
            if reserved_amount is not None and task_id is not None:
                self.budget.refund(task_id, used=0, reserved=reserved_amount)
            cabal_class = _classify(exc)
            await self.hook_registry.fire(
                HookName.LITELLM_POST,
                {
                    **pre_payload,
                    "outcome": "stream_error",
                    "error_type": type(exc).__name__,
                    "partial_text": full_text,
                },
            )
            raise cabal_class(str(exc)) from exc

        self.breaker.on_success()
        # No usage object on most stream APIs — estimate by character count.
        # ~4 chars per token is a reasonable approximation.
        approx_used = len(full_text) // 4
        if reserved_amount is not None and task_id is not None:
            self.budget.refund(task_id, used=approx_used, reserved=reserved_amount)
        await self.hook_registry.fire(
            HookName.LITELLM_POST,
            {
                **pre_payload,
                "outcome": "success",
                "full_text": full_text,
                "tokens_used_approx": approx_used,
            },
        )


def _extract_delta(chunk: Any) -> str:
    """Pull the delta text out of a streaming chunk.

    Handles dict-shaped chunks (`{"choices":[{"delta":{"content":"..."}}]}`)
    and object-shaped chunks (LiteLLM ModelResponse). Empty / non-content
    chunks return ''.
    """
    try:
        if isinstance(chunk, dict):
            choices = chunk.get("choices") or []
            if not choices:
                return ""
            delta = choices[0].get("delta") or {}
            return delta.get("content") or ""
        # Attribute-style access for litellm ModelResponse-like objects
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return ""
        delta = getattr(choices[0], "delta", None)
        if delta is None:
            return ""
        return getattr(delta, "content", None) or ""
    except Exception:  # noqa: BLE001
        return ""


def _tokens_used(response: Any) -> int:
    """Best-effort total-token extraction from a LiteLLM/OpenAI response."""
    try:
        usage = response.get("usage") if isinstance(response, dict) else None
        if usage is None:
            return 0
        total = usage.get("total_tokens")
        if total is not None:
            return int(total)
        prompt = int(usage.get("prompt_tokens", 0))
        completion = int(usage.get("completion_tokens", 0))
        return prompt + completion
    except Exception:  # noqa: BLE001
        return 0
