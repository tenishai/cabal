"""Tests for the Archivist MVP — schema + dedup + search + MCP handler (cabal#9)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("liminal.protocol")

from liminal.protocol import Context  # noqa: E402

from cabal.archivist import ArchivistStore, Entry  # noqa: E402
from cabal.handlers.archivist import ArchivistConfig, ArchivistHandler  # noqa: E402


@pytest.fixture
def archivist_db(tmp_path: Path) -> Path:
    return tmp_path / "archivist.db"


@pytest.fixture
def store(archivist_db: Path) -> ArchivistStore:
    return ArchivistStore(archivist_db)


# ---- Entry + content_hash ------------------------------------------------


def test_entry_content_hash_is_stable() -> None:
    a = Entry(resource_name="x", summary="hello", entry_type="fact")
    b = Entry(resource_name="x", summary="hello", entry_type="fact")
    assert a.compute_hash() == b.compute_hash()


def test_entry_content_hash_changes_on_summary_change() -> None:
    a = Entry(resource_name="x", summary="hello", entry_type="fact")
    b = Entry(resource_name="x", summary="goodbye", entry_type="fact")
    assert a.compute_hash() != b.compute_hash()


def test_entry_content_hash_changes_on_entry_type_change() -> None:
    a = Entry(resource_name="x", summary="hi", entry_type="fact")
    b = Entry(resource_name="x", summary="hi", entry_type="decision")
    assert a.compute_hash() != b.compute_hash()


# ---- publish: L0 / L1 / Tier-1 paths -------------------------------------


def test_publish_first_entry_is_new(store: ArchivistStore) -> None:
    result = store.publish(Entry(
        resource_name="gke-prod-cluster",
        summary="Cluster is in us-east-1.",
        entry_type="fact",
    ))
    assert result.outcome == "new"
    assert result.related_ids == []
    fetched = store.get(result.entry_id)
    assert fetched is not None
    assert fetched.resource_name == "gke-prod-cluster"


def test_publish_same_content_hits_l0(store: ArchivistStore) -> None:
    """First call writes; second call (same hash) hits L0 in-memory cache."""
    a = store.publish(Entry(
        resource_name="r", summary="same content", entry_type="fact",
    ))
    b = store.publish(Entry(
        resource_name="r", summary="same content", entry_type="fact",
    ))
    assert a.outcome == "new"
    assert b.outcome == "duplicate_l0"
    assert b.entry_id == a.entry_id


def test_publish_same_content_in_fresh_store_hits_l1(
    archivist_db: Path,
) -> None:
    """Two store instances over the same DB — second one sees L1 hit."""
    store_a = ArchivistStore(archivist_db)
    first = store_a.publish(Entry(
        resource_name="r", summary="abc", entry_type="fact",
    ))
    # Fresh store has empty L0 cache; same DB → L1 should hit
    store_b = ArchivistStore(archivist_db)
    second = store_b.publish(Entry(
        resource_name="r", summary="abc", entry_type="fact",
    ))
    assert first.outcome == "new"
    assert second.outcome == "duplicate_l1"
    assert second.entry_id == first.entry_id


def test_publish_resource_overlap_is_related(store: ArchivistStore) -> None:
    """Two entries with same resource_name + entry_type but different
    summaries → second one is `related` and links via RELATED edges."""
    first = store.publish(Entry(
        resource_name="payment-api",
        summary="Service runs on port 8080.",
        entry_type="fact",
    ))
    second = store.publish(Entry(
        resource_name="payment-api",
        summary="Service uses Postgres.",
        entry_type="fact",
    ))
    assert first.outcome == "new"
    assert second.outcome == "related"
    assert first.entry_id in second.related_ids

    edges = store.list_related(second.entry_id)
    assert any(e["relation_type"] == "RELATED" for e in edges)


def test_publish_different_resource_is_new(store: ArchivistStore) -> None:
    store.publish(Entry(
        resource_name="payment-api", summary="x", entry_type="fact",
    ))
    second = store.publish(Entry(
        resource_name="auth-api", summary="x", entry_type="fact",
    ))
    assert second.outcome == "new"
    assert second.related_ids == []


# ---- search --------------------------------------------------------------


def test_search_finds_by_summary(store: ArchivistStore) -> None:
    store.publish(Entry(
        resource_name="r1", summary="circuit breaker pattern saves us", entry_type="pattern",
    ))
    store.publish(Entry(
        resource_name="r2", summary="postgres replication is hard", entry_type="decision",
    ))
    hits = store.search("circuit breaker")
    assert len(hits) >= 1
    assert any("circuit breaker" in h.summary for h in hits)


def test_search_filters_by_entry_type(store: ArchivistStore) -> None:
    store.publish(Entry(
        resource_name="r1", summary="kubernetes restart strategy", entry_type="decision",
    ))
    store.publish(Entry(
        resource_name="r2", summary="kubernetes restart issue", entry_type="error",
    ))
    decisions_only = store.search("kubernetes", entry_type="decision")
    assert all(e.entry_type == "decision" for e in decisions_only)


def test_search_filters_by_tags(store: ArchivistStore) -> None:
    store.publish(Entry(
        resource_name="r1", summary="cache problem", entry_type="error",
        tags=["redis", "production"],
    ))
    store.publish(Entry(
        resource_name="r2", summary="cache problem", entry_type="error",
        tags=["staging"],
    ))
    prod_only = store.search("cache", tags=["production"])
    assert len(prod_only) == 1
    assert "production" in prod_only[0].tags


def test_search_returns_empty_on_no_match(store: ArchivistStore) -> None:
    store.publish(Entry(
        resource_name="r1", summary="something", entry_type="fact",
    ))
    assert store.search("nothing-here-at-all") == []


def test_search_returns_empty_on_blank_query(store: ArchivistStore) -> None:
    store.publish(Entry(
        resource_name="r1", summary="something", entry_type="fact",
    ))
    assert store.search("   ") == []


# ---- gotchas + commands fields ------------------------------------------


def test_entry_round_trip_preserves_gotchas_and_commands(store: ArchivistStore) -> None:
    result = store.publish(Entry(
        resource_name="jwt-mount",
        summary="Adding cluster to JWT mount.",
        entry_type="procedure",
        gotchas=(
            "1) One JWT mount = one OIDC URL — adding cluster OVERWRITES existing. "
            "2) ESO secret sync breaks when TLS cert mismatch."
        ),
        commands=["vault auth enable jwt", "vault write auth/jwt/config ..."],
        tags=["vault", "jwt", "production"],
    ))
    fetched = store.get(result.entry_id)
    assert fetched is not None
    assert "OVERWRITES" in fetched.gotchas
    assert "vault auth enable jwt" in fetched.commands
    assert "vault" in fetched.tags


# ---- schema -------------------------------------------------------------


def test_schema_reports_counts_by_type(store: ArchivistStore) -> None:
    store.publish(Entry(
        resource_name="r1", summary="a", entry_type="decision",
    ))
    store.publish(Entry(
        resource_name="r2", summary="b", entry_type="decision",
    ))
    store.publish(Entry(
        resource_name="r3", summary="c", entry_type="error",
    ))
    schema = store.schema()
    assert schema["counts"]["total_active"] == 3
    assert schema["counts"]["by_type"]["decision"] == 2
    assert schema["counts"]["by_type"]["error"] == 1


def test_schema_lists_sample_resources(store: ArchivistStore) -> None:
    store.publish(Entry(
        resource_name="alpha", summary="a", entry_type="fact",
    ))
    store.publish(Entry(
        resource_name="beta", summary="b", entry_type="fact",
    ))
    samples = store.schema()["sample_resources"]
    assert "alpha" in samples
    assert "beta" in samples


# ---- MCP handler --------------------------------------------------------


def _ctx() -> Context:
    return Context(identity="archivist", agent_name="archivist", credentials={}, extra={})


@pytest.fixture
def archivist_handler(archivist_db: Path) -> ArchivistHandler:
    return ArchivistHandler(
        default_config=ArchivistConfig(db_path=archivist_db),
    )


@pytest.mark.asyncio
async def test_handler_publish_then_search(archivist_handler) -> None:
    pub = await archivist_handler.dispatch(
        "publish",
        {
            "resource_name": "gke-cluster",
            "summary": "Cluster is in us-east-1, zone a.",
            "entry_type": "fact",
            "tags": ["gke", "production"],
        },
        _ctx(),
    )
    assert pub.is_error is False
    pub_data = pub.content[0]["data"]
    assert pub_data["outcome"] == "new"
    assert pub_data["entry_id"]

    search = await archivist_handler.dispatch(
        "search", {"query": "us-east-1"}, _ctx(),
    )
    assert search.is_error is False
    hits = search.content[0]["data"]["hits"]
    assert any("us-east-1" in h["summary"] for h in hits)


@pytest.mark.asyncio
async def test_handler_search_takes_a_natural_language_question(archivist_handler) -> None:
    # cabal#356: this raised sqlite3.OperationalError out of dispatch.
    await archivist_handler.dispatch(
        "publish",
        {"resource_name": "host-a", "summary": "The build host is host-a.local.", "entry_type": "fact"},
        _ctx(),
    )
    result = await archivist_handler.dispatch(
        "search", {"query": "what's the build host?"}, _ctx(),
    )
    assert result.is_error is False
    result = await archivist_handler.dispatch("search", {"query": "host-a.local"}, _ctx())
    assert [h["resource_name"] for h in result.content[0]["data"]["hits"]] == ["host-a"]


@pytest.mark.asyncio
async def test_handler_database_error_is_a_failure(archivist_handler, monkeypatch) -> None:
    import sqlite3

    def locked(self, *a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ArchivistStore, "search", locked)
    result = await archivist_handler.dispatch("search", {"query": "x"}, _ctx())
    assert result.is_error is True
    assert "database is locked" in result.error


@pytest.mark.asyncio
async def test_handler_get_marks_a_superseded_entry(archivist_handler) -> None:
    # cabal#357: get served a retired entry with no status and no pointer
    # to its replacement.
    async def publish(summary: str, **extra):
        result = await archivist_handler.dispatch(
            "publish",
            {"resource_name": "host-a", "summary": summary, "entry_type": "fact", **extra},
            _ctx(),
        )
        return result.content[0]["data"]["entry_id"]

    old = await publish("host-a is at .15")
    new = await publish("host-a is at .16", supersede=True)

    got_old = (await archivist_handler.dispatch("get", {"entry_id": old}, _ctx())).content[0]["data"]
    got_new = (await archivist_handler.dispatch("get", {"entry_id": new}, _ctx())).content[0]["data"]

    assert (got_old["entry"]["status"], got_old["superseded_by"]) == ("superseded", [new])
    assert (got_new["entry"]["status"], got_new["superseded_by"]) == ("active", [])


@pytest.mark.asyncio
async def test_handler_publish_rejects_unknown_entry_type(archivist_handler) -> None:
    result = await archivist_handler.dispatch(
        "publish",
        {
            "resource_name": "x", "summary": "y", "entry_type": "not-a-type",
        },
        _ctx(),
    )
    assert result.is_error is True
    assert "entry_type" in result.error


@pytest.mark.asyncio
async def test_handler_publish_rejects_missing_required(archivist_handler) -> None:
    result = await archivist_handler.dispatch(
        "publish", {"resource_name": "x", "entry_type": "fact"}, _ctx(),
    )
    assert result.is_error is True
    assert "summary" in result.error


@pytest.mark.asyncio
async def test_handler_get_returns_entry_with_relations(archivist_handler) -> None:
    # Publish two related entries
    first = await archivist_handler.dispatch(
        "publish",
        {
            "resource_name": "redis-cluster", "summary": "Runs on port 6379.",
            "entry_type": "fact",
        },
        _ctx(),
    )
    first_id = first.content[0]["data"]["entry_id"]
    second = await archivist_handler.dispatch(
        "publish",
        {
            "resource_name": "redis-cluster", "summary": "Uses AOF persistence.",
            "entry_type": "fact",
        },
        _ctx(),
    )
    second_id = second.content[0]["data"]["entry_id"]

    # Get the second one — it should expose its RELATED edge to the first
    get_result = await archivist_handler.dispatch(
        "get", {"entry_id": second_id}, _ctx(),
    )
    assert get_result.is_error is False
    data = get_result.content[0]["data"]
    assert data["entry"]["id"] == second_id
    assert any(
        e["dst_id"] == first_id and e["relation_type"] == "RELATED"
        for e in data["related"]
    )


@pytest.mark.asyncio
async def test_handler_publish_supersede_retires_prior(archivist_handler) -> None:
    first = await archivist_handler.dispatch(
        "publish",
        {"resource_name": "nas-share", "summary": "Mounted at /mnt/a.", "entry_type": "fact"},
        _ctx(),
    )
    first_id = first.content[0]["data"]["entry_id"]
    second = await archivist_handler.dispatch(
        "publish",
        {
            "resource_name": "nas-share", "summary": "Mounted at /mnt/b.",
            "entry_type": "fact", "supersede": True,
        },
        _ctx(),
    )
    data = second.content[0]["data"]
    assert data["outcome"] == "superseded"
    assert data["related_ids"] == [first_id]

    hits = (await archivist_handler.dispatch("search", {"query": "Mounted"}, _ctx())).content[0][
        "data"
    ]["hits"]
    assert [h["summary"] for h in hits] == ["Mounted at /mnt/b."]


@pytest.mark.asyncio
async def test_handler_publish_without_supersede_still_links_related(archivist_handler) -> None:
    # Pins the unchanged default; it passes with or without the supersede flag.
    await archivist_handler.dispatch(
        "publish",
        {"resource_name": "nas-share", "summary": "Mounted at /mnt/a.", "entry_type": "fact"},
        _ctx(),
    )
    second = await archivist_handler.dispatch(
        "publish",
        {"resource_name": "nas-share", "summary": "Mounted at /mnt/b.", "entry_type": "fact"},
        _ctx(),
    )
    assert second.content[0]["data"]["outcome"] == "related"
    hits = (await archivist_handler.dispatch("search", {"query": "Mounted"}, _ctx())).content[0][
        "data"
    ]["hits"]
    assert len(hits) == 2


@pytest.mark.asyncio
async def test_handler_identical_republish_with_supersede_is_a_duplicate(archivist_handler) -> None:
    args = {"resource_name": "nas-share", "summary": "Mounted at /mnt/a.", "entry_type": "fact"}
    first = await archivist_handler.dispatch("publish", args, _ctx())
    again = await archivist_handler.dispatch("publish", {**args, "supersede": True}, _ctx())
    assert again.content[0]["data"]["outcome"] in ("duplicate_l0", "duplicate_l1")
    assert again.content[0]["data"]["entry_id"] == first.content[0]["data"]["entry_id"]
    hits = (await archivist_handler.dispatch("search", {"query": "Mounted"}, _ctx())).content[0][
        "data"
    ]["hits"]
    assert len(hits) == 1


@pytest.mark.asyncio
async def test_handler_duplicate_supersede_reports_what_it_retired(archivist_handler) -> None:
    # cabal#363: an exact re-publish under supersede left other entries active.
    async def publish(summary: str, entry_type: str, **extra):
        result = await archivist_handler.dispatch(
            "publish",
            {"resource_name": "nas-share", "summary": summary, "entry_type": entry_type, **extra},
            _ctx(),
        )
        return result.content[0]["data"]

    old = (await publish("Mounted at /mnt/a.", "fact"))["entry_id"]
    proc = (await publish("Mount it at /mnt/b.", "procedure"))["entry_id"]
    again = await publish(
        "Mount it at /mnt/b.", "procedure", supersede=True, supersede_scope="resource"
    )
    assert again["outcome"] in ("duplicate_l0", "duplicate_l1")
    assert (again["entry_id"], again["related_ids"]) == (proc, [old])
    hits = (await archivist_handler.dispatch("search", {"query": "nas"}, _ctx())).content[0][
        "data"
    ]["hits"]
    assert [h["id"] for h in hits] == [proc]


@pytest.mark.asyncio
async def test_handler_publish_rejects_non_boolean_supersede(archivist_handler) -> None:
    result = await archivist_handler.dispatch(
        "publish",
        {"resource_name": "x", "summary": "y", "entry_type": "fact", "supersede": "yes"},
        _ctx(),
    )
    assert result.is_error is True
    assert "supersede" in result.error


@pytest.mark.asyncio
async def test_handler_publish_resource_scope_retires_a_different_type(archivist_handler) -> None:
    first = await archivist_handler.dispatch(
        "publish",
        {"resource_name": "nas-share", "summary": "Mounted at /mnt/a.", "entry_type": "fact"},
        _ctx(),
    )
    first_id = first.content[0]["data"]["entry_id"]
    second = await archivist_handler.dispatch(
        "publish",
        {
            "resource_name": "nas-share", "summary": "Mount it at /mnt/b.",
            "entry_type": "procedure", "supersede": True, "supersede_scope": "resource",
        },
        _ctx(),
    )
    data = second.content[0]["data"]
    assert data["outcome"] == "superseded"
    assert data["related_ids"] == [first_id]
    hits = (await archivist_handler.dispatch("search", {"query": "nas"}, _ctx())).content[0][
        "data"
    ]["hits"]
    assert [h["summary"] for h in hits] == ["Mount it at /mnt/b."]


@pytest.mark.asyncio
async def test_handler_publish_default_scope_keeps_a_different_type(archivist_handler) -> None:
    await archivist_handler.dispatch(
        "publish",
        {"resource_name": "nas-share", "summary": "Mounted at /mnt/a.", "entry_type": "fact"},
        _ctx(),
    )
    second = await archivist_handler.dispatch(
        "publish",
        {
            "resource_name": "nas-share", "summary": "Mount it at /mnt/b.",
            "entry_type": "procedure", "supersede": True,
        },
        _ctx(),
    )
    assert second.content[0]["data"]["outcome"] == "new"
    hits = (await archivist_handler.dispatch("search", {"query": "nas"}, _ctx())).content[0][
        "data"
    ]["hits"]
    assert len(hits) == 2


@pytest.mark.asyncio
async def test_handler_publish_rejects_unknown_supersede_scope(archivist_handler) -> None:
    result = await archivist_handler.dispatch(
        "publish",
        {
            "resource_name": "x", "summary": "y", "entry_type": "fact",
            "supersede": True, "supersede_scope": "everything",
        },
        _ctx(),
    )
    assert result.is_error is True
    assert "supersede_scope" in result.error


@pytest.mark.asyncio
async def test_handler_schema(archivist_handler) -> None:
    result = await archivist_handler.dispatch("schema", {}, _ctx())
    assert result.is_error is False
    schema = result.content[0]["data"]
    assert "memory_entries" in schema["tables"]
    assert "fact" in schema["entry_types"]
    assert "decision" in schema["entry_types"]
    assert schema["counts"]["total_active"] == 0


@pytest.mark.asyncio
async def test_handler_unknown_tool(archivist_handler) -> None:
    result = await archivist_handler.dispatch("ghost", {}, _ctx())
    assert result.is_error is True
    assert "unknown tool" in result.error


@pytest.mark.asyncio
async def test_handler_missing_db_returns_clear_error(tmp_path) -> None:
    """Handler with no db_path → structured failure, not exception."""
    handler = ArchivistHandler()
    result = await handler.dispatch(
        "publish",
        {
            "resource_name": "x", "summary": "y", "entry_type": "fact",
        },
        _ctx(),
    )
    assert result.is_error is True
    assert "no archivist corpus" in result.error.lower()


@pytest.mark.asyncio
async def test_handler_db_path_rejects_traversal(archivist_handler) -> None:
    ctx = Context(
        identity="x", agent_name="x", credentials={},
        extra={"archivist.db_path": "../../etc/passwd"},
    )
    result = await archivist_handler.dispatch("schema", {}, ctx)
    assert result.is_error is True


def test_get_handler_factory() -> None:
    from cabal.handlers.archivist import get_handler
    assert isinstance(get_handler(), ArchivistHandler)


def test_capability_card_shape() -> None:
    handler = ArchivistHandler()
    card = handler.capability_card()
    assert card["handler"] == "archivist"
    tool_names = {t["name"] for t in card["tools"]}
    assert tool_names == {"publish", "search", "get", "schema"}


@pytest.mark.asyncio
async def test_get_handler_reads_env_db_path(monkeypatch, tmp_path: Path) -> None:
    """cabal#129 — liminal discovery calls get_handler() with no arguments, so
    before CABAL_ARCHIVIST_DB the discovered handler refused every call with
    "no archivist corpus configured". Unset/empty keeps the old None."""
    from cabal.handlers.archivist import get_handler

    db = tmp_path / "archivist.db"
    monkeypatch.setenv("CABAL_ARCHIVIST_DB", str(db))
    handler = get_handler()
    assert handler.default_config.db_path == db
    # The discovered handler is live: a real call reaches the corpus.
    result = await handler.dispatch("schema", {}, _ctx())
    assert result.is_error is False, result.error

    monkeypatch.setenv("CABAL_ARCHIVIST_DB", "")
    assert get_handler().default_config.db_path is None
    monkeypatch.delenv("CABAL_ARCHIVIST_DB", raising=False)
    assert get_handler().default_config.db_path is None


# ---- #360 / #361: payload validation, importance 0, call logging -----------

_BASE = {"resource_name": "nas-share", "summary": "Mounted at /mnt/a.", "entry_type": "fact"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("importance", "high"),
        ("importance", [1]),
        ("importance", True),
        ("importance", 1.5),
        ("importance", -0.1),
        ("importance", float("nan")),
        ("details", 5),
        ("details", {"a": 1}),
        ("gotchas", ["x"]),
        ("source_thread", 7),
        ("tags", [1]),
        ("tags", {"a": 1}),
        ("commands", "make test, make lint"),
        ("commands", [1]),
        ("refs", [None]),
    ],
)
async def test_handler_publish_refuses_a_wrong_typed_field(
    archivist_handler, field_name, value
) -> None:
    # cabal#360: these raised out of dispatch, or were stored as given.
    result = await archivist_handler.dispatch(
        "publish", {**_BASE, field_name: value}, _ctx(),
    )
    assert result.is_error is True
    assert f"'{field_name}'" in result.error
    assert "internal error" not in result.error  # a validation failure, not a crash
    schema = (await archivist_handler.dispatch("schema", {}, _ctx())).content[0]["data"]
    assert schema["counts"]["total_active"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("sent", "stored"), [(0, 0.0), (0.0, 0.0), (None, 1.0), (1, 1.0), (0.4, 0.4)])
async def test_handler_publish_keeps_importance(archivist_handler, sent, stored) -> None:
    # cabal#361: `importance or 1.0` stored 0 as 1.0.
    payload = dict(_BASE) if sent is None else {**_BASE, "importance": sent}
    pub = await archivist_handler.dispatch("publish", payload, _ctx())
    entry_id = pub.content[0]["data"]["entry_id"]
    got = await archivist_handler.dispatch("get", {"entry_id": entry_id}, _ctx())
    assert got.content[0]["data"]["entry"]["importance"] == stored


@pytest.mark.asyncio
async def test_handler_publish_still_takes_comma_separated_tags(archivist_handler) -> None:
    pub = await archivist_handler.dispatch(
        "publish", {**_BASE, "tags": "nas, storage", "commands": ["mount -a"]}, _ctx(),
    )
    entry_id = pub.content[0]["data"]["entry_id"]
    entry = (await archivist_handler.dispatch("get", {"entry_id": entry_id}, _ctx())).content[0][
        "data"
    ]["entry"]
    assert (entry["tags"], entry["commands"]) == (["nas", "storage"], ["mount -a"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "value"), [("top_k", "abc"), ("top_k", 2.5), ("top_k", True), ("tags", [3])]
)
async def test_handler_search_refuses_a_wrong_typed_field(
    archivist_handler, field_name, value
) -> None:
    result = await archivist_handler.dispatch(
        "search", {"query": "nas", field_name: value}, _ctx(),
    )
    assert result.is_error is True
    assert f"'{field_name}'" in result.error


@pytest.mark.asyncio
async def test_handler_refuses_a_non_string_db_path_override(archivist_handler) -> None:
    ctx = Context(identity="x", agent_name="x", credentials={}, extra={"archivist.db_path": 5})
    result = await archivist_handler.dispatch("schema", {}, ctx)
    assert result.is_error is True
    assert "archivist.db_path" in result.error


@pytest.mark.asyncio
async def test_handler_unexpected_error_is_a_failure(archivist_handler, monkeypatch) -> None:
    def boom(self):
        raise FileExistsError(17, "File exists", "/srv/secret-corpus")

    monkeypatch.setattr(ArchivistStore, "schema", boom)
    result = await archivist_handler.dispatch("schema", {}, _ctx())
    assert result.is_error is True
    assert result.error == "archivist.schema: internal error: FileExistsError"
    assert "/srv/secret-corpus" not in result.error


@pytest.mark.asyncio
async def test_handler_logs_one_line_per_call(archivist_handler) -> None:
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        pub = await archivist_handler.dispatch("publish", _BASE, _ctx())
        await archivist_handler.dispatch("search", {"query": "nas", "top_k": "x"}, _ctx())
    calls = [e for e in logs if e["event"] == "archivist.call"]
    assert len(calls) == 2
    ok, failed = calls
    assert ok["log_level"] == "debug"
    assert (ok["tool"], ok["outcome"], ok["resource"]) == ("publish", "new", "nas-share")
    assert ok["entry_id"] == pub.content[0]["data"]["entry_id"]
    assert isinstance(ok["duration_ms"], float)
    assert (failed["tool"], failed["outcome"]) == ("search", "failure")
    assert "top_k" in failed["error"]
