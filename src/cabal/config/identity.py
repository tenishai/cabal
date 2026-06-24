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
from typing import Any, Literal

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


class PersonaCheckpointingConfig(_Open):
    """Per-persona checkpoint opt-in (cabal#2).

    When `enabled = true`, LLMPersona.handle_task writes a checkpoint
    before every LLM call (initial + each tool-use iteration) and drops
    it on successful completion. Crashes mid-loop leave a recoverable
    row in `task_checkpoints`; the operator restarts the Task via
    POST /admin/tasks/{id}/resume.

    Heavy step writes aren't free — only flip this on for personas
    whose tool-use loops are long enough that restarting from scratch
    would burn meaningful compute.
    """

    enabled: bool = False


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
    # Capability tags advertised by this persona. Used by the dispatch
    # router's late-binding path: a Task with `required_capabilities=[...]`
    # and no explicit target gets routed to any persona whose capability
    # set is a superset of the requirement. Examples: ["architectural-advice"],
    # ["code-search", "blame"], ["team-memory.read"].
    capabilities: list[str] = Field(default_factory=list)
    # Output format for code-editing personas. "search-replace" appends
    # aider-style SEARCH/REPLACE block instructions to the system prompt
    # and tells `cbl chat` to parse + apply blocks (with operator y/n).
    # "none" (default) keeps current text-only behavior. "udiff" and
    # "whole-file" are reserved for follow-up issues — see cabal#4.
    edit_format: Literal["none", "search-replace"] = "none"
    # cabal#2 — opt into checkpoint-based durable execution + Task resume.
    checkpointing: PersonaCheckpointingConfig = Field(default_factory=PersonaCheckpointingConfig)


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

    url: str | None = None  # e.g. http://limenal:9100
    token_ttl_seconds: int = 60 * 60  # JWT TTL for per-request mints
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


def resolve_data_dir(identity: IdentityConfig | None, config_path: str | Path) -> Path:
    """Absolute agent data-root for cabal-owned, provisioned resources.

    `cabal.data_dir` is the agent's root — an agent's root is identity-defined,
    not the operator's home (techne#441). A relative value anchors at the
    identity.toml directory so an agent's config and data co-locate under one
    root; an absolute value is used as-is.

    Pure + dependency-free (no limenal/techne import) so it resolves the same
    whether or not limenal is wired — cabal works standalone.
    """
    raw = (identity.cabal.data_dir if identity is not None else CabalConfig().data_dir) or "data"
    d = Path(raw).expanduser()
    if d.is_absolute():
        return d
    return Path(config_path).expanduser().resolve().parent / d


def resolve_under_data_dir(raw: str | Path, data_dir: Path) -> Path:
    """Resolve a configured resource path: absolute used as-is, relative placed
    under the agent data-root. Lets an operator write `db_path = "techne.db"`
    and have it land under the agent root rather than an unanchored CWD path."""
    p = Path(raw).expanduser()
    return p if p.is_absolute() else data_dir / p
