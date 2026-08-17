"""Tests for face-kind usage of cleanup_tools.semantic.index /
cleanup_tools.semantic.cluster.

Most of the underlying schema/math this story needs was already built
generically in document-topic-clustering's semantic-index-and-clustering
story (the ``kind``/``face_index`` composite key, the threshold+union-find
clustering itself) -- these tests prove that generic groundwork actually
holds for the FACE domain specifically, not just documents, plus the one
real code addition this story needed: ``cluster()``'s ``prefix`` parameter,
so an unlabeled face cluster falls back to ``"person-<n>"`` rather than
document-topic-clustering's ``"cluster-<n>"``.

Uses fake/synthetic face embeddings with known identity-similarity
relationships throughout -- no real InsightFace/onnxruntime calls needed
here (face-detection-and-embedding's own test file already covers that).
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
# index.py: multi-face-per-photo storage, kind isolation from documents.
# ---------------------------------------------------------------------------


def test_a_photo_with_three_faces_stores_three_distinct_rows(adapter):
    for face_index, embedding in enumerate([[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]]):
        index.add_embedding(
            adapter, "group-photo-hash", "/photos/group.jpg", embedding,
            kind=index.KIND_FACE, face_index=face_index,
        )

    results = index.get_embeddings(adapter, kind=index.KIND_FACE)

    assert len(results) == 3
    assert {r.face_index for r in results} == {0, 1, 2}
    assert all(r.content_hash == "group-photo-hash" for r in results)
    assert all(r.path == "/photos/group.jpg" for r in results)


def test_face_embeddings_never_appear_when_querying_document_kind(adapter):
    index.add_embedding(adapter, "photo1", "/a.jpg", [0.9, 0.1], kind=index.KIND_FACE)
    index.add_embedding(adapter, "doc1", "/b.pdf", [0.1, 0.9], kind=index.KIND_DOCUMENT)

    documents = index.get_embeddings(adapter, kind=index.KIND_DOCUMENT)
    faces = index.get_embeddings(adapter, kind=index.KIND_FACE)

    assert [d.content_hash for d in documents] == ["doc1"]
    assert [f.content_hash for f in faces] == ["photo1"]


def test_face_rows_have_no_text_by_default(adapter):
    """Faces have no meaningful extracted text -- confirms the shared
    schema's `text` column is genuinely optional, not face-specific dead
    weight repurposed awkwardly.
    """
    index.add_embedding(adapter, "photo1", "/a.jpg", [0.9, 0.1], kind=index.KIND_FACE)

    result = index.get_embeddings(adapter, kind=index.KIND_FACE)[0]

    assert result.text is None


# ---------------------------------------------------------------------------
# cluster.py: face-domain clustering, "person-<n>" fallback naming.
# ---------------------------------------------------------------------------


def test_faces_with_known_identity_similarity_cluster_together():
    # Two embeddings close together (same "person"), one far apart (a
    # different person) -- same shape as document-topic-clustering's own
    # clustering test, applied to the face domain.
    embeddings = [
        [1.0, 0.01],
        [0.99, 0.02],
        [0.02, 1.0],
    ]

    result = cluster.cluster(embeddings, threshold=0.98, prefix="person")

    assert len(result) == 1
    assert sorted(result[0].member_indices) == [0, 1]


def test_unlabeled_face_cluster_falls_back_to_person_not_cluster():
    embeddings = [[1.0, 0.0], [0.99, 0.02]]

    result = cluster.cluster(embeddings, texts=None, threshold=0.9, prefix="person")

    assert len(result) == 1
    assert result[0].label == result[0].slug == "person-1"


def test_default_prefix_is_still_cluster_not_person_backward_compat():
    """document-topic-clustering's own call sites never pass prefix= --
    this confirms the default stays "cluster", unchanged by this story's
    addition.
    """
    embeddings = [[1.0, 0.0], [0.99, 0.02]]

    result = cluster.cluster(embeddings, texts=None, threshold=0.9)

    assert result[0].label == "cluster-1"


def test_single_dissimilar_face_is_never_clustered():
    embeddings = [[1.0, 0.0], [0.0, 1.0], [0.0, -1.0]]

    result = cluster.cluster(embeddings, threshold=0.9, prefix="person")

    assert result == []


def test_face_clustering_is_deterministic_across_repeated_calls():
    embeddings = [[1.0, 0.01], [0.99, 0.02], [0.02, 1.0], [0.01, 0.99]]

    result1 = cluster.cluster(embeddings, threshold=0.98, prefix="person")
    result2 = cluster.cluster(embeddings, threshold=0.98, prefix="person")

    assert [(c.member_indices, c.label, c.slug) for c in result1] == [
        (c.member_indices, c.label, c.slug) for c in result2
    ]


def test_multiple_person_clusters_get_distinct_sequential_fallback_labels():
    # Two separate identity clusters, both unlabeled (no texts) -- must get
    # "person-1"/"person-2", never both "person-1".
    embeddings = [
        [1.0, 0.0, 0.0],
        [0.99, 0.02, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.99, 0.02],
    ]

    result = cluster.cluster(embeddings, threshold=0.9, prefix="person")

    assert len(result) == 2
    assert {c.label for c in result} == {"person-1", "person-2"}
