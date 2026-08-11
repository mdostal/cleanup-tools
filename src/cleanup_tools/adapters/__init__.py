"""OS adapter factory.

Callers should go through :func:`get_adapter` rather than importing a
specific adapter class directly, so the rest of the codebase stays
OS-agnostic.
"""

from __future__ import annotations

import platform

from .arch_linux import ArchLinuxAdapter
from .base import OSAdapter
from .macos import MacOSAdapter

__all__ = ["OSAdapter", "MacOSAdapter", "ArchLinuxAdapter", "get_adapter"]


def get_adapter() -> OSAdapter:
    """Detect the current OS and return the matching :class:`OSAdapter`.

    Windows is explicitly not a v1 target.
    """
    system = platform.system()

    if system == "Darwin":
        return MacOSAdapter()

    if system == "Linux":
        # v1 only targets Arch Linux; other distros aren't distinguished yet.
        return ArchLinuxAdapter()

    raise RuntimeError(
        f"Unsupported OS: {system!r}. cleanup_tools supports macOS and Arch "
        "Linux only for v1 (Windows is explicitly not a v1 target)."
    )
