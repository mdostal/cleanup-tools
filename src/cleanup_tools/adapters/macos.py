"""macOS implementation of :class:`OSAdapter`.

All shared, stdlib-backed behavior (file/dir ops, disk usage, docker
detection, etc.) lives in :class:`~cleanup_tools.adapters.base.OSAdapter`.
This module only supplies the one genuinely macOS-specific method,
``find_installed_app``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .base import OSAdapter


class MacOSAdapter(OSAdapter):
    """OSAdapter implementation for macOS."""

    def __init__(self, applications_dir: Path = Path("/Applications")) -> None:
        """``applications_dir`` defaults to ``/Applications`` but can be
        overridden (e.g. in tests, to point at a ``tmp_path`` fixture
        containing a fake ``.app`` bundle) without touching the real
        filesystem location.
        """
        self.applications_dir = Path(applications_dir)

    def find_installed_app(self, installer_path: Path) -> bool:
        """Case-insensitive substring match of the installer's basename against
        app names in ``self.applications_dir``. Good enough for v1 (see base
        class docstring).
        """
        installer_path = Path(installer_path)
        stem = installer_path.name
        for suffix in (".dmg", ".pkg"):
            if stem.lower().endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        stem_lower = stem.lower()

        applications = self.applications_dir
        if not applications.is_dir():
            return False

        for entry in applications.iterdir():
            if entry.suffix.lower() != ".app":
                continue
            app_name = entry.stem.lower()
            if stem_lower in app_name or app_name in stem_lower:
                return True

        return False

    def set_screenshot_save_location(self, path: Path) -> None:
        """Point macOS's built-in screenshot tool at ``path`` going forward.

        Two ``subprocess.run`` calls, mirroring ``reclaim.py``'s
        ``_run_docker`` calling convention (list-form argv, no
        ``shell=True``, output captured rather than left to inherit the
        caller's stdout/stderr): first ``defaults write
        com.apple.screencapture location <path>`` writes the actual
        preference -- this one uses ``check=True`` because a failure here
        means the preference was never actually changed, which is a real
        error the caller needs to see, not something to swallow. Then
        ``killall SystemUIServer`` restarts the process that reads that
        preference, so the new location takes effect immediately instead of
        only after the next login; this second call is deliberately NOT
        ``check=True`` -- SystemUIServer might not be running (uncommon but
        possible), and failing to restart it is a cosmetic inconvenience
        (the new location still takes effect after the next login) rather
        than a reason to report the whole operation as failed.
        """
        path = Path(path)
        subprocess.run(
            ["defaults", "write", "com.apple.screencapture", "location", str(path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["killall", "SystemUIServer"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
