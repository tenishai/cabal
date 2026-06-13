"""Tests for the Librarian persona's filesystem adapter, store, and handler (cabal#5).

End-to-end:
  1. Create a small docs tree on disk
  2. Configure FilesystemAdapter pointing at it
  3. LibrarianStore.refresh() ingests everything into a techne knowledge DB
  4. LibrarianStore.search() returns matching documents
  5. DocumentsHandler exposes the same surface through the limenal Handler protocol
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("limenal")
pytest.importorskip("techne")

from limenal.protocol import Context  # noqa: E402

from cabal.handlers.documents import DocumentsConfig, DocumentsHandler  # noqa: E402
from cabal.librarian import FilesystemAdapter, LibrarianStore  # noqa: E402

# ---- FilesystemAdapter ---------------------------------------------------


@pytest.fixture
def docs_tree(tmp_path: Path) -> Path:
    """A minimal docs tree:
        runbooks/
          README.md          → kind=readme
          deploy.md          → kind=doc
          incident-001.md    → kind=doc
          subdir/runbook-restart.md   → kind=runbook
          .git/index         → excluded
          huge.md            → over size cap, skipped
    """
    root = tmp_path / "docs"
    root.mkdir()
    (root / "README.md").write_text("# Engineering handbook\nWelcome.")
    (root / "deploy.md").write_text("# Deploy\nHow to deploy the service.")
    (root / "incident-001.md").write_text(
        "# Incident 001\nThe payment-api went down on 2026-01-15."
    )
    (root / "subdir").mkdir()
    (root / "subdir" / "runbook-restart.md").write_text(
        "# Runbook: restart\nSSH into the box and run systemctl restart."
    )
    (root / ".git").mkdir()
    (root / ".git" / "index").write_text("not a real index")
    # Over the size cap (set very low in the test) → adapter skips
    (root / "huge.md").write_text("X" * 1_500_000)
    return root


@pytest.mark.asyncio
async def test_filesystem_adapter_lists_matching_docs(docs_tree) -> None:
    adapter = FilesystemAdapter(
        name="runbooks",
        roots=[docs_tree],
        include_globs=["*.md"],
        exclude_globs=["**/.git/**"],
        max_bytes=1_000_000,
    )
    refs = [ref async for ref in adapter.list_documents()]
    titles = {r.title for r in refs}
    # README, deploy, incident-001, subdir/runbook-restart (4)
    # huge.md (excluded by size), .git/index (excluded by glob)
    assert titles == {"README.md", "deploy.md", "incident-001.md", "runbook-restart.md"}


@pytest.mark.asyncio
async def test_filesystem_adapter_uris_namespaced_by_source(docs_tree) -> None:
    adapter = FilesystemAdapter(name="runbooks", roots=[docs_tree])
    refs = [ref async for ref in adapter.list_documents()]
    for r in refs:
        assert r.uri.startswith("librarian://runbooks/")


@pytest.mark.asyncio
async def test_filesystem_adapter_fetch_reads_content(docs_tree) -> None:
    adapter = FilesystemAdapter(name="runbooks", roots=[docs_tree])
    refs = [r async for r in adapter.list_documents()]
    incident = next(r for r in refs if r.title == "incident-001.md")
    doc = await adapter.fetch_document(incident)
    assert "payment-api went down" in doc.content
    assert "source:runbooks" in doc.tags
    assert doc.metadata["size_bytes"] > 0


@pytest.mark.asyncio
async def test_filesystem_adapter_kinds_classified(docs_tree) -> None:
    adapter = FilesystemAdapter(name="runbooks", roots=[docs_tree])
    refs = [r async for r in adapter.list_documents()]
    docs = {}
    for r in refs:
        docs[r.title] = await adapter.fetch_document(r)
    assert docs["README.md"].kind == "readme"
    assert docs["runbook-restart.md"].kind == "runbook"
    assert docs["deploy.md"].kind == "doc"


@pytest.mark.asyncio
async def test_filesystem_adapter_healthcheck(docs_tree, tmp_path) -> None:
    healthy = FilesystemAdapter(name="ok", roots=[docs_tree])
    h = await healthy.healthcheck()
    assert h.healthy is True

    missing = FilesystemAdapter(name="bad", roots=[tmp_path / "does-not-exist"])
    h2 = await missing.healthcheck()
    assert h2.healthy is False


# ---- LibrarianStore ------------------------------------------------------


@pytest.fixture
def techne_db(tmp_path: Path) -> Path:
    """Fresh techne DB for librarian to use."""
    from techne.core.database import init_db
    p = tmp_path / "librarian.db"
    init_db(p).close()
    return p


@pytest.mark.asyncio
async def test_store_refresh_ingests_documents(docs_tree, techne_db) -> None:
    adapter = FilesystemAdapter(
        name="runbooks",
        roots=[docs_tree],
        exclude_globs=["**/.git/**"],
        max_bytes=1_000_000,
    )
    store = LibrarianStore(db_path=techne_db, sources={"runbooks": adapter})
    result = await store.refresh()
    assert result.documents_added == 4
    assert result.sources == {"runbooks": 4}


@pytest.mark.asyncio
async def test_store_refresh_is_idempotent(docs_tree, techne_db) -> None:
    """Re-running with no source changes adds nothing."""
    adapter = FilesystemAdapter(
        name="runbooks", roots=[docs_tree],
        exclude_globs=["**/.git/**"], max_bytes=1_000_000,
    )
    store = LibrarianStore(db_path=techne_db, sources={"runbooks": adapter})
    first = await store.refresh()
    second = await store.refresh()
    assert first.documents_added == 4
    assert second.documents_added == 0


@pytest.mark.asyncio
async def test_store_search_returns_documents(docs_tree, techne_db) -> None:
    adapter = FilesystemAdapter(
        name="runbooks", roots=[docs_tree],
        exclude_globs=["**/.git/**"], max_bytes=1_000_000,
    )
    store = LibrarianStore(db_path=techne_db, sources={"runbooks": adapter})
    await store.refresh()
    hits = await store.search("payment-api", top_k=5)
    assert len(hits) >= 1
    assert any("payment-api" in h["content"] for h in hits)


@pytest.mark.asyncio
async def test_store_search_filters_by_source(docs_tree, techne_db) -> None:
    adapter = FilesystemAdapter(
        name="runbooks", roots=[docs_tree],
        exclude_globs=["**/.git/**"], max_bytes=1_000_000,
    )
    store = LibrarianStore(db_path=techne_db, sources={"runbooks": adapter})
    await store.refresh()
    hits = await store.search("deploy", source="runbooks")
    for h in hits:
        assert "source:runbooks" in h["tags"]
    hits_other = await store.search("deploy", source="nonexistent")
    assert hits_other == []


@pytest.mark.asyncio
async def test_store_search_filters_by_kind(docs_tree, techne_db) -> None:
    adapter = FilesystemAdapter(
        name="runbooks", roots=[docs_tree],
        exclude_globs=["**/.git/**"], max_bytes=1_000_000,
    )
    store = LibrarianStore(db_path=techne_db, sources={"runbooks": adapter})
    await store.refresh()
    hits = await store.search("restart", kind="runbook")
    for h in hits:
        assert "kind:runbook" in h["tags"]


@pytest.mark.asyncio
async def test_store_list_sources_reports_health(docs_tree, techne_db) -> None:
    adapter = FilesystemAdapter(name="runbooks", roots=[docs_tree])
    store = LibrarianStore(db_path=techne_db, sources={"runbooks": adapter})
    items = await store.list_sources()
    assert len(items) == 1
    assert items[0]["name"] == "runbooks"
    assert items[0]["healthy"] is True
    assert items[0]["type"] == "FilesystemAdapter"


# ---- DocumentsHandler (MCP surface) -------------------------------------


@pytest.fixture
def documents_handler(docs_tree, techne_db) -> DocumentsHandler:
    adapter = FilesystemAdapter(
        name="runbooks", roots=[docs_tree],
        exclude_globs=["**/.git/**"], max_bytes=1_000_000,
    )
    return DocumentsHandler(
        default_config=DocumentsConfig(
            db_path=techne_db,
            adapters=[adapter],
        ),
    )


def _ctx() -> Context:
    return Context(identity="librarian", agent_name="librarian", credentials={}, extra={})


@pytest.mark.asyncio
async def test_handler_refresh_then_search(documents_handler) -> None:
    # Refresh first
    r1 = await documents_handler.dispatch("refresh", {}, _ctx())
    assert r1.is_error is False
    refresh_data = r1.content[0]["data"]
    assert refresh_data["documents_added"] == 4

    # Then search
    r2 = await documents_handler.dispatch("search", {"query": "payment-api"}, _ctx())
    assert r2.is_error is False
    search_data = r2.content[0]["data"]
    assert len(search_data["hits"]) >= 1


@pytest.mark.asyncio
async def test_handler_refresh_unknown_source(documents_handler) -> None:
    await documents_handler.dispatch("refresh", {}, _ctx())  # populate first
    result = await documents_handler.dispatch(
        "refresh", {"source_name": "nonexistent"}, _ctx()
    )
    assert result.is_error is True
    assert "unknown source" in result.error


@pytest.mark.asyncio
async def test_handler_search_rejects_empty_query(documents_handler) -> None:
    result = await documents_handler.dispatch("search", {"query": "   "}, _ctx())
    assert result.is_error is True


@pytest.mark.asyncio
async def test_handler_list_sources(documents_handler) -> None:
    result = await documents_handler.dispatch("list_sources", {}, _ctx())
    assert result.is_error is False
    data = result.content[0]["data"]
    assert len(data["sources"]) == 1
    assert data["sources"][0]["name"] == "runbooks"


@pytest.mark.asyncio
async def test_handler_unknown_tool(documents_handler) -> None:
    result = await documents_handler.dispatch("ghost", {}, _ctx())
    assert result.is_error is True
    assert "unknown tool" in result.error.lower()


@pytest.mark.asyncio
async def test_handler_without_db_returns_clear_error(docs_tree) -> None:
    handler = DocumentsHandler()  # no default_config.db_path
    result = await handler.dispatch("search", {"query": "anything"}, _ctx())
    assert result.is_error is True
    assert "no librarian corpus" in result.error.lower()


def test_get_handler_factory_returns_documents_handler() -> None:
    from cabal.handlers.documents import get_handler
    h = get_handler()
    assert isinstance(h, DocumentsHandler)


def test_capability_card_has_required_fields() -> None:
    handler = DocumentsHandler()
    card = handler.capability_card()
    assert card["handler"] == "documents"
    tool_names = {t["name"] for t in card["tools"]}
    assert tool_names == {"search", "refresh", "list_sources"}
