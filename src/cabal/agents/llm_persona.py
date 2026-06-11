"""LLM-backed persona base class.

A LocalHandler that calls an LLM via LiteLLMClient and wraps the response
as a completed Task. Real personas (Architect, Archivist, Librarian, Rook
implementer/reviewer subordinates) subclass this and override `system_prompt`
or `build_messages` to specialize behavior.

Phase 1 uses this with a stub completion_fn in tests. Phase 2 docker-compose
wiring connects litellm.acompletion to a running LiteLLM proxy and gives
each persona its own model from identity.toml.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cabal.core.a2a.codes import (
    CODE_BUDGET_EXHAUSTED,
    CODE_POLICY_VIOLATION,
    CabalReason,
    annotate,
)
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.llm.litellm_client import (
    CabalLLMError,
    LiteLLMClient,
    LLMInvalidRequestError,
    LLMRateLimitedError,
    LLMUnavailableError,
)


@dataclass
class LLMPersona:
    """LocalHandler-shaped LLM agent.

    Subclass + set `name`, `model`, `system_prompt`. Override
    `build_messages` if you need to inject persona-specific context (memory
    lookups, tool descriptions, etc.).
    """

    name: str
    model: str
    llm_client: LiteLLMClient
    system_prompt: str = "You are a helpful agent."
    max_tokens: int = 8192
    extra_kwargs: dict[str, Any] = field(default_factory=dict)

    def build_messages(self, task: Task) -> list[dict[str, Any]]:
        """Translate Task history + current message into chat messages.

        Default: [system, ...history (user/agent alternating), current].
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt}
        ]
        for hist in task.history:
            messages.append(_message_to_chat(hist))
        if task.status.message is not None:
            messages.append(_message_to_chat(task.status.message))
        return messages

    def parse_response(self, response: Any) -> str:
        """Extract assistant text from the LiteLLM response."""
        try:
            return response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise CabalLLMError(f"unexpected response shape: {exc}") from exc

    async def handle_task(self, task: Task) -> Task:
        messages = self.build_messages(task)
        try:
            response = await self.llm_client.acompletion(
                model=self.model,
                messages=messages,
                agent=self.name,
                trace_id=task.metadata.get("cabal_trace_id"),
                span_id=task.metadata.get("cabal_span_id"),
                max_tokens=self.max_tokens,
                **self.extra_kwargs,
            )
        except LLMUnavailableError as exc:
            return annotate(
                task,
                CabalReason(
                    code=CODE_BUDGET_EXHAUSTED,
                    message=f"LLM unavailable: {exc}",
                ),
            )
        except LLMRateLimitedError as exc:
            return annotate(
                task,
                CabalReason(
                    code=CODE_BUDGET_EXHAUSTED,
                    message=f"LLM rate-limited: {exc}",
                ),
            )
        except LLMInvalidRequestError as exc:
            return annotate(
                task,
                CabalReason(
                    code=CODE_POLICY_VIOLATION,
                    message=f"LLM rejected request: {exc}",
                ),
            )

        text = self.parse_response(response)
        agent_msg = Message(role="agent", parts=[TextPart(text=text)])
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


def _message_to_chat(message: Message) -> dict[str, Any]:
    """Lossy A2A Message → OpenAI-style chat message."""
    role = "assistant" if message.role == "agent" else message.role
    text_parts: list[str] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            text_parts.append(part.text)
    return {"role": role, "content": "\n".join(text_parts)}
