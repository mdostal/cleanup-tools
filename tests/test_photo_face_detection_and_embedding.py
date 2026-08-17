"""Tests for cleanup_tools.semantic.faces.

Real-framework tests, not mocked: FaceDetector calls the ACTUAL InsightFace/
onnxruntime models -- per this session's established "no test depends on a
mocked platform-framework call returning fabricated results as if they were
real" discipline. Skipped (not faked) when the vendored model weights
haven't been fetched via scripts/fetch-semantic-face-models.py, mirroring
semantic-extraction-and-embedding's "skippable, never mocked into false
confidence" pattern for a real-framework dependency.

No face-photo test fixture is committed to this repo (a redistributable,
unambiguously-licensed real face photo small enough to commit is a real
constraint -- model weights aren't committed for the same class of reason,
see scripts/fetch-semantic-face-models.py). Tests that need a real
detectable face fetch the well-known OpenCV project's own "lena.jpg" sample
test asset (used ubiquitously for exactly this purpose across the computer-
vision testing ecosystem) at test setup time into a session-scoped cache,
and skip gracefully if unreachable (e.g. offline CI) rather than failing.
"""

from __future__ import annotations

import ssl
import urllib.request
from pathlib import Path

import pytest

from cleanup_tools.adapters import MacOSAdapter
from cleanup_tools.semantic import faces

_MODELS_PRESENT = (
    faces.default_models_dir(MacOSAdapter()) / faces.DETECTION_MODEL_FILENAME
).exists() and (faces.default_models_dir(MacOSAdapter()) / faces.RECOGNITION_MODEL_FILENAME).exists()

pytestmark_models = pytest.mark.skipif(
    not _MODELS_PRESENT,
    reason="face model weights not vendored -- run scripts/fetch-semantic-face-models.py",
)

_TEST_PHOTO_URL = "https://raw.githubusercontent.com/opencv/opencv/master/samples/data/lena.jpg"
_TEST_PHOTO_CACHE = Path.home() / ".cache" / "cleanup-tools" / "test-fixtures" / "lena.jpg"


@pytest.fixture(scope="session")
def real_face_photo() -> Path:
    if not _TEST_PHOTO_CACHE.exists():
        _TEST_PHOTO_CACHE.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Some macOS Python.org installs ship without a working default
            # CA bundle for urllib specifically (see
            # scripts/fetch-semantic-face-models.py's identical fix) --
            # use certifi's bundle explicitly.
            try:
                import certifi

                ctx = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                ctx = ssl.create_default_context()
            with urllib.request.urlopen(_TEST_PHOTO_URL, context=ctx) as response:
                _TEST_PHOTO_CACHE.write_bytes(response.read())
        except OSError:
            pytest.skip("could not fetch the real-face test photo (offline?)")
    return _TEST_PHOTO_CACHE


@pytest.fixture
def adapter() -> MacOSAdapter:
    return MacOSAdapter()  # real home -- this is where the real vendored models live.


def test_default_models_dir_is_under_cache_not_config(adapter):
    result = faces.default_models_dir(adapter)
    assert ".cache" in result.parts
    assert "cleanup-tools" in result.parts
    assert "buffalo_l" in result.parts


def test_get_face_detector_raises_file_not_found_when_models_missing(tmp_path):
    class EmptyHomeAdapter(MacOSAdapter):
        def resolve_home(self):
            return tmp_path

    with pytest.raises(FileNotFoundError):
        faces.get_face_detector(EmptyHomeAdapter())


@pytestmark_models
def test_detect_finds_a_real_face_in_a_real_photo(adapter, real_face_photo):
    detector = faces.get_face_detector(adapter)

    results = detector.detect(real_face_photo)

    assert len(results) == 1
    assert len(results[0].bbox) == 4
    assert len(results[0].embedding) == faces.EMBEDDING_DIMENSION


@pytestmark_models
def test_detect_returns_empty_list_for_an_image_with_no_faces(adapter, tmp_path):
    from PIL import Image

    blank = tmp_path / "blank.png"
    Image.new("RGB", (100, 100), color="white").save(blank)
    detector = faces.get_face_detector(adapter)

    assert detector.detect(blank) == []


@pytestmark_models
def test_detect_returns_empty_list_for_a_nonexistent_file_never_raises(adapter, tmp_path):
    detector = faces.get_face_detector(adapter)

    assert detector.detect(tmp_path / "does-not-exist.jpg") == []


@pytestmark_models
def test_detect_returns_empty_list_for_a_corrupt_file_never_raises(adapter, tmp_path):
    corrupt = tmp_path / "corrupt.jpg"
    corrupt.write_bytes(b"not actually a real jpeg, just garbage bytes" * 20)
    detector = faces.get_face_detector(adapter)

    assert detector.detect(corrupt) == []


@pytestmark_models
def test_detect_is_deterministic(adapter, real_face_photo):
    detector = faces.get_face_detector(adapter)

    result1 = detector.detect(real_face_photo)
    result2 = detector.detect(real_face_photo)

    assert result1[0].embedding == result2[0].embedding
    assert result1[0].bbox == result2[0].bbox


@pytestmark_models
def test_model_loading_never_reaches_the_network(adapter, real_face_photo, monkeypatch):
    """The load-bearing offline-reliability guarantee: constructing a
    FaceDetector and running real detection/embedding must never open a
    real socket connection -- proves insightface.model_zoo.get_model()'s
    local-file-only contract holds in practice, not just by source reading.
    """

    def _blow_up(*_args, **_kwargs):
        raise RuntimeError("Real network call attempted during face detection -- must never happen")

    monkeypatch.setattr("socket.socket.connect", _blow_up)

    detector = faces.get_face_detector(adapter)
    results = detector.detect(real_face_photo)

    assert len(results) == 1  # the real work still happened, not silently skipped
