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

from collections.abc import AsyncIterator
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
from cabal.core.memory.session_tracker import NoOpSessionTracker, SessionTracker
from cabal.core.memory.store import MemoryStore, NoOpMemoryStore


@dataclass
class LLMPersona:
    """LocalHandler-shaped LLM agent.

    Subclass + set `name`, `model`, `system_prompt`. Override
    `build_messages` if you need to inject persona-specific context (memory
    lookups, tool descriptions, etc.).

    `memory_store` is optional; when set, `build_messages` adds a
    "Relevant memories" block to the system prompt with up to
    `memory_recall_limit` hits from the store. Defaults to a no-op store.
    """

    name: str
    model: str
    llm_client: LiteLLMClient
    system_prompt: str = "You are a helpful agent."
    max_tokens: int = 8192
    extra_kwargs: dict[str, Any] = field(default_factory=dict)
    memory_store: MemoryStore = field(default_factory=NoOpMemoryStore)
    memory_recall_limit: int = 5
    session_tracker: SessionTracker = field(default_factory=NoOpSessionTracker)
    commit_to_memory: bool = False
    memory_commit_type: str = "fact"
    memory_commit_confidence: str = "medium"
    memory_commit_max_chars: int = 2000

    def _recall_for_task(self, task: Task) -> str:
        """Return a formatted memory-hits block, or empty string."""
        if task.status.message is None:
            return ""
        query_text = " ".join(
            p.text for p in task.status.message.parts if isinstance(p, TextPart)
        ).strip()
        if not query_text:
            return ""
        hits = self.memory_store.recall(query_text, limit=self.memory_recall_limit)
        if not hits:
            return ""
        lines = ["", "Relevant memories (older first):"]
        for h in hits:
            tag_note = f" [tags: {h.tags}]" if h.tags else ""
            lines.append(f"- ({h.type}, {h.confidence}) {h.content}{tag_note}")
        return "\n".join(lines)

    def build_messages(self, task: Task) -> list[dict[str, Any]]:
        """Translate Task history + current message into chat messages.

        Default: [system+memories, ...history (user/agent alternating), current].
        """
        system_content = self.system_prompt + self._recall_for_task(task)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_content}
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
        budget_override = task.metadata.get("cabal_token_budget")
        if (
            budget_override is not None
            and getattr(self.llm_client, "budget", None) is not None
        ):
            self.llm_client.budget.set_budget(task.id, int(budget_override))

        session_id = self.session_tracker.start(
            external_id=task.id, agent_identity=self.name
        )

        messages = self.build_messages(task)
        try:
            response = await self.llm_client.acompletion(
                model=self.model,
                messages=messages,
                agent=self.name,
                task_id=task.id,
                trace_id=task.metadata.get("cabal_trace_id"),
                span_id=task.metadata.get("cabal_span_id"),
                max_tokens=self.max_tokens,
                **self.extra_kwargs,
            )
        except LLMUnavailableError as exc:
            self.session_tracker.end(
                session_id=session_id, summary=f"[failed] LLM unavailable: {exc}"
            )
            return annotate(
                task,
                CabalReason(
                    code=CODE_BUDGET_EXHAUSTED,
                    message=f"LLM unavailable: {exc}",
                ),
            )
        except LLMRateLimitedError as exc:
            self.session_tracker.end(
                session_id=session_id, summary=f"[failed] LLM rate-limited: {exc}"
            )
            return annotate(
                task,
                CabalReason(
                    code=CODE_BUDGET_EXHAUSTED,
                    message=f"LLM rate-limited: {exc}",
                ),
            )
        except LLMInvalidRequestError as exc:
            self.session_tracker.end(
                session_id=session_id, summary=f"[failed] LLM rejected: {exc}"
            )
            return annotate(
                task,
                CabalReason(
                    code=CODE_POLICY_VIOLATION,
                    message=f"LLM rejected request: {exc}",
                ),
            )

        text = self.parse_response(response)
        self.session_tracker.end(session_id=session_id, summary=text)
        self._commit_qa_to_memory(task, text)
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

    def _commit_qa_to_memory(self, task: Task, agent_text: str) -> None:
        """Write the Q&A pair to the memory store as a captured exchange.

        Opt-in via `commit_to_memory`. The committed content is
        '[persona] Q: <user>\\nA: <agent>' so search results stay
        attributable. Truncates to `memory_commit_max_chars` to keep the
        DB tidy.
        """
        if not self.commit_to_memory:
            return
        user_text = ""
        if task.status.message is not None:
            user_text = " ".join(
                p.text for p in task.status.message.parts if isinstance(p, TextPart)
            ).strip()
        if not user_text or not agent_text.strip():
            return
        content = f"[{self.name}] Q: {user_text}\nA: {agent_text.strip()}"
        if len(content) > self.memory_commit_max_chars:
            content = content[: self.memory_commit_max_chars - 1] + "…"
        try:
            self.memory_store.commit(
                content,
                type=self.memory_commit_type,
                tags=self.name,
                confidence=self.memory_commit_confidence,
            )
        except Exception:  # noqa: BLE001
            # Memory writes must not block dispatch.
            pass

    async def astream_task(self, task: Task) -> AsyncIterator[str]:
        """Yield text deltas as the LLM produces them.

        Wraps LiteLLMClient.astream + session lifecycle. Memory recall +
        history replay reuse `build_messages`, so streaming sees the same
        system prompt and history a non-streaming dispatch would.

        Errors mid-stream propagate via the underlying LiteLLMClient — the
        caller (typically the SSE route) decides what status-update event
        to emit.
        """
        session_id = self.session_tracker.start(
            external_id=task.id, agent_identity=self.name
        )
        messages = self.build_messages(task)
        full_text = ""
        try:
            async for delta in self.llm_client.astream(
                model=self.model,
                messages=messages,
                agent=self.name,
                task_id=task.id,
                trace_id=task.metadata.get("cabal_trace_id"),
                span_id=task.metadata.get("cabal_span_id"),
                max_tokens=self.max_tokens,
                **self.extra_kwargs,
            ):
                full_text += delta
                yield delta
        except Exception as exc:
            self.session_tracker.end(
                session_id=session_id, summary=f"[stream-failed] {exc}"
            )
            raise
        self.session_tracker.end(session_id=session_id, summary=full_text)
        self._commit_qa_to_memory(task, full_text)


def _message_to_chat(message: Message) -> dict[str, Any]:
    """Lossy A2A Message → OpenAI-style chat message."""
    role = "assistant" if message.role == "agent" else message.role
    text_parts: list[str] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            text_parts.append(part.text)
    return {"role": role, "content": "\n".join(text_parts)}
