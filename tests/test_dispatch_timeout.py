"""CABAL_DISPATCH_TIMEOUT_S resolution — the knob, its guardrails, and the
connect ceiling that deliberately does not follow it.
"""

from __future__ import annotations

import pytest

from cabal.core.app import DISPATCH_CONNECT_TIMEOUT_S, _dispatch_timeout


def test_default_preserves_the_historical_30s(monkeypatch):
    monkeypatch.delenv("CABAL_DISPATCH_TIMEOUT_S", raising=False)
    timeout = _dispatch_timeout()
    assert timeout.read == 30.0
    assert timeout.write == 30.0
    assert timeout.pool == 30.0


def test_env_override_raises_the_read_ceiling(monkeypatch):
    monkeypatch.setenv("CABAL_DISPATCH_TIMEOUT_S", "300")
    assert _dispatch_timeout().read == 300.0


def test_connect_stays_short_when_the_read_ceiling_is_raised(monkeypatch):
    """A slow peer gets minutes; an unreachable one still fails fast."""
    monkeypatch.setenv("CABAL_DISPATCH_TIMEOUT_S", "300")
    assert _dispatch_timeout().connect == DISPATCH_CONNECT_TIMEOUT_S


def test_connect_never_exceeds_a_lower_read_ceiling(monkeypatch):
    monkeypatch.setenv("CABAL_DISPATCH_TIMEOUT_S", "2")
    assert _dispatch_timeout().connect == 2.0


@pytest.mark.parametrize("bad", ["abc", "", "30s"])
def test_unparseable_value_is_a_clear_config_error(monkeypatch, bad):
    monkeypatch.setenv("CABAL_DISPATCH_TIMEOUT_S", bad)
    with pytest.raises(ValueError, match="CABAL_DISPATCH_TIMEOUT_S"):
        _dispatch_timeout()


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_non_positive_value_is_rejected(monkeypatch, bad):
    monkeypatch.setenv("CABAL_DISPATCH_TIMEOUT_S", bad)
    with pytest.raises(ValueError, match="must be > 0"):
        _dispatch_timeout()


@pytest.mark.parametrize("bad", ["nan", "inf", "1e400"])
def test_non_finite_value_is_rejected(monkeypatch, bad):
    """'nan' <= 0 is False and 'inf'/'1e400' (which overflows to inf) are both
    > 0, so the plain non-positive guard lets all three through; only an
    explicit finiteness check catches them.
    """
    monkeypatch.setenv("CABAL_DISPATCH_TIMEOUT_S", bad)
    with pytest.raises(ValueError, match="must be finite"):
        _dispatch_timeout()


def test_negative_infinity_stays_rejected_by_the_non_positive_guard(monkeypatch):
    """'-inf' is already caught by the '<= 0' check; confirm the new
    finiteness check doesn't change that outcome or its error message.
    """
    monkeypatch.setenv("CABAL_DISPATCH_TIMEOUT_S", "-inf")
    with pytest.raises(ValueError, match="must be > 0"):
        _dispatch_timeout()

