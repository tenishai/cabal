"""Built-in echo agent.

Phase 1 dev/test handler. Wired into the dispatch router on startup so
operators can verify the loop end-to-end with `cbl dispatch echo "hi"`.
"""

from __future__ import annotations

from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart


class EchoAgent:
    name: str = "echo"

    async def handle_task(self, task: Task) -> Task:
        original = ""
        if task.status.message and task.status.message.parts:
            first = task.status.message.parts[0]
            if isinstance(first, TextPart):
                original = first.text
        reply = Message(role="agent", parts=[TextPart(text=f"echo: {original}")])
        return Task(
            id=task.id,
            status=TaskStatus(state="completed", message=reply),
            history=task.history + ([task.status.message] if task.status.message else []),
            metadata=task.metadata,
        )
