"""PrimusPersona — shared PLAN→IMPLEMENT→(REVIEW)→AGGREGATE base.

All primus engineering personas (Mason, Wright, …) inherit from here.
The fan-out machinery — planning, subordinate dispatch, aggregation,
doom-loop guard, hook firing — lives entirely in this module.

Lifecycle per dispatch:
    1. PLAN. The primus makes one LLM call to produce a precise instruction
       for the leg(s). Output: a short planning text.
    2. FAN-OUT. Every non-reviewer subordinate is a *leg*, dispatched
       CONCURRENTLY on the plan (asyncio.gather over independent sub-tasks —
       unique ids, so per-leg checkpointing / abort / doom-loop stay isolated;
       cabal#69). A single-leg primus (just 'implementer') is behaviour-
       identical to the old sequential path. Fire subordinate_dispatched
       BEFORE and subordinate_returned AFTER each leg. If a reviewer is
       present, it runs AFTER the legs join, reviewing all their outputs.
    3. SYNTHESIS (AGGREGATE). The primus makes one LLM call joining the leg
       outputs (+ reviewer critique) into a final response for the operator.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from dataclasses import dataclass, field

import structlog

from cabal.agents.llm_persona import LLMPersona, _message_to_chat
from cabal.agents.subordinate import Subordinate
from cabal.core.a2a.codes import (
    CODE_BUDGET_EXHAUSTED,
    CODE_ESCALATION,
    CODE_POLICY_VIOLATION,
    CabalReason,
    annotate,
)
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.abort import KIND_SUBORDINATE
from cabal.core.guardrails import GuardrailContext, VerdictType
from cabal.core.guardrails_builtin import (
    BUDGET_KEY,
    DEPTH_KEY,
    DelegationBudgetGuardrail,
    DelegationDepthGuardrail,
    DoomLoopGuardrail,
    next_delegation_depth,
)
from cabal.core.hooks.registry import HookName, HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient, LLMTruncatedError

log = structlog.get_logger(__name__)

# Sentinel: distinguishes "caller passed doom_loop=None (disabled)" from
# "caller omitted doom_loop (use default DoomLoopGuardrail())".
_DOOM_LOOP_DEFAULT: object = object()


class _LegFailedError(Exception):
    """A fan-out leg came back as a failed Task.

    Carries the leg's OWN reason code so the primus fails with what actually
    happened rather than flattening every subordinate failure into one code.
    """

    def __init__(self, *, role: str, code: int, message: str) -> None:
        super().__init__(f"{role} subordinate failed: {message}")
        self.role = role
        self.code = code
        self.message = message


class _DoomLoopError(Exception):
    """Internal signal: A ROUTER-LEVEL GUARD refused a subordinate dispatch.

    Named for the doom-loop guard because it was the only one at this boundary
    until #158 added the delegation budget. Kept rather than renamed: the name
    is internal, and the user-visible message carries the refusing guard's own
    reason, so an operator reads "budget exhausted" rather than "doom loop".

    Raised inside _dispatch_subordinate and caught in handle_task so the
    primus can return an annotated failed Task to its own caller.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason


#: The most subordinate legs one fan-out may open at once (cabal#156).
#:
#: A HARD CEILING IN CODE, and TODAY IT IS THE ONLY CEILING. There is no
#: identity-config field for breadth: `max_legs` reaches a primus only through
#: its constructor, and persona_factory passes none, so `_max_legs` is this
#: constant in every shipped deployment and the only callers that can lower it
#: are tests.
#:
#: SO DO NOT READ THE "CLAMPED, NOT HONOURED" STORY ONTO IT (#156 r2). That
#: phrase is meaningful about MAX_DELEGATIONS_PER_CONTEXT because
#: `delegation_budget_limit` IS operator-settable and reaches the guard, so
#: there is something to clamp FROM. Here there is nothing, and three comments
#: claimed a policy could lower this when no policy path exists. Wiring one is
#: cabal#172; until it lands, this is a constructor seam for tests with the
#: ceiling fixed in code.
#:
#: BREADTH IS ITS OWN AXIS and neither existing bound covers it. #144 bounds
#: how DEEP a chain runs; #146 bounds how MANY delegations a conversation makes
#: in total. Forty concurrent legs sit inside a depth bound of 8 and inside a
#: total budget of 40 -- by spending the whole budget in one burst. Concurrency
#: has costs total-volume cannot express: connections, tokens, and the liveness
#: starvation that killed the container in the incident behind #146.
#:
#: Sympozium checks the same thing at spawn_router.go:422, with a policy
#: default of 3 children against a controller constant.
MAX_LEGS_PER_FANOUT = 8


class _FanOutTooWideError(_DoomLoopError):
    """The plan fans out past MAX_LEGS_PER_FANOUT (cabal#156).

    A SUBCLASS so handle_task's existing `except _DoomLoopError` turns it into
    the same annotated policy-violation Task -- no second refusal path -- while
    the type stays honest about which bound was hit.
    """


@dataclass
class PrimusPersona(LLMPersona):
    """Generic PLAN→IMPLEMENT→(REVIEW)→AGGREGATE primus base.

    Subclasses provide persona-specific `name`, `system_prompt`,
    `aggregate_prompt`, and `default_model` via their own thin `__init__`
    that calls `super().__init__()` with those values.

    `subordinates` maps role name → Subordinate instance. Every role EXCEPT
    the reserved 'reviewer' is a concurrent fan-out leg (cabal#69), so at
    least one non-reviewer leg is required. 'reviewer', if present, runs after
    the legs join and reviews all their outputs — it is the single reserved
    role, and only one review phase is expressible.

    `doom_loop` controls autonomous-loop protection at the subordinate
    boundary (cabal#59). By default a fresh ``DoomLoopGuardrail()`` is
    created so repeated identical delegations from a looping caller are
    blocked.  Pass ``doom_loop=None`` to disable the guard entirely (e.g.
    for tests that intentionally dispatch the same instruction many times).
    """

    subordinates: dict[str, Subordinate] = field(default_factory=dict)
    hook_registry: HookRegistry | None = None

    def __init__(
        self,
        llm_client: LiteLLMClient,
        subordinates: dict[str, Subordinate],
        *,
        name: str,
        system_prompt: str,
        aggregate_prompt: str,
        hook_registry: HookRegistry | None = None,
        model: str | None = None,
        default_model: str,
        max_tokens: int = 8192,
        doom_loop: DoomLoopGuardrail | None = _DOOM_LOOP_DEFAULT,  # type: ignore[assignment]
        max_legs: int = MAX_LEGS_PER_FANOUT,
        delegation_budget: DelegationBudgetGuardrail | None = None,
        delegation_depth: DelegationDepthGuardrail | None = None,
    ) -> None:
        legs = [role for role in subordinates if role != "reviewer"]
        if not legs:
            raise ValueError(
                f"{name!r} requires at least one non-reviewer subordinate "
                "(a fan-out leg); 'reviewer' alone has nothing to review"
            )
        # BREADTH IS A CONFIG ERROR, SO IT IS REFUSED WHERE THE CONFIG IS KNOWN
        # (cabal#156 r1). `leg_roles` is derived from `subordinates`, which is
        # assigned once and never mutated, and `_max_legs` is fixed here -- so
        # the runtime comparison is INVARIANT for the life of the instance and
        # cannot depend on the task. Leaving it only at dispatch meant a
        # deployment declaring nine legs constructed fine and then failed EVERY
        # dispatch forever, each one after paying for a full plan call.
        #
        # Raising rather than clamping, unlike `max_legs` below: a clamped
        # breadth would silently drop subordinates the operator declared, and
        # dropping work is worse than refusing to start. The ceiling is not
        # policy-settable, so there is no "still boot, bounded" case to serve.
        ceiling = max(1, min(max_legs, MAX_LEGS_PER_FANOUT))
        if len(legs) > ceiling:
            raise ValueError(
                f"{name!r} declares {len(legs)} fan-out legs, above the ceiling "
                f"of {ceiling}. Breadth is bounded because concurrency costs "
                "connections and tokens at once rather than over time. Declare "
                "fewer subordinates."
            )
        super().__init__(
            name=name,
            model=model or default_model,
            llm_client=llm_client,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
        )
        # Plan + aggregate prompts are stored on immutable private fields, NOT
        # read from self.system_prompt. The factory mutates handler.system_prompt
        # (system_prompt_override, edit_format append) — those knobs are meant
        # for a persona whose system_prompt drives its LLM call, but a primus's
        # plan/aggregate stages have their own prompts. Reading self.system_prompt
        # here would silently graft e.g. search-replace edit instructions onto a
        # *planning* prompt. Keep the primus behaviour-identical to pre-extraction
        # (both stages used module constants immune to factory mutation).
        self._plan_prompt = system_prompt
        self._aggregate_prompt = aggregate_prompt
        self.subordinates = subordinates
        self.hook_registry = hook_registry
        # doom_loop is default-on: a fresh guard is created unless the caller
        # explicitly passes None (disabled) or a custom DoomLoopGuardrail instance.
        # CLAMPED RATHER THAN HONOURED so a caller cannot raise its own bound,
        # and "a policy may only lower it" is finally a true story rather than
        # a vacuous one: `max_legs_override` (cabal#172) is the policy, and
        # MAX_LEGS_PER_FANOUT is the backstop it cannot raise. Until that field
        # existed the clamp was real and had nothing to clamp FROM (#156 r2).
        #
        # AND THE CLAMP SAYS SO NOW. Silently lowering a number an operator
        # wrote down is the same defect shape as silently discarding one -- the
        # deployment runs bounded, the config says otherwise, and nothing
        # reconciles them. Only an explicit over-wide value is logged; the
        # unset default reaches here as MAX_LEGS_PER_FANOUT and is not a
        # request to be refused.
        #
        # The floor exists so a sub-1 value is a working narrow fan-out rather
        # than a boot failure. It is unreachable from config -- the field's
        # validator refuses <1 outright, because zero legs is not a fan-out an
        # operator could have meant -- and remains for direct constructor
        # callers. The ceiling check that CAN fail boot is above, on the
        # DECLARED subordinates, where dropping work would be the alternative.
        if max_legs > MAX_LEGS_PER_FANOUT:
            log.warning(
                "primus.max_legs_clamped",
                persona=name,
                requested=max_legs,
                ceiling=MAX_LEGS_PER_FANOUT,
            )
        self._max_legs = max(1, min(max_legs, MAX_LEGS_PER_FANOUT))
        if doom_loop is _DOOM_LOOP_DEFAULT:
            self._doom_loop: DoomLoopGuardrail | None = DoomLoopGuardrail()
        else:
            self._doom_loop = doom_loop
        # cabal#158 — the SAME DelegationBudgetGuardrail instance the router
        # holds, not a second one, and that is the whole point.
        #
        # The budget is per-conversation and keyed on context_id. Two instances
        # would give one conversation two separate allowances, so an agent that
        # mixed router hops with fan-out legs would get double the budget --
        # which is worse than the gap being closed, because it would look
        # bounded. Sharing the instance makes a conversation's delegations
        # count once, wherever they happen.
        #
        # None when the operator disabled the budget, or when a primus is
        # constructed outside app.py (tests, direct use). Nothing here creates
        # one by default: an unshared default would be exactly the second
        # instance this comment exists to prevent.
        self._delegation_budget = delegation_budget
        # cabal#171 — THE ROUTER'S depth instance, for the same reason #158 took
        # the router's budget: one enforcement point rather than two that can
        # disagree. Until this landed the counter and the check NEVER MET --
        # `next_delegation_depth` stamped a depth on every fan-out leg and
        # nothing read it at a decision point, because the guard ran only in
        # `build_router_input_guardrails` and a leg goes straight to
        # `subord.handle_task`. The increment was write-only, so cabal's own
        # delegation could not accumulate depth and the limit of 8 was
        # unreachable by accumulation.
        self._delegation_depth = delegation_depth

    async def handle_task(self, task: Task) -> Task:
        # cabal#11 — the parent dispatch node (task.id) is registered by the
        # router; checks here cancel the primus's own pipeline between stages.
        self.abort_coordinator.raise_if_aborted(task.id)
        # _plan and _aggregate are the two stages that can raise
        # LLMTruncatedError: a subordinate leg annotates its own failure and
        # returns a Task, so it never reaches here as an exception (cabal#134).
        try:
            plan_text = await self._plan(task)
        except LLMTruncatedError as exc:
            return self._truncated(task, exc)

        # cabal#69 — fan-out: every non-reviewer subordinate is a leg,
        # dispatched CONCURRENTLY on the plan. `subordinates` is insertion-
        # ordered, so leg order is stable. A single 'implementer' leg reduces
        # to the old sequential behaviour (gather-of-one).
        leg_roles = [role for role in self.subordinates if role != "reviewer"]
        try:
            # A BACKSTOP, NOT THE BOUND. The bound is in __init__, where the
            # input is known and nothing has been spent; this cannot be reached
            # by a primus constructed through it, because `subordinates` is
            # never mutated after construction.
            #
            # AN EARLIER COMMENT HERE SAID "BOUND THE BREADTH BEFORE SPENDING
            # ANYTHING" AND IT WAS FALSE (#156 r1): `_plan` runs above this, so
            # a refused dispatch had already paid for one full primus LLM call.
            # What IS true, and is what the test pins, is the narrower claim --
            # refusing after the gather has opened the connections bounds
            # nothing, because the cost is paid at open time.
            #
            # INSIDE the try so it becomes an annotated policy-violation Task
            # through the existing handler, rather than propagating as an
            # exception the caller never asked to handle.
            if len(leg_roles) > self._max_legs:
                raise _FanOutTooWideError(
                    f"this agent is configured with {len(leg_roles)} "
                    f"subordinates, above the breadth ceiling of "
                    f"{self._max_legs}. Breadth is bounded separately from "
                    "depth and from total volume because concurrency costs "
                    "connections and tokens at once rather than over time. "
                    # NO REVIEW-ROUND CITATION IN A RUNTIME STRING. An earlier
                    # wording said "narrow the plan" -- advice the reader cannot
                    # act on -- and the commit that fixed it left the review
                    # reference inside the f-string rather than in a comment, so
                    # operators and models read it off the annotated Task
                    # (#156 r3 Nit).
                    "This is a configuration error; no plan can avoid it."
                )
            leg_outputs = await self._dispatch_legs(task, leg_roles, plan_text)
            reviewer_critique: str | None = None
            self.abort_coordinator.raise_if_aborted(task.id)
            if "reviewer" in self.subordinates:
                reviewer_critique = await self._dispatch_subordinate(
                    task,
                    role="reviewer",
                    instruction=self._reviewer_instruction(plan_text, leg_outputs),
                )
        except _LegFailedError as exc:
            return annotate(
                task,
                CabalReason(code=exc.code, message=f"{self.name}: {exc.message}"),
            )
        except _DoomLoopError as exc:
            # Doom-loop guard blocked a subordinate dispatch. Return an
            # annotated failed Task to the caller (mirrors the router's own
            # guardrail-block pattern — cabal#59).
            _label = self.name.removesuffix("-primus")
            return annotate(
                task,
                CabalReason(
                    code=CODE_POLICY_VIOLATION,
                    message=f"{_label} guardrail blocked: {exc.reason}",
                ),
            )

        try:
            final_text, truncated = await self._aggregate(
                task, plan_text, leg_outputs, reviewer_critique
            )
        except LLMTruncatedError as exc:
            return self._truncated(task, exc)

        agent_msg = Message(role="agent", parts=[TextPart(text=final_text)])
        history = list(task.history)
        if task.status.message is not None:
            history.append(task.status.message)
        return Task(
            **{
                "id": task.id,
                "contextId": task.context_id,
                "status": TaskStatus(state="completed", message=agent_msg),
                "history": history,
                # A kept-but-truncated summary is still the answer, so the task
                # completes — but it says so, rather than the flag being
                # computed and dropped (cabal#134 review).
                "metadata": (
                    {**task.metadata, "cabal_truncated": "aggregate"}
                    if truncated
                    else task.metadata
                ),
            }
        )

    # ---- internal pipeline -------------------------------------------------

    async def _dispatch_legs(
        self, task: Task, roles: list[str], instruction: str
    ) -> dict[str, str]:
        """Dispatch each leg CONCURRENTLY on the same instruction (the plan);
        return an insertion-ordered role -> output map (cabal#69).

        Each leg goes through `_dispatch_subordinate`, so it's an independent
        sub-task with its own id, abort node, doom-loop guard, and hooks — no
        cross-leg interference, and per-leg checkpointing stays isolated.

        Failure posture mirrors the old sequential path: a doom-loop BLOCK on
        any leg fails the whole fan-out (re-raised as `_DoomLoopError` for
        `handle_task` to annotate); an abort or any other error propagates.

        DESIGN DECISION (cabal#69 review): we use `gather(return_exceptions=
        True)` and CONSCIOUSLY DECLINE prompt sibling-cancellation. A
        `TaskGroup` would cancel in-flight siblings on the first failure
        (saving their tokens), but its `ExceptionGroup` is ordered by
        wall-clock — whichever leg fails first — whereas `gather` collects
        every leg so we can select the failure DETERMINISTICALLY by leg order
        (real errors before doom-loops; first insertion-order wins, below).
        The cost is bounded: on a *self-inflicted* leg failure (a doom-loop or
        a leg's own error) the siblings run one extra round before we re-raise.
        A PARENT abort is unaffected — it walks all descendants and cancels
        every leg's in-flight call promptly. Given aborts (the common
        cancellation) already collapse cleanly and self-inflicted failures are
        rare and bounded, deterministic ordering was judged the better trade.
        `return_exceptions=True` also prevents pending-task leaks when one leg
        raises.
        """
        results = await asyncio.gather(
            *(
                self._dispatch_subordinate(task, role=role, instruction=instruction)
                for role in roles
            ),
            return_exceptions=True,
        )
        # PRECEDENCE, AND WHY: a real exception beats both deferred kinds
        # (raised in-loop at its position); between the two deferred kinds a
        # DOOM BLOCK OUTRANKS A LEG FAILURE, because a guardrail block is a
        # structural refusal — the leg never ran — while a leg failure is the
        # outcome of one that did. That order was comment-only until
        # test_a_doom_loop_outranks_a_leg_failure; swapping the two raises left
        # the whole suite green.
        #
        # A LEG FAILURE MUST NOT OUTRANK A SIBLING'S ABORT. Results are in
        # insertion order, so raising the first BaseException would let leg 1's
        # _LegFailedError win over leg 2's TaskAborted and report an operator
        # cancellation as a budget failure. Both deferred kinds are held back
        # until every real exception has had its chance to propagate. Latent
        # today — no shipped identity configures two concurrent legs — and a
        # live bug the day one does.
        doom: _DoomLoopError | None = None
        leg_failed: _LegFailedError | None = None
        for res in results:
            if isinstance(res, _DoomLoopError):
                doom = doom or res
            elif isinstance(res, _LegFailedError):
                leg_failed = leg_failed or res
            elif isinstance(res, BaseException):
                raise res  # abort / real errors propagate, as before
        if doom is not None:
            raise doom
        if leg_failed is not None:
            raise leg_failed
        return dict(zip(roles, results, strict=True))

    def _reviewer_instruction(
        self, plan_text: str, leg_outputs: dict[str, str]
    ) -> str:
        """Build the reviewer's instruction from the plan + every leg's output.
        A single 'implementer' leg yields the familiar single-output review."""
        leg_block = "\n\n".join(
            f"[{role}] OUTPUT:\n{out}" for role, out in leg_outputs.items()
        )
        return (
            f"PLAN given to the leg(s):\n{plan_text}\n\n"
            f"{leg_block}\n\n"
            "Critique. Focus on real problems — correctness, security, edge "
            "cases the leg(s) missed, broken invariants. If you find nothing "
            "serious, say 'no significant issues'."
        )

    async def _plan(self, task: Task) -> str:
        # THE BUDGET NOTE BELONGS ON THIS HOP, and reaching it needed a second
        # site (cabal#156 r1 Important 1). It was folded into
        # LLMPersona.build_messages under the belief that that is where every
        # persona's system content is composed. A primus overrides handle_task
        # and never calls build_messages: instrumented across a real Mason
        # dispatch, the note landed on the LEG and on neither the plan nor the
        # aggregate call. So the number reached a Subordinate, which is a leaf
        # that cannot delegate, and was withheld from the one agent deciding
        # how wide to fan out.
        #
        # NOT ON _aggregate, deliberately: it joins outputs and spends nothing,
        # so telling it the budget is noise in a prompt that is already long.
        messages = [
            {
                "role": "system",
                "content": self._plan_prompt + self._delegation_budget_note(task),
            },
            *(_message_to_chat(m) for m in task.history),
        ]
        if task.status.message is not None:
            messages.append(_message_to_chat(task.status.message))

        response = await self.abort_coordinator.guard(
            task.id,
            self.llm_client.acompletion(
                model=self.model,
                messages=messages,
                agent=f"{self.name}:plan",
                task_id=task.id,
                trace_id=task.metadata.get("cabal_trace_id"),
                span_id=task.metadata.get("cabal_span_id"),
                max_tokens=self.max_tokens,
                # LLMPersona.extra_kwargs reaches its own call, the tool-loop
                # re-entry that shares that call's dict, and the stream — but
                # BOTH primus stages omitted it, so a provider knob set on a
                # primus applied to its subordinates and not to the primus
                # itself. Two of the five acompletion/astream call sites.
                **self.extra_kwargs,
            ),
        )
        plan = self.parse_response(response)
        # EMPTINESS FIRST, INDEPENDENT OF THE REASON. check_truncation blesses
        # empty-with-`stop`, and litellm's map_finish_reason defaults every
        # reason it cannot map to exactly that — provider ERROR states included
        # — so an empty plan escaped through `stop`, fanned out, and completed
        # a confident summary over nothing. A terminal answer may legitimately
        # be empty; an INSTRUCTION never can.
        if not plan.strip():
            raise LLMTruncatedError(
                f"{self.name}: the plan hop returned no content, so there is "
                "no instruction to hand a subordinate."
            )
        # An empty plan hands the implementer an empty instruction, so this
        # must fail here rather than fan out (cabal#134). A HALF plan is fatal
        # too, and that is the one place this differs from a terminal answer:
        # a cut-off answer reaches a human who can see it stops mid-word, while
        # a cut-off plan reaches an agent that cannot tell a truncated
        # instruction from a terse one.
        if self.check_truncation(response, plan):
            raise LLMTruncatedError(
                f"{self.name}: the plan was cut off at the output ceiling, and "
                "a truncated instruction is indistinguishable from a terse one "
                "to the subordinate receiving it."
            )
        return plan

    def _truncated(self, task: Task, exc: LLMTruncatedError) -> Task:
        """A failed Task with a reason code, not a raw exception at the router.

        Same treatment a plain persona's own dispatch gets for this condition,
        so a primus and a singleton fail identically (cabal#134).
        """
        return annotate(
            task, CabalReason(code=CODE_BUDGET_EXHAUSTED, message=str(exc))
        )

    async def _dispatch_subordinate(
        self, parent_task: Task, *, role: str, instruction: str
    ) -> str:
        subord = self.subordinates[role]
        # cabal#59 — thread parent's context_id into the sub-task so the
        # doom-loop guard can group re-delegations by session key.
        sub_task = Task(
            **{
                "id": f"{parent_task.id}.{role}-{secrets.token_hex(4)}",
                "contextId": parent_task.context_id,
                "status": TaskStatus(
                    state="submitted",
                    message=Message(role="user", parts=[TextPart(text=instruction)]),
                ),
                "metadata": {
                    **{
                        k: v
                        for k, v in parent_task.metadata.items()
                        # THE BUDGET NOTE IS FOR SPENDERS (#156 r2 Nit). A leg
                        # is a leaf -- no tool_proxy, so it cannot delegate --
                        # and inheriting the stamp put "plan the delegations
                        # you have left" in front of N subordinates that have
                        # none to plan. That is the same noise the aggregate
                        # hop was deliberately spared, multiplied by the
                        # fan-out width. The primus keeps it; the legs do not.
                        if k != BUDGET_KEY
                    },
                    "cabal_parent_task": parent_task.id,
                    "cabal_subordinate_role": role,
                    # cabal#144 — the hop counter. Inherited-plus-one rather
                    # than recomputed, so a chain that leaves this process and
                    # comes back keeps counting; depth is a property of the
                    # CHAIN, not of one primus's view of it.
                    DEPTH_KEY: next_delegation_depth(parent_task),
                    "cabal_span_id": secrets.token_hex(8),
                },
            }
        )

        # cabal#59 — doom-loop guard at the subordinate boundary. This mirrors
        # the router's input_guardrails check (cabal#42) for the primus fan-out
        # path, which bypasses the router entirely. The guard runs BEFORE we
        # register the abort node so there is nothing to clean up on a BLOCK.
        # BOTH GUARDS RUN HERE, and #158 is that only the first one did. The
        # router chain never sees a subordinate leg -- handle_task is called
        # directly below -- so a delegation budget wired only into the router
        # bounds router-mediated delegation and nothing else. That left fan-out
        # legs bounded by the sameness counter alone, which is the counter #146
        # exists precisely because rewording defeats.
        if (
            self._doom_loop is not None
            or self._delegation_budget is not None
            or self._delegation_depth is not None
        ):
            sub_task.metadata["cabal_target_agent"] = role
            guard_ctx = GuardrailContext(
                persona_name=self.name, task_id=sub_task.id, phase="router"
            )
            # ORDER MATCHES build_router_input_guardrails. It is immaterial to
            # WHETHER a leg is refused and material to two other things -- see
            # both paragraphs below, which an earlier version of this block ran
            # together under the heading "WHY ORDER CANNOT MATTER" while the
            # tuple beneath it had three entries and the prose said "neither"
            # and "both" (#177 r1 nit 4).
            #
            # WHY ORDER CANNOT DECIDE THE OUTCOME, structurally rather than by
            # threshold. No guard starves another before the outcome is decided:
            # each is checked on every call until one of them first refuses, so
            # whichever is listed first still reaches its own natural trip
            # point. The rest are only skipped AFTER the first has begun
            # refusing -- by which point the task is refused either way.
            #
            # WHAT ORDER DOES DECIDE, and it is more than the wording. The
            # obvious part is the MESSAGE: the router's own comment says "a
            # tight repeat is reported as a doom loop and a paraphrase storm as
            # an exhausted budget", and reporting a tight repeat as a depth
            # violation instead is a worse message, not a neutral one.
            #
            # The part I had wrong, measured by the #177 reviewer rather than
            # reasoned: a guard that is SKIPPED does not run its SIDE EFFECT.
            # DoomLoopGuardrail's counter increments near the top of `check()`,
            # BEFORE the threshold comparison, so merely being called advances
            # it. Under doom-FIRST a leg that depth later refuses has still
            # advanced the sameness counter; under depth-first it has not. That
            # changes a later, shallower delegation's outcome. Doom-first is
            # what a router hop does, so matching it makes the fan-out's counter
            # accumulate the same way -- the order fix is more than cosmetic,
            # and correct for a better reason than this comment first gave.
            #
            # THE BUDGET: NO DEPTH-REFUSED LEG REACHES IT UNDER EITHER ORDER,
            # since it is last in both, so the no-refund property is not
            # implicated by THIS reorder. Do not read that as order-invariance
            # (#177 r2, and the reviewer falsified her own r1 claim to get it).
            # The set of legs that DO reach it is "legs where doom and depth
            # both passed", and doom's verdict depends on the state that
            # diverges above -- so the divergence carries one guard further.
            # Measured over a sequence rather than a single leg: three deep
            # legs sharing a context and a delegation key, then a shallow one,
            # leaves the budget's counts EMPTY under doom-first and holding an
            # entry under depth-first. Nothing escapes a bound either way --
            # doom-first REFUSES the shallow leg that depth-first dispatches --
            # so the divergence is in accounting, not in safety.
            #
            # And "empty" is a property of that sequence, not of the guard:
            # since #158 this is the ROUTER'S shared instance and it persists
            # across hops, so what can be asserted is "unaffected by that leg",
            # never "empty".
            #
            # Said that way it holds for any configuration. An earlier version
            # of this comment argued instead that "the doom threshold trips
            # long before the budget limit", which was true of the shipped
            # defaults AT THE TIME and NOT enforced anywhere: the two limits have
            # independent validators and no cross-field check, so
            # doom_loop_threshold=30 with the default budget of 20 is a legal
            # config in which the budget trips first. A default dressed as an
            # invariant (#158 pre-pass, r2).
            #
            # Matching the router's order stops the module contradicting itself.
            #
            # ANY NON-PASS VERDICT STOPS, rather than an enumeration of the
            # ones that exist today. Fail-closed is the right default at a
            # guard boundary, and it absorbs #152's TERMINATE without anyone
            # remembering to come back -- a terminating budget falling through
            # a `== BLOCK` check would dispatch the leg silently. It also
            # stops REWRITE being dropped on the floor, which the enumeration
            # did: neither guard here returns one, but ignoring it would have
            # dispatched the UNREWRITTEN task.
            #
            # IT ABSORBS THE STOP, NOT THE DISTINCTION (#158 r3 Nit). Both
            # verdicts funnel into _DoomLoopError(verdict.reason) and the type
            # is DELIBERATELY FLATTENED here -- a terminal budget and an
            # advisory doom-loop produce the same annotated failed Task,
            # because _DoomLoopError already fails the whole fan-out and that
            # is the "end the task" effect either way. Said explicitly so
            # whoever merges #152 does not read "absorbs TERMINATE" as a reason
            # to skip this site and go looking for a distinction to preserve. Note this REFUSES a rewrite
            # rather than applying it -- the honest fail-closed reading, and
            # unreachable besides: this boundary calls guard.check() directly
            # rather than run_guardrails, so the PASS-carrying-`rewritten`
            # shape never arises here and neither guard returns REWRITE.
            # DEPTH IS CHECKED AGAINST THE STAMP THIS SAME METHOD APPLIED.
            # `sub_task`'s DEPTH_KEY was set above from
            # `next_delegation_depth(parent_task)`, and the guard reads that key
            # -- so the number the guard SEES is the number the fan-out STAMPED.
            # That identity is what cabal#171 was about and what every prior
            # test missed, because each looked at only one of the two.
            #
            # ORDER MATCHES `build_router_input_guardrails` EXACTLY, and an
            # earlier version of this comment CLAIMED that while the code did
            # the opposite (cabal#177 pre-pass). The router appends doom_loop,
            # then depth, then budget, and `run_guardrails` evaluates in list
            # order with first-refusal-wins -- so a depth-first loop here made
            # the two paths disagree about a leg that is BOTH a tight repeat and
            # past its limit. Same task, two different refusals, decided by
            # which path dispatched it.
            #
            # THE ROUTER'S ORDER IS DELIBERATE and says so: "ORDER MATTERS ONLY
            # FOR THE MESSAGE, not for safety: the first BLOCK short-circuits,
            # so a tight repeat is reported as a doom loop and a paraphrase
            # storm as an exhausted budget." Reporting a tight repeat as a depth
            # violation instead is a worse message, not a neutral one.
            #
            # (`app.py`'s "depth first among the three bounds" is about depth
            # against BUDGET and EDGES -- the three bounds. DoomLoopGuardrail is
            # a sameness counter, not a bound, and is not one of the three. Both
            # files are self-consistent under that reading; only my comment was
            # not.)
            for guard in (
                self._doom_loop,
                self._delegation_depth,
                self._delegation_budget,
            ):
                if guard is None:
                    continue
                verdict = await guard.check(sub_task, guard_ctx)
                if verdict.type is not VerdictType.PASS:
                    raise _DoomLoopError(verdict.reason)

        # cabal#11 — register the subordinate under the parent so an abort of
        # the parent reaches its in-flight LLM call. The primus calls
        # handle_task directly (bypassing the router), so the node must be
        # registered here.
        self.abort_coordinator.register(
            sub_task.id, parent_id=parent_task.id, kind=KIND_SUBORDINATE
        )
        try:
            await self._fire(HookName.SUBORDINATE_DISPATCHED, sub_task, role)
            response = await subord.handle_task(sub_task)
            await self._fire(HookName.SUBORDINATE_RETURNED, response, role)
        finally:
            self.abort_coordinator.complete(sub_task.id)

        # A FAILED LEG IS NOT OUTPUT (cabal#134 review). This read the parts
        # without ever looking at the state, so a subordinate that failed —
        # truncated, guardrail-blocked, anything — had its REASON STRING
        # consumed as its answer, and the aggregator wrote a confident summary
        # on top of it. The task then completed clean, which is worse than the
        # empty answer it replaced: an operator can spot zero characters.
        if response.status.state == "failed":
            raise _LegFailedError(
                role=role,
                # 704, not 701: an unexplained leg failure is "subordinate
                # handed back to caller", whereas 701 asserts a safety-tier
                # refusal the code has no way to know happened — the same class
                # of confident-wrong-label this PR exists to remove.
                code=int(response.metadata.get("cabal_reason_code", CODE_ESCALATION)),
                message=str(response.metadata.get("cabal_reason_message", "no reason given")),
            )
        if response.status.message is None:
            return ""
        return " ".join(
            p.text for p in response.status.message.parts if isinstance(p, TextPart)
        )

    async def _aggregate(
        self,
        task: Task,
        plan_text: str,
        leg_outputs: dict[str, str],
        reviewer_critique: str | None = None,
    ) -> tuple[str, bool]:
        messages = [
            {"role": "system", "content": self._aggregate_prompt},
            *(_message_to_chat(m) for m in task.history),
        ]
        if task.status.message is not None:
            messages.append(_message_to_chat(task.status.message))

        # Join every leg's output (a single 'implementer' leg is the common
        # case). Strip noisy LLM artifacts (numbered bullets, code-fences)
        # before quoting in the aggregation prompt.
        user_blocks = [f"PLAN: {plan_text}"]
        for role, out in leg_outputs.items():
            condensed = re.sub(r"\n{3,}", "\n\n", out)
            user_blocks.append(f"[{role}] OUTPUT:\n{condensed}")
        if reviewer_critique is not None:
            condensed_review = re.sub(r"\n{3,}", "\n\n", reviewer_critique)
            user_blocks.append(f"REVIEWER CRITIQUE:\n{condensed_review}")
        user_blocks.append("Summarize the result for the operator.")
        messages.append({"role": "user", "content": "\n\n".join(user_blocks)})
        response = await self.abort_coordinator.guard(
            task.id,
            self.llm_client.acompletion(
                model=self.model,
                messages=messages,
                agent=f"{self.name}:aggregate",
                task_id=task.id,
                trace_id=task.metadata.get("cabal_trace_id"),
                span_id=task.metadata.get("cabal_span_id"),
                max_tokens=self.max_tokens,
                **self.extra_kwargs,
            ),
        )
        summary = self.parse_response(response)
        # Every empty answer in cabal#134's evidence came from this hop. A
        # truncated-but-present summary is kept — it is the terminal answer and
        # a human can see it stops mid-word — and the flag RIDES BACK ON THE
        # RETURN VALUE for handle_task to stamp into the Task. Deliberately not
        # an instance attribute: register_local holds ONE handler per agent, so
        # concurrent dispatches interleave on this object at every await and a
        # flag written here would be whatever the last one wrote.
        return summary, self.check_truncation(response, summary)

    async def _fire(self, name: HookName, task: Task, role: str) -> None:
        if self.hook_registry is None:
            return
        await self.hook_registry.fire(
            name,
            {
                "task_id": task.id,
                "parent_task_id": task.metadata.get("cabal_parent_task"),
                "role": role,
                "state": task.status.state,
                "trace_id": task.metadata.get("cabal_trace_id"),
                "span_id": task.metadata.get("cabal_span_id"),
            },
        )
