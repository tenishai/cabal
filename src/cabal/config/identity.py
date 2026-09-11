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
from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    "liminal",
    "guardrails",
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


#: Keyword names cabal itself binds on the way to the provider. An
#: `extra_kwargs` entry using one of these is rejected at config load rather
#: than at dispatch, because the two collision classes both fail badly and in
#: opposite directions (found by review on PR #131, both executed):
#:
#:   `max_tokens` — TypeError("got multiple values for keyword argument") on
#:   EVERY dispatch to that persona, hours after a clean boot that logged the
#:   offending key by name. `max_tokens` is also the likely wrong guess,
#:   because cabal's own field is called `max_tokens_override`.
#:
#:   `api_base` / `api_key` — silently WIN over the daemon's configured values
#:   (LiteLLMClient uses setdefault), so one persona's traffic is redirected
#:   with a 200 and no log line saying so.
#:
#:   `tools` — silently discarded, since the tool proxy overwrites it. This
#:   one is over-broad ON PURPOSE: `tools` is only cabal's when a proxy is
#:   wired, so a proxy-less persona could otherwise pass it through. Refused
#:   anyway, because such a persona gets `tool_calls` back with nothing able to
#:   execute them and returns empty content — the failure this feature exists
#:   to stop.
#:
#: This is the complement of the whitelist we deliberately do not keep: it
#: enumerates OUR OWN signature, not any provider's, so it changes only when
#: LiteLLMClient does. test_reserved_covers_the_client_signature pins that.
RESERVED_EXTRA_KWARGS = frozenset(
    {
        # bound by name in LiteLLMClient.acompletion / .astream
        "model",
        "messages",
        "agent",
        "trace_id",
        "span_id",
        "task_id",
        # passed explicitly by every persona call site
        "max_tokens",
        # setdefault-ed by the client from daemon config
        "api_base",
        "api_key",
        # owned by the tool proxy
        "tools",
        "stream",
        # the streaming path's terminal-reason callback (cabal#136)
        "on_finish",
    }
)


def _reject_reserved(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """Refuse provider kwargs that would collide with cabal's own."""
    if not value:
        return value
    clashes = sorted(set(value) & RESERVED_EXTRA_KWARGS)
    if clashes:
        raise ValueError(
            f"extra_kwargs may not set {', '.join(repr(c) for c in clashes)} — "
            "cabal binds these itself on the way to the provider. "
            "Use max_tokens_override for the token ceiling, and the [litellm] "
            "block for api_base / api_key."
        )
    return value


class SubordinateConfig(_Open):
    model: str
    max_tokens: int = 8192
    #: Provider kwargs for THIS subordinate's calls. `None` (the default) means
    #: inherit the owning primus's — one setting in the persona block then
    #: covers every hop, which is what an operator switching model families
    #: actually wants. An explicit `{}` opts out and sends nothing.
    extra_kwargs: dict[str, Any] | None = None

    @field_validator("extra_kwargs")
    @classmethod
    def _no_reserved_keys(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        return _reject_reserved(v)


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

    Even if `[liminal]` is configured at the top level, a persona only
    gets a tool_proxy when its `mcp.enabled = true`. This keeps
    operators in control — Architect-the-thinker probably doesn't want
    tools, Mason-the-engineer probably does.

    `tool_extras` is a dict of arbitrary scoping config forwarded to
    liminal as `Context.extra`. The handler-author's contract decides
    the shape — e.g. for the web handler the operator writes
    `tool_extras = { "web.allowed_hosts" = ["docs.python.org"] }`.
    """

    enabled: bool = False
    max_steps: int | None = None  # overrides LLMPersona.tool_use_max_steps
    tool_extras: dict[str, Any] = Field(default_factory=dict)
    # First-class shortcut for the most common scoping choice — which
    # handlers this persona can see + call. Folded into tool_extras as
    # `liminal.allowed_handlers` at JWT-mint time. Operators who'd
    # rather put it directly in tool_extras can; both paths land in the
    # same claim. None means "unrestricted" (every handler liminal
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
    #: Extra keyword arguments passed through to `litellm.acompletion`, for
    #: provider- or model-specific knobs cabal has no first-class field for.
    #: Deliberately untyped and unvalidated: the set is defined by the provider,
    #: not by cabal, and a whitelist here would need editing every time a
    #: provider adds one. Example — turning a reasoning model's thinking off:
    #:     extra_kwargs = { chat_template_kwargs = { enable_thinking = false } }
    extra_kwargs: dict[str, Any] = Field(default_factory=dict)

    @field_validator("extra_kwargs")
    @classmethod
    def _no_reserved_keys(cls, v: dict[str, Any]) -> dict[str, Any]:
        return _reject_reserved(v) or {}
    commit_to_memory: bool | None = None
    mcp: PersonaMCPConfig = Field(default_factory=PersonaMCPConfig)
    # Capability tags advertised by this persona. Used by the dispatch
    # router's late-binding path: a Task with `required_capabilities=[...]`
    # and no explicit target gets routed to any persona whose capability
    # set is a superset of the requirement. Examples: ["architectural-advice"],
    # ["code-search", "blame"], ["team-memory.read"].
    #: cabal#154 -- the personas this one may DELEGATE to. Operator-authored,
    #: never agent-supplied; that is what makes it an authorization control.
    #:
    #: READ THE DEFAULT CAREFULLY, because it is the opposite of the one on
    #: `mcp.allowed_handlers` a few lines up: omitting `allowed_handlers` means
    #: UNRESTRICTED, while omitting `may_delegate_to` means NO DELEGATION. The
    #: two are not siblings and the names are deliberately unalike so they do
    #: not read as a pair. "No relationships declared permits no delegation" is
    #: the property being borrowed; a permissive default would make this a
    #: suggestion rather than a control.
    #:
    #: Inert unless `[guardrails] delegation_edges = true`.
    #:
    #: Names are PERSONAS. A role="primus" persona is reached under the agent
    #: name "<persona>-primus"; the guard resolves that, so declare the persona.
    may_delegate_to: list[str] = Field(default_factory=list)
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


class LiminalConfig(_Open):
    """Cabal's connection settings for a liminal MCP gateway."""

    url: str | None = None  # e.g. http://liminal:9100
    token_ttl_seconds: int = 60 * 60  # JWT TTL for per-request mints
    jwt_aud: str = "liminal"


class GuardrailsConfig(_Open):
    """Router-level input guardrails (cabal#76).

    These run in ``DispatchRouter.dispatch`` *before* the local-vs-remote
    branch, so they are the one enforcement point that also covers
    ``kind="federated"`` peers — the in-process Mason ``DoomLoopGuardrail``
    (primus.py) only fires at the subordinate boundary and never reaches a
    remote hop. Default-on; a deployment opts out here.

    The default threshold (5) is deliberately looser than the in-process Mason
    guard (3): this guard runs on ALL production dispatch, human ``cbl chat``
    turns included, so a slightly higher bar avoids blocking a user who sends
    the same short message (``yes`` / ``continue``) a few times in a session,
    while a genuine agent doom loop blows past 5 easily.
    """

    doom_loop: bool = True
    doom_loop_threshold: int = 5
    # Per-key window in which repeats must recur to count toward the threshold;
    # None disables the time-based reset. MUST stay comfortably above the
    # federated dispatch timeout — a hanging peer burns the whole read ceiling
    # per attempt, so if the TTL is at or below that ceiling every check lands
    # outside the window, the count resets to 1, and the guard never fires.
    # app.py raises this at wiring time rather than letting it be silently
    # defeated; see _doom_loop_ttl.
    doom_loop_ttl_s: float | None = 600.0

    @field_validator("doom_loop_ttl_s")
    @classmethod
    def _ttl_positive(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError(
                f"guardrails.doom_loop_ttl_s must be > 0 (got {v}); "
                "omit it for the default, or set it to null to disable the "
                "time-based reset entirely"
            )
        return v

    #: cabal#146 -- total delegations per conversation, payload-independent.
    #: Complements doom_loop rather than replacing it: that one catches a tight
    #: repeat quickly, this one catches the paraphrase storm that walks past it.
    #: A deployment may LOWER this; MAX_DELEGATIONS_PER_CONTEXT is the ceiling
    #: and a larger value is clamped, not honoured.
    #: cabal#154 -- delegation is permitted where an edge was declared.
    #: OPT-IN, and deliberately unlike its two neighbours, which default True.
    #: The guard's own default is DENY (a persona with no `may_delegate_to`
    #: may delegate to nobody), which is the load-bearing half of the control;
    #: every room that exists today has no edges declared, so switching this on
    #: by default would turn all delegation off at upgrade with no warning.
    #: The default-deny lives in the guard; the migration lives here.
    delegation_edges: bool = False
    delegation_budget: bool = True
    #: THE MEANING SHIFTED WITH #158, SO THE NUMBER MOVED WITH IT. The budget
    #: now counts fan-out legs as well as router hops, so on the shipped config
    #: an operator turn costs 2 (the router hop plus the implementer leg) where
    #: it cost 1 -- and at the old default of 20 that halved `cbl chat` from
    #: twenty turns to ten, then blocked permanently, because a conversation
    #: key does not refill with time. Measured at r3: turn 11 refused, no
    #: recovery. (#152 adds the one exception: a cold key can be evicted by the
    #: LRU, which needs 512 other conversations and is not something the
    #: blocked operator can cause.)
    #:
    #: 40 RESTORES THE EFFECTIVE ALLOWANCE rather than raising the bound: it is
    #: the same twenty operator turns the old default bought, re-expressed in
    #: the unit the field now counts. It is also MAX_DELEGATIONS_PER_CONTEXT,
    #: so a deployment wanting more must change the backstop in code, which is
    #: the intended shape.
    #:
    #: A DEPLOYMENT WITH A REVIEWER COSTS 3 PER TURN, not 2, so it gets
    #: thirteen. Tune this per identity rather than assuming the default suits
    #: a wider fan-out.
    delegation_budget_limit: int = 40
    #: Seconds before a CALLER-keyed delegation budget refills. Only the caller
    #: fallback is windowed; a conversation budget does not refill with time
    #: (an LRU eviction can still retire a cold key -- see
    #: DelegationBudgetGuardrail). Without this a caller -- whose key is stable
    #: for the daemon's lifetime -- is blocked until restart once it is over
    #: budget, because retrying keeps its key too warm for the LRU to reclaim.
    delegation_budget_caller_window_seconds: float = 3600.0

    @field_validator("delegation_budget_limit")
    @classmethod
    def _budget_at_least_one(cls, v: int) -> int:
        # Mirrors the doom_loop validator: surface a field-named error at
        # config load rather than aborting create_app inside router
        # construction. The guard itself also raises for < 1.
        if v < 1:
            raise ValueError(
                f"guardrails.delegation_budget_limit must be >= 1 (got {v}); "
                "raise it or disable the guard with delegation_budget = false"
            )
        return v

    @field_validator("delegation_budget_caller_window_seconds")
    @classmethod
    def _caller_window_is_positive(cls, v: float) -> float:
        # THE DANGEROUS DIRECTION HERE IS DOWNWARD, which is why this needs a
        # floor at all (#159). For delegation_budget_limit, weakening the guard
        # means raising the number, and that is clamped at the ceiling. For the
        # window, weakening means LOWERING it -- and at 0 the window expires
        # before every check, so the counter is pinned at 1 and the caller-keyed
        # budget is silently off. An operator shortening the window to help
        # blocked callers recover sooner is walking toward disabling the
        # backstop, and without this gets no error on arrival.
        if v <= 0:
            raise ValueError(
                "guardrails.delegation_budget_caller_window_seconds must be > 0 "
                f"(got {v}); a window of zero silently disables the caller-keyed "
                "budget. Disable the guard with delegation_budget = false if that "
                "is what you mean."
            )
        return v

    @field_validator("doom_loop_threshold")
    @classmethod
    def _threshold_at_least_one(cls, v: int) -> int:
        # DoomLoopGuardrail itself raises for threshold < 1; catch it here at
        # config-load so a bad identity.toml surfaces a field-named error
        # instead of aborting create_app deep in router construction.
        if v < 1:
            raise ValueError(
                f"guardrails.doom_loop_threshold must be >= 1 (got {v}); "
                "raise it or disable the guard with doom_loop = false"
            )
        return v


class IdentityConfig(_Open):
    cabal: CabalConfig = Field(default_factory=CabalConfig)
    personas: dict[str, PersonaConfig] = Field(default_factory=dict)
    litellm: LiteLLMConfig = Field(default_factory=LiteLLMConfig)
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)
    techne: TechneConfig = Field(default_factory=TechneConfig)
    liminal: LiminalConfig = Field(default_factory=LiminalConfig)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
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
    not the operator's home (techne#441). A relative `data_dir` anchors at the
    directory holding `config_path`, so config and data co-locate under one
    root; an absolute `data_dir` is used as-is.

    `config_path` must name the identity *file*, not a directory — its parent
    is taken as the anchor. When it's a bare filename (the app default,
    "identity.toml"), the anchor is the resolved CWD — which is exactly where
    that file is read from — so data still lands beside the identity file. With
    a bare default this coincides with a CWD-relative root; pass a config path
    with a directory to anchor elsewhere.

    Pure + dependency-free (no liminal/techne import) so it resolves the same
    whether or not liminal is wired — cabal works standalone.
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
