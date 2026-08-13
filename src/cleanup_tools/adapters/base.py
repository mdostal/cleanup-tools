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

import contextlib
import fcntl
import os
import shutil
import subprocess
import tempfile
import time
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

    def write_file_atomic(self, path: Path, content: str) -> None:
        """Write ``content`` to ``path`` without ever leaving a partial file.

        The content is first written to a temp file created in ``path``'s
        own directory -- not the system temp dir -- so the final
        ``os.replace`` stays on one filesystem; a cross-filesystem replace
        is not atomic (and can outright fail). ``os.replace`` is atomic on
        POSIX, so readers of ``path`` only ever see the old content or the
        new content in full, never a half-written file. If anything raises
        before the replace happens, the temp file is cleaned up rather than
        left behind.
        """
        path = Path(path)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    @contextlib.contextmanager
    def file_lock(self, path: Path, timeout: float = 5.0):
        """Context manager guarding ``path`` with an exclusive advisory lock.

        The lock is taken on a sibling ``<path>.lock`` file, never on
        ``path`` itself, so acquiring/releasing the lock never touches
        ``path``'s actual content. Lock acquisition is polled (rather than
        making a single blocking ``fcntl.flock`` call) so that ``timeout``
        is genuinely respected instead of blocking forever; raises
        ``TimeoutError`` if the lock isn't acquired within ``timeout``
        seconds. The lock is released on exit, including when the body
        raises.
        """
        path = Path(path)
        lock_path = Path(f"{path}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Timed out after {timeout}s waiting for lock on {lock_path}"
                        )
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

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

    def list_subdirs(self, path: Path) -> list[Path]:
        """Return the immediate child directories of ``path`` (not recursive).

        Files directly inside ``path`` are excluded; only entries where
        ``is_dir()`` is true are returned.
        """
        root = Path(path)
        return sorted(p for p in root.iterdir() if p.is_dir())

    def find_dirs(
        self,
        root: Path,
        name: str,
        max_depth: int | None = None,
    ) -> list[Path]:
        """Recursively find directories under ``root`` named exactly ``name``.

        Mirrors ``find <root> -type d -name <name> -prune``: once a matching
        directory is found, its contents are not walked looking for further
        (nested) matches. ``max_depth`` limits how far the walk descends
        (``None`` means unlimited), using the same depth semantics as
        :meth:`list_dir`.
        """
        root = Path(root)
        results: list[Path] = []

        for dirpath, dirnames, _filenames in os.walk(root):
            depth = len(Path(dirpath).relative_to(root).parts)

            matched = [d for d in dirnames if d == name]
            for d in matched:
                results.append(Path(dirpath) / d)
            # Prune matches: don't descend into a matched dir looking for
            # nested matches.
            dirnames[:] = [d for d in dirnames if d not in matched]

            if max_depth is not None and depth >= max_depth:
                dirnames[:] = []

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

    def dir_size_bytes(self, path: Path) -> int:
        """Return the recursive size of ``path`` in bytes, via ``du -sk``.

        Shells out to ``du -sk <path>`` rather than walking the tree in
        Python (``os.walk`` + per-file ``Path.stat()``): on a real home
        directory with hundreds of thousands of small files, a Python walk
        can take minutes, while ``du`` uses a fast kernel-level syscall path
        and finishes in seconds. ``-k`` (kilobytes) is used instead of
        ``-sh`` because its output is a plain, machine-parseable integer
        that's consistent across macOS and GNU ``du`` -- ``-h`` applies
        human-readable unit formatting that differs between BSD (macOS) and
        GNU (Linux) ``du``.

        Permission errors on individual subdirectories are normal (e.g.
        under ``~/Library``) and are suppressed via ``stderr=DEVNULL``; ``du``
        still prints a total for what it *could* read, even when it exits
        non-zero because of those errors, so a non-zero return code alone
        doesn't raise. Only an empty/unparseable stdout raises.
        """
        result = subprocess.run(
            ["du", "-sk", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        first_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        fields = first_line.split()
        if not fields:
            raise RuntimeError(f"du -sk {path!s} produced no parseable output")
        try:
            size_kb = int(fields[0])
        except ValueError as exc:
            raise RuntimeError(
                f"du -sk {path!s} produced unparseable output: {first_line!r}"
            ) from exc
        return size_kb * 1024

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

    @abstractmethod
    def set_screenshot_save_location(self, path: Path) -> None:
        """Point the OS's built-in screenshot tool at ``path`` going forward.

        This is a macOS-only concept: ``defaults write
        com.apple.screencapture location <path>`` (plus restarting
        SystemUIServer so it takes effect immediately) changes where new
        screenshots are saved system-wide. It is declared here so callers
        have a single interface, but not every OS has an equivalent
        system-wide screenshot-location preference (see
        ``ArchLinuxAdapter``), following ``find_installed_app``'s precedent
        exactly for this "one real implementation, one NotImplementedError
        stub" shape.
        """
