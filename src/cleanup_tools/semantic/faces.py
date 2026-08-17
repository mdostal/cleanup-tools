"""Local face detection + identity embedding via InsightFace's SCRFD
detector and ArcFace recognizer, run through ``onnxruntime`` (CoreML
execution provider auto-engaging on Apple Silicon, plain CPU elsewhere --
including Arch, a genuine cross-platform capability from day one; see
``.pHive/epics/photo-face-clustering/docs/design-discussion.md`` §2 for why
this epic can't reuse phase 1's Apple-native-only pattern: Apple's public
Vision framework has no face-*identity*-embedding API, only detection).

**Model weights are never downloaded by this module, or by the running app
at all.** They're fetched ONCE, explicitly, by
``scripts/fetch-semantic-face-models.py`` (a developer/build-time step,
checksum-verified) into ``default_models_dir()``. Loaded here via
``insightface.model_zoo.get_model(<local .onnx path>, ...)`` -- NOT
``insightface.app.FaceAnalysis``'s high-level auto-downloading convenience
wrapper. Confirmed via direct source inspection of the installed
``insightface`` package (not assumed) that ``get_model()`` treats a
``.onnx``-suffixed ``name`` argument as a literal local file path and only
ever attempts a network fetch when the caller explicitly passes
``download=True`` -- never done here, and the offline-reliability test in
this module's test file blocks real socket connections to prove it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from ..adapters.base import OSAdapter

MODEL_PACK = "buffalo_l"
DETECTION_MODEL_FILENAME = "det_10g.onnx"
RECOGNITION_MODEL_FILENAME = "w600k_r50.onnx"
EMBEDDING_DIMENSION = 512

# CoreML first (auto-engages ANE/GPU acceleration on Apple Silicon; falls
# back to CPU transparently on Intel Mac / when a given op isn't
# CoreML-supported), CPU always as the universal fallback -- including Arch,
# where CoreML simply isn't in onnxruntime's available-providers list at
# all. Never CUDA: this app never assumes/requires a discrete GPU.
_PROVIDERS = ["CoreMLExecutionProvider", "CPUExecutionProvider"]


@dataclass
class FaceDetection:
    """One detected face within a photo: its bounding box and 512-d
    ArcFace identity embedding.
    """

    bbox: tuple[float, float, float, float]
    embedding: list[float]


def _frozen_models_dir() -> Path | None:
    """The vendored ``.onnx`` files bundled directly into a frozen
    PyInstaller build, if this process IS one -- ``None`` when running from
    source. ``sys._MEIPASS`` is PyInstaller's own runtime attribute
    pointing at the bundle's resource root; for this project's onedir build
    target (see ``packaging/pyinstaller/cleanup_ui.spec``'s own docstring
    for why onedir was chosen over onefile) that's a stable directory next
    to the executable, not a transient extraction -- so reading straight
    out of it needs no first-run copy step.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass is None:
        return None
    return Path(meipass) / "cleanup_tools" / "semantic" / "models" / MODEL_PACK


def default_models_dir(adapter: OSAdapter) -> Path:
    """Where vendored face-model ``.onnx`` files live.

    Checked in order:
    1. Bundled into a frozen PyInstaller build (see ``_frozen_models_dir``)
       -- a shipped desktop app carries its own model weights, so an end
       user never has to run a separate fetch step.
    2. ``scripts/fetch-semantic-face-models.py``'s dev-mode target,
       ``~/.cache/cleanup-tools/models/buffalo_l/`` -- used when running
       from source, where no frozen bundle exists.

    Either way, never downloaded by this running app at runtime -- see this
    module's docstring.
    """
    frozen = _frozen_models_dir()
    if (
        frozen is not None
        and (frozen / DETECTION_MODEL_FILENAME).exists()
        and (frozen / RECOGNITION_MODEL_FILENAME).exists()
    ):
        return frozen
    return adapter.resolve_home() / ".cache" / "cleanup-tools" / "models" / MODEL_PACK


class FaceDetector:
    """Wraps InsightFace's SCRFD detector + ArcFace recognizer, loaded from
    vendored local files only -- see this module's docstring.
    """

    def __init__(self, models_dir: Path):
        det_path = models_dir / DETECTION_MODEL_FILENAME
        rec_path = models_dir / RECOGNITION_MODEL_FILENAME
        if not det_path.exists() or not rec_path.exists():
            raise FileNotFoundError(
                f"Face model weights not found at {models_dir} -- run "
                "scripts/fetch-semantic-face-models.py first."
            )

        import insightface.model_zoo as model_zoo

        self._detector = model_zoo.get_model(str(det_path), providers=_PROVIDERS)
        self._recognizer = model_zoo.get_model(str(rec_path), providers=_PROVIDERS)
        self._detector.prepare(ctx_id=0, input_size=(640, 640))

    def detect(self, image_path: Path) -> list[FaceDetection]:
        """Every face detected in ``image_path``, each with its own
        bounding box and identity embedding -- one entry per face, so a
        multi-person photo yields multiple entries (design discussion §5's
        multi-person-photo handling is a LATER module's concern --
        ``pipeline.py``, not this one).

        Never raises for a real-but-unusual image (corrupt, unreadable,
        unsupported format, zero faces) -- returns an empty list instead,
        mirroring ``extract.extract_text``'s "never raise" contract from
        phase 1.
        """
        import cv2
        from insightface.utils import face_align

        img = cv2.imread(str(image_path))
        if img is None:
            return []

        try:
            bboxes, kpss = self._detector.detect(img, max_num=0, metric="default")
        except Exception:
            return []
        if bboxes is None or len(bboxes) == 0:
            return []

        results: list[FaceDetection] = []
        for bbox, kps in zip(bboxes, kpss):
            try:
                aligned = face_align.norm_crop(img, landmark=kps, image_size=self._recognizer.input_size[0])
                embedding = self._recognizer.get_feat(aligned).flatten()
            except Exception:
                continue
            results.append(
                FaceDetection(
                    bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
                    embedding=[float(x) for x in embedding],
                )
            )
        return results


def get_face_detector(adapter: OSAdapter) -> FaceDetector:
    """Return the local face detector.

    Cross-platform (Mac + Arch) from day one -- unlike phase 1's text
    embedder, no local-native alternative exists on ANY platform, so
    there's no Mac-only path to prefer. Raises ``FileNotFoundError`` (not
    ``NotImplementedError``) if the vendored model weights haven't been
    fetched yet -- a setup-step gap, not a platform gap.
    """
    return FaceDetector(default_models_dir(adapter))
