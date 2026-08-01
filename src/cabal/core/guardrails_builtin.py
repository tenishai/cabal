"""Built-in concrete guardrails — cabal#42.

Concrete Guardrail implementations that ship with cabal. Import and wire into
DispatchRouter or LLMPersona as needed.

These guardrails are independently unit-testable and are deliberately kept
separate from the Guardrail/Verdict abstraction defined in guardrails.py —
that module defines the protocol; this one provides concrete implementations.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from typing import Any

from cabal.core.a2a.types import Task
from cabal.core.guardrails import GuardrailContext, Verdict


class DoomLoopGuardrail:
    """Detect and block repeated identical delegations within a session.

    Implements opencode's doom-loop detector (cabal#42): count *identical*
    delegations to the same target within a conversation session; at
    ``threshold`` (default 3) return a BLOCK verdict with a CorrectedError-shaped
    reason message so the caller can change approach. Counting is per-delegation-
    key *cumulative* within a session (not merely consecutive) — see Counter
    lifecycle — so interleaved multi-role loops (impl, rev, impl, rev, …) are
    caught, not just back-to-back repeats.

    **Session key** — ``task.context_id`` when present; falls back to
    ``task.id`` when ``context_id`` is absent. Grouping only works when a
    *stable, shared, non-None* key recurs across the delegations.

    SCOPE / EFFICACY (cabal#59, now closed): the counter trips when the same
    session key recurs. Two loop paths are now covered:

    1. **Mason fan-out** (cabal#59): ``MasonPrimusPersona`` holds a shared
       ``DoomLoopGuardrail`` instance (default-on) and runs ``check()`` at
       the subordinate boundary in ``_dispatch_subordinate`` before calling
       ``subord.handle_task``. Sub-tasks inherit the parent's ``context_id``
       so repeated identical delegations within a session share a stable
       session key and accumulate toward the threshold.

    2. **A2A inbound** (cabal#59): ``MessageSendParams`` now accepts a
       ``contextId`` field; both ``message/send`` and ``message/stream``
       routes propagate it onto the inbound ``Task``. When a client (or
       peer agent) supplies a stable ``contextId`` across hops, the router's
       ``input_guardrails`` can group those hops and detect the loop — but
       ONLY on the non-streaming ``dispatch()`` path: ``dispatch_streaming``
       does not run ``input_guardrails`` at all (and refuses federated
       targets outright), so streaming hops are never router-guarded,
       ``contextId`` or not. Without a caller-supplied ``contextId`` each hop
       falls back to its fresh task uuid, so cross-hop detection on the
       non-streaming path requires the client to thread the id (``cbl chat``
       non-streaming reuses one context id per session — see "Original scope").

       As of cabal#76 the production router (``app.py``
       ``build_router_input_guardrails``) wires a ``DoomLoopGuardrail`` as a
       router ``input_guardrail`` by DEFAULT (identity.toml ``[guardrails]
       doom_loop = false`` opts out) — so the inbound ``contextId`` now feeds
       the guard, and crucially this is the enforcement point for
       ``kind="federated"`` peers (the **Mason** path below is in-process only
       and never reaches a remote hop).

    Original scope (still accurate for standalone use): a client re-sending
    a byte-identical message under a shared ``context_id`` (e.g. ``cbl chat``
    non-streaming, which reuses one context id per session) is caught by the
    router's ``input_guardrails``.

    **Delegation key** — ``SHA-256(target_agent_name || NUL || payload)``
    where:
    - ``target_agent_name`` is read from ``task.metadata["cabal_target_agent"]``
      (set by the router before calling ``run_guardrails`` — cabal#42's router
      change).  Absent metadata falls back to the empty string; the delegation
      key still works, it just distinguishes by payload only.
    - ``payload`` is the canonical JSON serialisation of the task's current
      message parts (``task.status.message``).  History is intentionally
      excluded: the doom loop is about the *current delegation*, not the
      accumulated conversation.

    **Counter lifecycle** — each session holds a map of ``delegation_key ->
    cumulative_count``. Every delegation increments its own key's count; a
    *different* delegation increments a *different* key (it does not reset the
    others), so an interleaved ``impl, rev, impl, rev, …`` loop still drives
    ``impl``'s count to the threshold. Once a key hits ``threshold`` its
    identical repeats keep returning BLOCK; distinct delegations (distinct keys)
    return PASS on their own counts. Trade-off vs. a consecutive-only counter:
    this will also flag the same ``(target, payload)`` recurring ``threshold``
    times spread across a session — which for a byte-identical delegation is
    itself a strong doom-loop signal.

    **Dict growth (bounded — cabal#63)** — memory is bounded on BOTH axes by
    LRU eviction. The outer session dict caps at ``maxsize`` (default 10 000);
    each session's inner map caps at ``per_session_maxsize`` (default 512)
    distinct delegation keys. Eviction drops the *least-recently-checked*
    entry, so total memory is O(``maxsize`` × ``per_session_maxsize``) tuples,
    regardless of session lifetime. A hot looping key survives eviction *as
    long as it recurs within ``per_session_maxsize`` distinct intervening
    keys* — a loop cycling through more than ``per_session_maxsize`` distinct
    delegations would evict its own members before recurrence (the default
    dwarfs any realistic cycle).

    A per-key ``ttl_seconds`` (default 600) lazily resets a delegation's count
    when it hasn't recurred within the window — so the guard fires on N repeats
    *without progress* rather than N ever; ``None`` disables it. NOTE: this
    applies even to an already-blocking key — a loop that pauses longer than
    the TTL and resumes restarts from count 1 (intentional: a repeat only every
    >``ttl`` seconds is not the resource-burning runaway this guard targets).

    **No hard-halt** — BLOCK becomes a policy-violation Task in the router
    (via ``annotate``).  The reason message is surfaced back into the calling
    persona's context, nudging it to change approach without terminating the
    overall chain.  Auto-escalation to a supervisor persona is a follow-on
    (see cabal#42).

    Wiring example::

        from cabal.core.guardrails_builtin import DoomLoopGuardrail
        router = DispatchRouter(
            ...,
            input_guardrails=[DoomLoopGuardrail()],
        )
    """

    name = "doom_loop"

    def __init__(
        self,
        threshold: int = 3,
        maxsize: int = 10_000,
        per_session_maxsize: int = 512,
        ttl_seconds: float | None = 600.0,
        *,
        _now: Callable[[], float] = time.monotonic,
    ) -> None:
        if threshold < 1:
            raise ValueError(f"threshold must be >= 1, got {threshold}")
        if maxsize < 1 or per_session_maxsize < 1:
            raise ValueError("maxsize and per_session_maxsize must be >= 1")
        self._threshold = threshold
        self._maxsize = maxsize
        self._per_session_maxsize = per_session_maxsize
        self._ttl = ttl_seconds  # None disables the time-based reset
        self._now = _now
        # {session_key: {delegation_key: (cumulative_count, last_seen)}}
        # Per-key *cumulative* counts within a session — NOT a single
        # consecutive slot. Consecutive-only counting misses the interleaved
        # multi-role loop (Mason's implementer/reviewer fan-out produces
        # impl, rev, impl, rev, … so no two identical keys are ever adjacent);
        # cumulative per-key counting catches the repeated `impl(X)` regardless
        # of what runs between the repeats.
        #
        # BOTH dicts are bounded LRU (see check): the outer caps sessions
        # (maxsize), the inner caps distinct delegation keys per session
        # (per_session_maxsize). "Recently used" = most-recently checked, so
        # eviction only ever drops a cold entry — a guard against runaway
        # behaviour can't itself grow unboundedly. CAVEAT (standard LRU): a hot
        # key survives only if it recurs within `per_session_maxsize` *distinct*
        # intervening keys; a loop cycling through C distinct delegation keys
        # therefore needs `per_session_maxsize >= C` or its members evict each
        # other before recurrence and the loop escapes detection. The default
        # (512) dwarfs any realistic C. A per-key TTL resets a stale count so
        # "N identical *without progress*" means within a time window, not
        # forever.
        self._state: dict[str, dict[str, tuple[int, float]]] = {}

    async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
        """Return PASS until a delegation key recurs `threshold` times in the
        session (within the TTL window); BLOCK at the threshold-th occurrence.

        MUST REMAIN AWAIT-FREE. A primus fan-out (cabal#69) calls this
        concurrently across legs sharing ONE guard instance. On a cooperative
        event loop an await-free `async def` runs atomically to completion, so
        the shared `_state` mutations can't interleave. Adding any `await` here
        (a cache/DB lookup, say) would reintroduce a data race on the counter —
        guard the state instead of awaiting inside it.
        """
        if not isinstance(target, Task):
            # Non-Task targets (input/output phase) are not doom-loop candidates.
            return Verdict.pass_()

        task: Task = target

        # Session key: prefer context_id (stable across hops) over task id.
        session_key = task.context_id or task.id

        # Delegation key: (target_agent, canonical payload).
        target_name = str(task.metadata.get("cabal_target_agent", ""))
        payload = _canonical_payload(task)
        delegation_key = _delegation_hash(target_name, payload)
        now = self._now()

        # Outer LRU: move an existing session to most-recent; for a new session
        # evict the least-recently-used one if at capacity. (pop-then-reinsert
        # puts the touched session last in insertion order = most-recent.)
        counts = self._state.pop(session_key, None)
        if counts is None:
            if len(self._state) >= self._maxsize:
                self._state.pop(next(iter(self._state)))  # evict LRU session
            counts = {}
        self._state[session_key] = counts

        # Per-key cumulative count with lazy TTL reset.
        prev = counts.pop(delegation_key, None)
        if prev is not None and self._ttl is not None and (now - prev[1]) > self._ttl:
            prev = None  # stale beyond the TTL window — restart this key's count
        count = (prev[0] if prev is not None else 0) + 1
        counts[delegation_key] = (count, now)  # reinsert last = most-recent

        # Inner LRU: cap distinct delegation keys per session. The just-touched
        # key is most-recent (last), so eviction drops the least-recently-
        # checked key — never the current one. (See the __init__ CAVEAT: a loop
        # whose distinct-key cardinality exceeds the cap evicts its own members
        # before they recur.)
        if len(counts) > self._per_session_maxsize:
            counts.pop(next(iter(counts)))

        if count >= self._threshold:
            label = target_name or "unknown"
            return Verdict.block(
                f"{count} identical delegations to {label!r} without progress"
                f" — pausing; change approach"
            )
        return Verdict.pass_()


# ---------------------------------------------------------------------------
# ValidateBeforeRecord — cabal#43
# ---------------------------------------------------------------------------


class ValidateBeforeRecord:
    """Validate a mutating-tool result before it is appended to the conversation.

    Applied as a ``tool_result_guardrail`` on an LLMPersona.  For tools
    identified as *mutating*, the pluggable ``validate`` callable is run; on
    failure (a non-None error string), a BLOCK verdict is returned so the
    structured error reaches the persona *before* the observation is recorded.
    The existing #52 tool_result BLOCK path injects the error as the tool
    message and sets is_error=True — no extra wiring needed.

    For non-mutating tools (or when tool identity is absent from the context)
    the guardrail is a no-op pass.

    Note: the str_replace-vs-line-number edit-format preference mentioned in
    issue #43 relates to the edit tool itself, not this guardrail — that is a
    separate concern tracked outside this implementation.

    Wiring example::

        from cabal.core.guardrails_builtin import ValidateBeforeRecord

        def _run_lint(tool_name: str, arguments: dict, result: str) -> str | None:
            # Return an error string on failure, None on pass.
            ...

        persona = LLMPersona(
            ...,
            tool_result_guardrails=[
                ValidateBeforeRecord(
                    mutating_tools={"code_edit", "write_file"},
                    validate=_run_lint,
                )
            ],
        )

    Args:
        mutating_tools: Either a ``set[str]`` of tool names that are
            considered mutating, or a ``Callable[[str], bool]`` predicate
            that returns True for mutating tool names.  Any tool NOT in this
            set (or for which the predicate returns False) passes unconditionally.
        validate: ``(tool_name, arguments, result) -> str | None``.  Return
            an error string on validation failure, or ``None`` on pass.  The
            callable must be synchronous; wrap async validators in
            ``asyncio.run`` or restructure the guardrail.  WARNING: it runs
            inline in the async dispatch — a blocking validator (e.g. a linter
            subprocess) stalls the event loop for its whole duration; offload
            anything heavyweight with ``loop.run_in_executor``.
    """

    name = "validate_before_record"

    def __init__(
        self,
        mutating_tools: set[str] | Callable[[str], bool],
        validate: Callable[[str, dict[str, Any], str], str | None],
    ) -> None:
        if callable(mutating_tools):
            self._is_mutating: Callable[[str], bool] = mutating_tools
        else:
            _frozen = frozenset(mutating_tools)
            self._is_mutating = lambda n, _s=_frozen: n in _s
        self._validate = validate

    async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
        """Return BLOCK when the validator fails for a mutating tool; PASS otherwise."""
        tool_name = ctx.tool_name
        if tool_name is None or not self._is_mutating(tool_name):
            return Verdict.pass_()

        arguments: dict[str, Any] = ctx.tool_arguments or {}
        result_str = target if isinstance(target, str) else str(target)

        try:
            error = self._validate(tool_name, arguments, result_str)
        except Exception as exc:  # noqa: BLE001
            # A buggy validator must not crash the whole dispatch (cf. #57's
            # malformed-rewrite fix). Fail CLOSED: a mutation we could not
            # validate is blocked with the cause surfaced, rather than silently
            # recorded as if it passed.
            return Verdict.block(
                f"validator for {tool_name} raised {type(exc).__name__}: {exc}"
            )
        if error is not None:
            return Verdict.block(f"validation failed for {tool_name}: {error}")
        return Verdict.pass_()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _canonical_payload(task: Task) -> str:
    """Stable, human-readable serialisation of the task's current message parts.

    Only ``task.status.message`` is considered — history is excluded because
    we care about the *current* delegation payload, not the accumulated
    conversation context.
    """
    msg = task.status.message
    if msg is None:
        return ""
    parts: list[tuple[str, str]] = []
    for i, part in enumerate(msg.parts):
        if hasattr(part, "text"):
            parts.append(("text", part.text))
        elif hasattr(part, "data"):
            # Sort keys for a deterministic representation of structured data.
            # A non-JSON-serialisable data part must NOT crash the dispatch this
            # guardrail is meant to protect — fall back to repr() on failure.
            try:
                encoded = json.dumps(part.data, sort_keys=True)
            except (TypeError, ValueError):
                encoded = repr(part.data)
            parts.append(("data", encoded))
        else:
            # Neither text nor data (e.g. a FilePart): include a position+type
            # discriminator. This is only a partial identity — it distinguishes
            # parts by slot and class, NOT by content — so two *different*
            # single FileParts still hash identically ("0:FilePart"). It's
            # strictly better than the old all-empty-payload collision; precise
            # file-content hashing is out of scope until such parts are used.
            parts.append(("part", f"{i}:{type(part).__name__}"))
    return json.dumps(parts)


def _delegation_hash(*parts: str) -> str:
    """SHA-256 of the joined parts with null-byte separators.

    Null bytes prevent "a" + "bc" from colliding with "ab" + "c".
    """
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode())
        h.update(b"\x00")
    return h.hexdigest()
