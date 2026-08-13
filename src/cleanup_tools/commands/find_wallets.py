"""Read-only crypto-wallet artifact scanner -- the Python port of
``scripts/find-wallets.sh``.

Like ``survey.py``, this command never writes, moves, or deletes anything --
there is no ``--go``/dry-run concept here at all, matching the bash
script's existing safety property (it only ever prints candidate *paths*).

The bash script has two independent phases, both preserved here:

1. ``by filename`` -- a single ``find`` with 14 ``-iname`` alternatives
   (case-insensitive filename globs), excluding anything under a
   ``node_modules`` or ``Library/Caches`` path segment, capped at the first
   200 matches (``head -200``).
2. ``by content`` -- a ``grep -rlIE`` over ``Documents``/``Desktop``/
   ``Downloads`` for 5 text-ish extensions, excluding ``node_modules``/
   ``.git``, matching any of 4 regex alternatives (seed phrase word-runs,
   BIP32 ``xprv...`` extended keys, PEM private-key headers, or an eth
   keystore's ``"crypto": {... "cipher"`` shape), capped at the first 100
   matches (``head -100``).

Every one of the patterns/regexes below is copied character-for-character
from ``scripts/find-wallets.sh`` -- see that file for the canonical source.
Only file *paths* are ever returned; the actual matched text (a real seed
phrase, an actual private key, etc.) must never appear anywhere in this
module's output. Concretely: a ``re.Match`` object is never assigned to a
variable that outlives the ``if`` check that produced it, and the
match-text-extracting method these functions never call -- the one you'd
reach for via ``match_obj<dot>group()`` to pull matched text back out of a
``re.Match`` -- is never invoked anywhere in this module: there is nothing
here capable of extracting or returning matched substrings, by
construction.
"""

from __future__ import annotations

import re
from pathlib import Path

from cleanup_tools.adapters._util import matches_pattern
from cleanup_tools.adapters.base import OSAdapter

# --- "by filename" -- 14 case-insensitive glob patterns, verbatim from the
# single `find ... -iname ... -o -iname ...` expression in find-wallets.sh.
FILENAME_PATTERNS = (
    "wallet.dat",
    "*.wallet",
    "keystore*",
    "UTC--*",
    "*.kdbx",
    "electrum*",
    "*mnemonic*",
    "*seed*phrase*",
    "*recovery*phrase*",
    "*.keychain",
    "default_wallet",
    "metamask*",
    "exodus*",
    "atomic*wallet*",
)

FILENAME_EXCLUDED_SEGMENTS = ("node_modules",)
FILENAME_EXCLUDED_PAIR = ("Library", "Caches")
FILENAME_RESULT_CAP = 200

# --- "by content" -- 4 regex alternatives, verbatim from the single
# `grep -rlIE '<pattern1>|<pattern2>|<pattern3>|<pattern4>'` in
# find-wallets.sh. Deliberately case-sensitive (the bash script uses no -i),
# and each name below identifies *which* alternative matched -- never the
# matched text itself.
CONTENT_PATTERNS = (
    ("seed_phrase", re.compile(r"(\b[a-z]+ ){11,23}[a-z]+\b")),
    ("xprv_key", re.compile(r"xprv[0-9A-Za-z]{20,}")),
    ("pem_private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("eth_keystore_cipher", re.compile(r'"crypto"\s*:\s*\{[^}]*"cipher"')),
)

CONTENT_SCAN_SUBDIRS = ("Documents", "Desktop", "Downloads")
CONTENT_EXTENSIONS = (".txt", ".json", ".md", ".rtf", ".csv")
CONTENT_EXCLUDED_SEGMENTS = ("node_modules", ".git")
CONTENT_RESULT_CAP = 100


def _filename_excluded(path: Path) -> bool:
    """True if ``path`` has a ``node_modules`` or ``Library/Caches`` segment.

    Mirrors ``! -path '*/node_modules/*' ! -path '*/Library/Caches/*'`` --
    case-sensitive, matching bash's plain (non-``-ipath``) ``-path`` test.
    """
    parts = path.parts
    if any(segment in FILENAME_EXCLUDED_SEGMENTS for segment in parts):
        return True
    a, b = FILENAME_EXCLUDED_PAIR
    return any(parts[i] == a and parts[i + 1] == b for i in range(len(parts) - 1))


def _content_excluded(path: Path) -> bool:
    """True if ``path`` has a ``node_modules`` or ``.git`` segment.

    Mirrors ``--exclude-dir=node_modules --exclude-dir=.git``.
    """
    return any(segment in CONTENT_EXCLUDED_SEGMENTS for segment in path.parts)


def _by_filename(adapter: OSAdapter, root: Path) -> list[dict]:
    """Filename-pattern scan -- a single ``adapter.list_dir`` walk, each file
    tested against all 14 patterns.

    Mirrors ``_by_content``'s structure (walk once, test every pattern per
    file encountered) rather than looping ``adapter.list_dir(root,
    pattern=p)`` once per pattern: draining one pattern's matches to
    completion before moving to the next means that once the 200-result cap
    is hit, patterns later in ``FILENAME_PATTERNS`` can be starved or
    excluded entirely, even though the bash original's single
    ``find ... -iname p1 -o -iname p2 ...`` naturally interleaves matches
    across patterns in on-disk traversal order. Walking once and testing
    every pattern per file reproduces that traversal-order-faithful
    interleaving instead.

    Pattern matching reuses ``matches_pattern`` -- the same case-insensitive
    glob/exact-match helper ``adapter.list_dir``'s ``pattern=`` argument
    uses internally -- so per-file matching here has identical semantics to
    the old per-pattern ``list_dir`` calls.

    A file can legitimately match more than one pattern (e.g.
    ``metamask.wallet`` matches both ``metamask*`` and ``*.wallet``); it is
    tagged with the first matching pattern in ``FILENAME_PATTERNS`` order,
    same contract as before this fix.

    Permission errors on individual subtrees are not fatal: ``adapter.list_dir``
    is built on ``os.walk``, which already swallows ``OSError`` from
    unreadable directories by default (yielding whatever was scannable), so
    no directory-permission error here can crash the scan.
    """
    results: list[dict] = []
    seen: set[str] = set()
    for path in adapter.list_dir(root):
        if len(results) >= FILENAME_RESULT_CAP:
            break
        if _filename_excluded(path):
            continue
        key = str(path)
        if key in seen:
            continue
        name_lower = path.name.lower()
        for pattern in FILENAME_PATTERNS:
            if matches_pattern(name_lower, pattern.lower()):
                seen.add(key)
                results.append({"path": path, "match_type": "filename", "pattern": pattern})
                break
    return results


def _by_content(adapter: OSAdapter, root: Path) -> list[dict]:
    """Content-signature scan across Documents/Desktop/Downloads.

    For each of the 5 allowed extensions' files (excluding node_modules/.git),
    read the text and test it against each of the 4 regex alternatives via
    ``re.search``. On a match, record ONLY the path, a fixed ``match_type``,
    and the alternative's name -- never the matched text. The ``re.Match``
    result of ``re.search`` is never bound to a name; it is consumed
    entirely within the ``if ... is not None`` check, and the matched-text
    extraction method is never called anywhere in this module (see the
    module docstring).

    Unreadable or undecodable files (permission errors, binary-ish content
    that isn't valid text) are skipped rather than aborting the scan, same
    as bash's ``grep -I`` silently skipping binaries and ``2>/dev/null``
    silently skipping permission errors.
    """
    results: list[dict] = []
    seen: set[str] = set()
    for subdir_name in CONTENT_SCAN_SUBDIRS:
        if len(results) >= CONTENT_RESULT_CAP:
            break
        subdir = root / subdir_name
        if not subdir.is_dir():
            continue
        for file_path in adapter.list_dir(subdir):
            if len(results) >= CONTENT_RESULT_CAP:
                break
            if file_path.suffix.lower() not in CONTENT_EXTENSIONS:
                continue
            if _content_excluded(file_path):
                continue
            key = str(file_path)
            if key in seen:
                continue
            try:
                text = adapter.read_file(file_path)
            except (OSError, UnicodeDecodeError):
                continue
            for pattern_name, pattern in CONTENT_PATTERNS:
                if pattern.search(text) is not None:
                    seen.add(key)
                    results.append(
                        {"path": file_path, "match_type": "content", "pattern": pattern_name}
                    )
                    break
    return results


def run(adapter: OSAdapter, args=None) -> dict:
    """Scan for crypto-wallet artifacts by filename and by content signature.

    ``root`` is ``args.root`` if supplied, else ``adapter.resolve_home()`` --
    mirroring the bash script's ``ROOT="${1:-$HOME}"``. Read-only: nothing
    is ever written, moved, or deleted, and no ``--go``/dry-run flag applies.
    """
    raw_root = getattr(args, "root", None) if args is not None else None
    root = Path(raw_root) if raw_root else adapter.resolve_home()

    return {
        "root": root,
        "by_filename": _by_filename(adapter, root),
        "by_content": _by_content(adapter, root),
    }
