"""Small helpers shared between OS adapter implementations."""

from __future__ import annotations

import fnmatch


def matches_pattern(name: str, pattern: str) -> bool:
    """Case-insensitive glob match, falling back to a prefix match.

    ``name`` and ``pattern`` are expected to already be lowercased by the
    caller. ``pattern`` is expected to look like a filename glob (e.g.
    ``"screenshot*"``); if it contains no glob characters, treat it as a
    simple prefix match.
    """
    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatch(name, pattern)
    return name.startswith(pattern)
