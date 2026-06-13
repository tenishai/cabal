"""cabal#2 — CheckpointStore Protocol + SQLite impl tests."""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI

from cabal.core.checkpoint import (
    Checkpoint,
    NoOpCheckpointStore,
    SqliteCheckpointStore,
)


@pytest_asyncio.fixture
async def store(app: FastAPI) -> SqliteCheckpointStore:
    """A real SqliteCheckpointStore tied to the test app's DB."""
    return SqliteCheckpointStore(sessionmaker=app.state.sessionmaker)


def _sample_checkpoint(task_id: str = "task-1", step: int = 0) -> Checkpoint:
    return Checkpoint(
        task_id=task_id,
        step_index=step,
        messages=[
            {"role": "system", "content": "you are a helpful agent"},
            {"role": "user", "content": "hello"},
        ],
        task_json={"id": task_id, "metadata": {}},
        persona_state={"session_id": "s-1"},
    )


@pytest.mark.asyncio
async def test_save_then_load_roundtrip(store: SqliteCheckpointStore) -> None:
    ckpt = _sample_checkpoint()
    await store.save(ckpt)

    loaded = await store.load("task-1")

    assert loaded is not None
    assert loaded.task_id == "task-1"
    assert loaded.step_index == 0
    assert loaded.messages == ckpt.messages
    assert loaded.task_json == {"id": "task-1", "metadata": {}}
    assert loaded.persona_state == {"session_id": "s-1"}


@pytest.mark.asyncio
async def test_load_missing_returns_none(store: SqliteCheckpointStore) -> None:
    assert await store.load("never-existed") is None


@pytest.mark.asyncio
async def test_save_upserts_latest_step(store: SqliteCheckpointStore) -> None:
    """Latest write wins — resume needs only the most recent step."""
    await store.save(_sample_checkpoint("task-2", step=0))
    await store.save(
        Checkpoint(
            task_id="task-2",
            step_index=2,
            messages=[{"role": "user", "content": "step 2 state"}],
            task_json={"id": "task-2"},
        )
    )

    loaded = await store.load("task-2")

    assert loaded is not None
    assert loaded.step_index == 2
    assert loaded.messages == [{"role": "user", "content": "step 2 state"}]


@pytest.mark.asyncio
async def test_drop_removes_row(store: SqliteCheckpointStore) -> None:
    await store.save(_sample_checkpoint("task-3"))

    dropped = await store.drop("task-3")

    assert dropped is True
    assert await store.load("task-3") is None


@pytest.mark.asyncio
async def test_drop_missing_returns_false(store: SqliteCheckpointStore) -> None:
    assert await store.drop("never-existed") is False


@pytest.mark.asyncio
async def test_noop_store_does_nothing() -> None:
    store: NoOpCheckpointStore = NoOpCheckpointStore()
    # Calls return None / False without error — and there's no DB.
    await store.save(_sample_checkpoint())
    assert await store.load("task-1") is None
    assert await store.drop("task-1") is False


@pytest.mark.asyncio
async def test_app_state_exposes_default_store(app: FastAPI) -> None:
    """The lifespan wires app.state.checkpoint_store to a real impl so
    persona_factory + the admin route can pull it without re-constructing."""
    assert app.state.checkpoint_store is not None
    assert hasattr(app.state.checkpoint_store, "save")
    assert hasattr(app.state.checkpoint_store, "load")
    assert hasattr(app.state.checkpoint_store, "drop")
