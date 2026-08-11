"""macOS implementation of :class:`OSAdapter`.

All shared, stdlib-backed behavior (file/dir ops, disk usage, docker
detection, etc.) lives in :class:`~cleanup_tools.adapters.base.OSAdapter`.
This module only supplies the one genuinely macOS-specific method,
``find_installed_app``.
"""

from __future__ import annotations

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
