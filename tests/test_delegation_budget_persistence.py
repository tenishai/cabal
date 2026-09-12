"""cabal#174 — the delegation budget must survive a restart.

cabal#146's incident did not end when the budget was exhausted; it ended with
the agent liveness-killed, restarted, "resumed from persisted state, and
rebuilt the storm". Task state persisted. The counter did not, because it was
a dict on the guard instance. So the bound was 40 delegations per conversation
PER PROCESS LIFETIME, and a kill/restart/resume cycle re-armed it.

Every test here uses a REAL SqliteDelegationBudgetStore against the test app's
real database. A mocked store would prove the guard calls a method, which is
not the property in question -- the property is that a NEW guard instance,
standing in for the process that came back, refuses where the old one left off.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from fastapi import FastAPI

from cabal.core.a2a.types import Message, Task, TaskStatus, TextPart
from cabal.core.budget_store import (
    NoOpDelegationBudgetStore,
    SqliteDelegationBudgetStore,
)
from cabal.core.guardrails import GuardrailContext, VerdictType
from cabal.core.guardrails_builtin import DelegationBudgetGuardrail


@pytest_asyncio.fixture
async def store(app: FastAPI) -> SqliteDelegationBudgetStore:
    return SqliteDelegationBudgetStore(sessionmaker=app.state.sessionmaker)


def _task(context_id: str | None = "ctx-1", task_id: str = "t-1") -> Task:
    kwargs: dict = {
        "id": task_id,
        "status": TaskStatus(
            state="submitted",
            message=Message(role="user", parts=[TextPart(text="q")]),
        ),
        "metadata": {"cabal_target_agent": "target"},
    }
    if context_id is not None:
        kwargs["contextId"] = context_id
    return Task(**kwargs)


def _ctx() -> GuardrailContext:
    return GuardrailContext(persona_name="caller", task_id="t-1", phase="router")


async def _drain() -> None:
    """Let the write-behind tasks run.

    `_persist` schedules rather than awaits, on purpose -- `check()` must not
    await. So a test that reads the table immediately after `check()` returns
    is racing the write it is asserting about. One `sleep(0)` yields to the
    loop; the writes are a single upsert each.
    """
    for _ in range(10):
        await asyncio.sleep(0)


# --- THE ONE THAT MATTERS ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_restart_does_not_re_arm_the_conversation_budget(
    store: SqliteDelegationBudgetStore,
) -> None:
    """A new guard instance refuses where the old one left off.

    This is the incident in a test: spend the budget, throw the guard away
    (the process died), build a fresh one (it came back), and the FIRST ask on
    the same context_id must be refused -- not granted a new 40.
    """
    limit = 3
    before = DelegationBudgetGuardrail(limit=limit, store=store)
    for _ in range(limit):
        assert (await before.check(_task(), _ctx())).type is VerdictType.PASS
    assert (await before.check(_task(), _ctx())).type is VerdictType.TERMINATE
    await _drain()

    # The process dies here. Nothing is carried over but the database.
    del before
    after = DelegationBudgetGuardrail(limit=limit, store=store)
    assert await after.hydrate() == 1

    verdict = await after.check(_task(), _ctx())
    assert verdict.type is VerdictType.TERMINATE, (
        "a restart re-armed the budget: the resumed conversation was granted a "
        "fresh allowance, which is how the #146 storm was rebuilt"
    )


@pytest.mark.asyncio
async def test_without_a_store_a_restart_still_re_arms(
    store: SqliteDelegationBudgetStore,
) -> None:
    """THE CONTROL. Without it the test above proves only that some code ran.

    A guard on the default no-op store must show the OLD behaviour, so the
    passing assertion above is attributable to persistence and not to a shared
    class attribute, a cache, or a mistake in the fixture.
    """
    limit = 3
    before = DelegationBudgetGuardrail(limit=limit)  # no store
    for _ in range(limit):
        await before.check(_task(context_id="ctx-control"), _ctx())
    assert (
        await before.check(_task(context_id="ctx-control"), _ctx())
    ).type is VerdictType.TERMINATE
    await _drain()

    after = DelegationBudgetGuardrail(limit=limit)
    assert await after.hydrate() == 0
    assert (
        await after.check(_task(context_id="ctx-control"), _ctx())
    ).type is VerdictType.PASS

    # And the no-op store wrote nothing, so the real store cannot see it either.
    real = DelegationBudgetGuardrail(limit=limit, store=store)
    assert await real.hydrate() == 0


# --- the clock, which is why the scope is what it is -------------------------


@pytest.mark.asyncio
async def test_a_caller_keyed_budget_is_not_persisted(
    store: SqliteDelegationBudgetStore,
) -> None:
    """A windowed entry's window_start sits on `time.monotonic`, which resets
    on restart, so restoring one would measure a count against an origin that
    no longer exists. It must not be written at all."""
    guard = DelegationBudgetGuardrail(limit=3, store=store)
    for _ in range(3):
        # context_id absent -> falls back to the caller key, windowed.
        await guard.check(_task(context_id=None), _ctx())
    await _drain()

    assert await store.load_recent(limit=100, retention_s=3600.0) == []


@pytest.mark.asyncio
async def test_a_hydrated_conversation_key_still_never_refills(
    store: SqliteDelegationBudgetStore,
) -> None:
    """A restored conversation key must keep the no-refund property, not
    acquire a window because hydrate stamped a fresh timestamp on it."""
    guard = DelegationBudgetGuardrail(limit=2, caller_window_seconds=0.01, store=store)
    for _ in range(2):
        await guard.check(_task(), _ctx())
    await _drain()

    after = DelegationBudgetGuardrail(
        limit=2, caller_window_seconds=0.01, store=store
    )
    await after.hydrate()
    await asyncio.sleep(0.05)  # well past the caller window
    assert (await after.check(_task(), _ctx())).type is VerdictType.TERMINATE


# --- the store's own contract ------------------------------------------------


@pytest.mark.asyncio
async def test_the_stored_count_never_decreases(
    store: SqliteDelegationBudgetStore,
) -> None:
    """The in-memory LRU eviction is a known refill path. If a write-behind
    could lower the stored count, an eviction would wipe the durable record
    too -- re-opening the hole this table closes."""
    await store.bump("ctx-mono", 40)
    await store.bump("ctx-mono", 1)

    rows = dict(await store.load_recent(limit=100, retention_s=3600.0))
    assert rows["ctx-mono"] == 40


@pytest.mark.asyncio
async def test_retention_prunes_a_stale_row(
    store: SqliteDelegationBudgetStore,
) -> None:
    await store.bump("ctx-old", 5)
    assert await store.load_recent(limit=100, retention_s=3600.0) != []

    # retention_s smaller than the row's age -> pruned, and pruned for good.
    assert await store.load_recent(limit=100, retention_s=-1.0) == []
    assert await store.load_recent(limit=100, retention_s=3600.0) == []


@pytest.mark.asyncio
async def test_load_recent_returns_oldest_first(
    store: SqliteDelegationBudgetStore,
) -> None:
    """The caller replays these into an insertion-ordered LRU, so the LAST one
    inserted must be the most recently used. Reversed, the most active
    conversation would be first in line for eviction."""
    for key in ("ctx-a", "ctx-b", "ctx-c"):
        await store.bump(key, 1)
        await asyncio.sleep(0.01)  # distinct updated_at

    keys = [k for k, _ in await store.load_recent(limit=100, retention_s=3600.0)]
    assert keys == ["ctx-a", "ctx-b", "ctx-c"]


@pytest.mark.asyncio
async def test_hydrate_is_bounded_by_the_lru_ceiling(
    store: SqliteDelegationBudgetStore,
) -> None:
    """Loading more keys than the in-memory ceiling would evict what it just
    restored, so hydrate must ask for at most that many."""
    guard = DelegationBudgetGuardrail(limit=3, store=store)
    guard._maxsize = 2
    for key in ("ctx-1", "ctx-2", "ctx-3"):
        await store.bump(key, 3)
        await asyncio.sleep(0.01)

    assert await guard.hydrate() == 2
    assert len(guard._counts) == 2
    # The newest survive; the oldest is the one dropped.
    assert set(guard._counts) == {"ctx-2", "ctx-3"}


# --- failure modes -----------------------------------------------------------


@pytest.mark.asyncio
async def test_hydrate_survives_a_broken_store() -> None:
    """A broken store must leave the guard in its pre-#174 state -- a weaker
    bound, but a working one -- rather than take startup with it."""

    class Broken(NoOpDelegationBudgetStore):
        async def load_recent(self, *, limit: int, retention_s: float):
            raise RuntimeError("table is gone")

    guard = DelegationBudgetGuardrail(limit=2, store=Broken())
    assert await guard.hydrate() == 0

    for _ in range(2):
        assert (await guard.check(_task(), _ctx())).type is VerdictType.PASS
    assert (await guard.check(_task(), _ctx())).type is VerdictType.TERMINATE


@pytest.mark.asyncio
async def test_check_does_not_await_the_store() -> None:
    """THE ATOMICITY INVARIANT, asserted rather than trusted.

    `check()` must remain await-free: a primus fan-out calls it concurrently
    across legs sharing one instance, and being await-free is what makes the
    counter mutation atomic on a cooperative loop. A store whose write never
    completes must therefore not delay a verdict by one microsecond.
    """
    started = asyncio.Event()

    class Hangs(NoOpDelegationBudgetStore):
        async def bump(self, session_key: str, count: int) -> None:
            started.set()
            await asyncio.Event().wait()  # never returns

    guard = DelegationBudgetGuardrail(limit=5, store=Hangs())
    verdict = await asyncio.wait_for(guard.check(_task(), _ctx()), timeout=1.0)
    assert verdict.type is VerdictType.PASS

    # And the write really was scheduled -- otherwise this test would pass on a
    # guard that had simply stopped persisting.
    await _drain()
    assert started.is_set()
    assert len(guard._writes) == 1


@pytest.mark.asyncio
async def test_a_negative_retention_is_refused_at_construction() -> None:
    """A non-positive retention prunes every row on every hydrate, silently
    restoring the pre-#174 behaviour while looking configured."""
    with pytest.raises(ValueError, match="retention_s must be > 0"):
        DelegationBudgetGuardrail(limit=2, retention_s=0)


@pytest.mark.asyncio
async def test_the_app_wires_a_real_store_into_the_router_guard(
    app: FastAPI,
) -> None:
    """THE WIRING, asserted separately from the behaviour.

    Every test above builds its own guard and hands it a store. That proves the
    mechanism and says nothing about production: a guard that persists in a unit
    test and silently receives the no-op store from `create_app` would pass all
    of them while leaving the hole exactly where cabal#174 found it.
    """
    guards = app.state.dispatch_router._input_guardrails
    budget = [g for g in guards if isinstance(g, DelegationBudgetGuardrail)]
    assert budget, (
        "no DelegationBudgetGuardrail on the router; got "
        f"{[type(g).__name__ for g in guards]}"
    )
    assert isinstance(budget[0]._store, SqliteDelegationBudgetStore), (
        "the router's budget guard got a no-op store, so the bound does not "
        "survive a restart in the app -- only in these tests"
    )
