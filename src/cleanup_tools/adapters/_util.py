"""Small helpers shared between OS adapter implementations."""

from __future__ import annotations

import fnmatch


def matches_pattern(name: str, pattern: str) -> bool:
    """Case-insensitive glob match, falling back to an exact match.

    ``name`` and ``pattern`` are expected to already be lowercased by the
    caller. ``pattern`` is expected to look like a filename glob (e.g.
    ``"screenshot*"``); if it contains no glob characters, it names a literal
    filename (e.g. ``".DS_Store"``), so it must match exactly rather than as
    a prefix -- a prefix match would incorrectly sweep up unrelated files
    like ``.DS_Storee`` or ``Thumbs.dbx`` into anything that filters on this
    helper (notably ``reclaim``'s OS-junk category, which deletes what it
    matches).
    """
    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatch(name, pattern)
    return name == pattern
