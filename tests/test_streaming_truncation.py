"""The streaming path checks finish_reason too (cabal#136).

#134/#135 fixed the three non-streaming call sites. `astream_task` was the
fourth and could not check at all: `astream` yields plain strings, so the
terminal `finish_reason` — which rides the final chunk — was discarded inside
the client and never reached the persona.

Worse than the non-streaming case in one respect: on a truncated stream the
operator has already watched text arrive, so the absence of an error reads as a
finished answer rather than an empty one.
"""

from __future__ import annotations

import pytest

from cabal.agents.subordinate import Subordinate
from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.hooks.registry import HookRegistry
from cabal.core.llm.litellm_client import LiteLLMClient, LLMTruncatedError


def _task(tid: str = "t-1") -> Task:
    return Task(
        id=tid,
        status=TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text="stream me")]),
        ),
    )


def _chunk(text: str | None = None, finish_reason: str | None = None) -> dict:
    return {"choices": [{"delta": {"content": text}, "finish_reason": finish_reason}]}


def _streaming_client(*chunks: dict) -> LiteLLMClient:
    """A client whose provider call returns the given chunks in order."""

    async def fn(**kwargs):
        assert kwargs.get("stream") is True, "astream must set stream=True"

        class _Stream:
            def __aiter__(self):
                async def gen():
                    for c in chunks:
                        yield c

                return gen()

        return _Stream()

    return LiteLLMClient(acompletion_fn=fn, hook_registry=HookRegistry())


class _RecordingTracker:
    """Captures session summaries — the only place the streaming path records
    that an answer was cut off, and therefore the only thing that can tell a
    flagged run from an unflagged one."""

    def __init__(self) -> None:
        self.summaries: list[str] = []

    def start(self, *, external_id: str, agent_identity: str) -> str:
        return external_id

    def end(self, *, session_id: str, summary: str) -> None:
        self.summaries.append(summary)


def _persona(client: LiteLLMClient, tracker: object | None = None) -> Subordinate:
    return Subordinate(
        "wright",
        "implementer",
        client,
        model="m",
        session_tracker=tracker,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_a_stream_cut_off_with_no_content_raises() -> None:
    """THE DEFECT on this path: it ended indistinguishably from a clean one."""
    p = _persona(_streaming_client(_chunk(None, None), _chunk(None, "length")))

    with pytest.raises(LLMTruncatedError, match="output ceiling"):
        async for _ in p.astream_task(_task()):
            pass


@pytest.mark.asyncio
async def test_a_stream_cut_off_mid_answer_keeps_the_text() -> None:
    """Deltas already reached the operator, so the partial answer is kept —
    and the session summary is what records that it was cut off, so it is
    asserted rather than merely described."""
    tracker = _RecordingTracker()
    p = _persona(
        _streaming_client(_chunk("half an "), _chunk("answer"), _chunk(None, "length")),
        tracker,
    )

    out = [d async for d in p.astream_task(_task())]

    assert "".join(out) == "half an answer"
    assert tracker.summaries == ["[truncated] half an answer"], tracker.summaries


@pytest.mark.asyncio
async def test_a_complete_stream_is_untouched() -> None:
    """Guards the guard: a healthy stream must not start failing."""
    p = _persona(_streaming_client(_chunk("all "), _chunk("done"), _chunk(None, "stop")))

    out = [d async for d in p.astream_task(_task())]

    assert "".join(out) == "all done"


@pytest.mark.asyncio
async def test_an_empty_stream_that_stopped_is_not_silently_fine() -> None:
    """`stop` with nothing streamed: litellm collapses provider ERROR,
    MALFORMED_RESPONSE and network_error onto `stop`, so this is the shape a
    failed generation arrives in. It does not raise — the classifier keeps it
    completable — but it is FLAGGED rather than passing as a clean run.

    The flag is the whole assertion. Asserting `out == []` holds whether or not
    the empty-plus-`stop` arm flags anything, so the first version left this
    arm of the streaming path with no coverage at all: a mutation turning it
    off was caught only by the NON-streaming tests, which exercise a different
    call site.
    """
    tracker = _RecordingTracker()
    p = _persona(_streaming_client(_chunk(None, "stop")), tracker)

    out = [d async for d in p.astream_task(_task())]

    assert out == []
    assert tracker.summaries == ["[truncated] "], tracker.summaries


@pytest.mark.asyncio
async def test_an_empty_stream_with_no_reason_at_all_raises() -> None:
    """A provider that ends the stream saying nothing about why."""
    p = _persona(_streaming_client(_chunk(None, None)))

    # match= pins WHICH arm fired: without it this passes on the `length`
    # message too, and the two are different failures.
    with pytest.raises(LLMTruncatedError, match="not a reason cabal treats"):
        async for _ in p.astream_task(_task()):
            pass


@pytest.mark.asyncio
async def test_the_reason_is_taken_from_the_last_chunk_that_has_one() -> None:
    """finish_reason is None on every chunk but the last, so the client keeps
    the last NON-NULL value rather than the last value.

    Both readings KEEP the text, so asserting on the text cannot tell them
    apart — the mutation proved exactly that. The difference is only visible in
    the flag: last-non-null sees `length` and marks the summary [truncated];
    last-value sees the trailing None and marks nothing.
    """
    tracker = _RecordingTracker()
    p = _persona(
        _streaming_client(
            _chunk("text", None), _chunk(None, "length"), _chunk(None, None)
        ),
        tracker,
    )

    out = [d async for d in p.astream_task(_task())]

    assert "".join(out) == "text"  # kept: truncated-with-content
    assert tracker.summaries == ["[truncated] text"], tracker.summaries


@pytest.mark.asyncio
async def test_a_complete_stream_is_not_flagged() -> None:
    """Guards the guard: the flag must not appear on a healthy stream."""
    tracker = _RecordingTracker()
    p = _persona(_streaming_client(_chunk("all done"), _chunk(None, "stop")), tracker)

    async for _ in p.astream_task(_task()):
        pass

    assert tracker.summaries == ["all done"], tracker.summaries


def test_both_paths_share_one_classifier(monkeypatch) -> None:
    """The rule lives in the client module precisely so the streaming path can
    reach it; a second copy on the persona is how the two drift apart.

    Asserted by BEHAVIOUR, not by reading the source: a source-substring check
    passes just as happily when the delegation is dead code or its arguments
    are wrong. Replacing the shared function must change what the persona does.
    """
    import cabal.agents.llm_persona as lp

    calls: list[dict] = []

    def _spy(**kwargs):
        calls.append(kwargs)
        return False

    monkeypatch.setattr(lp, "classify_truncation", _spy)
    p = _persona(_streaming_client(_chunk("x")))
    assert p.check_truncation({"choices": [{"finish_reason": "length"}]}, "text") is False
    assert calls, "the persona did not go through the shared classifier"
    assert calls[0]["finish_reason"] == "length"
    assert calls[0]["text"] == "text"


@pytest.mark.asyncio
async def test_a_malformed_tool_call_array_does_not_escape_handle_task() -> None:
    """REVIEW, Important 1 — a regression the refactor introduced.

    `_extract_tool_calls` is not internally total: a `function` object with no
    `name`, or a non-dict entry, raises. Lifting the call into
    `classify_truncation(...)`'s argument list put it outside the guard that
    had covered it, so a bare KeyError escaped `handle_task` — which catches
    only `LLMTruncatedError` — and the session was never ended.

    Reachable with no tool proxy at all: the tool loop is gated on
    `tool_proxy is not None`, so `check_truncation` is then the FIRST caller.
    """
    tracker = _RecordingTracker()

    async def fn(**kwargs):
        return {
            "choices": [
                {
                    "message": {
                        "content": "an answer",
                        # a `function` with no `name`, and a non-dict entry
                        "tool_calls": [{"id": "1", "function": {}}, "not-a-dict"],
                    },
                    "finish_reason": "stop",
                }
            ]
        }

    client = LiteLLMClient(acompletion_fn=fn, hook_registry=HookRegistry())
    p = Subordinate(
        "wright", "implementer", client, model="m", session_tracker=tracker
    )
    assert p.tool_proxy is None, "the guard must be reachable with no tool proxy"

    out = await p.handle_task(_task("t-malformed"))

    assert out.status.state == "completed", out.status.state
    assert tracker.summaries, "the session must be ended, not abandoned mid-flight"
