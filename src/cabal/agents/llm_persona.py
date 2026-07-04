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

import time
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
from cabal.core.abort import AbortCoordinator, TaskAborted
from cabal.core.checkpoint import Checkpoint, CheckpointStore, NoOpCheckpointStore
from cabal.core.hooks.registry import HookName
from cabal.core.llm.litellm_client import (
    CabalLLMError,
    LiteLLMClient,
    LLMInvalidRequestError,
    LLMRateLimitedError,
    LLMUnavailableError,
)
from cabal.core.memory.session_tracker import NoOpSessionTracker, SessionTracker
from cabal.core.memory.store import MemoryStore, NoOpMemoryStore

# Hard cap on the tool-result summary stored in the activity log's tool_result
# row. The full result still flows to the LLM as the tool message; only the
# audit-log copy is bounded. `result_truncated` records whether this bit.
_RESULT_SUMMARY_MAX = 2000


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
    # Optional MCP tool surface (cabal.core.tools.mcp_proxy.MCPToolProxy).
    # When set, build_messages still emits the same system+history, but
    # acompletion is called with `tools=...`. tool_calls in the response
    # trigger a recursive loop capped at `tool_use_max_steps` total LLM
    # calls per dispatch.
    tool_proxy: Any | None = None
    tool_use_max_steps: int = 4
    # cabal#2 — when set to a real CheckpointStore (i.e. not the no-op),
    # handle_task writes a checkpoint before every LLM call and drops it
    # on successful completion. Opt-in per persona via identity.toml
    # `[personas.NAME.checkpointing] enabled = true`; the persona_factory
    # swaps the no-op default for the SQLite-backed store at registration.
    checkpoint_store: CheckpointStore = field(default_factory=NoOpCheckpointStore)
    # cabal#11 — shared AbortGraph coordinator. Default is a private,
    # empty coordinator: cooperative checks against an unregistered task.id
    # are no-ops, so the persona runs identically standalone. The
    # persona_factory swaps in the router's shared coordinator at
    # registration, at which point abort() actually reaches this loop.
    abort_coordinator: AbortCoordinator = field(default_factory=AbortCoordinator)

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
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
        for hist in task.history:
            messages.append(_message_to_chat(hist))
        if task.status.message is not None:
            messages.append(_message_to_chat(task.status.message))
        return messages

    def parse_response(self, response: Any) -> str:
        """Extract assistant text from the LiteLLM response.

        When the response is a tool_calls-only message (no text content),
        return the empty string rather than raising — the caller has
        already decided this is the final response, and tools/no-text is
        a legitimate (if unusual) final state."""
        try:
            content = response["choices"][0]["message"].get("content")
        except (KeyError, IndexError, TypeError) as exc:
            raise CabalLLMError(f"unexpected response shape: {exc}") from exc
        if content is None:
            return ""
        return content

    async def handle_task(self, task: Task, *, resume_from: Checkpoint | None = None) -> Task:
        """Run the LLM dispatch for `task`.

        When `resume_from` is provided, skip the initial LLM call and
        re-enter the tool-use loop with the saved `messages` and
        `step_index`. The initial LLM call would have been the *first*
        thing checkpointed, so its replay is just `acompletion(messages)`
        with the checkpointed history — same code path as the tool-use
        iteration, hence the unified loop below.
        """
        budget_override = task.metadata.get("cabal_token_budget")
        if budget_override is not None and getattr(self.llm_client, "budget", None) is not None:
            self.llm_client.budget.set_budget(task.id, int(budget_override))

        session_id = self.session_tracker.start(external_id=task.id, agent_identity=self.name)
        try:
            return await self._run_dispatch(
                task, session_id=session_id, resume_from=resume_from
            )
        except TaskAborted:
            # cabal#11 — cancellation hit a cooperative boundary; close the
            # session cleanly (a checkpoint was already written for resume).
            self.session_tracker.end(session_id=session_id, summary="[canceled] aborted")
            raise

    async def _run_dispatch(
        self, task: Task, *, session_id: str, resume_from: Checkpoint | None
    ) -> Task:
        """Drive the LLM tool-use loop for `task` (session already started)."""
        if resume_from is None:
            messages = self.build_messages(task)
            step_index = 0
        else:
            messages = list(resume_from.messages)
            step_index = resume_from.step_index

        # If a tool proxy is wired, fetch the tool surface and pass it to
        # the LLM. Returned tool_calls drive a recursive call loop capped
        # at tool_use_max_steps total LLM invocations per dispatch.
        extra_acompletion_kwargs = dict(self.extra_kwargs)
        if self.tool_proxy is not None:
            try:
                tools_for_llm = await self.tool_proxy.list_tools_for_llm()
                if tools_for_llm:
                    extra_acompletion_kwargs["tools"] = tools_for_llm
            except Exception:  # noqa: BLE001
                # Tool surface lookup must not block dispatch
                pass

        # Checkpoint BEFORE the LLM call so a crash inside the call (or
        # the network) leaves a recoverable state. The no-op store
        # short-circuits when checkpointing is disabled.
        await self.checkpoint_store.save(
            Checkpoint(
                task_id=task.id,
                step_index=step_index,
                messages=messages,
                task_json=task.model_dump(by_alias=True, exclude_none=True),
                persona_state={"session_id": session_id},
            )
        )

        # cabal#11 — clean cancellation point: the checkpoint above is written,
        # so an abort here leaves a resumable Task.
        self.abort_coordinator.raise_if_aborted(task.id)

        try:
            response = await self.abort_coordinator.guard(
                task.id,
                self.llm_client.acompletion(
                    model=self.model,
                    messages=messages,
                    agent=self.name,
                    task_id=task.id,
                    trace_id=task.metadata.get("cabal_trace_id"),
                    span_id=task.metadata.get("cabal_span_id"),
                    max_tokens=self.max_tokens,
                    **extra_acompletion_kwargs,
                ),
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
            self.session_tracker.end(session_id=session_id, summary=f"[failed] LLM rejected: {exc}")
            return annotate(
                task,
                CabalReason(
                    code=CODE_POLICY_VIOLATION,
                    message=f"LLM rejected request: {exc}",
                ),
            )

        # Tool-use loop. Each iteration:
        #   1. Inspect response for tool_calls; if none, exit with text.
        #   2. For each tool_call, dispatch via mcp_proxy, append the
        #      assistant message (with tool_calls) + each tool result, then
        #      call acompletion again with the extended history.
        #   3. Cap at tool_use_max_steps total LLM calls (initial + tool
        #      iterations) so a stuck model can't infinite-loop.
        if self.tool_proxy is not None:
            steps_remaining = max(0, self.tool_use_max_steps - 1)
            while steps_remaining > 0:
                tool_calls = _extract_tool_calls(response)
                if not tool_calls:
                    break
                # cabal#11 — stop before executing tool side-effects if aborted.
                self.abort_coordinator.raise_if_aborted(task.id)
                messages = _extend_with_tool_calls(messages, response, tool_calls)
                for call in tool_calls:
                    t0 = time.monotonic()
                    outcome = await self.tool_proxy.call_tool(call["name"], call["arguments"])
                    latency_ms = round((time.monotonic() - t0) * 1000)
                    result_content = _outcome_to_chat_content(outcome)
                    result_truncated = len(result_content) > _RESULT_SUMMARY_MAX
                    await self.llm_client.hook_registry.fire(
                        HookName.TOOL_USE,
                        {
                            "task_id": task.id,
                            "tool_call_id": call["id"],
                            "tool_name": call["name"],
                            "arguments": call["arguments"],
                            "result_summary": result_content[:_RESULT_SUMMARY_MAX],
                            "result_truncated": result_truncated,
                            "is_error": getattr(outcome, "is_error", False),
                            "latency_ms": latency_ms,
                        },
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": result_content,
                        }
                    )
                steps_remaining -= 1
                step_index += 1
                # Tool execution is the natural checkpoint boundary —
                # the loop has just produced new state, but the next
                # LLM call hasn't started yet. If we crash inside the
                # call, resume re-issues it with the same messages.
                await self.checkpoint_store.save(
                    Checkpoint(
                        task_id=task.id,
                        step_index=step_index,
                        messages=messages,
                        persona_state={"session_id": session_id},
                    )
                )
                try:
                    response = await self.abort_coordinator.guard(
                        task.id,
                        self.llm_client.acompletion(
                            model=self.model,
                            messages=messages,
                            agent=self.name,
                            task_id=task.id,
                            trace_id=task.metadata.get("cabal_trace_id"),
                            span_id=task.metadata.get("cabal_span_id"),
                            max_tokens=self.max_tokens,
                            **extra_acompletion_kwargs,
                        ),
                    )
                except (
                    LLMUnavailableError,
                    LLMRateLimitedError,
                    LLMInvalidRequestError,
                ) as exc:
                    self.session_tracker.end(
                        session_id=session_id,
                        summary=f"[failed mid-tool-loop] {exc}",
                    )
                    return annotate(
                        task,
                        CabalReason(
                            code=CODE_BUDGET_EXHAUSTED
                            if not isinstance(exc, LLMInvalidRequestError)
                            else CODE_POLICY_VIOLATION,
                            message=f"LLM error mid-tool-loop: {exc}",
                        ),
                    )

        text = self.parse_response(response)
        self.session_tracker.end(session_id=session_id, summary=text)
        self._commit_qa_to_memory(task, text)
        # Successful completion — drop the checkpoint so resume doesn't
        # try to replay a finished Task.
        await self.checkpoint_store.drop(task.id)
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
        session_id = self.session_tracker.start(external_id=task.id, agent_identity=self.name)
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
            self.session_tracker.end(session_id=session_id, summary=f"[stream-failed] {exc}")
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


# ---- Tool-use helpers -----------------------------------------------------


def _extract_tool_calls(response: Any) -> list[dict[str, Any]]:
    """Pull tool_calls out of a LiteLLM response into a uniform shape.

    LiteLLM normalizes Anthropic and OpenAI responses into the OpenAI
    `tool_calls` array shape: `[{id, function: {name, arguments}}]`.
    `arguments` is a JSON string per OpenAI; we parse it.
    """
    import json as _json

    try:
        message = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return []
    raw_calls = message.get("tool_calls") or []
    out: list[dict[str, Any]] = []
    for raw in raw_calls:
        if not raw.get("function"):
            continue
        try:
            args_raw = raw["function"].get("arguments") or "{}"
            args = _json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        except _json.JSONDecodeError:
            args = {}
        out.append(
            {
                "id": raw.get("id", ""),
                "name": raw["function"]["name"],
                "arguments": args,
            }
        )
    return out


def _extend_with_tool_calls(
    messages: list[dict[str, Any]],
    response: Any,
    tool_calls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Append the assistant message that triggered tool_use to the
    conversation, with the tool_calls intact so the next LLM hop sees
    the same thread of intent."""
    try:
        assistant_msg = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return list(messages)
    forwarded = {
        "role": "assistant",
        "content": assistant_msg.get("content") or "",
        "tool_calls": [
            {
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": __import__("json").dumps(call["arguments"]),
                },
            }
            for call in tool_calls
        ],
    }
    return list(messages) + [forwarded]


def _outcome_to_chat_content(outcome: Any) -> str:
    """Turn a limenal ToolCallOutcome into a single string the model
    can read in the tool-result message slot."""
    import json as _json

    blocks = getattr(outcome, "content", []) or []
    chunks: list[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and "text" in b:
            chunks.append(b["text"])
        elif b.get("type") == "json" and "data" in b:
            chunks.append(_json.dumps(b["data"]))
    if not chunks and getattr(outcome, "is_error", False):
        return "tool error"
    return "\n".join(chunks) or "(no content)"
