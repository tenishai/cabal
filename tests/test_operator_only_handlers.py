"""documents.refresh and patterns.record are operator-only (cabal#233).

The handlers do not check scope themselves; liminal refuses the call when a
tool's card entry sets ``requires_operator``. These tests fail if the flag is
dropped from either card, or if liminal's card validation stops carrying it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("liminal.protocol")

from liminal.protocol.card import validate_card  # noqa: E402

from cabal.handlers.documents import DocumentsHandler  # noqa: E402
from cabal.handlers.patterns import PatternsHandler  # noqa: E402

OPERATOR_ONLY = {
    "documents": (DocumentsHandler, {"refresh"}),
    "patterns": (PatternsHandler, {"record"}),
}


def _flags(handler) -> dict[str, bool]:
    # model_dump keeps the key on any liminal: a typed field once liminal
    # declares it, an extra field (extra="allow") before that.
    card = validate_card(handler.capability_card())
    return {
        t.name: t.model_dump().get("requires_operator") is True for t in card.tools
    }


@pytest.mark.parametrize("name", sorted(OPERATOR_ONLY))
def test_operator_only_tools_carry_the_flag(name: str) -> None:
    cls, expected = OPERATOR_ONLY[name]
    handler = cls()
    flags = _flags(handler)
    assert {t for t, on in flags.items() if on} == expected
    # The card names the same tools the handler serves, so the flag lands on
    # the tool liminal dispatches rather than a stale name.
    assert expected <= {spec.name for spec in handler.tools()}
