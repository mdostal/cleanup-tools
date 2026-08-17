"""Tests for cleanup_tools.semantic.pipeline.run_faces.

Real fixture files, a monkeypatched face detector (avoiding real
onnxruntime/InsightFace calls in this story's own tests --
face-detection-and-embedding's story already covers real-model testing).
Filenames encode which fake "identity" a photo contains, driving the fake
detector's canned responses deterministically.
"""

from __future__ import annotations

import pytest

from cleanup_tools import config as config_module
from cleanup_tools import queue as queue_module
from cleanup_tools.adapters import MacOSAdapter
from cleanup_tools.semantic import faces as faces_module
from cleanup_tools.semantic import pipeline


class _FakeFaceDetector:
    """Deterministic, filename-keyed fake detector -- two photos whose
    filenames share an identity marker ("persona"/"personb") get IDENTICAL
    embeddings (always cluster together); "groupphoto" yields two DIFFERENT
    embeddings (a real multi-identity photo); "singleton" yields one
    embedding that never matches anything else; "blank" yields zero faces.
    Records every path it was asked to detect, for the incremental-skip
    tests.
    """

    def __init__(self):
        self.detected_paths: list[str] = []

    def detect(self, image_path):
        self.detected_paths.append(str(image_path))
        name = str(image_path).lower()
        if "blank" in name:
            return []
        if "groupphoto" in name:
            return [
                faces_module.FaceDetection(bbox=(0.0, 0.0, 10.0, 10.0), embedding=[1.0, 0.0, 0.0]),
                faces_module.FaceDetection(bbox=(20.0, 0.0, 30.0, 10.0), embedding=[0.0, 1.0, 0.0]),
            ]
        if "persona" in name:
            return [faces_module.FaceDetection(bbox=(0.0, 0.0, 10.0, 10.0), embedding=[1.0, 0.0, 0.0])]
        if "personb" in name:
            return [faces_module.FaceDetection(bbox=(0.0, 0.0, 10.0, 10.0), embedding=[0.0, 1.0, 0.0])]
        if "singleton" in name:
            return [faces_module.FaceDetection(bbox=(0.0, 0.0, 10.0, 10.0), embedding=[0.0, 0.0, 1.0])]
        return []


@pytest.fixture
def adapter(tmp_path) -> MacOSAdapter:
    class FakeHomeAdapter(MacOSAdapter):
        def resolve_home(self):
            return tmp_path

    return FakeHomeAdapter()


@pytest.fixture
def fake_detector(monkeypatch) -> _FakeFaceDetector:
    detector = _FakeFaceDetector()
    monkeypatch.setattr(pipeline.faces_module, "get_face_detector", lambda adapter: detector)
    return detector


def _configure_root(adapter, root) -> None:
    config_module.save_config(
        adapter,
        config_module.Config(bucket_rules=config_module.DEFAULT_BUCKET_RULES, search_roots=[str(root)]),
    )


def _touch(path, content=None):
    # Unique content per file by default (incorporating the filename) --
    # content_hash is computed from bytes, not path, so two files with the
    # SAME default content would collide in the index under the same
    # (kind, content_hash, face_index) key, silently merging what should be
    # two distinct photos in these tests.
    path.write_bytes(content if content is not None else f"fake photo bytes for {path.name}".encode())


# ---------------------------------------------------------------------------
# Single-identity photos are staged.
# ---------------------------------------------------------------------------


def test_two_single_identity_photos_of_the_same_person_cluster_and_stage(adapter, tmp_path, fake_detector):
    root = tmp_path / "photos"
    root.mkdir()
    _touch(root / "persona-1.jpg", b"photo-1-bytes")
    _touch(root / "persona-2.jpg", b"photo-2-bytes")
    _configure_root(adapter, root)

    result = pipeline.run_faces(adapter, threshold=0.9)

    assert len(result["staged_entry_ids"]) == 2
    entries = queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))
    assert all(e.source == "local:cluster" for e in entries)
    assert all("_clusters/by-person/" in e.dest for e in entries)


def test_group_key_uses_the_shared_cluster_pipeline_prefix(adapter, tmp_path, fake_detector):
    root = tmp_path / "photos"
    root.mkdir()
    _touch(root / "persona-1.jpg")
    _touch(root / "persona-2.jpg")
    _configure_root(adapter, root)

    pipeline.run_faces(adapter, threshold=0.9)

    entries = queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))
    parsed = queue_module.parse_group_key(entries[0].group_key)
    assert parsed["pipeline"] == "cluster"
    assert parsed["bucket"] == "person-1"


# ---------------------------------------------------------------------------
# Multi-person-photo exclusion -- the central design decision.
# ---------------------------------------------------------------------------


def test_multi_person_photo_is_indexed_but_never_staged(adapter, tmp_path, fake_detector):
    root = tmp_path / "photos"
    root.mkdir()
    _touch(root / "groupphoto.jpg")
    _touch(root / "persona-1.jpg")
    _touch(root / "persona-2.jpg")
    _configure_root(adapter, root)

    result = pipeline.run_faces(adapter, threshold=0.9)

    # The group photo's 2 faces WERE detected/indexed (files_scanned/indexed
    # count it), but it never produces a staged entry.
    assert result["files_scanned"] == 3
    entries = queue_module.load_queue(adapter, queue_module.default_queue_path(adapter))
    assert len(entries) == 2  # only the two persona photos
    assert all("groupphoto" not in e.src for e in entries)


def test_photo_with_an_unclustered_singleton_face_is_never_staged(adapter, tmp_path, fake_detector):
    root = tmp_path / "photos"
    root.mkdir()
    _touch(root / "singleton.jpg")
    _configure_root(adapter, root)

    result = pipeline.run_faces(adapter, threshold=0.9)

    assert result["staged_entry_ids"] == []


def test_zero_face_photo_is_never_staged_and_indexed_as_scanned(adapter, tmp_path, fake_detector):
    root = tmp_path / "photos"
    root.mkdir()
    _touch(root / "blank.jpg")
    _configure_root(adapter, root)

    result = pipeline.run_faces(adapter, threshold=0.9)

    assert result["staged_entry_ids"] == []
    assert result["files_indexed"] == 1  # scanned once, sentinel recorded


# ---------------------------------------------------------------------------
# Incremental re-scanning -- including the zero-face sentinel's whole point.
# ---------------------------------------------------------------------------


def test_second_run_does_not_re_detect_already_scanned_photos(adapter, tmp_path, fake_detector):
    root = tmp_path / "photos"
    root.mkdir()
    _touch(root / "persona-1.jpg")
    _configure_root(adapter, root)

    pipeline.run_faces(adapter, threshold=0.9)
    assert len(fake_detector.detected_paths) == 1

    pipeline.run_faces(adapter, threshold=0.9)  # nothing changed on disk

    assert len(fake_detector.detected_paths) == 1  # no new detect() calls


def test_zero_face_photo_is_not_re_detected_on_a_second_run(adapter, tmp_path, fake_detector):
    """The real point of index.mark_scanned_no_faces: without it, a
    zero-face photo would be indistinguishable from a never-scanned one and
    pay the real detection cost again every single run.
    """
    root = tmp_path / "photos"
    root.mkdir()
    _touch(root / "blank.jpg")
    _configure_root(adapter, root)

    pipeline.run_faces(adapter, threshold=0.9)
    assert len(fake_detector.detected_paths) == 1

    pipeline.run_faces(adapter, threshold=0.9)

    assert len(fake_detector.detected_paths) == 1


# ---------------------------------------------------------------------------
# Shared walk reuse -- protected-path guard fires identically to documents.
# ---------------------------------------------------------------------------


def test_protected_path_is_never_scanned_for_faces_either(adapter, tmp_path, fake_detector, monkeypatch):
    fake_system_root = tmp_path / "FakeSystem"
    fake_system_root.mkdir()
    _touch(fake_system_root / "persona-1.jpg")
    monkeypatch.setattr(queue_module, "PROTECTED_PATH_ROOTS", [fake_system_root])
    _configure_root(adapter, fake_system_root)

    result = pipeline.run_faces(adapter, threshold=0.9)

    assert result["files_scanned"] == 0
    assert fake_detector.detected_paths == []


def test_non_image_files_are_skipped_by_the_face_pipeline(adapter, tmp_path, fake_detector):
    root = tmp_path / "photos"
    root.mkdir()
    _touch(root / "persona-1.jpg")
    (root / "notes.txt").write_text("not a photo")
    _configure_root(adapter, root)

    result = pipeline.run_faces(adapter, threshold=0.9)

    assert result["files_scanned"] == 1  # only the .jpg
    assert all("notes.txt" not in p for p in fake_detector.detected_paths)
