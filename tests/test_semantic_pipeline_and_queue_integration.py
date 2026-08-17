"""Tests for cleanup_tools.semantic.pipeline.

Uses real fixture files (nested subdirectories, name collisions) and a
monkeypatched embedder (avoiding real PyObjC/NLEmbedding calls in this
story's own tests -- semantic-extraction-and-embedding's story already
covers real-framework testing). Plain .txt fixtures mean no PyObjC call
happens anywhere in extract_text() either, so these tests exercise the real
recursive-scan/guard/staging logic without depending on the installed
PyObjC frameworks at all.
"""

from __future__ import annotations

import pytest

from cleanup_tools import config as config_module
from cleanup_tools import queue as queue_module
from cleanup_tools.adapters import MacOSAdapter
from cleanup_tools.semantic import embeddings as embeddings_module
from cleanup_tools.semantic import pipeline


class _FakeEmbedder:
    """Deterministic, keyword-based fake embedder -- two texts mentioning
    the same keyword get IDENTICAL vectors (cosine similarity 1.0, always
    clusters at any real threshold); unrelated texts get orthogonal vectors
    (cosine similarity 0.0, never clusters). Records every text it was
    asked to embed, for the incremental-skip test.
    """

    def __init__(self):
        self.embedded_texts: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.embedded_texts.append(text)
        lower = text.lower()
        if "invoice" in lower:
            return [1.0, 0.0, 0.0]
        if "recipe" in lower:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]


@pytest.fixture
def adapter(tmp_path) -> MacOSAdapter:
    class FakeHomeAdapter(MacOSAdapter):
        def resolve_home(self):
            return tmp_path

    return FakeHomeAdapter()


@pytest.fixture
def fake_embedder(monkeypatch) -> _FakeEmbedder:
    embedder = _FakeEmbedder()
    monkeypatch.setattr(embeddings_module, "get_embedder", lambda adapter: embedder)
    return embedder


def _configure_root(adapter, root) -> None:
    config_module.save_config(
        adapter,
        config_module.Config(bucket_rules=config_module.DEFAULT_BUCKET_RULES, search_roots=[str(root)]),
    )


# ---------------------------------------------------------------------------
# Recursive scan.
# ---------------------------------------------------------------------------


def test_pipeline_scans_recursively_finds_nested_files(adapter, tmp_path, fake_embedder):
    root = tmp_path / "docs"
    nested = root / "Taxes" / "2023"
    nested.mkdir(parents=True)
    (nested / "invoice-a.txt").write_text("Invoice for Acme Corp, total due 100.")
    (root / "invoice-b.txt").write_text("Invoice statement, total due 200.")
    _configure_root(adapter, root)

    result = pipeline.run(adapter, threshold=0.9)

    assert result["files_scanned"] == 2
    assert len(result["staged_entry_ids"]) == 2  # both invoices cluster together


# ---------------------------------------------------------------------------
# Protected-path guard.
# ---------------------------------------------------------------------------


def test_pipeline_refuses_protected_path(adapter, tmp_path, fake_embedder, monkeypatch):
    fake_system_root = tmp_path / "FakeSystem"
    fake_system_root.mkdir()
    (fake_system_root / "invoice-a.txt").write_text("Invoice for Acme Corp.")
    monkeypatch.setattr(queue_module, "PROTECTED_PATH_ROOTS", [fake_system_root])
    _configure_root(adapter, fake_system_root)

    result = pipeline.run(adapter, threshold=0.9)

    assert result["files_scanned"] == 0
    assert result["staged_entry_ids"] == []


def test_pipeline_never_stages_a_previously_indexed_path_that_later_became_protected(
    adapter, tmp_path, fake_embedder, monkeypatch
):
    """A defensive re-check at staging time, not just at scan time: staging
    reads from the ACCUMULATED index (built across every past run), so a
    path indexed under a then-unprotected location must still be refused if
    it's protected by the time clustering/staging actually runs.
    """
    root = tmp_path / "docs"
    root.mkdir()
    (root / "invoice-a.txt").write_text("Invoice for Acme Corp.")
    (root / "invoice-b.txt").write_text("Invoice statement from Acme.")
    _configure_root(adapter, root)

    pipeline.run(adapter, threshold=0.9)  # indexed while root is NOT protected
    entries_before = queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))
    assert len(entries_before) == 2

    # Reject the queue and re-mark the root as protected before the next run.
    for entry in entries_before:
        queue_module.set_status(adapter, entry.id, "rejected", queue_module.default_queue_path(adapter))
    monkeypatch.setattr(queue_module, "PROTECTED_PATH_ROOTS", [root])
    (root / "invoice-c.txt").write_text("Invoice statement number three.")

    result = pipeline.run(adapter, threshold=0.9)

    assert result["staged_entry_ids"] == []


# ---------------------------------------------------------------------------
# iCloud-placeholder guard.
# ---------------------------------------------------------------------------


def test_pipeline_skips_icloud_placeholder_without_reading(adapter, tmp_path, fake_embedder):
    root = tmp_path / "docs"
    root.mkdir()
    (root / ".invoice-a.txt.icloud").write_bytes(b"")  # placeholder -- no real content
    (root / "invoice-b.txt").write_text("Invoice for Acme Corp.")
    _configure_root(adapter, root)

    pipeline.run(adapter, threshold=0.9)

    assert not any(".icloud" in text for text in fake_embedder.embedded_texts)
    assert fake_embedder.embedded_texts == ["Invoice for Acme Corp."]


# ---------------------------------------------------------------------------
# Dest-collision disambiguation.
# ---------------------------------------------------------------------------


def test_pipeline_disambiguates_filename_collisions(adapter, tmp_path, fake_embedder):
    root = tmp_path / "docs"
    (root / "Taxes").mkdir(parents=True)
    (root / "HouseSale").mkdir(parents=True)
    (root / "Taxes" / "notes.txt").write_text("Invoice notes from Taxes folder.")
    (root / "HouseSale" / "notes.txt").write_text("Invoice notes from HouseSale folder.")
    _configure_root(adapter, root)

    pipeline.run(adapter, threshold=0.9)

    entries = queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))
    assert len(entries) == 2
    dests = {e.dest for e in entries}
    assert len(dests) == 2  # distinct -- neither silently overwrote the other


# ---------------------------------------------------------------------------
# Real staging + queue.py "cluster" pipeline recognition.
# ---------------------------------------------------------------------------


def test_pipeline_stages_real_entries_with_correct_shape(adapter, tmp_path, fake_embedder):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "invoice-a.txt").write_text("Invoice for Acme Corp.")
    (root / "invoice-b.txt").write_text("Invoice statement from Acme.")
    _configure_root(adapter, root)

    result = pipeline.run(adapter, threshold=0.9)

    entries = queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))
    assert len(entries) == 2
    for entry in entries:
        assert entry.action == "move"
        assert entry.status == "pending"
        assert entry.source == "local:cluster"
        assert "_clusters/by-topic/" in entry.dest
    assert len(result["staged_entry_ids"]) == 2


def test_pipeline_group_key_recognized_by_parse_group_key(adapter, tmp_path, fake_embedder):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "invoice-a.txt").write_text("Invoice for Acme Corp.")
    (root / "invoice-b.txt").write_text("Invoice statement from Acme.")
    _configure_root(adapter, root)

    pipeline.run(adapter, threshold=0.9)

    entries = queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))
    parsed = queue_module.parse_group_key(entries[0].group_key)
    assert parsed["pipeline"] == "cluster"
    assert parsed["location"] == str(root.resolve())
    assert parsed["bucket"] == "invoice"  # the fake embedder's texts share the word "invoice"


def test_pipeline_group_key_lands_in_dashboard_tree_correctly(adapter, tmp_path, fake_embedder):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "invoice-a.txt").write_text("Invoice for Acme Corp.")
    (root / "invoice-b.txt").write_text("Invoice statement from Acme.")
    _configure_root(adapter, root)

    pipeline.run(adapter, threshold=0.9)

    entries = queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))
    tree = queue_module.group_entries_hierarchical(entries)
    assert len(tree) == 1
    assert tree[0]["location"] == str(root.resolve())
    assert tree[0]["count"] == 2


# ---------------------------------------------------------------------------
# Incremental reindexing.
# ---------------------------------------------------------------------------


def test_pipeline_second_run_skips_already_indexed_files(adapter, tmp_path, fake_embedder):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "invoice-a.txt").write_text("Invoice for Acme Corp.")
    (root / "invoice-b.txt").write_text("Invoice statement from Acme.")
    _configure_root(adapter, root)

    pipeline.run(adapter, threshold=0.9)
    assert len(fake_embedder.embedded_texts) == 2

    pipeline.run(adapter, threshold=0.9)  # nothing changed on disk

    assert len(fake_embedder.embedded_texts) == 2  # no new embed calls


def test_pipeline_files_indexed_reflects_only_newly_indexed_files_not_files_scanned(
    adapter, tmp_path, fake_embedder
):
    """Regression guard: files_indexed must count actual new indexing work,
    not merely mirror files_scanned under a different name.
    """
    root = tmp_path / "docs"
    root.mkdir()
    (root / "invoice-a.txt").write_text("Invoice for Acme Corp.")
    _configure_root(adapter, root)

    first = pipeline.run(adapter, threshold=0.9)
    assert first["files_scanned"] == 1
    assert first["files_indexed"] == 1

    (root / "invoice-b.txt").write_text("Invoice statement from Acme.")
    second = pipeline.run(adapter, threshold=0.9)

    assert second["files_scanned"] == 2  # both files scanned again
    assert second["files_indexed"] == 1  # only the new one was actually indexed


# ---------------------------------------------------------------------------
# Singletons never staged.
# ---------------------------------------------------------------------------


def test_pipeline_progress_callback_called_once_per_scanned_file(adapter, tmp_path, fake_embedder):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "invoice-a.txt").write_text("Invoice for Acme Corp.")
    (root / "invoice-b.txt").write_text("Invoice statement from Acme.")
    _configure_root(adapter, root)

    calls = []
    pipeline.run(adapter, threshold=0.9, progress_callback=lambda current, total: calls.append((current, total)))

    assert calls == [(1, 1), (2, 2)]


def test_pipeline_singleton_is_indexed_but_never_staged(adapter, tmp_path, fake_embedder):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "weather.txt").write_text("A completely unrelated note about the weather today.")
    _configure_root(adapter, root)

    result = pipeline.run(adapter, threshold=0.9)

    assert result["files_indexed"] == 1
    assert result["staged_entry_ids"] == []
