"""Rook primus — engineering persona that fans out to subordinates.

Lifecycle per dispatch:
    1. PLAN. The primus makes one LLM call to decide which subordinate(s)
       to invoke and what instructions to give them. Output: a short
       planning text + a structured list of `(subordinate, instruction)`
       pairs.
    2. DISPATCH. For each pair, build a sub-Task and call the
       subordinate's `handle_task` directly. Fire subordinate_dispatched
       BEFORE and subordinate_returned AFTER. Sub-Tasks inherit the
       parent's trace_id; the span_id is fresh per hop.
    3. AGGREGATE. The primus makes one LLM call summarizing the
       subordinate outputs into a final response.

Phase K v1 keeps the planner deterministic (single-subordinate by default)
to make the loop testable without hoping the planner LLM produces clean
JSON. Phase L will add a JSON planner.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field

from cabal.agents.llm_persona import LLMPersona, _message_to_chat
from cabal.agents.subordinate import Subordinate
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.hooks.registry import HookName, HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient


DEFAULT_MODEL = "anthropic/claude-opus-4-7"

ROOK_SYSTEM_PROMPT = """\
You are Cabal's Rook: engineering primus.

You don't write code yourself. You DELEGATE to subordinates:
  - implementer: produces concrete code/diffs/steps.
  - reviewer: critiques the implementer's output for real problems.

For each operator request, your job is to PLAN: in 1-3 sentences, restate
the task as a precise instruction the implementer should follow. Be
specific about acceptance criteria.
"""

ROOK_AGGREGATE_PROMPT = """\
You are Cabal's Rook: engineering primus.

The implementer has returned its output. Summarize the result for the
operator in 2-4 sentences. Quote the implementer's specific code/steps
when useful; flag anything the implementer skipped or got wrong.
"""


@dataclass
class RookPrimusPersona(LLMPersona):
    """Primus persona — overrides handle_task to do plan+dispatch+aggregate.

    `subordinates` maps role name → Subordinate instance. Required to have
    at least 'implementer'.
    """

    subordinates: dict[str, Subordinate] = field(default_factory=dict)
    hook_registry: HookRegistry | None = None

    def __init__(
        self,
        llm_client: LiteLLMClient,
        subordinates: dict[str, Subordinate],
        *,
        hook_registry: HookRegistry | None = None,
        model: str | None = None,
        max_tokens: int = 8192,
    ) -> None:
        if "implementer" not in subordinates:
            raise ValueError("Rook requires at least an 'implementer' subordinate")
        super().__init__(
            name="rook-primus",
            model=model or DEFAULT_MODEL,
            llm_client=llm_client,
            system_prompt=ROOK_SYSTEM_PROMPT,
            max_tokens=max_tokens,
        )
        self.subordinates = subordinates
        self.hook_registry = hook_registry

    async def handle_task(self, task: Task) -> Task:
        plan_text = await self._plan(task)
        implementer_output = await self._dispatch_subordinate(
            task, role="implementer", instruction=plan_text
        )
        final_text = await self._aggregate(task, plan_text, implementer_output)

        agent_msg = Message(role="agent", parts=[TextPart(text=final_text)])
        history = list(task.history)
        if task.status.message is not None:
            history.append(task.status.message)
        return Task(
            id=task.id,
            context_id=task.context_id,
            status=TaskStatus(state="completed", message=agent_msg),
            history=history,
            metadata=task.metadata,
        )

    # ---- internal pipeline -------------------------------------------------

    async def _plan(self, task: Task) -> str:
        messages = [
            {"role": "system", "content": ROOK_SYSTEM_PROMPT},
            *(_message_to_chat(m) for m in task.history),
        ]
        if task.status.message is not None:
            messages.append(_message_to_chat(task.status.message))

        response = await self.llm_client.acompletion(
            model=self.model,
            messages=messages,
            agent="rook-primus:plan",
            task_id=task.id,
            trace_id=task.metadata.get("cabal_trace_id"),
            span_id=task.metadata.get("cabal_span_id"),
            max_tokens=self.max_tokens,
        )
        return self.parse_response(response)

    async def _dispatch_subordinate(
        self, parent_task: Task, *, role: str, instruction: str
    ) -> str:
        subord = self.subordinates[role]
        sub_task = Task(
            id=f"{parent_task.id}.{role}-{secrets.token_hex(4)}",
            status=TaskStatus(
                state="submitted",
                message=Message(role="user", parts=[TextPart(text=instruction)]),
            ),
            metadata={
                **parent_task.metadata,
                "cabal_parent_task": parent_task.id,
                "cabal_subordinate_role": role,
                "cabal_span_id": secrets.token_hex(8),
            },
        )

        await self._fire(HookName.SUBORDINATE_DISPATCHED, sub_task, role)
        response = await subord.handle_task(sub_task)
        await self._fire(HookName.SUBORDINATE_RETURNED, response, role)

        if response.status.message is None:
            return ""
        return " ".join(
            p.text for p in response.status.message.parts if isinstance(p, TextPart)
        )

    async def _aggregate(
        self, task: Task, plan_text: str, implementer_output: str
    ) -> str:
        # Strip noisy LLM artifacts the implementer might emit (numbered
        # bullets, code-fences) before quoting in the aggregation prompt.
        condensed = re.sub(r"\n{3,}", "\n\n", implementer_output)
        messages = [
            {"role": "system", "content": ROOK_AGGREGATE_PROMPT},
            *(_message_to_chat(m) for m in task.history),
        ]
        if task.status.message is not None:
            messages.append(_message_to_chat(task.status.message))
        messages.append(
            {
                "role": "user",
                "content": (
                    f"PLAN: {plan_text}\n\nIMPLEMENTER OUTPUT:\n{condensed}\n\n"
                    "Summarize the result for the operator."
                ),
            }
        )
        response = await self.llm_client.acompletion(
            model=self.model,
            messages=messages,
            agent="rook-primus:aggregate",
            task_id=task.id,
            trace_id=task.metadata.get("cabal_trace_id"),
            span_id=task.metadata.get("cabal_span_id"),
            max_tokens=self.max_tokens,
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
