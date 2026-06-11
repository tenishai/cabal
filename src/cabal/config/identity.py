"""identity.toml loader.

The single source of truth for declared personas, their safety tiers,
LLM routing, and Cabal Core's own bind config. Subordinates are declared
under personas.<name>.subordinates and instantiated per dispatch.

Schema is enforced by pydantic; unknown keys are allowed (forward-compat
with future Cabal versions adding fields) but unknown top-level sections
warn at load time.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.12+ only per pyproject
    import tomli as tomllib

log = structlog.get_logger(__name__)


KNOWN_TOP_LEVEL = {
    "cabal",
    "personas",
    "litellm",
    "hooks",
    "secrets",
    "langfuse",
    "techne",
    "limenal",
}


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class CabalConfig(_Open):
    host: str = "127.0.0.1"
    port: int = 7400
    data_dir: str = "data"
    # Identifies who holds the operator token — used as the `sub` claim in
    # bootstrap JWTs. Required; fails closed at bootstrap if absent.
    operator_id: str | None = None


class SubordinateConfig(_Open):
    model: str
    max_tokens: int = 8192


class PersonaMCPConfig(_Open):
    """Per-persona MCP tool surface settings.

    Even if `[limenal]` is configured at the top level, a persona only
    gets a tool_proxy when its `mcp.enabled = true`. This keeps
    operators in control — Architect-the-thinker probably doesn't want
    tools, Rook-the-engineer probably does.

    `tool_extras` is a dict of arbitrary scoping config forwarded to
    limenal as `Context.extra`. The handler-author's contract decides
    the shape — e.g. for the web handler the operator writes
    `tool_extras = { "web.allowed_hosts" = ["docs.python.org"] }`.
    """

    enabled: bool = False
    max_steps: int | None = None  # overrides LLMPersona.tool_use_max_steps
    tool_extras: dict[str, Any] = Field(default_factory=dict)
    # First-class shortcut for the most common scoping choice — which
    # handlers this persona can see + call. Folded into tool_extras as
    # `limenal.allowed_handlers` at JWT-mint time. Operators who'd
    # rather put it directly in tool_extras can; both paths land in the
    # same claim. None means "unrestricted" (every handler limenal
    # serves is reachable).
    allowed_handlers: list[str] | None = None


class PersonaConfig(_Open):
    description: str | None = None
    role: str  # "primus" | "singleton"
    safety_tier: str = "strict"
    subordinates: dict[str, SubordinateConfig] = Field(default_factory=dict)
    # Operator overrides — applied at handler-construction time.
    system_prompt_override: str | None = None
    model_override: str | None = None
    max_tokens_override: int | None = None
    commit_to_memory: bool | None = None
    mcp: PersonaMCPConfig = Field(default_factory=PersonaMCPConfig)


class LiteLLMConfig(_Open):
    api_base: str | None = None
    api_key_env: str | None = None


class LangfuseConfig(_Open):
    host: str | None = None
    public_key_env: str | None = None
    secret_key_env: str | None = None


class TechneConfig(_Open):
    db_path: str | None = None  # path to techne sqlite DB
    use_vectors: bool = False


class LimenalConfig(_Open):
    """Cabal's connection settings for a limenal MCP gateway."""

    url: str | None = None              # e.g. http://limenal:9100
    token_ttl_seconds: int = 60 * 60    # JWT TTL for per-request mints
    jwt_aud: str = "limenal"


class IdentityConfig(_Open):
    cabal: CabalConfig = Field(default_factory=CabalConfig)
    personas: dict[str, PersonaConfig] = Field(default_factory=dict)
    litellm: LiteLLMConfig = Field(default_factory=LiteLLMConfig)
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)
    techne: TechneConfig = Field(default_factory=TechneConfig)
    limenal: LimenalConfig = Field(default_factory=LimenalConfig)
    hooks: dict[str, Any] = Field(default_factory=dict)


def load_identity(path: str | Path) -> IdentityConfig:
    """Parse identity.toml; emit warnings for unknown top-level sections."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"identity config not found: {p}")
    with p.open("rb") as f:
        raw: dict[str, Any] = tomllib.load(f)

    unknown = set(raw.keys()) - KNOWN_TOP_LEVEL
    for section in unknown:
        log.warning("identity.unknown_section", section=section, path=str(p))

    return IdentityConfig.model_validate(raw)
