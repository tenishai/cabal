"""MCPToolProxy — Cabal's adapter to a limenal MCP gateway.

Wraps `limenal.client.AsyncMCPClient` with a `token_provider` that mints a
short-lived JWT using Cabal Core's active signing key. This is the single
boundary where Cabal hands a credential to the gateway; everything else
(persona name → identity, agent name → sub) flows through the JWT claims
that limenal already verifies.

Tool definitions returned by `list_tools()` are reshaped into the
"OpenAI tools" array LiteLLM expects (`type: function`), so personas can
hand them straight to `litellm.acompletion(tools=...)`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import structlog

# limenal is loaded lazily — cabal can be imported without it. The MCPToolProxy
# is only instantiated when a persona declares `[mcp].enabled = true` in
# identity.toml, so the import deferral is exactly the right shape. With
# `from __future__ import annotations` above, the type hints in this module
# stay as strings and don't trigger the import at module load.

if TYPE_CHECKING:  # pragma: no cover
    from limenal.client import AsyncMCPClient, ToolCallOutcome


def _require_limenal() -> Any:
    """Lazy-load limenal.client at runtime. Raises a clear error if missing."""
    try:
        from limenal import client as _limenal_client
    except ImportError as exc:
        raise ImportError(
            "cabal MCP tool surface requires limenal. Install it via the "
            "`mcp` extra: `pip install cabal[mcp]` (or `pip install -e ../limenal` "
            "if you're using the sibling-checkout layout)."
        ) from exc
    return _limenal_client


log = structlog.get_logger(__name__)


@dataclass(slots=True)
class MCPToolProxy:
    """Per-persona MCP client + tool-translation surface.

    Args:
        client: an AsyncMCPClient already wired to limenal's URL.
        persona: the persona name — gets baked into the JWT `cabal_persona`
            claim by the token_provider supplied at construction.
        agent_name: the specific agent (e.g. `archivist`); becomes the
            JWT `sub` claim.
    """

    client: AsyncMCPClient
    persona: str
    agent_name: str

    async def list_tools_for_llm(self) -> list[dict[str, Any]]:
        """Fetch limenal's tool surface and reshape into LiteLLM's
        `tools=[...]` array (OpenAI function-tool shape)."""
        limenal_client = _require_limenal()
        try:
            tools = await self.client.list_tools()
        except limenal_client.MCPError as exc:
            log.warning(
                "mcp_proxy.list_tools_failed",
                persona=self.persona,
                error=str(exc),
            )
            return []
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name.replace(".", "__"),  # LLMs hate dots in function names
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]

    async def call_tool(
        self, llm_name: str, arguments: dict[str, Any]
    ) -> ToolCallOutcome:
        """Translate from the LiteLLM-flat name back to limenal's
        `<handler>.<tool>` and dispatch."""
        limenal_client = _require_limenal()
        mcp_name = llm_name.replace("__", ".", 1)
        try:
            return await self.client.call_tool(mcp_name, arguments)
        except limenal_client.MCPRemoteError as exc:
            return limenal_client.ToolCallOutcome(
                content=[{"type": "text", "text": f"tool error: {exc.message}"}],
                is_error=True,
            )
        except limenal_client.MCPError as exc:
            return limenal_client.ToolCallOutcome(
                content=[{"type": "text", "text": f"tool transport error: {exc}"}],
                is_error=True,
            )


def build_token_provider(
    *,
    persona: str,
    agent_name: str,
    mint_jwt: Callable[..., str],
    ttl_seconds: int = 60 * 60,
) -> Callable[[], str]:
    """Construct a no-arg callable that returns a freshly-minted JWT.

    `mint_jwt` is `cabal.core.auth.jwt.mint`. We pre-bind everything but
    the timestamp so the closure stays cheap (Authlib JWT mint is sub-ms).
    """

    def provider() -> str:
        return mint_jwt(
            sub=agent_name,
            aud="limenal",
            persona=persona,
            scope=["dispatch"],
            ttl_seconds=ttl_seconds,
        )

    return provider


def build_proxy(
    *,
    base_url: str,
    persona: str,
    agent_name: str,
    token_provider: Callable[[], str],
    http_client: httpx.AsyncClient | None = None,
) -> MCPToolProxy:
    limenal_client = _require_limenal()
    client = limenal_client.AsyncMCPClient(
        base_url=base_url,
        token_provider=token_provider,
        http_client=http_client,
        client_name=f"cabal:{persona}",
    )
    return MCPToolProxy(client=client, persona=persona, agent_name=agent_name)
