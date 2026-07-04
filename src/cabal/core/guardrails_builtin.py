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
from collections.abc import Callable
from typing import Any

from cabal.core.a2a.types import Task
from cabal.core.guardrails import GuardrailContext, Verdict


class DoomLoopGuardrail:
    """Detect and block repeated identical delegations within a session.

    Implements opencode's doom-loop detector (cabal#42): count consecutive
    *identical* delegations to the same target within a conversation session;
    at ``threshold`` (default 3) return a BLOCK verdict with a
    CorrectedError-shaped reason message so the caller can change approach.

    **Session key** — ``task.context_id`` when present; falls back to
    ``task.id`` when ``context_id`` is absent. Grouping only works when a
    *stable, shared, non-None* key recurs across the delegations.

    SCOPE / EFFICACY (honest limitation — cabal#59): the counter only trips
    when the same session key recurs. Today that holds for a client re-sending
    a byte-identical message under a shared ``context_id`` (e.g. ``cbl chat``,
    which reuses one context id per session). It does **not** yet hold for the
    autonomous multi-persona loop that motivated #42: the rook fan-out calls
    ``subord.handle_task`` directly (bypassing the router, so router
    ``input_guardrails`` never run) with a fresh sub-task id and no
    ``context_id``; and the A2A inbound routes mint a fresh uuid per hop and
    drop ``context_id`` — so each re-delegation looks like a new session and
    the counter resets. Closing that gap (propagating a stable ``context_id``
    across hops, or running this guardrail at the subordinate/``handle_task``
    boundary) is tracked in #59. This class is a correct building block; wiring
    it to catch autonomous loops depends on #59.

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

    **Counter lifecycle** — on an *identical* delegation the counter
    increments; on a *different* delegation (new target or new payload) the
    counter resets to 1 and the new key is recorded.  Once blocked (count >=
    threshold) subsequent identical calls keep returning BLOCK; a genuinely
    different delegation resets the counter and returns PASS.

    **Dict growth** — the state dict is capped at ``maxsize`` entries (default
    10 000).  When the cap is reached the oldest entry is evicted (Python
    dict insertion-order → FIFO).  Each session has at most one entry
    (one tuple), so memory is O(min(sessions, maxsize)).  There is no TTL;
    long-running daemons with many short-lived sessions will asymptotically
    reach ``maxsize`` and then stay there.  A TTL-based eviction policy is a
    follow-on improvement.

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

    def __init__(self, threshold: int = 3, maxsize: int = 10_000) -> None:
        if threshold < 1:
            raise ValueError(f"threshold must be >= 1, got {threshold}")
        self._threshold = threshold
        self._maxsize = maxsize
        # {session_key: (last_delegation_key, consecutive_count)}
        self._state: dict[str, tuple[str, int]] = {}

    async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
        """Return PASS while consecutive count < threshold; BLOCK at threshold."""
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

        last_key, count = self._state.get(session_key, ("", 0))

        if delegation_key == last_key:
            count += 1
        else:
            # Different delegation — reset counter.
            count = 1

        # Evict oldest to keep dict bounded before inserting a new session.
        if session_key not in self._state and len(self._state) >= self._maxsize:
            self._state.pop(next(iter(self._state)))

        self._state[session_key] = (delegation_key, count)

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
            ``asyncio.run`` or restructure the guardrail.
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

        error = self._validate(tool_name, arguments, result_str)
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
