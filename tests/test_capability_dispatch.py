"""Tests for capability-based dispatch (cabal#3).

Two layers: router-level capability matching (unit tests with a
hand-rolled DispatchRouter), and the HTTP route doing late-binding
resolution via TaskRequest.required_capabilities.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from cabal.core.broker.router import DispatchRouter

# ---- router unit: select_by_capabilities ----------------------------------


def _make_router() -> DispatchRouter:
    """Construct a router with stub collaborators; only capability bits exercised."""
    return DispatchRouter(
        sessionmaker=MagicMock(),
        bookkeeper=MagicMock(),
        jwks_provider=MagicMock(),
    )


def test_select_returns_none_when_no_agents_registered() -> None:
    r = _make_router()
    assert r.select_by_capabilities(["pattern-lookup"]) is None


def test_select_returns_agent_with_matching_capability() -> None:
    r = _make_router()
    r.register_capabilities("architect", ["pattern-lookup", "architectural-advice"])
    r.register_capabilities("rook-primus", ["code-write"])
    assert r.select_by_capabilities(["pattern-lookup"]) == "architect"


def test_select_requires_superset_not_intersection() -> None:
    """Requesting [A, B] must match agents with {A, B, …}, not {A} alone."""
    r = _make_router()
    r.register_capabilities("partial", ["A"])
    r.register_capabilities("full", ["A", "B", "C"])
    assert r.select_by_capabilities(["A", "B"]) == "full"


def test_select_picks_deterministic_among_multiple_matches() -> None:
    """Sorted-by-name selection — stable across calls."""
    r = _make_router()
    r.register_capabilities("zeta", ["common"])
    r.register_capabilities("alpha", ["common"])
    r.register_capabilities("mu", ["common"])
    assert r.select_by_capabilities(["common"]) == "alpha"


def test_select_with_empty_requirement_returns_none() -> None:
    """Empty required-list is meaningless for capability-based dispatch."""
    r = _make_router()
    r.register_capabilities("anyone", ["a"])
    assert r.select_by_capabilities([]) is None


def test_select_ignores_blank_strings_in_requirement() -> None:
    r = _make_router()
    r.register_capabilities("anyone", ["a"])
    assert r.select_by_capabilities(["", "  "]) is None


def test_register_capabilities_is_idempotent() -> None:
    """Re-registering replaces (not unions) the capability set."""
    r = _make_router()
    r.register_capabilities("a", ["x", "y"])
    r.register_capabilities("a", ["z"])  # replaces
    assert r.capabilities_for("a") == {"z"}


def test_unregister_local_clears_capabilities() -> None:
    r = _make_router()
    r.register_capabilities("a", ["x"])
    r.unregister_local("a")
    assert r.capabilities_for("a") == set()


def test_capabilities_for_returns_independent_copy() -> None:
    """Caller mutating the returned set must not corrupt internal state."""
    r = _make_router()
    r.register_capabilities("a", ["x"])
    caps = r.capabilities_for("a")
    caps.add("y")
    assert r.capabilities_for("a") == {"x"}


# ---- TaskRequest schema --------------------------------------------------


def test_task_request_accepts_target_only() -> None:
    from cabal.core.a2a.types import Message, Task, TaskStatus
    from cabal.core.routes.tasks import TaskRequest

    task = Task(
        id="t1",
        status=TaskStatus(state="submitted", message=Message(role="user", parts=[])),
    )
    req = TaskRequest(target="archivist", task=task)
    assert req.target == "archivist"
    assert req.required_capabilities is None


def test_task_request_accepts_required_capabilities_only() -> None:
    from cabal.core.a2a.types import Message, Task, TaskStatus
    from cabal.core.routes.tasks import TaskRequest

    task = Task(
        id="t1",
        status=TaskStatus(state="submitted", message=Message(role="user", parts=[])),
    )
    req = TaskRequest(required_capabilities=["pattern-lookup"], task=task)
    assert req.target is None
    assert req.required_capabilities == ["pattern-lookup"]


# ---- identity.toml capabilities flow -------------------------------------


def test_persona_config_has_capabilities_default_empty() -> None:
    from cabal.config.identity import PersonaConfig

    cfg = PersonaConfig(role="singleton")
    assert cfg.capabilities == []


def test_persona_config_accepts_capabilities() -> None:
    from cabal.config.identity import PersonaConfig

    cfg = PersonaConfig(
        role="singleton",
        capabilities=["architectural-advice", "pattern-lookup"],
    )
    assert cfg.capabilities == ["architectural-advice", "pattern-lookup"]


def test_load_identity_round_trips_capabilities(tmp_path) -> None:
    from cabal.config.identity import load_identity

    path = tmp_path / "identity.toml"
    path.write_text(
        '[cabal]\n'
        'operator_id = "rich"\n'
        '\n'
        '[personas.architect]\n'
        'role = "singleton"\n'
        'capabilities = ["pattern-lookup", "architectural-advice"]\n'
    )
    identity = load_identity(path)
    assert identity.personas["architect"].capabilities == [
        "pattern-lookup", "architectural-advice"
    ]
