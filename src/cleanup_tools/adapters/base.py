"""Abstract OS adapter interface.

Each supported operating system implements this interface so that the rest
of the cleanup-tools codebase can stay OS-agnostic. Adapters are intentionally
"dumb": they perform the requested filesystem operation without dry-run or
safety checks. Any such policy belongs in the calling code, not here.

Concrete OS adapters (macOS, Arch Linux) currently share every method's
implementation except ``find_installed_app``, which is genuinely OS-specific
(see its docstring below). Rather than keep two copies of nine identical
methods that could silently drift, those shared implementations live here as
concrete methods; subclasses only need to implement ``find_installed_app``.
"""

from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod
from pathlib import Path

from ._util import matches_pattern


class OSAdapter(ABC):
    """Interface for OS-specific filesystem and system operations."""

    def read_file(self, path: Path) -> str:
        """Read and return the full text contents of ``path``."""
        return Path(path).read_text()

    def write_file(self, path: Path, content: str) -> None:
        """Write ``content`` to ``path``, overwriting any existing file."""
        Path(path).write_text(content)

    def list_dir(
        self,
        path: Path,
        max_depth: int | None = None,
        pattern: str | None = None,
    ) -> list[Path]:
        """Walk ``path`` up to ``max_depth`` levels and return matching files.

        ``max_depth`` of ``None`` means unlimited depth; ``0`` means only the
        entries directly inside ``path``. ``pattern`` is a case-insensitive
        glob/prefix match against filenames (e.g. ``"screenshot*"``).
        """
        root = Path(path)
        pattern_lower = pattern.lower() if pattern else None
        results: list[Path] = []

        for dirpath, dirnames, filenames in os.walk(root):
            depth = len(Path(dirpath).relative_to(root).parts)
            if max_depth is not None and depth >= max_depth:
                dirnames[:] = []

            for filename in filenames:
                if pattern_lower is None or matches_pattern(filename.lower(), pattern_lower):
                    results.append(Path(dirpath) / filename)

        return results

    def move(self, src: Path, dst: Path) -> None:
        """Move ``src`` to ``dst``, creating destination parent dirs as needed."""
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))

    def delete(self, path: Path, recursive: bool = False) -> None:
        """Delete ``path``. A no-op if ``path`` does not exist (never raises)."""
        path = Path(path)
        if not path.exists():
            return
        if path.is_dir():
            if recursive:
                shutil.rmtree(path)
            else:
                path.rmdir()
        else:
            path.unlink()

    def resolve_home(self) -> Path:
        """Return the current user's home directory."""
        return Path.home()

    def resolve_standard_dir(self, kind: str) -> Path:
        """Return the path for a standard user directory.

        ``kind`` is one of ``"downloads"``, ``"desktop"``, ``"documents"``,
        or ``"pictures"``.
        """
        home = self.resolve_home()
        mapping = {
            "downloads": home / "Downloads",
            "desktop": home / "Desktop",
            "documents": home / "Documents",
            "pictures": home / "Pictures",
        }
        if kind not in mapping:
            raise ValueError(f"Unknown standard dir kind: {kind!r}")
        return mapping[kind]

    def disk_usage(self, path: Path) -> dict:
        """Return ``{"total": int, "used": int, "free": int}`` in bytes for ``path``."""
        usage = shutil.disk_usage(path)
        return {"total": usage.total, "used": usage.used, "free": usage.free}

    def is_docker_installed(self) -> bool:
        """Return whether a ``docker`` executable is available on PATH."""
        return shutil.which("docker") is not None

    @abstractmethod
    def find_installed_app(self, installer_path: Path) -> bool:
        """Return whether the app an installer would install is already present.

        This is a macOS-only concept: a leftover ``.dmg``/``.pkg`` installer
        can be safely deleted if the app it installs already lives in
        ``/Applications``. It is declared here so callers have a single
        interface, but not every OS has an equivalent notion of "orphaned
        installer file" (see ``ArchLinuxAdapter``).
        """
