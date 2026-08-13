"""Arch Linux implementation of :class:`OSAdapter`.

All shared, stdlib-backed behavior (file/dir ops, disk usage, docker
detection, etc.) lives in :class:`~cleanup_tools.adapters.base.OSAdapter`.
This module only supplies the one genuinely OS-specific method,
``find_installed_app``, which has no equivalent on Arch Linux (see below).
"""

from __future__ import annotations

from pathlib import Path

from .base import OSAdapter


class ArchLinuxAdapter(OSAdapter):
    """OSAdapter implementation for Arch Linux."""

    def find_installed_app(self, installer_path: Path) -> bool:
        """Not applicable on Arch Linux.

        Arch's package manager (pacman/AUR helpers) already tracks installed
        state, so there's no equivalent notion of an "orphaned installer
        file" the way a leftover ``.dmg``/``.pkg`` works on macOS. Detecting
        "is this downloaded package file already installed" would mean
        parsing package metadata (pacman database, PKGBUILD, etc.), which is
        out of scope for v1.
        """
        raise NotImplementedError(
            "find_installed_app is a macOS-only concept for v1: orphaned "
            "installer detection relies on comparing a .dmg/.pkg basename "
            "against /Applications. Arch Linux's package manager already "
            "tracks installed state, so there is no equivalent 'orphaned "
            "installer file' to detect here."
        )

    def set_screenshot_save_location(self, path: Path) -> None:
        """Not applicable on Arch Linux.

        There is no single system-wide "default screenshot save location"
        preference on Arch the way macOS's screencapture utility has one --
        that's a property of whichever screenshot tool (flameshot, grim,
        GNOME Screenshot, ...) happens to be installed and configured, each
        with its own config format, not something this adapter can set
        uniformly. Out of scope for v1.
        """
        raise NotImplementedError(
            "set_screenshot_save_location is a macOS-only concept for v1: "
            "it maps directly onto `defaults write com.apple.screencapture "
            "location`. Arch Linux has no single system-wide screenshot "
            "tool/preference to target the same way."
        )
