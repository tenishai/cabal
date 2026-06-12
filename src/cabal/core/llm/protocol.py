"""LLMClient Protocol — the contract for LLM access across cabal.

Personas call `app.state.llm_client.acompletion(...)` (or `.astream(...)`)
without knowing what backs the client. Today the only impl is
`LiteLLMClient`, which itself supports two deployment shapes via
identity.toml configuration:

  Library mode — direct LiteLLM SDK calls:
      [litellm]
      # api_base and api_key_env unset

      LiteLLM library hits providers directly using each provider's
      conventional env vars (ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.).

  Proxy mode — calls forwarded to a LiteLLM proxy server:
      [litellm]
      api_base = "http://litellm:4000"
      api_key_env = "LITELLM_PROXY_SERVICE_TOKEN"

      LiteLLM library hits the proxy using OpenAI-compatible HTTP. The
      proxy holds the provider credentials, enforces budgets centrally,
      and gives one place to observe + rate-limit all LLM traffic.

Both modes use the same `LiteLLMClient` wrapper (breaker + budget +
hooks + error classification). The mode is a deployment decision, not a
code decision.

Future LLM clients (e.g., a direct provider SDK that bypasses LiteLLM)
just implement this Protocol; persona code does not change.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

__all__ = ["LLMClient"]


@runtime_checkable
class LLMClient(Protocol):
    """The contract every LLM client in cabal must satisfy.

    All cross-cutting concerns (circuit breaker, budget reservation,
    pre/post hooks, error classification) belong inside the impl — they
    are not the caller's responsibility.
    """

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
        """Single completion call. Returns the provider response verbatim.

        Implementations MUST:
          - Fire LITELLM_PRE before the upstream call
          - Fire LITELLM_POST after, with outcome + tokens_used
          - Reserve / refund budget via BudgetGuard if `task_id` set
          - Pass through circuit-breaker state
          - Translate upstream errors to CabalLLMError variants
        """
        ...

    def astream(
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
        """Streaming completion. Yields text deltas as they arrive.

        Same cross-cutting requirements as `acompletion`. Budget refund
        uses an approximate token count derived from the total streamed
        text length when usage metadata is not surfaced mid-stream.
        """
        ...
