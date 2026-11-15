"""identity.toml loader.

The single source of truth for declared personas, their safety tiers,
LLM routing, and Cabal Core's own bind config. Subordinates are declared
under personas.<name>.subordinates and instantiated per dispatch.

Schema is enforced by pydantic; unknown keys are allowed (forward-compat
with future Cabal versions adding fields) but unknown top-level sections
warn at load time.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.12+ only per pyproject
    import tomli as tomllib

log = structlog.get_logger(__name__)


KNOWN_TOP_LEVEL = {
    "cabal",
    "groups",
    "review",
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
    # The base URL clients reach this cabal at, e.g. "https://cabal.example".
    # A2A v1.0 cards name each persona's interface by it (#270), and are not
    # served without it. $CABAL_PUBLIC_URL wins over this.
    public_url: str | None = None
    # The persona whose v1.0 card is served at /.well-known/agent-card.json,
    # the domain's front door (#270), e.g. "mason-primus". Unset: no root card.
    # $CABAL_FRONT_DOOR wins over this.
    front_door: str | None = None
    # How many messages of a conversation an A2A v1.0 SendMessage replays to
    # the persona when it starts a new task in an existing contextId (#276):
    # the newest ones are kept. 0 turns replay off.
    context_replay_messages: int = Field(default=40, ge=0)
    # A2A v1.0 SendMessage with returnImmediately runs detached (#277): at most
    # this many at once (more are refused), and on shutdown each gets this
    # long to stop before it is cancelled.
    detached_task_limit: int = Field(default=16, ge=0)
    detached_drain_seconds: float = Field(default=10.0, ge=0)
    # How often the Scheduler looks for scheduled dispatches that are due
    # (cabal issue 406). A task starts up to this long after its time.
    schedule_poll_seconds: float = Field(default=15.0, gt=0)
    # How many times in a row cabal continues an agent that replied "waiting
    # until <time>" (reason 705, issue 406). Past it, the task pauses as
    # input-required for the caller, so an agent can't wait forever.
    max_wait_continuations: int = Field(default=5, ge=0)
    # Where the operator bootstrap code minted at startup goes (#325): "log"
    # (the warning line `make bootstrap` and the k8s steps read), "file"
    # ($CABAL_HOME/bootstrap_code, mode 0600, with only its path logged) or
    # "off" (mint nothing). $CABAL_BOOTSTRAP_CODE wins over this.
    bootstrap_code: Literal["log", "file", "off"] = "log"
    # How long an operator token lasts, from `POST /auth/bootstrap` or
    # `POST /auth/renew` (#395). A token renewed before it expires gets a fresh
    # one this long; a lapsed one needs a new bootstrap code.
    operator_token_ttl_seconds: int = Field(default=60 * 60, ge=60)
    # The longest a `POST /admin/token/mint-service` token may last, in days
    # (#403). A longer request is cut to this; the response's expires_at shows it.
    service_token_max_ttl_days: int = Field(default=365, ge=1, le=365)


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
    #: cabal#172 -- the most fan-out legs THIS primus may DECLARE. None means
    #: the module ceiling (`MAX_LEGS_PER_FANOUT`). Ignored for a non-primus
    #: persona, which has no fan-out.
    #:
    #: A DECLARATION CAP, NOT A RUNTIME THROTTLE, and the difference is the
    #: whole behaviour of the field (#178 r1). It is checked ONCE, in the
    #: constructor, against the non-reviewer `subordinates` -- which are
    #: assigned at construction and never mutated. THE REVIEWER DOES NOT COUNT:
    #: legs are what a fan-out dispatches concurrently, and the reviewer runs
    #: after them (`primus.py` builds `legs` by excluding it). A mason
    #: declaring implementer and reviewer declares ONE leg. It does not select a subset of legs at dispatch and
    #: there is no path by which it could: `leg_roles` there is derived from
    #: the same `subordinates` the constructor already bounded.
    #:
    #: SO SETTING IT BELOW A PERSONA'S DECLARED LEG COUNT DOES NOT NARROW THAT
    #: PERSONA -- IT REMOVES IT. The constructor raises, `persona_factory`
    #: catches and skips the handler with a `personas.primus_config_rejected`
    #: error, and the deployment boots WITHOUT that agent. Measured on a mason
    #: declaring three legs: override 3, 4 or 8 all register and all open three
    #: legs; override 1 or 2 register nothing at all.
    #:
    #: That is the shape cabal#172 asked for and the reason it asked ("a config
    #: path has to feed the constructor check, not the dispatch one, or a
    #: misconfigured deployment goes back to booting successfully and failing
    #: every dispatch"). Read it as a POLICY ASSERTION -- this persona must not
    #: declare more than N legs -- rather than as a dial that trims a wide
    #: fan-out down to size. Bounding CONCURRENCY without dropping declared
    #: work is a different feature and is cabal#181.
    #:
    #: PER-PERSONA RATHER THAN A SINGLE `[guardrails]` NUMBER, because breadth
    #: is a property of the persona: a mason with three legs and a wright with
    #: one do not want the same ceiling. cabal#172 flagged this as setting a
    #: precedent for per-persona guardrail knobs -- it does not. `may_delegate_to`
    #: a few lines down is already one, and the `*_override` naming here is
    #: already the shape for a per-persona bound on a constructor argument.
    #:
    #: LOWERS ONLY. A value above `MAX_LEGS_PER_FANOUT` is clamped to it and
    #: logged, never honoured; the module constant is the hard backstop and a
    #: policy cannot raise it. Until this field existed the clamp had nothing
    #: to clamp FROM, which made the "a policy may only lower it" story
    #: vacuous -- accurate about the code and about a policy that could not
    #: exist (#156 r2).
    max_legs_override: int | None = None

    @field_validator("max_legs_override")
    @classmethod
    def _max_legs_at_least_one(cls, v: int | None) -> int | None:
        # Refused rather than clamped up, unlike an over-wide value. Zero or
        # negative is not a narrow fan-out an operator could have meant -- it
        # is a primus that can never dispatch, and the constructor's floor
        # would silently turn it into 1. Same argument as
        # `delegation_depth_limit`'s validator (#144 pre-pass).
        if v is not None and v < 1:
            raise ValueError(
                f"max_legs_override must be >= 1 (got {v}); a primus with no "
                "legs cannot dispatch, and the floor would silently read it "
                "as 1."
            )
        return v
    #: Issue 394, for a primus. Tokens each fan-out leg (the reviewer too) may
    #: spend, carved out of the fan-out's budget when the leg starts. Unset
    #: means the token budget's default (200k).
    leg_token_budget_override: int | None = None
    #: Issue 394, for a primus. Tokens the whole fan-out may spend: the primus's
    #: own plan and aggregate calls plus every leg. A fan-out that can't cover
    #: every leg's allotment and one aggregate call is refused before any leg
    #: starts. Unset means one default budget plus one allotment per leg.
    fanout_token_budget_override: int | None = None

    @field_validator("leg_token_budget_override", "fanout_token_budget_override")
    @classmethod
    def _token_budget_positive(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError(f"a token budget must be >= 1 (got {v})")
        return v

    @field_validator("leg_token_budget_override")
    @classmethod
    def _leg_budget_covers_a_call(cls, v: int | None) -> int | None:
        # Every model call reserves RESERVE_PER_DISPATCH up front, so a smaller
        # allotment passes the fan-out's affordability check and then fails
        # every worker's first call with 702, after the plan call was paid for.
        from cabal.core.llm.budget import RESERVE_PER_DISPATCH

        if v is not None and v < RESERVE_PER_DISPATCH:
            raise ValueError(
                f"leg_token_budget_override must be at least {RESERVE_PER_DISPATCH} "
                f"(got {v}): each model call reserves that much before it runs"
            )
        return v

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
    #: cabal#189 -- edges for callers that are NOT personas of this cabal: room
    #: colleagues and outside agents, whose tokens carry ``sub="agent:<name>"``
    #: and no ``cabal_persona`` claim. Keyed by that FULL sub, prefix included,
    #: e.g. ``"agent:ext-acme" = ["archivist"]``; targets are persona names,
    #: resolved like ``may_delegate_to``.
    #:
    #: A SEPARATE TABLE, NOT MORE KEYS ON THE PERSONAS, because a persona name
    #: and an agent name are two namespaces. Stripping the prefix would hand a
    #: colleague or outsider named ``archivist`` the local archivist persona's
    #: edges. Keeping ``agent:`` in the key means the two can never meet.
    #:
    #: Same default as personas: a sub with no entry may delegate to nobody.
    #: Inert unless ``delegation_edges = true``. The room's dispatch pairs
    #: (cabal#127) are a separate check and do not count as edges here: they
    #: are room-synced, and an edge must be operator-authored.
    caller_edges: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("caller_edges")
    @classmethod
    def _caller_edges_keyed_by_agent_sub(
        cls, v: dict[str, list[str]]
    ) -> dict[str, list[str]]:
        # "agent:" is AGENT_SUB_PREFIX in core/auth/jwt.py; this module does
        # not import core, so the literal is repeated and a test pins the two.
        bad = sorted(k for k in v if not k.startswith("agent:") or k == "agent:")
        if bad:
            raise ValueError(
                f"guardrails.caller_edges keys must be a full token sub of the "
                f"form 'agent:<name>' (got {bad}). A persona's own edges go in "
                "may_delegate_to under [personas.<name>]."
            )
        return v
    #: cabal#144 — bound how deep a delegation chain runs. Default-on like its
    #: two neighbours; the guard's own ceiling (MAX_DELEGATION_DEPTH) is a hard
    #: backstop a policy can only lower.
    #:
    #: IT BOUNDS A DEPTH AN ARRIVING CHAIN DECLARES, not one cabal accumulates:
    #: cabal's own fan-out stamps a depth the router never sees, and a federated
    #: hop forwards without incrementing, so this cannot trip on cabal-only
    #: traffic (cabal#171). Reading it as a bound on your own delegation is the
    #: misreading to avoid, and this is the field an operator sets.
    delegation_depth: bool = True
    delegation_depth_limit: int = 8
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
    #: cabal#174 -- how long a PERSISTED conversation count is honoured. Only
    #: the conversation class is persisted: the guard's clock is
    #: `time.monotonic`, which resets on restart, so a restored caller-keyed
    #: window would be measured against an origin that no longer exists.
    #:
    #: This has no in-memory counterpart because `_BUDGET_MAXSIZE` is the
    #: counter's ceiling there and a table has none. A restored count only ever
    #: tightens the bound, so erring long is the safe direction.
    delegation_budget_retention_s: float = 86400.0

    @field_validator("delegation_budget_retention_s")
    @classmethod
    def _retention_positive(cls, v: float) -> float:
        # Same door-count argument as caller_window_seconds: the guard is
        # constructible directly and validates there too, so neither the config
        # model nor the constructor is the only entry point. A zero or negative
        # retention prunes every row on every hydrate, which would silently
        # restore the pre-#174 behaviour while looking configured.
        if v <= 0:
            raise ValueError(
                f"guardrails.delegation_budget_retention_s must be > 0 (got {v}); "
                "a non-positive retention prunes every persisted count on "
                "startup, silently re-arming the budget on restart."
            )
        return v

    @field_validator("delegation_depth_limit")
    @classmethod
    def _depth_at_least_one(cls, v: int) -> int:
        # ADDED BY THE #144 PRE-PASS, which found depth was the one bounded
        # axis with no config validator: delegation_depth_limit = 0 loaded
        # silently and the guard floored it to 1, where the same typo in
        # delegation_budget_limit refuses to start. The guard still clamps
        # rather than raising -- a depth of 1 is a working bound, unlike a
        # budget of 0 -- but a value the operator cannot have meant should be
        # named at load on every axis.
        if v < 1:
            raise ValueError(
                f"guardrails.delegation_depth_limit must be >= 1 (got {v}); "
                "raise it or disable the guard with delegation_depth = false"
            )
        return v

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


class ReviewRepoConfig(_Open):
    """One repo the review watch loop looks at: `[review.repos."owner/name"]`."""

    # The seats for this repo's runs, in order. Unset: `[review] seats_default`.
    seats: list[str] | None = None
    # The merge gate (step 5). On: a run that ends approved, with an APPROVED
    # review among its steps, goes to `merging`; once CI is green at its sha it
    # is merged, pinned to that sha. Off (the default): it stays `approved`.
    merge: bool = False
    # "github": every check run and commit status at the sha must be green.
    # "none": no CI wait (for a repo without any).
    ci: Literal["github", "none"] = "github"
    merge_method: Literal["squash", "merge", "rebase"] = "squash"
    # Review PRs whose head branch is in another repo (issue 431). Off: such a PR's
    # label is removed with a comment saying why. The code under review can read the
    # reviewer's token, and its author can push to the branch, so it's opt-in and
    # never combined with the merge gate.
    allow_forks: bool = False

    @model_validator(mode="after")
    def _no_fork_merges(self) -> ReviewRepoConfig:
        if self.allow_forks and self.merge:
            raise ValueError("allow_forks and merge can't both be on: a fork PR is never merged by cabal")
        return self


def _default_seat_kinds() -> dict[str, Literal["prepass", "github_review"]]:
    return {"temperance": "prepass", "prudence": "github_review"}


class ReviewConfig(_Open):
    """The review workflow (cabal issue 399): `[review]` in identity.toml.

    Off unless `enabled`. When on, a loop every `poll_seconds` claims queued
    review runs (at most `max_running` at once) and takes each through its seats.
    """

    enabled: bool = False
    poll_seconds: float = Field(default=30.0, gt=0)
    max_running: int = Field(default=3, ge=1)
    # A seat with no outcome this long after its dispatch fails the step, once
    # retried. Fates on a large diff take 20-40 minutes.
    step_timeout_s: float = Field(default=5400.0, gt=0)
    seats_default: list[str] = Field(default_factory=lambda: ["temperance", "prudence"])
    # How each seat's outcome is read. "prepass": its reply carries `## Verdict`
    # with an "N wrong" count, and any WRONG stops the run. "github_review": the
    # outcome is the review `review_logins[seat]` posted on the PR at the run's sha,
    # never the seat's reply or exit code.
    seat_kinds: dict[str, Literal["prepass", "github_review"]] = Field(
        default_factory=_default_seat_kinds
    )
    review_logins: dict[str, str] = Field(default_factory=lambda: {"prudence": "prudence-tenishai"})
    # Prepares one seat's checkout. argv; each element may use {seat}, {repo}, {pr},
    # {sha}, {notes} (a file holding the round's notes for the brief, or empty) and
    # {prepass} (for a github_review seat, a file holding the reply of the pre-pass
    # seat that ran before it, or empty).
    # Its stdout's last line is one JSON object: {"workspace": <path>, "brief":
    # <text>, "metadata": {...}}. The brief and metadata go to the seat as they are.
    # The {notes} and {prepass} files are removed when the command exits, so it must
    # copy what it needs (fates-dispatch.sh puts both into the brief).
    prepare_command: list[str] = Field(default_factory=list)
    # Run after a step ends, argv with {workspace}. Unset: the workspace stays.
    cleanup_command: list[str] = Field(default_factory=list)
    # Run once per finished run with the run's JSON on stdin. Unset: logged only.
    notify_command: list[str] = Field(default_factory=list)
    # GitHub, for the PR head and reviews. The token is read from this variable.
    github_api: str = "https://api.github.com"
    github_token_env: str = "CABAL_GITHUB_TOKEN"
    # Who the engine dispatches as: the sub of the operator-scoped claims it mints
    # under. A run's `requested_by` is recorded, not used for dispatch.
    dispatch_sub: str = "cabal:reviews"
    # The watch loop (step 4): every `watch_poll_seconds`, each open, non-draft PR in
    # `repos` carrying `label` gets a run at its head, and the label is removed. A
    # repo not listed here is never watched; `cbl review start` still works for it.
    repos: dict[str, ReviewRepoConfig] = Field(default_factory=dict)
    label: str = "ready-for-review"
    # The least repo role whose label starts a run (issue 431). GitHub lets triage
    # add labels; in a repo with the merge gate on, write is required whatever this says.
    label_min_role: Literal["triage", "write", "maintain", "admin"] = "write"
    watch_poll_seconds: float = Field(default=300.0, gt=0)
    # The merge gate: how long a `merging` run waits, from its approval, for CI to
    # go green (or a GitHub outage to end) before it ends `unmerged`.
    ci_timeout_s: float = Field(default=3600.0, gt=0)


class IdentityConfig(_Open):
    cabal: CabalConfig = Field(default_factory=CabalConfig)
    personas: dict[str, PersonaConfig] = Field(default_factory=dict)
    litellm: LiteLLMConfig = Field(default_factory=LiteLLMConfig)
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)
    techne: TechneConfig = Field(default_factory=TechneConfig)
    liminal: LiminalConfig = Field(default_factory=LiminalConfig)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    hooks: dict[str, Any] = Field(default_factory=dict)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    # cabal issue 423: `[groups]`, name -> member agents, in the order a task
    # goes to them when several are idle. A task sent to a group starts on
    # the first member with nothing running.
    groups: dict[str, list[str]] = Field(default_factory=dict)


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


#: Overrides `cabal.data_dir`. The image sets it to the volume (`/data`), the same
#: way it sets CABAL_DB_URL, because the identity file a container mounts is the
#: operator's copy of a template that says `data_dir = "data"` — which resolves to
#: `/app/data`, the container's writable layer, and is lost on recreate (#214).
DATA_DIR_ENV = "CABAL_DATA_DIR"


def resolve_data_dir(identity: IdentityConfig | None, config_path: str | Path) -> Path:
    """Absolute agent data-root for cabal-owned, provisioned resources.

    `cabal.data_dir` is the agent's root — an agent's root is identity-defined,
    not the operator's home (techne#441). A relative `data_dir` anchors at the
    directory holding `config_path`, so config and data co-locate under one
    root; an absolute `data_dir` is used as-is.

    `config_path` must name the identity *file*, not a directory — its parent
    is taken as the anchor. When it's a bare filename (e.g. ``--config
    identity.toml``), the anchor is the resolved CWD — which is exactly where
    that file is read from — so data still lands beside the identity file. The
    app's own default is a full path from ``cabal.config.home`` (#211).

    ``$CABAL_DATA_DIR``, when set, wins over the identity file and anchors the
    same way (#214).

    Dependency-free (no liminal/techne import) so it resolves the same whether
    or not liminal is wired — cabal works standalone.
    """
    raw = (
        os.environ.get(DATA_DIR_ENV)
        or (identity.cabal.data_dir if identity is not None else CabalConfig().data_dir)
        or "data"
    )
    d = Path(raw).expanduser()
    if d.is_absolute():
        return d
    return Path(config_path).expanduser().resolve().parent / d


def _env_or(name: str, configured: str | None) -> str | None:
    """The environment value when it is set and not blank, else the
    configured one when that is not blank, else None. A blank variable is
    unset, so it never hides the identity file's value."""
    for value in (os.environ.get(name), configured):
        if value is not None and value.strip():
            return value
    return None


#: Overrides `cabal.public_url` (#270).
PUBLIC_URL_ENV = "CABAL_PUBLIC_URL"


def resolve_public_url(identity: IdentityConfig | None) -> str | None:
    """The configured public base URL, without a trailing slash, or None when
    none is set. Raises ValueError for a value that is not an absolute http(s)
    URL, so a typo is not published in a card."""
    raw = _env_or(PUBLIC_URL_ENV, identity.cabal.public_url if identity is not None else None)
    if raw is None:
        return None
    url = raw.strip().rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"cabal.public_url must be an absolute http(s) URL, not {raw!r}")
    if parsed.query or parsed.fragment:
        raise ValueError(f"cabal.public_url must not carry a query or fragment: {raw!r}")
    return url


#: Overrides `cabal.bootstrap_code` (#325).
BOOTSTRAP_CODE_ENV = "CABAL_BOOTSTRAP_CODE"
BOOTSTRAP_CODE_MODES = ("log", "file", "off")


def resolve_bootstrap_code(identity: IdentityConfig | None) -> str:
    """Where the startup bootstrap code goes: "log", "file" or "off". Raises
    ValueError for an unknown $CABAL_BOOTSTRAP_CODE, so a typo can't fall
    back to writing the code to the log."""
    raw = _env_or(BOOTSTRAP_CODE_ENV, identity.cabal.bootstrap_code if identity is not None else None)
    mode = raw.strip().lower() if raw is not None else "log"
    if mode not in BOOTSTRAP_CODE_MODES:
        raise ValueError(
            f"{BOOTSTRAP_CODE_ENV} must be one of {', '.join(BOOTSTRAP_CODE_MODES)}, not {raw!r}"
        )
    return mode


#: Overrides `cabal.front_door` (#270).
FRONT_DOOR_ENV = "CABAL_FRONT_DOOR"


def resolve_front_door(identity: IdentityConfig | None) -> str | None:
    """The bulletin agent name configured as the front door, or None."""
    raw = _env_or(FRONT_DOOR_ENV, identity.cabal.front_door if identity is not None else None)
    return raw.strip() if raw is not None else None


def resolve_under_data_dir(raw: str | Path, data_dir: Path) -> Path:
    """Resolve a configured resource path: absolute used as-is, relative placed
    under the agent data-root. Lets an operator write `db_path = "techne.db"`
    and have it land under the agent root rather than an unanchored CWD path."""
    p = Path(raw).expanduser()
    return p if p.is_absolute() else data_dir / p
