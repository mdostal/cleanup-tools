"""Optional pytest wrapper around ``scripts/smoke_test_sidecar.py``.

Skipped by default (and therefore harmless in the normal ``pytest`` run
this repo's other 400+ tests are part of): it needs a pre-built PyInstaller
binary, which doesn't exist until someone has run
``pyinstaller packaging/pyinstaller/cleanup_ui.spec`` (see that spec
file's docstring for the ``ONEFILE=0``/``ONEFILE=1`` build modes). Opt in
by pointing ``CLEANUP_TOOLS_SIDECAR_BINARY`` at a built binary:

    ONEFILE=0 pyinstaller packaging/pyinstaller/cleanup_ui.spec
    CLEANUP_TOOLS_SIDECAR_BINARY=packaging/dist/cleanup-ui-onedir/cleanup-ui-onedir \\
        pytest tests/test_smoke_sidecar.py -v

This shells out to the script (via ``subprocess.run``) rather than
importing and calling its functions directly -- the script starts real OS
subprocesses, binds real sockets, and is meant to run standalone against
whatever binary path it's given; keeping it a subprocess call here means
this test file has zero import-time dependency on anything
PyInstaller-specific, and a broken/incompatible binary can never crash
pytest's own collection or the rest of the suite.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

BINARY = os.environ.get("CLEANUP_TOOLS_SIDECAR_BINARY")
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "smoke_test_sidecar.py"


@pytest.mark.skipif(
    not BINARY,
    reason=(
        "set CLEANUP_TOOLS_SIDECAR_BINARY to a built PyInstaller binary path "
        "(see packaging/pyinstaller/cleanup_ui.spec) to run this"
    ),
)
def test_frozen_sidecar_binary_passes_the_full_smoke_test():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), BINARY],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"smoke test failed against {BINARY!r}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
