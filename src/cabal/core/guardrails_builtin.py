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

    1. **Rook fan-out** (cabal#59): ``RookPrimusPersona`` holds a shared
       ``DoomLoopGuardrail`` instance (default-on) and runs ``check()`` at
       the subordinate boundary in ``_dispatch_subordinate`` before calling
       ``subord.handle_task``. Sub-tasks inherit the parent's ``context_id``
       so repeated identical delegations within a session share a stable
       session key and accumulate toward the threshold.

    2. **A2A inbound** (cabal#59): ``MessageSendParams`` now accepts a
       ``contextId`` field; both ``message/send`` and ``message/stream``
       routes propagate it onto the inbound ``Task``. When a client (or
       peer agent) supplies a stable ``contextId`` across hops, the router's
       ``input_guardrails`` can group those hops and detect the loop. Without
       a caller-supplied ``contextId`` each hop still falls back to its
       fresh task uuid, so cross-hop detection requires the client to thread
       the id (``cbl chat`` streaming does this as of cabal#59).

       CAVEAT: this A2A path is only active when a ``DoomLoopGuardrail`` is
       actually wired as a router ``input_guardrail``. The production router
       (``app.py``) currently constructs with none, so the inbound ``contextId``
       feeds nothing until a deployment opts in. The **Rook** path above needs
       no such opt-in — it is default-on.

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

    **Dict growth** — the outer session dict is capped at ``maxsize`` entries
    (default 10 000), oldest-evicted-first (insertion-order FIFO). Each
    session's inner map holds one small int per *distinct* delegation key seen
    in that session (typically a handful). There is no TTL; a long-running
    daemon asymptotically reaches ``maxsize`` sessions and stays there. A
    TTL-based eviction policy is a follow-on improvement.

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
        # {session_key: {delegation_key: cumulative_count}}
        # Per-key *cumulative* counts within a session — NOT a single
        # consecutive slot. Consecutive-only counting misses the interleaved
        # multi-role loop (e.g. Rook's implementer/reviewer fan-out produces
        # impl, rev, impl, rev, … so no two identical keys are ever adjacent);
        # cumulative per-key counting catches the repeated `impl(X)` regardless
        # of what runs between the repeats.
        self._state: dict[str, dict[str, int]] = {}

    async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
        """Return PASS until a delegation key recurs `threshold` times in the
        session; BLOCK at the threshold-th occurrence."""
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

        # Evict oldest session to keep the outer dict bounded before inserting.
        if session_key not in self._state and len(self._state) >= self._maxsize:
            self._state.pop(next(iter(self._state)))

        counts = self._state.setdefault(session_key, {})
        count = counts.get(delegation_key, 0) + 1
        counts[delegation_key] = count

        if count >= self._threshold:
            label = target_name or "unknown"
            return Verdict.block(
                f"{count} identical delegations to {label!r} without progress"
                f" — pausing; change approach"
            )
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
