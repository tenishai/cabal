"""The `cabal_` metadata prefix is cabal's own on a task that arrives from the wire (#401).

Callers' metadata reaches personas verbatim, so a caller could set keys cabal acts on:
`cabal_token_budget` raised its own token budget, `cabal_parent_task` placed its task under
any task in the AbortGraph, and `cabal_session_started` skipped the session hooks. Where a
task enters (POST /a2a/tasks, JSON-RPC message/send and message/stream, v1 SendMessage),
every `cabal_*` key a caller may not declare is dropped. cabal stamps its own keys after
this point. `_public_metadata` already reserves the prefix on the way back out.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

PREFIX = "cabal_"

#: The `cabal_*` keys a caller may declare. Depth only ever tightens the bound a chain gets
#: (an unreadable value counts as the top, guardrails_builtin.delegation_depth), and an
#: external chain is bounded only if the caller declares it. A trace id is continuity.
#: A not-before time (issue 406) only ever delays the caller's own task; A2A v1
#: SendMessage acts on it, and the router refuses it on every other path.
CALLER_DECLARED = frozenset({"cabal_delegation_depth", "cabal_trace_id", "cabal_not_before"})


def inbound_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """`metadata` as cabal accepts it from a caller: other keys unchanged, and every
    `cabal_*` key outside CALLER_DECLARED dropped, with one log line naming them."""
    if not metadata:
        return {}
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in metadata.items():
        if isinstance(key, str) and key.startswith(PREFIX) and key not in CALLER_DECLARED:
            dropped.append(key)
            continue
        kept[key] = value
    if dropped:
        log.info("dropped caller-supplied metadata keys reserved for cabal: %s", ", ".join(sorted(dropped)))
    return kept
