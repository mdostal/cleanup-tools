"""Read-only duplicate-file finder -- the Python port of ``scripts/dedupe.sh``.

Like ``find_wallets.py``, this command never writes, moves, or deletes
anything -- there is no ``--go``/dry-run concept here at all, matching the
bash script's existing safety property (it only ever prints candidate
groups).

The bash script is a two-stage filter: first a cheap ``stat`` pass groups
files by size (duplicates must be the same size), then only files that
share a size with at least one other file get hashed and grouped by hash.
That two-stage shape is preserved here, with two deliberate corrections
versus the bash original:

1. Full, uncapped SHA-256 over each candidate's entire content, instead of
   the bash script's checksum utility (a 160-bit digest) truncated down to
   just 12 hex characters. A truncated hash is exactly the kind of thing
   that can coincidentally collide; truncating it further to 48 bits makes
   that measurably more likely. This module never truncates a digest and
   never reuses the byte-capped content-hash helper ``queue.py`` defines
   for its staleness checks -- that helper is a deliberate, documented
   tradeoff for a hot-path gate, not for exact duplicate detection. A
   dedupe report that misses a true duplicate (or worse, mis-reports two
   different files as duplicates) because only a leading slice of each was
   compared would defeat the entire point of this command.
2. Structured ``{hash, size_bytes, paths}`` output groups, instead of
   bash's raw ``<hash>  <path>`` line pairs.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from cleanup_tools.adapters.base import OSAdapter

EXCLUDED_SEGMENTS = ("node_modules",)


def _excluded(path: Path) -> bool:
    """True if ``path`` has a ``node_modules`` path segment.

    Mirrors bash's ``! -path '*/node_modules/*'``.
    """
    return any(segment in EXCLUDED_SEGMENTS for segment in path.parts)


def _group_by_size(adapter: OSAdapter, target_dir: Path) -> dict[int, list[Path]]:
    """Group every file under ``target_dir`` by its exact byte size.

    This is a cheap per-file ``Path.stat()`` call, not the recursive-tree-size
    concept ``OSAdapter.dir_size_bytes`` exists for (see that method's
    docstring) -- each file here is stat'd individually, no ``du`` involved.
    Only groups with 2+ members are kept: a file whose size matches no other
    file's size cannot possibly be a duplicate, so it's dropped right here,
    before any file content is ever read. This is the bash script's
    ``sort -n | awk '... if(sz==p) ...'`` pre-filter, expressed directly.
    """
    by_size: dict[int, list[Path]] = {}
    for path in adapter.list_dir(target_dir, pattern=None):
        if _excluded(path):
            continue
        size = path.stat().st_size
        by_size.setdefault(size, []).append(path)

    return {size: paths for size, paths in by_size.items() if len(paths) >= 2}


def _hash_file(path: Path) -> str:
    """Return the full, uncapped SHA-256 hex digest of ``path``'s content.

    Reads the entire file regardless of size -- deliberately not the
    byte-capped-prefix pattern ``queue.py``'s content-hash helper uses (see
    the module docstring for why: an exact duplicate report cannot
    tolerate comparing only a prefix of each file).
    """
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _group_by_hash(candidates: dict[int, list[Path]]) -> dict[str, list[Path]]:
    """Hash every size-matched candidate and group by full hex digest.

    ``candidates`` is expected to already be the output of
    ``_group_by_size`` -- i.e. every path passed in here shares its size
    with at least one other path. A singleton-size file is never a
    candidate in the first place (``_group_by_size`` already dropped it),
    so ``_hash_file`` is never called against one: hashing only ever
    touches files that already cleared the cheap size pre-filter. Only hash
    groups with 2+ members (a true duplicate) are kept.
    """
    by_hash: dict[str, list[Path]] = {}
    for paths in candidates.values():
        for path in paths:
            digest = _hash_file(path)
            by_hash.setdefault(digest, []).append(path)

    return {digest: paths for digest, paths in by_hash.items() if len(paths) >= 2}


def run(adapter: OSAdapter, args=None) -> dict:
    """Find duplicate files (same size, then same full-content hash).

    ``target_dir`` is ``args.dir`` if supplied, else
    ``adapter.resolve_standard_dir("downloads")`` -- mirroring the bash
    script's ``DIR="${1:-$HOME/Downloads}"``. Read-only: nothing is ever
    written, moved, or deleted, and no ``--go``/dry-run flag applies.
    """
    raw_dir = getattr(args, "dir", None) if args is not None else None
    target_dir = Path(raw_dir) if raw_dir else adapter.resolve_standard_dir("downloads")

    size_groups = _group_by_size(adapter, target_dir)
    hash_groups = _group_by_hash(size_groups)

    duplicate_groups = [
        {"hash": digest, "size_bytes": paths[0].stat().st_size, "paths": paths}
        for digest, paths in hash_groups.items()
    ]

    return {"dir": target_dir, "duplicate_groups": duplicate_groups}
