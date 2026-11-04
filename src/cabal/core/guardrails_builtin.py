"""Built-in concrete guardrails — cabal#42.

Concrete Guardrail implementations that ship with cabal. Import and wire into
DispatchRouter or LLMPersona as needed.

These guardrails are independently unit-testable and are deliberately kept
separate from the Guardrail/Verdict abstraction defined in guardrails.py —
that module defines the protocol; this one provides concrete implementations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any

from cabal.core.a2a.types import Task
from cabal.core.auth.jwt import AGENT_SUB_PREFIX, OPERATOR_SUB_PREFIX
from cabal.core.guardrails import GuardrailContext, Verdict

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cabal.core.budget_store import DelegationBudgetStore

log = logging.getLogger(__name__)


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
       on BOTH dispatch paths since cabal#151 -- ``dispatch_streaming`` runs
       the same ``input_guardrails`` chain, after its structural refusals and
       before any delta is yielded, so a streamed hop is guarded exactly as a
       non-streaming one is. (It still refuses federated targets outright.)
       Before #151 streaming hops were never router-guarded at all, which
       made every bound here opt-out by endpoint. Without a caller-supplied
       ``contextId`` each hop falls back to its fresh task uuid, so cross-hop
       detection on EITHER path requires the client to thread the id -- the
       scoping to "the non-streaming path" here was left over from before
       #151 and is no longer true (``cbl chat`` non-streaming reuses one
       context id per session — see "Original scope").

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


#: The ceiling no identity.toml can raise. A policy value may only LOWER the
#: effective budget (cabal#146). A policy-only limit fails open exactly when
#: the policy is missing or unparsed, which is the state a newly-added or
#: misconfigured persona is in -- so the backstop lives here, in code.
MAX_DELEGATIONS_PER_CONTEXT = 40

#: Task metadata key carrying "<spent> of <limit> used" for the reader that
#: folds it into a prompt (cabal#156). Named rather than repeated, because the
#: stamp and its two readers live in three files.
BUDGET_KEY = "cabal_delegation_budget"

#: Bound on tracked conversations, mirroring DoomLoopGuardrail's outer LRU.
#: Without it a long-lived process leaks one counter per context forever.
_BUDGET_MAXSIZE = 512
#: How long a persisted conversation count is honoured (cabal#174). The
#: in-memory counter needs no equivalent because `_BUDGET_MAXSIZE` is its
#: ceiling; a table has none, so retention stands in for one. A day is well
#: past any conversation that is still resumable, and a restored count only
#: ever tightens the bound -- so erring long is the safe direction.
BUDGET_RETENTION_SECONDS = 86_400.0

#: How long a CALLER-keyed budget takes to refill, in seconds. Applies ONLY to
#: the caller fallback, never to a conversation key -- see the "No TTL" note on
#: DelegationBudgetGuardrail, which is correct for a conversation and wrong for
#: a caller. A conversation ends and its key retires; a caller is stable for the
#: daemon's lifetime, so without a window "no TTL" means "permanently blocked
#: after `limit` un-threaded dispatches", recoverable only by a restart.
CALLER_RATE_WINDOW_SECONDS = 3600


class DelegationBudgetGuardrail:
    """Bound the TOTAL delegations in one conversation, ignoring payload.

    cabal#146. `DoomLoopGuardrail` counts *identical* delegations and is
    defeated by paraphrase: its own BLOCK message tells the model to change
    approach, a language model changes approach by rewording, and rewording
    moves it off the `(target, payload)` key the counter watches. An incident
    produced ~270 asks of materially one question across a dozen wordings,
    each wording counting separately toward a threshold of 3.

    So this counts nothing about the payload and nothing about the target --
    only how many delegations a conversation has made. No amount of rewording
    evades a counter that never looks at the words.

    **No TTL for a CONVERSATION key, deliberately.** DoomLoopGuardrail lazily
    resets a key after `ttl_seconds`, which is right for a sameness counter: a
    repeat every ten minutes is not a runaway. It is wrong for a conversation
    budget. A slow storm is still a storm, and a budget that forgets is not a
    budget. The conversation is its own window -- it ends, and the key retires.

    **A CALLER key does get a window**, because its subject never retires and
    "no TTL" would there mean "blocked until the daemon restarts". See
    `CALLER_RATE_WINDOW_SECONDS` and the key-class comment in `check()`. The
    two classes want two policies: a budget for a conversation, a rate limit
    for a caller.

    **THE VERDICT FOLLOWS THE KEY CLASS (cabal#152).** A CONVERSATION key
    terminates: its counter never decreases and it has no window, so no wording
    and no waiting gets the task through -- there is nothing left for it to do,
    which is what `TERMINATE` means. A CALLER key BLOCKS: it is a rolling rate
    limit, the refusal message names the refill window, and a refusal the caller
    can legitimately wait out must not kill its task.

    **THE DURABLE RECORD DOES NOT ADD A THIRD PATH, and cabal#175 had to be
    narrowed to keep that true.** Persisting the count (cabal#174) introduced a
    retention cutoff on the store, keyed on time since last write -- which
    would have been exactly the time-based reset this paragraph denies, for a
    conversation idle past the cutoff that then survives a restart. So an
    exhausted count is exempt from that prune (`keep_at_or_above`), and the
    table is bounded by a recency cap of the same shape as the LRU below rather
    than by time. A spent conversation stays spent across a restart however
    long it sat; only partial counts lapse.

    **"NEVER REFILLS" IS TOO STRONG FOR THE CONVERSATION KEY, and an earlier
    version of this paragraph said it (#152 r1 Important 3).** The LRU is a
    second refill path: `_BUDGET_MAXSIZE` is 512, and a conversation that has
    gone quiet -- as a terminated one has, by construction -- is an eviction
    candidate. Measured at limit=1 with the clock frozen so no window could be
    blamed: terminate after exhaustion, PASS again once enough other
    conversations had passed through (the probe used 600, comfortably past the
    512-key cap -- two numbers for one fact, so read 512 as the threshold and
    600 as the probe that cleared it). `test_a_conversation_budget_does_not_refill` is honest about
    this, because it says no amount of elapsed TIME; eviction is a different
    mechanism and nothing tests it. The paragraph ten lines above knew --
    "a conversation is its own window: it ends, its key goes cold, the LRU
    reclaims it" -- and this one overreached past it.

    IT DOES NOT CHANGE THE VERDICT, which is the part worth being precise
    about since this is the justification the split rests on. The budget is
    genuinely spent at the moment the refusal fires, and nothing the agent can
    do inside that task will unspend it: the eviction needs 512 OTHER
    conversations to pass through the guard, which is not an affordance the
    refused agent has. So permanence is not a property of the conversation key
    either -- it is a much longer-lived one, and long-lived enough that
    terminating is the right answer while "never" is the wrong word.

    This docstring previously argued **BLOCK is enough** for both, and that was
    right until `TERMINATE` existed. What is NOT right -- and is what the #152
    pre-pass caught -- is reading "the counter only rises" as a property of the
    guard. It is a property of one of its two key classes.

    **A delegation to an unreachable colleague DOES consume budget, and that
    is deliberate.** The counter increments in `check()`, before the hop, and
    there is no rollback -- so a failed dispatch costs the same as a successful
    one. That is the behaviour the incident requires: the agents in it stormed
    a colleague scaled to zero replicas, so if unreachable targets were free
    the exact case this guard exists for would be unbounded. An agent that
    chose to ask again spent an ask, whether or not anyone was listening.

    What genuinely does NOT consume budget is a retry *inside* one dispatch:
    `check()` runs once per `dispatch()` call, not once per connection attempt,
    so transport-level retries beneath the router never re-enter it. Those two
    were conflated in an earlier draft of this docstring, which claimed "one
    unreachable colleague cannot eat the budget" -- backwards, and describing
    behaviour that would reopen the hole.

    MUST REMAIN AWAIT-FREE, for the same reason DoomLoopGuardrail must: a
    primus fan-out (cabal#69) calls this concurrently across legs sharing one
    instance, and an await-free `async def` runs atomically to completion on a
    cooperative event loop. Any `await` here reintroduces a data race on the
    counter -- guard the state, do not await inside it.
    """

    def __init__(
        self,
        limit: int = MAX_DELEGATIONS_PER_CONTEXT,
        caller_window_seconds: float = CALLER_RATE_WINDOW_SECONDS,
        *,
        store: DelegationBudgetStore | None = None,
        retention_s: float = BUDGET_RETENTION_SECONDS,
    ) -> None:
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        # A zero or negative window expires before every check, pinning the
        # counter at 1 and silently disabling the caller-keyed budget (#159).
        # Raised here as well as in the config model, matching `limit`: the
        # guard is constructible directly, so the config validator is not the
        # only door.
        if caller_window_seconds <= 0:
            raise ValueError(
                f"caller_window_seconds must be > 0, got {caller_window_seconds}"
            )
        # A policy may only tighten. Clamping rather than raising is
        # deliberate: a deployment asking for more than the backstop should
        # still boot, bounded, rather than refuse to start.
        self._limit = min(limit, MAX_DELEGATIONS_PER_CONTEXT)
        #: key -> (count, window_start). window_start is only consulted for a
        #: caller-keyed entry; a conversation entry carries it and ignores it.
        self._counts: dict[str, tuple[int, float]] = {}
        self._maxsize = _BUDGET_MAXSIZE
        self._caller_window = caller_window_seconds
        if retention_s <= 0:
            raise ValueError(f"retention_s must be > 0, got {retention_s}")
        # DEFAULTS TO A NO-OP, so nothing about an install without a database
        # changes: bounded in memory, re-armed by a restart, exactly as before
        # cabal#174. A store is what makes the bound survive the restart.
        if store is None:
            from cabal.core.budget_store import NoOpDelegationBudgetStore

            store = NoOpDelegationBudgetStore()
        self._store = store
        self._retention_s = retention_s
        #: Strong refs to in-flight write-behind tasks. Without this the event
        #: loop is the only owner and a task can be garbage-collected mid-flight
        #: (documented asyncio behaviour), which would drop increments silently
        #: -- the exact failure mode cabal#174 is about.
        self._writes: set[asyncio.Task[None]] = set()

    async def hydrate(self) -> int:
        """Restore conversation counts from the store. Returns how many keys
        were loaded.

        CALLED ONCE AT STARTUP, BEFORE SERVING, and that placement is the whole
        design: `check()` must stay await-free (see its docstring), so the hot
        path can never read the store. Hydration is the only read.

        BOUNDED BY THE SAME LRU CEILING the in-memory counter uses. Loading
        more keys than `_BUDGET_MAXSIZE` would evict what it just restored, so
        the store is asked for at most that many and returns them oldest-first
        -- leaving the most recently active conversation most-recently-used,
        which is the one an immediate resume is most likely to touch.

        NEVER BLOCKS STARTUP. A store that raises here leaves the guard in its
        pre-cabal#174 state -- bounded in memory -- which is a worse bound but
        a working one, and strictly better than refusing to boot. The log line
        is the signal that durability is off.
        """
        try:
            rows = await self._store.load_recent(
                limit=self._maxsize,
                retention_s=self._retention_s,
                # PASSING THE LIMIT IS WHAT KEEPS "does not refill" TRUE
                # (cabal#175 concordance). It exempts an exhausted count from
                # the store's idle-keyed time prune, so a conversation that
                # spent its budget stays spent across a restart however long it
                # sat. Omit it and retention becomes a second time-based refill
                # path on a key this class's docstring says has none.
                keep_at_or_above=self._limit,
            )
        except Exception:
            log.exception("cabal.delegation_budget.hydrate_failed")
            return 0
        now = time.monotonic()
        loaded = 0
        for session_key, count in rows:
            if count < 1:
                continue
            # A RESTORED CONVERSATION KEY CARRIES A FRESH window_start, and that
            # is correct rather than sloppy: the field is only consulted for a
            # caller-keyed entry, and caller keys are never persisted because
            # `time.monotonic` resets across the restart this method exists for.
            self._counts[session_key] = (count, now)
            loaded += 1
        return loaded

    def _persist(self, session_key: str, count: int) -> None:
        """Schedule a write-behind for one conversation key.

        NOT AWAITED, DELIBERATELY. `asyncio.create_task` is not a yield point,
        so `check()` still runs to completion atomically on a cooperative loop
        -- the invariant its docstring calls load-bearing. Awaiting the write
        here would reintroduce the data race the whole method is arranged to
        avoid.

        `def`, NOT `async def`, AND THAT IS THE ENFORCEMENT. Found by mutation:
        an attempt to `await` the store from here is a SyntaxError, so the
        await-free property is checked by the compiler rather than by a test
        that someone has to keep running. Making this `async def` is therefore
        the change to refuse in review -- it moves a structural guarantee back
        into the category of things that can regress quietly.
        """
        try:
            task = asyncio.create_task(self._store.bump(session_key, count))
        except RuntimeError:
            # No running loop. Reachable from a synchronous test calling
            # check() via asyncio.run on a loop that has already closed, and
            # from any future non-loop caller. Durability is lost for this
            # increment; the in-memory bound is not.
            log.debug("cabal.delegation_budget.no_loop_for_write_behind")
            return
        self._writes.add(task)
        task.add_done_callback(self._on_write_done)

    def _on_write_done(self, task: asyncio.Task[None]) -> None:
        """Discard the reference AND retrieve the exception (#175 r1 nit 5).

        `set.discard` alone was the only callback, and it never calls
        `task.exception()` -- so a write-behind that raised surfaced only as
        asyncio's GC-time "Task exception was never retrieved", on the asyncio
        logger, with no key or task context. For a change whose whole subject
        is durability, "hydrate succeeded at boot and every write since has
        failed" is the state most worth naming and was the one that read
        quietest. `hydrate()` already logs structurally; this matches it.
        """
        self._writes.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(
                "cabal.delegation_budget.write_failed: %s: %s",
                type(exc).__name__,
                exc,
            )

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def caller_window(self) -> float:
        """Seconds a caller-keyed budget window lasts before it refills."""
        return self._caller_window

    def usage(self, session_key: str) -> tuple[int, int]:
        """(spent, limit) for a key. cabal#156.

        AN AGENT THAT KNOWS IT HAS THREE DELEGATIONS LEFT CAN PLAN; AN AGENT
        THAT DISCOVERS A WALL CAN ONLY RETRY. That is the incident in one
        sentence -- it hit a limit it could not see, concluded nothing, and
        asked again roughly 270 times. The BLOCK message tells an agent the
        budget is EXHAUSTED; nothing told it the budget EXISTED while it still
        had room to spend deliberately.

        TEST-FACING TODAY, and labelled rather than quietly load-bearing
        (#156 r2 Nit). Nothing in `src/` calls this; the delivery path that
        works is the metadata stamp below plus the reader in
        `LLMPersona._delegation_budget_note`. It stays because a
        non-incrementing read is the right shape for the question "how much is
        left" and inventing it later under pressure would be worse -- but the
        feature's thesis rests on the stamp, not on this.
        """
        entry = self._counts.get(session_key)
        return ((entry[0] if entry else 0), self._limit)

    async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
        """PASS until the budget is exhausted, then refuse every time.

        TERMINATE on a conversation key, BLOCK on a caller key -- see the class
        docstring for why the verdict follows the key class.
        """
        if not isinstance(target, Task):
            return Verdict.pass_()

        task: Task = target
        # THE FALLBACK IS THE CALLER, NOT THE TASK, AND THAT IS THE WHOLE
        # POINT OF THIS GUARD (#150 review, Critical).
        #
        # context_id is preferred: it is stable across hops, so the hops of one
        # conversation share a budget. That is the per-conversation budget this
        # guard is named for.
        #
        # AND THE PRIMUS FAN-OUT, since #158 -- which is the reverse of what
        # this comment said through two prior corrections, so read the history
        # rather than trusting either version blind.
        #
        # It ONCE claimed a fan-out shared one budget with its children (wrong:
        # nothing connected them). It was then corrected to say subordinate legs
        # "never reach this check at all" and the two enforcement points are
        # "disjoint" -- true at the time, because this guard was wired only into
        # build_router_input_guardrails and primus._dispatch_subordinate calls
        # handle_task directly. #158 made it false by giving the primus THE SAME
        # INSTANCE (persona_factory asks the router for it), so legs now reach
        # this check on every dispatch and the two points are one object.
        #
        # The keying is what makes that safe WHEN THERE IS A context_id: a
        # sub-task inherits its parent's, so both directions land on the same
        # counter rather than on two allowances for one conversation.
        #
        # UN-THREADED, THEY DO NOT (#158 r3). The two boundaries pick different
        # caller keys -- the router keys on the CALLER
        # (ctx.caller_persona or ctx.caller_agent), the primus on ITSELF
        # (persona_name=self.name) -- so one un-threaded conversation gets two
        # buckets, which is the shape this sharing exists to prevent. Worse on
        # the primus side: its key is the persona name, so every operator and
        # every unrelated un-threaded conversation shares one allowance, where
        # the router's fallback at least distinguishes callers.
        #
        # NOT A REGRESSION, and that is why it is filed rather than fixed here:
        # before #158 those legs hit this budget zero times, so un-threaded
        # fan-out went from unbounded to bounded-but-coarsely. `cbl dispatch`
        # is the live un-threaded path; `cbl chat` threads a contextId and gets
        # the design as advertised.
        #
        # But context_id is CALLER-SUPPLIED AND OPTIONAL (a2a/jsonrpc.py:47
        # defaults it to None) and a task id is FRESH PER DISPATCH wherever one
        # is minted -- freshness is the load-bearing property here, not which
        # generator produced it. So keying the fallback on task.id -- what
        # DoomLoopGuardrail does -- gives an un-threaded caller a NEW key on
        # every single dispatch, a count of 1 forever, and no bound at all.
        # Measured on the incident's own volume: 270 un-threaded delegations
        # at limit=20 produced 270 counters and zero blocks. DoomLoopGuardrail
        # has carried that hole since cabal#59 and is honest about it; an
        # earlier version of THIS docstring claimed the fallback was "weaker
        # than intended, never absent", which was exactly backwards.
        #
        # Falling back to the CALLER bounds that traffic. It is deliberately
        # coarser than per-conversation: one busy caller shares a budget across
        # its unrelated conversations.
        #
        # TWO KEY CLASSES, TWO POLICIES, and the class is what decides whether
        # the budget ever refills (#150 review r2, Critical).
        #
        # A conversation is its own window: it ends, its key goes cold, the LRU
        # reclaims it. "No TTL" is correct there and stays.
        #
        # A CALLER NEVER RETIRES -- persona_name is stable for the daemon's
        # lifetime -- so on this path "no TTL" does not mean "a slow storm is
        # still a storm", it means PERMANENT. Measured: an operator is refused
        # at un-threaded dispatch 21 and stays refused across 5000 unrelated
        # conversations, because the increment reinserts at the most-recent end
        # BEFORE the limit check, so a retried key can never become the LRU's
        # eviction candidate. Retrying is what makes it permanent. Only a
        # restart cleared it.
        #
        # So a caller key gets a refill window and a conversation key does not.
        # NOT "skip the increment while blocked", which looks cheaper and would
        # restore eviction as the recovery path: it regresses the no-refund
        # property built in 9b22fa4 and breaks both guards that pin it.
        if task.context_id:
            session_key, windowed = task.context_id, False
        elif ctx.persona_name:
            session_key, windowed = ctx.persona_name, True
        else:
            # UN-KEYABLE, SO FAIL OPEN HONESTLY AND IN ONE PLACE. The old tail
            # here was `or task.id`, which could never bound anything -- a task
            # id is fresh per dispatch, so it minted one counter per call and
            # blocked nothing. Its only reachable effect was to disguise this
            # same fail-open as a working bound, which was r1's Critical.
            return Verdict.pass_()

        # `time.monotonic()` is not an await, so the atomicity invariant above
        # holds: this method still runs to completion on a cooperative loop.
        now = time.monotonic()
        entry = self._counts.pop(session_key, None)
        if entry is None:
            count, window_start = 0, now
        else:
            count, window_start = entry
            if windowed and (now - window_start) >= self._caller_window:
                count, window_start = 0, now

        # Outer LRU, matching DoomLoopGuardrail: pop-then-reinsert marks this
        # conversation most-recent; a new one evicts the least-recent at cap.
        count += 1
        if len(self._counts) >= self._maxsize:
            self._counts.pop(next(iter(self._counts)))
        self._counts[session_key] = (count, window_start)

        # DURABLE FOR A CONVERSATION KEY ONLY (cabal#174). A restart used to
        # re-arm this budget, which is the path cabal#146's incident actually
        # took -- liveness-killed, restarted, resumed from persisted task state
        # against a counter that did not persist. Write-behind rather than
        # write-through because this method must not await; see `_persist`.
        #
        # `not windowed` IS THE WHOLE CONDITION, and it is about the clock, not
        # about importance. A caller-keyed entry's `window_start` sits on
        # `time.monotonic`, which resets on restart, so a restored caller count
        # would be measured against a window whose origin is gone. Losing one
        # costs at most a single extra window of a rolling rate limit -- the
        # failure mode it already has by design.
        #
        # BEFORE THE LIMIT CHECK BELOW, so a refused ask is recorded as spent.
        # That is the no-refund property 9b22fa4 built and two guards pin; a
        # write placed after an early return would quietly reintroduce refunds
        # for exactly the calls a storm consists of.
        if not windowed:
            self._persist(session_key, count)

        # RIDE BACK IN THE REPLY (cabal#156). The cheapest of the three places
        # the issue considers -- a system prompt is stale the moment anything
        # is spent, and per-turn injection needs a path that does not exist --
        # and the most accurate, because it is written at the moment of
        # spending rather than recalled later.
        #
        # AN AGENT THAT KNOWS IT HAS THREE DELEGATIONS LEFT CAN PLAN; AN AGENT
        # THAT DISCOVERS A WALL CAN ONLY RETRY. The BLOCK message tells it the
        # budget is EXHAUSTED; until now nothing told it the budget EXISTED.
        #
        # THE STAMP IS HALF THE PATH, AND THE HALF THAT IS EASY TO MISTAKE FOR
        # ALL OF IT. Writing it here puts the number in Task.metadata and on the
        # wire, where an external A2A client can read it -- but cabal's own
        # personas do not read metadata into a prompt, so on its own this
        # delivers the sentence above to nobody we ship. The reader is
        # LLMPersona._delegation_budget_note, which folds it into system
        # content; the pre-pass that caught the gap is why it exists. Deleting
        # either end silently restores "stops at the wire".
        task.metadata[BUDGET_KEY] = f"{count} of {self._limit} used"

        if count > self._limit:
            # THE TAIL IS DIFFERENT PER KEY CLASS, and the conversation one was
            # missing (#158 r3 Nit). The windowed branch tells the caller when
            # it refills; the conversation branch ended by telling an AGENT to
            # stop re-asking, which is the right advice for the reader it was
            # written for and no help at all to the human whose `cbl chat`
            # session has just hit a wall that does not refill. This PR is what
            # makes operators meet that message.
            # "BY THIS CALLER" ON THE WINDOWED BRANCH (cabal#161). A caller
            # key is shared across that caller's unrelated conversations, so
            # "in this conversation" told the reader a fresh conversation
            # would clear it -- false, and at odds with the refill clause.
            scope = "by this caller" if windowed else "in this conversation"
            refill = (
                f" This budget refills {self._caller_window:.0f}s after its first "
                "delegation; thread a contextId (`cbl dispatch --context`) to "
                "get a per-conversation budget instead."
                if windowed
                else (
                    " This budget belongs to the conversation and does not "
                    "refill; a new conversation starts a fresh one."
                )
            )
            reason = (
                f"Delegation budget exhausted: {self._limit} delegations "
                f"already made {scope} (limit {self._limit}). Every "
                "delegation counts regardless of wording, so rephrasing will "
                "not get through. If you cannot answer from what you have, "
                f"say so and stop rather than asking again.{refill}"
            )
            # THE VERDICT FOLLOWS THE KEY CLASS, and `windowed` is the whole
            # discriminator. A windowed refusal has just told the caller when
            # its budget refills; terminating the task after saying so would
            # contradict the sentence above in the same breath.
            return Verdict.block(reason) if windowed else Verdict.terminate(reason)
        return Verdict.pass_()


class DelegationEdgeGuardrail:
    """Delegation is permitted where an edge was declared, and nowhere else (cabal#154).

    The colleague list is a phone book: anything visible is askable. This makes
    it an adjacency list. Verified on main before this guard existed: a grep for
    ``authoriz|permit|may_ask|can_ask|allowed_targets|visib|edge`` across
    ``core/broker/`` returned exactly one hit, the OUTBOUND ``Authorization``
    header -- there was no caller-to-target authorization at dispatch at all.

    **WHY THIS OUTRANKS THE CAPS.** #144 bounds depth, #146 bounds volume, #151
    makes those bounds unbypassable. All three ration an affordance. This
    removes it. In the incident behind #146 the two looping agents had no
    business asking each other the question they asked ~270 times; no edge
    would have existed, and the cap stops the 271st ask where an edge stops the
    first. The caps remain the backstop for a *permitted* edge over-used.

    **EDGES ARE OPERATOR-AUTHORED, NEVER AGENT-SUPPLIED.** That is the property
    that makes this an authorization control rather than a suggestion. They are
    read from ``identity.toml`` at startup and this guard holds an immutable
    snapshot; nothing on the dispatch path can add one.

    **NO DECLARED EDGES MEANS NO DELEGATION**, which is the load-bearing half of
    the borrowed pattern -- a room with no relationships permits none rather
    than permitting any-to-any. Because that default is breaking for every room
    that exists today, the guard as a whole is OPT-IN for now
    (``[guardrails] delegation_edges``, default false). The default-deny is
    inside the guard where it belongs; the migration is outside it.

    **THE OPERATOR EXEMPTION IS POSITIVE, AND THAT CORRECTS AN EARLIER SHAPE
    OF THIS GUARD THAT WAS WRONG.** An operator is exempt -- edges govern
    agent-to-agent delegation and a human at the CLI is not delegating -- but
    the exemption is granted only on a token whose ``sub`` carries
    ``OPERATOR_SUB_PREFIX``. Anything else that is not an identified persona is
    REFUSED.

    The first version inferred the exemption from an ABSENCE: no
    ``cabal_persona`` claim meant operator, justified by ``routes/auth.py``
    being "the only mint site producing a claim-less token". The pre-pass found
    a second one -- ``admin.py``'s ``mint_service_token`` takes an optional
    ``persona`` defaulting to None -- so a service token was claim-less too and
    would have been waved through. That is the third enumeration I got wrong on
    this codebase in a day, and the lesson is not "enumerate harder": a
    default-deny control must not depend on a list of exceptions being
    complete. It now depends on a signal being PRESENT, so a mint site nobody
    thought of fails closed instead of open.

    KNOWN WEAKNESS, and it is the sub prefix rather than the scope. The
    system's own definition of an operator is ``"operator" in cabal_scope``
    (``admin.py:_require_operator_claims``), which is the signal this SHOULD
    use -- but ``routes/tasks.py`` re-mints the dispatch token with
    ``scope=["dispatch"]``, so operator scope does not survive to the router
    and the prefix is all that reaches here. Tracked separately. mint-service
    no longer mints an ``operator:`` sub (#403), so only bootstrap and
    /auth/renew produce one.

    **AN ``agent:<name>`` SUB IS A CALLER IN ITS OWN RIGHT (cabal#189).** Room
    colleagues and outside agents normally carry no ``cabal_persona`` -- the
    room service mints colleague passes without one, and so does the
    documented outsider admission -- and before this, once edges were on,
    every such caller was refused. Their edges are declared
    against the FULL sub in ``[guardrails] caller_edges`` and looked up here by
    the full sub, so ``agent:archivist`` never inherits the persona
    ``archivist``'s edges. A token that carries a persona claim is still judged
    as that persona, whatever its sub.

    **AN UNIDENTIFIABLE TARGET IS REFUSED, NOT WAVED THROUGH.** A default-deny
    control that fails open on a missing field is not default-deny.

    Borrowed from sympozium-ai/sympozium (MIT, Go), ``resolvePersonaTarget``,
    whose threat model states the case: both the pack name and the target are
    agent-supplied and therefore adversarial, so an ensemble with no
    relationships must permit no delegation rather than allowing any-to-any.
    """

    def __init__(
        self,
        edges: Mapping[str, Iterable[str]],
        caller_edges: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        #: caller persona -> the AGENT names it may dispatch to. Agent names,
        #: not persona names: identity_sync gives a role="primus" persona the
        #: agent name "<persona>-primus", while an edge is declared against the
        #: persona. Resolving that once here beats stripping a suffix at check
        #: time, which would silently authorize a persona literally named
        #: "x-primus" as though it were the primus of "x".
        self._edges: dict[str, frozenset[str]] = {
            caller: frozenset(targets) for caller, targets in edges.items()
        }
        #: ``agent:<name>`` sub -> the agent names it may dispatch to. Kept apart
        #: from ``_edges`` so a sub and a persona name can never collide.
        self._caller_edges: dict[str, frozenset[str]] = {
            sub: frozenset(targets) for sub, targets in (caller_edges or {}).items()
        }

    def edges_for(self, caller_persona: str) -> frozenset[str]:
        return self._edges.get(caller_persona, frozenset())

    async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
        """PASS where an edge was declared; BLOCK otherwise.

        MUST REMAIN AWAIT-FREE for the same reason the other two must -- a
        primus fan-out calls guardrails concurrently across legs sharing one
        instance, and an await-free ``async def`` runs atomically to completion
        on a cooperative event loop.
        """
        if not isinstance(target, Task):
            return Verdict.pass_()

        caller = ctx.caller_persona
        edges = self._edges
        remedy = "adds '{target}' to may_delegate_to under [personas.{caller}]"
        if caller is None:
            # NOT AUTOMATICALLY THE OPERATOR -- see the class docstring. Grant
            # the exemption only on a positive signal, and refuse otherwise, so
            # that a token shape nobody enumerated fails CLOSED.
            sub = ctx.caller_agent or ""
            if sub.startswith(OPERATOR_SUB_PREFIX):
                return Verdict.pass_()
            if sub.startswith(AGENT_SUB_PREFIX) and sub != AGENT_SUB_PREFIX:
                caller, edges = sub, self._caller_edges
                remedy = "adds '{target}' to \"{caller}\" under [guardrails.caller_edges]"
        if caller is None:
            return Verdict.terminate(
                reason=(
                    "Delegation refused: the caller is neither an identified "
                    f"persona nor the operator (sub={sub!r}). Delegation edges "
                    "are checked against a caller persona, so a token carrying "
                    "neither a cabal_persona claim nor an agent:<name> sub "
                    "cannot be authorized. A service token that needs to "
                    "delegate should be minted with the persona it acts as."
                )
            )

        task: Task = target
        target_agent = str(task.metadata.get("cabal_target_agent", "") or "")
        if not target_agent:
            return Verdict.terminate(
                reason=(
                    "Delegation refused: the target could not be identified, so "
                    "no edge could be checked. This is a refusal rather than a "
                    "pass because an authorization control that fails open on a "
                    "missing field is not one."
                )
            )

        if target_agent in edges.get(caller, frozenset()):
            return Verdict.pass_()

        declared = sorted(edges.get(caller, frozenset()))
        may = ", ".join(declared) if declared else "nothing"
        return Verdict.terminate(
            reason=(
                f"Delegation refused: no declared edge from '{caller}' to "
                f"'{target_agent}'. {caller} may delegate to: {may}. Edges are "
                "operator-authored config, so this will not change by retrying "
                "or by rewording. If this delegation should be permitted, the "
                "operator " + remedy.format(target=target_agent, caller=caller)
                + " in identity.toml."
            )
        )


#: The deepest a delegation chain may run (cabal#144).
#:
#: A HARD BACKSTOP IN CODE, and the two-layer shape is the load-bearing part:
#: a policy-only limit fails open whenever the policy is missing or unparsed,
#: which is exactly the state a misconfigured or newly-added persona is in.
#: A per-persona value may LOWER this and never raise it.
#:
#: WHY DEPTH RATHER THAN SPEND. cabal's core/llm/budget.py and
#: circuit_breaker.py bound LLM CALLS and know nothing about the delegation
#: graph, so a cycle that reaches them is refused late, by exhaustion --
#: indistinguishable from legitimate expensive work, and it takes the whole
#: Task down. A depth refusal is immediate, local, and names the problem.
#:
#: A CYCLE IS NOT UNREFUSED WITHOUT THIS, and an earlier version of this
#: comment said it was. DelegationBudgetGuardrail (#146) already bounds the
#: graph by COUNT rather than by spend, default-on in the same builder, so a
#: runaway chain stops at MAX_DELEGATIONS_PER_CONTEXT however it is worded.
#: What depth adds is precision and earliness: 40 hops of real work before a
#: refusal is a bound, but it is not a useful one, and the budget cannot tell
#: a deep chain from a wide conversation.
#:
#: DEPTH IS STRUCTURAL, so paraphrase cannot evade it -- unlike the sameness
#: counter it sits beside. Borrowed from sympozium's spawn_router.go:32,
#: `const maxDelegationDepth = 8`, an unconditional backstop with a separate
#: policy that can only tighten.
MAX_DELEGATION_DEPTH = 8

#: Task metadata key carrying how many delegation hops led here.
DEPTH_KEY = "cabal_delegation_depth"


def delegation_depth(task: Task) -> int:
    """Hops that led to this task. 0 for one a human started."""
    raw = task.metadata.get(DEPTH_KEY, 0)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError, OverflowError):
        # An unreadable depth is treated as the TOP of the chain rather than
        # as an error, deliberately: a malformed value must not be a way to
        # claim a lower depth than you have. Anything that then delegates
        # onward is stamped 1 and bounded normally from there.
        #
        # OverflowError IS NOT REDUNDANT (#144 pre-pass). `int(float("inf"))`
        # raises it rather than ValueError, and an infinity reaches here: the
        # Task model keeps the value unvalidated, and Python's own json module
        # accepts a bare `Infinity` token. Without this arm the one shape most
        # obviously meant as an attack is the one that crashes the dispatch --
        # in a function whose entire purpose is that garbage costs a hop of
        # slack instead. (`nan` lands on ValueError and always did.)
        return 0


def next_delegation_depth(task: Task) -> int:
    """The depth to stamp on a task delegated FROM `task`."""
    return delegation_depth(task) + 1


class DelegationDepthGuardrail:
    """Refuse a delegation that runs deeper than the chain is allowed (#144).

    WHERE THIS RUNS (#144 pre-pass, corrected by #171). It is wired into
    ``build_router_input_guardrails``, so it runs on every hop that crosses
    ``DispatchRouter`` -- AND, since cabal#171, on the primus's own fan-out:
    ``_dispatch_subordinate`` checks the ROUTER'S instance at the subordinate
    boundary, handed to it by ``persona_factory`` through
    ``input_guardrail_of``. One instance, one bound, no second counter to
    drift.

    THE COUNTER AND THE CHECK USED NOT TO MEET, and this paragraph is kept
    because the shape is worth recognising again. ``next_delegation_depth`` has
    exactly one call site -- the sub_task built in
    ``primus._dispatch_subordinate`` -- and that task goes straight to
    ``subord.handle_task``, which the router never sees. Before #171 the
    increment was therefore WRITE-ONLY: nothing read a fan-out-stamped depth at
    a decision point, so cabal's own delegation could not accumulate depth and
    a cabal-to-cabal A-to-B-to-A ping-pong carried a constant value for as many
    hops as it liked. The limit was unreachable by accumulation. It is now read
    at the boundary that stamps it, and
    ``test_the_depth_the_guard_sees_is_the_depth_the_fanout_stamped`` pins that
    the two numbers are one number.

    WHAT ARRIVES FROM OUTSIDE IS STILL ONLY WHAT A CALLER PUT ON THE WIRE. The
    routes keep a caller's ``cabal_delegation_depth`` (core/a2a/inbound.py drops the
    other ``cabal_*`` keys, #401), and a federated
    hop forwards ``task.model_dump(...)`` whole WITHOUT incrementing -- so an
    EXTERNAL chain accumulates depth only if the client increments, which is
    what the companion daedalus#40 does. That half is unchanged by #171, which
    bounds the delegation cabal ORIGINATES, not the depth a peer declares.

    SO ``delegation_depth = true`` WITH A LIMIT OF 8 NOW MEANS BOTH: an honest
    bound on a depth an external chain declares, refused immediately and
    locally rather than by spend, and a bound on cabal's own fan-out. Until
    #171 it read to an operator as the second and was only the first.

    MUST REMAIN AWAIT-FREE, for the same reason its two siblings must: a
    primus fan-out calls guardrails concurrently across legs sharing one
    instance, and an await-free ``async def`` runs atomically to completion on
    a cooperative event loop.
    """

    def __init__(self, max_depth: int = MAX_DELEGATION_DEPTH) -> None:
        # CLAMPED, NOT HONOURED -- a policy may only lower the backstop.
        #
        # NOT THE SAME SHAPE AS THE BUDGET'S limit, which an earlier version of
        # this comment claimed (#144 pre-pass). The budget RAISES below 1, in
        # the guard and at config load; this clamps up to 1 silently. That is
        # defensible -- a depth of 1 is a working bound where a budget of 0 is
        # one nothing can satisfy -- but it is two shapes, not one.
        #
        # IT IS THE SHAPE OF THE PRIMUS'S max_legs: `max(1, min(max_legs,
        # MAX_LEGS_PER_FANOUT))` in agents/primus.py, which #156 added and
        # `max_legs_override` (#178) made settable from config. This comment
        # was written in the future tense before that symbol existed (#144 r1
        # Nit) and outlived the condition by several PRs (#182).
        #
        # `delegation_depth_limit` does now get a config validator, so a typo
        # in identity.toml is caught at load on every axis even where the
        # guards themselves differ.
        self._max_depth = max(1, min(max_depth, MAX_DELEGATION_DEPTH))

    @property
    def max_depth(self) -> int:
        return self._max_depth

    async def check(self, target: Any, ctx: GuardrailContext) -> Verdict:
        if not isinstance(target, Task):
            return Verdict.pass_()
        depth = delegation_depth(target)
        if depth < self._max_depth:
            return Verdict.pass_()
        return Verdict.block(
            reason=(
                f"Delegation depth {depth} has reached the limit of "
                f"{self._max_depth}. This chain is already "
                f"{depth} hops from the request that started it; going deeper "
                "is refused. Depth is counted structurally, so rewording will "
                "not get through. Answer from what you have, or say you "
                "cannot."
            )
        )


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
