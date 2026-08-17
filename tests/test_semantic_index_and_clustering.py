"""Tests for cleanup_tools.semantic.index / cleanup_tools.semantic.cluster.

Uses fake/synthetic embedding vectors with known geometric relationships
throughout -- proves the clustering MATH is correct independent of embedding
quality, per this story's own test-step discipline (real PyObjC-derived
embeddings are covered by semantic-extraction-and-embedding's story).
"""

from __future__ import annotations

import pytest

from cleanup_tools.adapters import MacOSAdapter
from cleanup_tools.semantic import cluster, index


@pytest.fixture
def adapter(tmp_path) -> MacOSAdapter:
    class FakeHomeAdapter(MacOSAdapter):
        def resolve_home(self):
            return tmp_path

    return FakeHomeAdapter()


# ---------------------------------------------------------------------------
# index.py -- content-hash-keyed incremental cache, kind isolation.
# ---------------------------------------------------------------------------


def test_is_indexed_false_for_unseen_content_hash(adapter):
    assert index.is_indexed(adapter, "abc123") is False


def test_add_embedding_then_is_indexed_true(adapter):
    index.add_embedding(adapter, "abc123", "/some/doc.pdf", [0.1, 0.2, 0.3])

    assert index.is_indexed(adapter, "abc123") is True


def test_get_embeddings_returns_stored_embedding(adapter):
    index.add_embedding(adapter, "abc123", "/some/doc.pdf", [0.1, 0.2, 0.3])

    results = index.get_embeddings(adapter, kind=index.KIND_DOCUMENT)

    assert len(results) == 1
    assert results[0].content_hash == "abc123"
    assert results[0].path == "/some/doc.pdf"
    assert results[0].embedding == [0.1, 0.2, 0.3]
    assert results[0].face_index == 0


def test_multiple_faces_same_content_hash_stored_as_distinct_rows(adapter):
    index.add_embedding(
        adapter, "photo123", "/some/photo.jpg", [0.1, 0.2], kind=index.KIND_FACE, face_index=0
    )
    index.add_embedding(
        adapter, "photo123", "/some/photo.jpg", [0.9, 0.8], kind=index.KIND_FACE, face_index=1
    )
    index.add_embedding(
        adapter, "photo123", "/some/photo.jpg", [0.5, 0.5], kind=index.KIND_FACE, face_index=2
    )

    results = index.get_embeddings(adapter, kind=index.KIND_FACE)

    assert len(results) == 3
    assert {r.face_index for r in results} == {0, 1, 2}
    assert all(r.content_hash == "photo123" for r in results)


def test_get_embeddings_never_mixes_kinds(adapter):
    index.add_embedding(adapter, "doc1", "/a.pdf", [0.1, 0.2], kind=index.KIND_DOCUMENT)
    index.add_embedding(adapter, "face1", "/b.jpg", [0.9, 0.9], kind=index.KIND_FACE)

    documents = index.get_embeddings(adapter, kind=index.KIND_DOCUMENT)
    faces = index.get_embeddings(adapter, kind=index.KIND_FACE)

    assert [d.content_hash for d in documents] == ["doc1"]
    assert [f.content_hash for f in faces] == ["face1"]


def test_add_embedding_upsert_overwrites_existing_row(adapter):
    index.add_embedding(adapter, "abc123", "/some/doc.pdf", [0.1, 0.2, 0.3])
    index.add_embedding(adapter, "abc123", "/some/doc.pdf", [0.9, 0.9, 0.9])

    results = index.get_embeddings(adapter, kind=index.KIND_DOCUMENT)

    assert len(results) == 1
    assert results[0].embedding == [0.9, 0.9, 0.9]


def test_document_text_round_trips(adapter):
    index.add_embedding(
        adapter, "abc123", "/some/doc.pdf", [0.1, 0.2],
        kind=index.KIND_DOCUMENT, text="Invoice for Acme Corp.",
    )

    results = index.get_embeddings(adapter, kind=index.KIND_DOCUMENT)

    assert results[0].text == "Invoice for Acme Corp."


def test_get_text_returns_stored_text_for_one_row(adapter):
    index.add_embedding(
        adapter, "abc123", "/some/doc.pdf", [0.1, 0.2],
        kind=index.KIND_DOCUMENT, text="Invoice for Acme Corp.",
    )

    assert index.get_text(adapter, "abc123") == "Invoice for Acme Corp."


def test_get_text_returns_none_for_unindexed_content_hash(adapter):
    assert index.get_text(adapter, "does-not-exist") is None


def test_face_bbox_round_trips(adapter):
    index.add_embedding(
        adapter, "photo123", "/some/photo.jpg", [0.1, 0.2],
        kind=index.KIND_FACE, face_index=0, bbox=(0.1, 0.2, 0.3, 0.4),
    )

    results = index.get_embeddings(adapter, kind=index.KIND_FACE)

    assert results[0].bbox == (0.1, 0.2, 0.3, 0.4)


# ---------------------------------------------------------------------------
# cluster.py -- cosine-similarity threshold + union-find, label/slug.
# ---------------------------------------------------------------------------


def test_cosine_similarity_identical_vectors_is_one():
    assert cluster.cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_vectors_is_zero():
    assert cluster.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cluster_groups_similar_vectors_above_threshold():
    # Two tight groups: [0,1] near [1.0, 0], and [2,3] near [0, 1.0]; 4 is an outlier.
    embeddings = [
        [1.0, 0.01],
        [0.99, 0.02],
        [0.01, 1.0],
        [0.02, 0.99],
        [0.7, 0.7],  # outlier -- not close enough to either group at threshold 0.98
    ]

    result = cluster.cluster(embeddings, threshold=0.98)

    member_sets = sorted(sorted(c.member_indices) for c in result)
    assert member_sets == [[0, 1], [2, 3]]


def test_cluster_excludes_singletons():
    embeddings = [[1.0, 0.0], [0.0, 1.0], [0.0, -1.0]]  # all mutually dissimilar

    result = cluster.cluster(embeddings, threshold=0.9)

    assert result == []


def test_cluster_is_deterministic_across_repeated_calls():
    embeddings = [[1.0, 0.01], [0.99, 0.02], [0.01, 1.0], [0.02, 0.99]]
    texts = ["invoice alpha", "invoice beta", "recipe alpha", "recipe beta"]

    result1 = cluster.cluster(embeddings, texts, threshold=0.98)
    result2 = cluster.cluster(embeddings, texts, threshold=0.98)

    assert [(c.member_indices, c.label, c.slug) for c in result1] == [
        (c.member_indices, c.label, c.slug) for c in result2
    ]


def test_cluster_label_uses_most_common_word_across_member_texts():
    embeddings = [[1.0, 0.0], [0.99, 0.02]]
    texts = ["invoice from acme corp", "invoice statement for acme corp"]

    result = cluster.cluster(embeddings, texts, threshold=0.9)

    assert len(result) == 1
    assert result[0].label == "invoice"


def test_cluster_label_and_slug_relationship():
    label, slug = cluster.label_and_slug(["House Sale documents", "the House Sale contract"], fallback_id=1)

    assert label == "house"
    assert slug == "house"  # already filesystem-safe -- lowercase, no special chars


def test_slugify_produces_filesystem_safe_names():
    label, slug = cluster.label_and_slug(["Tax Records 2023!!", "Tax Records for filing"], fallback_id=1)

    assert label == "tax"
    assert "/" not in slug
    assert not slug.startswith(".")
    assert slug == slug.lower()


def test_cluster_fallback_label_when_no_texts_given():
    embeddings = [[1.0, 0.0], [0.99, 0.02]]

    result = cluster.cluster(embeddings, texts=None, threshold=0.9)

    assert len(result) == 1
    assert result[0].label == result[0].slug == "cluster-1"


def test_label_and_slug_fallback_when_no_word_clears_threshold():
    # Every word appears exactly once, across 3+ distinct texts -- no word
    # reaches the >=2-occurrences bar, and len(counts) > 1, so no single
    # word is confidently "the" label -- fallback applies.
    label, slug = cluster.label_and_slug(
        ["zebra elephant giraffe", "octopus narwhal penguin", "walrus dolphin manatee"],
        fallback_id=7,
    )

    assert label == slug == "cluster-7"


def test_label_and_slug_single_text_uses_its_most_common_word_even_once():
    label, slug = cluster.label_and_slug(["invoice invoice payment due"], fallback_id=1)

    assert label == "invoice"
