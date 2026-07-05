"""PrimusPersona — shared PLAN→IMPLEMENT→(REVIEW)→AGGREGATE base.

All primus engineering personas (Rook, Jackdaw, …) inherit from here.
The fan-out machinery — planning, subordinate dispatch, aggregation,
doom-loop guard, hook firing — lives entirely in this module.

Lifecycle per dispatch:
    1. PLAN. The primus makes one LLM call to produce a precise instruction
       for the implementer. Output: a short planning text.
    2. DISPATCH. Build a sub-Task and call the implementer's `handle_task`
       directly. Fire subordinate_dispatched BEFORE and subordinate_returned
       AFTER. If a reviewer is present, run a second dispatch with the
       implementer output in context.
    3. AGGREGATE. The primus makes one LLM call summarizing the subordinate
       outputs into a final response for the operator.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field

from cabal.agents.llm_persona import LLMPersona, _message_to_chat
from cabal.agents.subordinate import Subordinate
from cabal.core.a2a.codes import CODE_POLICY_VIOLATION, CabalReason, annotate
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.abort import KIND_SUBORDINATE
from cabal.core.guardrails import GuardrailContext, VerdictType
from cabal.core.guardrails_builtin import DoomLoopGuardrail
from cabal.core.hooks.registry import HookName, HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient

# Sentinel: distinguishes "caller passed doom_loop=None (disabled)" from
# "caller omitted doom_loop (use default DoomLoopGuardrail())".
_DOOM_LOOP_DEFAULT: object = object()


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

    `subordinates` maps role name → Subordinate instance. Required to have
    at least 'implementer'.

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
        if "implementer" not in subordinates:
            raise ValueError(
                f"{name!r} requires at least an 'implementer' subordinate"
            )
        super().__init__(
            name=name,
            model=model or default_model,
            llm_client=llm_client,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
        )
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
        plan_text = await self._plan(task)
        try:
            implementer_output = await self._dispatch_subordinate(
                task, role="implementer", instruction=plan_text
            )
            reviewer_critique: str | None = None
            self.abort_coordinator.raise_if_aborted(task.id)
            if "reviewer" in self.subordinates:
                reviewer_instruction = (
                    f"PLAN given to implementer:\n{plan_text}\n\n"
                    f"IMPLEMENTER OUTPUT:\n{implementer_output}\n\n"
                    "Critique. Focus on real problems — correctness, security, "
                    "edge cases the implementer missed, broken invariants. "
                    "If you find nothing serious, say 'no significant issues'."
                )
                reviewer_critique = await self._dispatch_subordinate(
                    task, role="reviewer", instruction=reviewer_instruction
                )
        except _DoomLoopError as exc:
            # Doom-loop guard blocked the subordinate dispatch. Return an
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

        final_text = await self._aggregate(
            task, plan_text, implementer_output, reviewer_critique
        )

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
                "metadata": task.metadata,
            }
        )

    # ---- internal pipeline -------------------------------------------------

    async def _plan(self, task: Task) -> str:
        messages = [
            {"role": "system", "content": self.system_prompt},
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
            ),
        )
        return self.parse_response(response)

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

        if response.status.message is None:
            return ""
        return " ".join(
            p.text for p in response.status.message.parts if isinstance(p, TextPart)
        )

    async def _aggregate(
        self,
        task: Task,
        plan_text: str,
        implementer_output: str,
        reviewer_critique: str | None = None,
    ) -> str:
        # Strip noisy LLM artifacts the implementer might emit (numbered
        # bullets, code-fences) before quoting in the aggregation prompt.
        condensed_impl = re.sub(r"\n{3,}", "\n\n", implementer_output)
        messages = [
            {"role": "system", "content": self._aggregate_prompt},
            *(_message_to_chat(m) for m in task.history),
        ]
        if task.status.message is not None:
            messages.append(_message_to_chat(task.status.message))

        user_blocks = [f"PLAN: {plan_text}", f"IMPLEMENTER OUTPUT:\n{condensed_impl}"]
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
            ),
        )
        return self.parse_response(response)

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
