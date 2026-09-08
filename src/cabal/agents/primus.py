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

from cabal.agents.llm_persona import HONESTY_FLOOR, LLMPersona, _message_to_chat
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
from cabal.core.guardrails_builtin import DoomLoopGuardrail
from cabal.core.hooks.registry import HookName, HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient, LLMTruncatedError

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
    """Internal signal: doom-loop guardrail blocked a subordinate dispatch.

    Raised inside _dispatch_subordinate and caught in handle_task so the
    primus can return an annotated failed Task to its own caller.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason


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
    ) -> None:
        if not any(role != "reviewer" for role in subordinates):
            raise ValueError(
                f"{name!r} requires at least one non-reviewer subordinate "
                "(a fan-out leg); 'reviewer' alone has nothing to review"
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
        if doom_loop is _DOOM_LOOP_DEFAULT:
            self._doom_loop: DoomLoopGuardrail | None = DoomLoopGuardrail()
        else:
            self._doom_loop = doom_loop

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
        messages = [
            {"role": "system", "content": self._plan_prompt + HONESTY_FLOOR},
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
                    **parent_task.metadata,
                    "cabal_parent_task": parent_task.id,
                    "cabal_subordinate_role": role,
                    "cabal_span_id": secrets.token_hex(8),
                },
            }
        )

        # cabal#59 — doom-loop guard at the subordinate boundary. This mirrors
        # the router's input_guardrails check (cabal#42) for the primus fan-out
        # path, which bypasses the router entirely. The guard runs BEFORE we
        # register the abort node so there is nothing to clean up on a BLOCK.
        if self._doom_loop is not None:
            sub_task.metadata["cabal_target_agent"] = role
            guard_ctx = GuardrailContext(
                persona_name=self.name, task_id=sub_task.id, phase="router"
            )
            verdict = await self._doom_loop.check(sub_task, guard_ctx)
            if verdict.type == VerdictType.BLOCK:
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
            {"role": "system", "content": self._aggregate_prompt + HONESTY_FLOOR},
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
