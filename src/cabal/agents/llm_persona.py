"""LLM-backed persona base class.

A LocalHandler that calls an LLM via LiteLLMClient and wraps the response
as a completed Task. Real personas (Architect, Archivist, Librarian, Mason
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
from cabal.core.guardrails import Guardrail, GuardrailContext, VerdictType, run_guardrails
from cabal.core.hooks.registry import HookName
from cabal.core.llm.litellm_client import (
    CabalLLMError,
    LiteLLMClient,
    LLMInvalidRequestError,
    LLMRateLimitedError,
    LLMTruncatedError,
    LLMUnavailableError,
    classify_truncation,
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
    # cabal#39 — composable guardrails. Empty defaults = zero behavior change
    # for existing personas. Two phase-specific lists so each guardrail's
    # target type is unambiguous (no ctx.phase branching needed):
    #   input_guardrails  target = list[dict] chat messages, before the LLM call
    #   output_guardrails target = str final text, after the LLM emits it
    # On BLOCK the Task is returned as failed/policy-violation; on REWRITE the
    # value is replaced inline. See cabal.core.guardrails for the full contract.
    #
    # NOTE: input guardrails run once at first dispatch (resume_from is None) —
    # NOT on resume — so a non-idempotent rewrite can't compound across resumes;
    # the guarded messages are what gets checkpointed. Input-phase covers the
    # first LLM call only; tool-loop turns are #52's scope. astream_task does
    # NOT apply guardrails (streaming path deferred). It DOES check
    # finish_reason as of cabal#136 — the client surfaces the stream's terminal
    # reason through an on_finish callback, since the reason rides the final
    # chunk and astream yields plain strings.
    input_guardrails: list[Guardrail] = field(default_factory=list)
    output_guardrails: list[Guardrail] = field(default_factory=list)
    # cabal#52 — tool-boundary guardrails. Empty defaults = zero behavior change.
    # Two phase-specific lists (unambiguous target types, no ctx.phase branching):
    #   tool_call_guardrails   target = {"name", "arguments", "tool_call_id"} dict
    #                          Applied BEFORE call_tool. BLOCK skips the dispatch
    #                          and injects an error tool message so the LLM sees the
    #                          refusal and can react. REWRITE patches name/arguments.
    #                          Task is NOT failed — only this one call is blocked.
    #   tool_result_guardrails target = str result from _outcome_to_chat_content()
    #                          Applied AFTER call_tool. REWRITE replaces the content
    #                          the LLM sees. BLOCK injects a policy-error string.
    tool_call_guardrails: list[Guardrail] = field(default_factory=list)
    tool_result_guardrails: list[Guardrail] = field(default_factory=list)

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

    def check_truncation(self, response: Any, text: str) -> bool:
        """Whether *response* was cut off, raising when it was cut off fatally.

        A thin adapter: it pulls `finish_reason` and the executable-tool-call
        shape out of a NON-STREAMING response and hands them to
        `classify_truncation`, which is shared with the streaming path so the
        two cannot drift apart (cabal#136). The rule itself, and why each arm
        exists, is documented there.
        """
        try:
            choice = response["choices"][0]
            finish_reason = choice.get("finish_reason")
            # INSIDE THE GUARD. _extract_tool_calls is not internally total —
            # its own try covers the choices/message access, so a `function`
            # object with no `name`, or a non-dict entry, still raises. Lifting
            # this into the classify_truncation() argument list put it OUTSIDE
            # this except, and a bare KeyError then escaped handle_task, which
            # catches only LLMTruncatedError — leaving the session never ended.
            # THE SAME PREDICATE THE TOOL LOOP USES, not a looser one beside
            # it: the blessing means "cabal can act on this".
            has_executable_tool_calls = bool(_extract_tool_calls(response))
        except (KeyError, IndexError, TypeError, AttributeError):
            # A malformed response shape. parse_response returns "" for these
            # rather than rejecting them, so nothing downstream owns the error
            # — raising here would surface a confusing one from the wrong layer.
            return False
        return classify_truncation(
            agent=self.name,
            finish_reason=finish_reason,
            text=text,
            has_executable_tool_calls=has_executable_tool_calls,
        )

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

    async def _apply_guardrails(
        self,
        guardrails: list[Guardrail],
        target: Any,
        phase: str,
        task: Task,
    ) -> tuple[Any, Task | None]:
        """Run ``guardrails`` for ``phase`` against ``target``.

        Returns ``(possibly-rewritten target, None)`` on PASS/REWRITE.
        Returns ``(original target, blocked_task)`` on BLOCK — the caller
        must return ``blocked_task`` immediately.

        The caller is responsible for ending the session (session_tracker.end)
        and dropping the checkpoint (checkpoint_store.drop) on BLOCK, to
        mirror the existing error-handling pattern in _run_dispatch.
        """
        if not guardrails:
            return target, None
        ctx = GuardrailContext(
            persona_name=self.name,
            task_id=task.id,
            phase=phase,
        )
        verdict = await run_guardrails(guardrails, target, ctx)
        # ANY NON-PASS IS A REFUSAL, rather than an enumeration of the verdicts
        # that exist today (#152 r1 Important 2). This read `== BLOCK`, so a
        # TERMINATE fell through and was treated as a PASS -- the dispatch
        # proceeded and the LLM ran. Since run_guardrails ALSO short-circuits on
        # TERMINATE, a terminating guard was strictly WEAKER here than the same
        # guard returning BLOCK, which inverts the verdict's whole point.
        #
        # SAFE AS A CATCH-ALL because run_guardrails only ever returns BLOCK,
        # TERMINATE, or PASS: a REWRITE is threaded internally and comes back as
        # PASS carrying `rewritten`, so the branch below still sees it.
        if verdict.type is not VerdictType.PASS:
            blocked = annotate(
                task,
                CabalReason(
                    code=CODE_POLICY_VIOLATION,
                    message=f"guardrail blocked ({phase}): {verdict.reason}",
                ),
            )
            return target, blocked
        if verdict.rewritten is not None:
            return verdict.rewritten, None
        return target, None

    async def _apply_tool_guardrails(
        self,
        guardrails: list[Guardrail],
        target: Any,
        phase: str,
        task: Task,
        *,
        tool_name: str | None = None,
        tool_arguments: dict[str, Any] | None = None,
    ) -> tuple[Any, str | None]:
        """Run tool-boundary guardrails against ``target``.

        Unlike ``_apply_guardrails``, a BLOCK here does **not** fail the whole
        Task — the caller injects an error tool message and skips
        ``call_tool`` (or replaces the result string). This is pi's model for
        per-call policy: block one call, not the whole dispatch.

        ``tool_name`` and ``tool_arguments`` are threaded into the
        GuardrailContext so guardrails can identify which tool produced the
        target (cabal#43: ValidateBeforeRecord needs this to gate on mutating
        tools only).

        Returns ``(possibly-rewritten target, None)`` on PASS/REWRITE.
        Returns ``(original target, block_reason)`` on BLOCK.
        """
        if not guardrails:
            return target, None
        ctx = GuardrailContext(
            persona_name=self.name,
            task_id=task.id,
            phase=phase,
            tool_name=tool_name,
            tool_arguments=tool_arguments,
        )
        verdict = await run_guardrails(guardrails, target, ctx)
        # ANY NON-PASS IS A REFUSAL -- see _apply_guardrails above for why the
        # enumeration was a fail-open (#152 r1 Important 2).
        #
        # A TERMINATE STILL ONLY BLOCKS THE CALL HERE, not the Task. This is
        # pi's per-call policy boundary: BLOCK already means "refuse this tool
        # call and let the dispatch continue", and escalating a terminal verdict
        # into a whole-Task failure from inside the tool loop would be a larger
        # change than closing the fail-open. Refusing the call is the
        # fail-closed reading, and it is what the caller is built to handle.
        if verdict.type is not VerdictType.PASS:
            return target, verdict.reason
        if verdict.rewritten is not None:
            return verdict.rewritten, None
        return target, None

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

        # cabal#39 — input guardrail check before checkpoint/LLM call. Runs only
        # at first dispatch (not on resume): the checkpointed messages already
        # passed the guardrail, and re-running would double-apply a
        # non-idempotent rewrite once per resume.
        if resume_from is None:
            messages, blocked = await self._apply_guardrails(
                self.input_guardrails, messages, "input", task
            )
            if blocked is not None:
                self.session_tracker.end(
                    session_id=session_id,
                    summary=f"[blocked:input] {blocked.metadata.get('cabal_reason_message', '')}",
                )
                return blocked

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
                    call_name = call["name"]
                    call_args = call["arguments"]
                    # Unified per-call recording: every call — dispatched,
                    # rewritten, or blocked — fires exactly one TOOL_USE row with a
                    # coherent (result_content, is_error, latency_ms), so a policy
                    # denial is as visible in the audit log as a real call.
                    result_content: str
                    is_error = False
                    latency_ms = 0
                    dispatched = True

                    # cabal#52 — pre-tool guardrails. BLOCK skips call_tool (the
                    # call is recorded as an error result the LLM sees and reacts
                    # to; the Task is NOT failed). REWRITE patches name/arguments.
                    # NOTE: emitting the tool_call row *before* call_tool for
                    # crash-gap detection is deferred (out of scope for #52).
                    if self.tool_call_guardrails:
                        tc_target = {
                            "name": call_name,
                            "arguments": call_args,
                            "tool_call_id": call["id"],
                        }
                        patched, block_reason = await self._apply_tool_guardrails(
                            self.tool_call_guardrails,
                            tc_target,
                            "tool_call",
                            task,
                            tool_name=call_name,
                            tool_arguments=call_args,
                        )
                        if block_reason is not None:
                            result_content = f"tool call blocked by policy: {block_reason}"
                            is_error = True
                            dispatched = False
                        elif not (
                            isinstance(patched, dict)
                            and "name" in patched
                            and "arguments" in patched
                        ):
                            # A malformed REWRITE must not crash the whole Task
                            # (issue #52's thesis is "block one call, not the
                            # dispatch"). Treat it as a blocked call with a clear
                            # reason instead of letting a KeyError escape.
                            result_content = (
                                "tool call blocked: guardrail returned a malformed "
                                "rewrite (expected a dict with 'name' and 'arguments')"
                            )
                            is_error = True
                            dispatched = False
                        else:
                            # REWRITE: patched name/arguments; tool_call_id is pinned
                            # to call["id"] (it must match the assistant message) —
                            # a rewrite that changes it is intentionally ignored.
                            call_name = patched["name"]
                            call_args = patched["arguments"]

                    if dispatched:
                        t0 = time.monotonic()
                        outcome = await self.tool_proxy.call_tool(call_name, call_args)
                        latency_ms = round((time.monotonic() - t0) * 1000)
                        result_content = _outcome_to_chat_content(outcome)
                        is_error = getattr(outcome, "is_error", False)

                        # cabal#52 — post-tool guardrails. REWRITE replaces the
                        # result the LLM sees; BLOCK injects a policy-error string
                        # and marks the row is_error (the recorded content is an
                        # error). A REWRITE leaves is_error tracking the tool's
                        # actual outcome, not the rewritten content.
                        if self.tool_result_guardrails:
                            patched_result, block_reason = await self._apply_tool_guardrails(
                                self.tool_result_guardrails,
                                result_content,
                                "tool_result",
                                task,
                                tool_name=call_name,
                                tool_arguments=call_args,
                            )
                            if block_reason is not None:
                                result_content = f"tool result blocked by policy: {block_reason}"
                                is_error = True
                            else:
                                result_content = patched_result

                    result_truncated = len(result_content) > _RESULT_SUMMARY_MAX
                    await self.llm_client.hook_registry.fire(
                        HookName.TOOL_USE,
                        {
                            "task_id": task.id,
                            "tool_call_id": call["id"],
                            "tool_name": call_name,
                            "arguments": call_args,
                            "result_summary": result_content[:_RESULT_SUMMARY_MAX],
                            "result_truncated": result_truncated,
                            "is_error": is_error,
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
        try:
            truncated = self.check_truncation(response, text)
        except LLMTruncatedError as exc:
            self.session_tracker.end(session_id=session_id, summary=f"[failed] {exc}")
            await self.checkpoint_store.drop(task.id)
            return annotate(
                task,
                CabalReason(code=CODE_BUDGET_EXHAUSTED, message=str(exc)),
            )

        # cabal#39 — output guardrail check before committing the result.
        text, blocked = await self._apply_guardrails(
            self.output_guardrails, text, "output", task
        )
        if blocked is not None:
            self.session_tracker.end(
                session_id=session_id,
                summary=f"[blocked:output] {blocked.metadata.get('cabal_reason_message', '')}",
            )
            await self.checkpoint_store.drop(task.id)
            return blocked

        self.session_tracker.end(
            session_id=session_id,
            summary=f"[truncated] {text}" if truncated else text,
        )
        # Machine-readable, not only a summary string: a truncated-but-kept LEG
        # output otherwise completed with nothing a primus could see, and the
        # aggregator then summarised half an answer with no signal in the task.
        if truncated:
            task = task.model_copy(
                update={"metadata": {**task.metadata, "cabal_truncated": self.name}}
            )
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
            # cabal#59: use the `contextId` alias — Task has populate_by_name=False,
            # so `context_id=` silently drops to an ignored extra attr (the field
            # stays None). Every completed LLMPersona task dropped context_id before.
            contextId=task.context_id,
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
        # A LOCAL, not a field: one handler serves concurrent dispatches, so an
        # instance attribute would hold whatever the last stream wrote.
        terminal_reason: list[str | None] = [None]
        try:
            async for delta in self.llm_client.astream(
                model=self.model,
                messages=messages,
                agent=self.name,
                task_id=task.id,
                trace_id=task.metadata.get("cabal_trace_id"),
                span_id=task.metadata.get("cabal_span_id"),
                max_tokens=self.max_tokens,
                on_finish=lambda reason: terminal_reason.__setitem__(0, reason),
                **self.extra_kwargs,
            ):
                full_text += delta
                yield delta
        except Exception as exc:
            self.session_tracker.end(session_id=session_id, summary=f"[stream-failed] {exc}")
            raise

        # cabal#136 — the stream is over, so the same rule the non-streaming
        # path uses can finally be applied. A stream cut off at the ceiling
        # previously ended INDISTINGUISHABLY from a complete one, which is
        # worse than the non-streaming case: the operator has already watched
        # text arrive, so the absence of an error reads as a finished answer.
        try:
            truncated = classify_truncation(
                agent=self.name,
                finish_reason=terminal_reason[0],
                text=full_text,
                # A stream that produced tool calls is not something this path
                # can act on — astream_task runs no tool loop.
                has_executable_tool_calls=False,
            )
        except LLMTruncatedError as exc:
            self.session_tracker.end(session_id=session_id, summary=f"[failed] {exc}")
            raise
        self.session_tracker.end(
            session_id=session_id,
            summary=f"[truncated] {full_text}" if truncated else full_text,
        )
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
    """Turn a liminal ToolCallOutcome into a single string the model
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
