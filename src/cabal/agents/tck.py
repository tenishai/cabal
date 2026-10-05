"""The `tck` persona: the agent the A2A compatibility kit runs against (#261).

The kit (a2aproject/a2a-tck) drives scripted behaviour through the incoming
message's `messageId` prefix (`scenarios/core_operations.feature` at
a2a-tck 263b9cf). The kit's own reference agents are generated from those
scenarios; this persona acts them out, so a kit run measures cabal's A2A
layer, not a persona's personality.

It is registered only when `$CABAL_TCK_PERSONA` is "1" (`cabal.core.app`),
which the kit's CI job sets. A real deployment never has it.

What it cannot act out, and why (each is in the kit baseline):
- `tck-message-response` asks for a direct Message instead of a Task; a
  persona returns a Task, so it completes with the same text instead.
"""

from __future__ import annotations

import base64

from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart

TCK_ENV = "CABAL_TCK_PERSONA"

_FILE_CONTENT = base64.b64encode(b"Generated file content").decode("ascii")


def _reply(text: str) -> Message:
    return Message(role="agent", parts=[TextPart(text=text)])


def _artifact(task_id: str, kind: str, part: dict[str, object]) -> dict[str, object]:
    return {"artifactId": f"{task_id}-{kind}", "parts": [part]}


class TckAgent:
    name: str = "tck"

    async def handle_task(self, task: Task) -> Task:
        incoming = task.status.message
        message_id = (incoming.message_id if incoming is not None else None) or ""
        history = [*task.history, *([incoming] if incoming is not None else [])]
        state = "completed"
        reply: Message | None = _reply("Hello from TCK")
        artifacts: list[dict[str, object]] = []

        # Longest prefix first: "tck-artifact-file-url" before "tck-artifact-file".
        if message_id.startswith("tck-artifact-text"):
            reply = None
            artifacts.append(
                _artifact(task.id, "text", {"kind": "text", "text": "Generated text content"})
            )
        elif message_id.startswith("tck-artifact-file-url"):
            reply = None
            artifacts.append(
                _artifact(
                    task.id,
                    "file-url",
                    {
                        "url": "https://example.com/output.txt",
                        "filename": "output.txt",
                        "mediaType": "text/plain",
                    },
                )
            )
        elif message_id.startswith("tck-artifact-file"):
            reply = None
            artifacts.append(
                _artifact(
                    task.id,
                    "file",
                    {"raw": _FILE_CONTENT, "filename": "output.txt", "mediaType": "text/plain"},
                )
            )
        elif message_id.startswith("tck-artifact-data"):
            reply = None
            artifacts.append(
                _artifact(task.id, "data", {"kind": "data", "data": {"key": "value", "count": 42}})
            )
        elif message_id.startswith("tck-message-response"):
            reply = _reply("Direct message response")
        elif message_id.startswith("tck-input-required"):
            state = "input-required"
            reply = _reply("input required")
        elif message_id.startswith("tck-reject-task"):
            state = "rejected"
            reply = _reply("rejected")

        return Task(
            id=task.id,
            contextId=task.context_id,
            status=TaskStatus(state=state, message=reply),  # type: ignore[arg-type]
            history=history,
            artifacts=artifacts,
            metadata=task.metadata,
        )
