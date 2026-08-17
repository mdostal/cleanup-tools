#!/usr/bin/env python3
"""One-time setup script: fetch and verify the InsightFace ``buffalo_l``
model weights the photo-face-clustering epic's face detection/identity-
embedding feature needs, into ``~/.cache/cleanup-tools/models/buffalo_l/``
-- the SAME directory ``semantic/faces.py``'s ``default_models_dir()`` reads
from at runtime, and the SAME files
``packaging/pyinstaller/cleanup_ui.spec`` bundles for a frozen build.

This is a deliberate, explicit, developer/build-time step -- never
something the running app does silently. ``semantic/faces.py`` never
downloads anything itself (see that module's docstring and
``insightface.model_zoo.get_model``'s local-file-only contract, confirmed
via direct source inspection during this story's implementation).

Only the two files this feature actually uses (SCRFD detection: face
bounding boxes + 5-point landmarks; ArcFace recognition: 512-d identity
embeddings) are kept -- the full ``buffalo_l`` release also bundles
age/gender and 2D/3D landmark models (none of which this epic's scope
needs), which are discarded after extraction. This cuts local disk usage
from the full pack's ~330MB down to ~190MB.

Usage: ``python3 scripts/fetch-semantic-face-models.py``
"""

from __future__ import annotations

import hashlib
import shutil
import ssl
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

BUFFALO_L_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"

# Pinned SHA256 checksums -- computed from a real, verified download during
# this story's implementation (2026-08-17). A mismatch means either a
# corrupted download or the upstream release changed unexpectedly; either
# way, this script fails loudly rather than silently vendoring unverified
# model weights.
EXPECTED_SHA256 = {
    "det_10g.onnx": "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91",
    "w600k_r50.onnx": "4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43",
}

DEST_DIR = Path.home() / ".cache" / "cleanup-tools" / "models" / "buffalo_l"


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _already_verified() -> bool:
    return all(
        (DEST_DIR / name).exists() and _sha256(DEST_DIR / name) == expected
        for name, expected in EXPECTED_SHA256.items()
    )


def main() -> int:
    DEST_DIR.mkdir(parents=True, exist_ok=True)

    if _already_verified():
        print(f"Model weights already present and verified at {DEST_DIR}")
        return 0

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        zip_path = tmp / "buffalo_l.zip"
        print(f"Downloading {BUFFALO_L_URL} ...")
        # Some macOS Python.org installs ship without a working default CA
        # bundle for urllib specifically (a well-known python.org-installer
        # gotcha -- curl/the system's own store works fine, plain urllib
        # doesn't) -- use certifi's bundle explicitly rather than assuming
        # the interpreter's default SSL context is populated.
        try:
            import certifi

            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ctx = ssl.create_default_context()
        with urllib.request.urlopen(BUFFALO_L_URL, context=ctx) as response, open(zip_path, "wb") as out:
            shutil.copyfileobj(response, out)

        with zipfile.ZipFile(zip_path) as zf:
            # The release zip's members are flat (no "buffalo_l/" prefix
            # inside the archive itself) -- confirmed via zf.namelist()
            # during this story's implementation, not assumed from the
            # archive's own filename.
            for name in EXPECTED_SHA256:
                zf.extract(name, tmp)

        for name, expected in EXPECTED_SHA256.items():
            extracted = tmp / name
            actual = _sha256(extracted)
            if actual != expected:
                print(
                    f"ERROR: {name} checksum mismatch: expected {expected}, got {actual} "
                    "-- refusing to vendor an unverified model file.",
                    file=sys.stderr,
                )
                return 1
            shutil.move(str(extracted), str(DEST_DIR / name))

    print(f"Fetched and verified: {', '.join(EXPECTED_SHA256)} -> {DEST_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
