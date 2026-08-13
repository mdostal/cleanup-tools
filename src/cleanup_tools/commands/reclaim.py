"""Reclaim disk space from build caches, OS junk, and stale installers — the
Python port of ``scripts/safe-reclaim.sh``, plus one category the shell
script never had (orphaned installers) and a stricter Docker gate.

Dry-run by default, exactly like :mod:`cleanup_tools.commands.sort`:
:func:`run` always computes the *full* plan -- every candidate in every
category, sized, and checked against every configured master path -- and
only calls :meth:`~cleanup_tools.adapters.base.OSAdapter.delete` (or runs
``docker system prune``) when explicitly told to via ``--go``. The returned
dict's shape is identical in both modes, so callers can inspect exactly what
would happen (or did happen) without branching on ``go``.

Categories are independently isolated: a bug or OS error while building one
category's candidate list is caught and recorded on that category (as
``{"error": ...}``) rather than aborting the whole command, so every other
category still gets computed. This mirrors sort.py's per-entry isolation,
just one level up -- per-category instead of per-file. Within a category,
each candidate's actual deletion (in ``--go`` mode) is *also* isolated
per-entry via a ``try/except OSError``, same as sort.py.

The one non-negotiable safety rule, checked before any deletion in any
category: a candidate that is, contains, or is contained by a configured
"master path" (see ``cleanup_tools.config.MasterPath``) that has not been
marked ``backed_up`` is refused unconditionally -- even under ``--go``. A
master path that *has* been marked backed up gets no special protection;
it's just a normal deletable candidate like anything else.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from cleanup_tools import config as config_module
from cleanup_tools import queue as queue_module
from cleanup_tools.adapters.base import OSAdapter

BUILD_CACHE_DIR_NAMES = ("node_modules", ".next", ".turbo", "dist")
OS_JUNK_PATTERNS = (".DS_Store", "~$*", "Thumbs.db")
RECYCLE_BIN_NAME = "$RECYCLE.BIN"
INSTALLER_SUFFIXES = (".dmg", ".pkg")

MASTER_PATH_REFUSAL_REASON = "refused: path is an unbacked-up master path"

_DOCKER_RECLAIMED_RE = re.compile(
    r"Total reclaimed space:\s*([\d.]+)\s*([KMGT]?I?B)", re.IGNORECASE
)
_UNIT_MULTIPLIERS = {
    "B": 1,
    "KB": 1024,
    "MB": 1024**2,
    "GB": 1024**3,
    "TB": 1024**4,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
}


def _dedupe(paths: list[Path]) -> list[Path]:
    """Drop duplicate paths while preserving first-seen order.

    Different junk-file patterns (or overlapping roots) can turn up the same
    path twice; the plan should only ever list a given candidate once.
    """
    seen: set[str] = set()
    result: list[Path] = []
    for p in paths:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        result.append(p)
    return result


def _resolve_loose(path: Path) -> Path:
    """Canonicalize ``path`` to an absolute, ``..``-free form without
    requiring it to exist.

    Uses ``Path.resolve()`` *without* ``strict=True`` deliberately: a master
    path configured as a relative path, or one containing ``..``, must still
    compare equal to an absolute candidate that refers to the identical
    on-disk directory -- and a master path config entry can legitimately
    point at something that doesn't exist right now (e.g. a directory
    that's already been partially cleaned up), so requiring existence here
    would make the refusal silently stop working for exactly the entries
    most in need of protection.
    """
    return path.resolve()


def _same_path_ci(a: Path, b: Path) -> bool:
    """True if already-resolved paths ``a`` and ``b`` name the same entry.

    ``Path.resolve()`` normalizes ``..``/symlinks/relativity but does
    *not* correct case, so on a case-insensitive filesystem (e.g. macOS's
    default APFS) two resolved paths that differ only in case -- like
    ``.../NODE_MODULES`` vs ``.../node_modules`` -- are the exact same
    directory on disk while still comparing unequal as strings. When both
    paths currently exist, ``samefile`` is the correct, case-agnostic check
    since it compares device+inode rather than characters. It requires
    both sides to exist, though, so falls back to a case-insensitive string
    comparison when either doesn't (e.g. a master path already partially
    cleaned up, or one that simply hasn't been created yet).
    """
    try:
        if a.exists() and b.exists():
            return a.samefile(b)
    except OSError:
        pass
    return str(a).lower() == str(b).lower()


def _is_relative_to_ci(child: Path, parent: Path) -> bool:
    """Case-insensitive containment check: is ``child`` ``parent`` or under it?

    Both inputs must already be resolved (see ``_resolve_loose``).
    ``Path.is_relative_to`` is a pure string/parts comparison with no
    filesystem access at all, so it cannot see that two differently-cased
    paths are the same directory on a case-insensitive filesystem. This
    reimplements the containment check case-insensitively: first the
    same-entry check above (covers equality, using ``samefile`` when
    possible), then a lowercased string ``startswith`` with an explicit
    path-separator boundary so e.g. ``/a/bc`` is never mistaken for being
    relative to ``/a/b``.
    """
    if _same_path_ci(child, parent):
        return True
    child_str = str(child).lower()
    parent_str = str(parent).lower()
    if not parent_str.endswith(os.sep):
        parent_str += os.sep
    return child_str.startswith(parent_str)


def _master_path_refusal(
    config: config_module.Config, candidate: Path
) -> config_module.MasterPath | None:
    """Return the first unbacked-up master path that overlaps ``candidate``.

    "Overlaps" means: the paths are equal, the master path is nested inside
    the candidate (deleting the candidate would take the master path with
    it), or the candidate is nested inside the master path (the candidate
    *is* part of the master-path tree). A master path marked ``backed_up``
    is deliberately excluded here -- it gets no special protection, per the
    story's "normal gating applies" rule.

    Both sides are resolved (``_resolve_loose``) and compared
    case-insensitively (``_is_relative_to_ci``, backed by ``samefile`` when
    both exist) before any of the above checks run. Without this, a master
    path configured as a relative path or containing ``..``, or one whose
    configured case doesn't match the on-disk case of an otherwise-identical
    candidate (both routine on a case-insensitive filesystem like macOS's
    default APFS), would silently fail to match an equivalent absolute
    candidate and the refusal -- the one thing standing between ``--go`` and
    permanently deleting a supposedly-protected directory -- would never
    fire.
    """
    resolved_candidate = _resolve_loose(candidate)
    for master_path in config.master_paths:
        if master_path.backed_up:
            continue
        mp = _resolve_loose(Path(master_path.path))
        if _is_relative_to_ci(mp, resolved_candidate) or _is_relative_to_ci(
            resolved_candidate, mp
        ):
            return master_path
    return None


def _category_entries(
    config: config_module.Config,
    category_name: str,
    candidates_with_sizes: list[tuple[Path, int]],
) -> tuple[list[dict], list[dict]]:
    """Build plan entries (and any master-path refusals) for one category.

    A refused entry gets its ``deleted``/``reason`` fields filled in right
    here, at plan time -- unconditionally, in both dry-run and ``--go`` mode
    -- rather than waiting for the ``--go`` execution loop, so the refusal is
    visible in dry-run output too and is never mistaken for a caught
    ``OSError`` (which uses an ``error`` key, not ``reason``).
    """
    entries: list[dict] = []
    refusals: list[dict] = []
    for path, size_bytes in candidates_with_sizes:
        refusing_master_path = _master_path_refusal(config, path)
        entry: dict = {
            "path": path,
            "size_bytes": size_bytes,
            "master_path_refused": refusing_master_path is not None,
        }
        if refusing_master_path is not None:
            entry["deleted"] = False
            entry["reason"] = MASTER_PATH_REFUSAL_REASON
            refusals.append(
                {
                    "path": path,
                    "category": category_name,
                    "master_path": refusing_master_path.path,
                }
            )
        entries.append(entry)
    return entries, refusals


def _build_caches_category(
    adapter: OSAdapter, config: config_module.Config, roots: list[Path]
) -> tuple[dict, list[dict]]:
    """node_modules / .next / .turbo / dist -- all regenerable via reinstall/rebuild."""
    candidates: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for name in BUILD_CACHE_DIR_NAMES:
            candidates.extend(adapter.find_dirs(root, name))
    candidates = _dedupe(candidates)

    # Size BEFORE any deletion -- these are directories, so go through the
    # adapter's du-backed dir_size_bytes rather than a raw stat().
    candidates_with_sizes = [(c, adapter.dir_size_bytes(c)) for c in candidates]
    entries, refusals = _category_entries(config, "build_caches", candidates_with_sizes)
    return {"entries": entries}, refusals


def _os_junk_category(
    adapter: OSAdapter, config: config_module.Config, roots: list[Path]
) -> tuple[dict, list[dict]]:
    """.DS_Store / Office lock files / Thumbs.db -- pure OS/app junk."""
    candidates: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in OS_JUNK_PATTERNS:
            candidates.extend(adapter.list_dir(root, max_depth=3, pattern=pattern))
    candidates = _dedupe(candidates)

    # These are individual files, so a plain stat() is the right sizing
    # tool -- no need for the du-backed recursive dir_size_bytes.
    candidates_with_sizes = [(c, c.stat().st_size) for c in candidates]
    entries, refusals = _category_entries(config, "os_junk", candidates_with_sizes)
    return {"entries": entries}, refusals


def _recycle_bin_category(
    adapter: OSAdapter, config: config_module.Config, roots: list[Path]
) -> tuple[dict, list[dict]]:
    """A stray Windows ``$RECYCLE.BIN`` dir left behind on an external/shared drive."""
    candidates = [root / RECYCLE_BIN_NAME for root in roots if (root / RECYCLE_BIN_NAME).exists()]
    candidates = _dedupe(candidates)

    candidates_with_sizes = [(c, adapter.dir_size_bytes(c)) for c in candidates]
    entries, refusals = _category_entries(config, "recycle_bin", candidates_with_sizes)
    return {"entries": entries}, refusals


def _orphaned_installers_category(
    adapter: OSAdapter,
    config: config_module.Config,
    roots: list[Path],
    downloads_dir: Path,
) -> tuple[dict, list[dict]]:
    """.dmg/.pkg installers whose app is already installed -- new vs. the old bash script.

    The whole category is wrapped in one try/except: ``find_installed_app``
    is a macOS-only concept (see ``ArchLinuxAdapter.find_installed_app``,
    which raises ``NotImplementedError`` unconditionally), and on Arch we'd
    rather skip this one category cleanly than either crash the whole
    ``reclaim`` run or silently pretend it produced zero candidates.
    """
    try:
        search_dirs = [root for root in roots if root.is_dir()]
        if downloads_dir.is_dir():
            search_dirs.append(downloads_dir)
        search_dirs = _dedupe(search_dirs)

        candidates: list[Path] = []
        for search_dir in search_dirs:
            for file_path in adapter.list_dir(search_dir, pattern=None):
                if file_path.suffix.lower() in INSTALLER_SUFFIXES:
                    candidates.append(file_path)
        candidates = _dedupe(candidates)

        # Only an installer whose app is already installed is "orphaned" --
        # one whose app isn't installed yet is still useful and must not be
        # offered up for deletion at all.
        orphaned = [c for c in candidates if adapter.find_installed_app(c)]
        candidates_with_sizes = [(c, c.stat().st_size) for c in orphaned]
        entries, refusals = _category_entries(
            config, "orphaned_installers", candidates_with_sizes
        )
        return {"entries": entries}, refusals
    except NotImplementedError:
        return {"skipped": True, "reason": "not supported on this OS"}, []


def _finalize_category_bytes(categories: dict[str, dict], go: bool) -> None:
    """Fill in each category's ``bytes_reclaimed``/``gb_reclaimed`` totals.

    In dry-run mode this counts every non-refused entry (what --go *would*
    reclaim); in --go mode it counts only entries that actually got deleted.
    Categories that errored or were skipped (no ``entries`` key) get zeroed
    totals rather than being left out, so downstream summation never has to
    special-case them.
    """
    for category in categories.values():
        entries = category.get("entries")
        if entries is None:
            category["bytes_reclaimed"] = 0
            category["gb_reclaimed"] = 0.0
            continue
        if go:
            reclaimed = sum(e["size_bytes"] for e in entries if e.get("deleted") is True)
        else:
            reclaimed = sum(e["size_bytes"] for e in entries if not e["master_path_refused"])
        category["bytes_reclaimed"] = reclaimed
        category["gb_reclaimed"] = round(reclaimed / (1024**3), 4)


def _execute_deletions(adapter: OSAdapter, categories: dict[str, dict]) -> None:
    """--go mode: delete every non-refused entry, independently.

    Mirrors sort.py's per-entry isolation exactly: each ``adapter.delete()``
    call is wrapped in its own try/except OSError, so one entry's failure
    (permission error, vanished mid-run, etc.) is recorded on that entry and
    never aborts the rest of the batch -- across every category. Refused
    entries are skipped here entirely; their ``deleted``/``reason`` fields
    were already set at plan time and must not be touched again.
    """
    for category in categories.values():
        entries = category.get("entries")
        if entries is None:
            continue
        for entry in entries:
            if entry["master_path_refused"]:
                continue
            try:
                adapter.delete(entry["path"], recursive=True)
            except OSError as exc:
                entry["deleted"] = False
                entry["error"] = str(exc)
            else:
                entry["deleted"] = True
                entry["error"] = None


def _parse_docker_reclaimed_bytes(output: str) -> int | None:
    """Best-effort parse of docker's own "Total reclaimed space: X.YGB" line.

    Returns ``None`` (rather than raising) on anything unparseable -- an
    unrecognized unit, a missing line entirely, an unexpected docker output
    format across versions -- so a parsing miss degrades to "unknown"
    instead of blowing up the whole command.
    """
    if not output:
        return None
    match = _DOCKER_RECLAIMED_RE.search(output)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    multiplier = _UNIT_MULTIPLIERS.get(match.group(2).upper())
    if multiplier is None:
        return None
    return int(value * multiplier)


def _run_docker(adapter: OSAdapter, go: bool, docker_requested: bool) -> dict:
    """Docker layer/volume cleanup -- gated far more strictly than the other categories.

    ``docker system prune -af --volumes`` is machine-wide, not scoped to any
    of the roots this command otherwise operates on, so it only actually
    runs when BOTH ``--go`` and ``--docker`` are passed -- ``--go`` alone is
    not enough, unlike every other category. The whole invocation is wrapped
    in try/except so a docker failure (daemon not running, permission
    denied, etc.) is recorded and never crashes the rest of ``reclaim``.
    """
    installed = adapter.is_docker_installed()
    docker_info: dict = {
        "installed": installed,
        "requested": docker_requested,
        "ran": False,
        "output": None,
        "bytes_reclaimed": None,
        "gb_reclaimed": None,
        "reclaimed_display": "unknown",
        "error": None,
    }
    if not installed or not (go and docker_requested):
        return docker_info

    try:
        result = subprocess.run(
            ["docker", "system", "prune", "-af", "--volumes"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        docker_info["ran"] = True
        docker_info["output"] = result.stdout
        reclaimed_bytes = _parse_docker_reclaimed_bytes(result.stdout)
        if reclaimed_bytes is not None:
            docker_info["bytes_reclaimed"] = reclaimed_bytes
            docker_info["gb_reclaimed"] = round(reclaimed_bytes / (1024**3), 4)
            docker_info["reclaimed_display"] = f"{docker_info['gb_reclaimed']} GB"
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        docker_info["error"] = str(exc)
    return docker_info


def _resolve_roots(
    adapter: OSAdapter, config: config_module.Config, raw_dirs: list[str] | None
) -> list[Path]:
    """CLI-supplied dirs win; else configured search_roots; else Documents+Desktop.

    Matches ``scripts/safe-reclaim.sh``'s hardcoded ``~/Documents
    ~/Desktop`` targets when nothing more specific has been configured or
    passed on the command line.
    """
    if raw_dirs:
        return [Path(d) for d in raw_dirs]
    if config.search_roots:
        return [Path(r) for r in config.search_roots]
    return [adapter.resolve_standard_dir("documents"), adapter.resolve_standard_dir("desktop")]


def _run_from_queue(
    adapter: OSAdapter, config: config_module.Config, roots: list[Path]
) -> dict:
    """Execute every approved, queue-sourced ``delete`` whose ``src`` is under a root.

    Mirrors ``sort.py``'s ``_run_from_queue`` (load the queue exactly once,
    process every qualifying entry, save exactly once, all inside a single
    ``with_queue_lock`` block), with two reclaim-specific twists:

    - "Under a root" is checked case-insensitively via ``_is_relative_to_ci``
      against every resolved root, matching how the rest of this module
      compares paths on a case-insensitive filesystem.
    - Before any delete attempt, every qualifying entry -- stale or not --
      is run through the *exact same* ``_master_path_refusal`` helper that
      ``--go``'s ``_category_entries`` already uses. A match refuses the
      entry unconditionally: no delete, no ``executed_at``, regardless of
      whether it was also stale. Staleness is still checked first (and, if
      stale, the entry is left alone -- status stays ``approved`` -- and is
      only noted in the returned report), but the master-path check itself
      always runs, never short-circuited by the staleness result.

    Each entry's actual delete attempt is isolated via a broad ``try/except
    Exception`` -- deliberately wider than a plain ``OSError`` catch, and
    the same broad-except precedent already used one level up by ``run()``'s
    per-category loop (see that loop's ``except Exception`` and the
    ``_execute_deletions``/docker-invocation broad catches) for exactly the
    same reason: one entry's unexpected bug must not sink the rest of the
    batch, and -- critically for ``delete`` specifically -- must never
    prevent ``save_queue`` below from running. Without this, a delete that
    actually *succeeds* on disk but is followed by some unrelated
    non-OSError bug before ``entry.executed_at`` is durably saved would
    leave the queue believing the entry was never executed at all; a later
    run would then see the now-missing ``src`` and misreport it as merely
    "stale, re-plan" rather than "already executed" -- effectively losing
    the record of a real deletion that already happened.
    """
    path = queue_module.default_queue_path(adapter)
    resolved_roots = [_resolve_loose(r) for r in roots]

    def _under_a_root(src_path: Path) -> bool:
        resolved_src = _resolve_loose(src_path)
        return any(_is_relative_to_ci(resolved_src, root) for root in resolved_roots)

    executed: list[dict] = []
    stale: list[dict] = []
    refused: list[dict] = []
    failed: list[dict] = []

    with queue_module.with_queue_lock(adapter, path):
        entries = queue_module.load_queue(adapter, path)

        qualifying = [
            entry
            for entry in entries
            if entry.status == "approved"
            and entry.action == "delete"
            and _under_a_root(Path(entry.src))
        ]

        for entry in qualifying:
            is_stale = queue_module.check_staleness(adapter, entry)
            refusing_master_path = _master_path_refusal(config, Path(entry.src))

            if refusing_master_path is not None:
                refused.append(
                    {
                        "id": entry.id,
                        "src": entry.src,
                        "reason": MASTER_PATH_REFUSAL_REASON,
                        "master_path": refusing_master_path.path,
                    }
                )
                continue

            if is_stale:
                stale.append({"id": entry.id, "src": entry.src, "reason": "stale, re-plan"})
                continue

            now = datetime.now(timezone.utc).isoformat()
            try:
                adapter.delete(Path(entry.src), recursive=True)
            except Exception as exc:  # noqa: BLE001 - one entry's bug must not sink the rest
                entry.executed_at = now
                entry.execution_error = str(exc)
                failed.append({"id": entry.id, "src": entry.src, "error": str(exc)})
            else:
                entry.executed_at = now
                entry.execution_error = None
                executed.append({"id": entry.id, "src": entry.src})

        queue_module.save_queue(adapter, entries, path)

    return {
        "queue_path": path,
        "qualifying_count": len(qualifying),
        "executed": executed,
        "stale": stale,
        "refused": refused,
        "failed": failed,
    }


def run(adapter: OSAdapter, args=None) -> dict:
    """Reclaim disk space across build caches, OS junk, and (optionally) Docker.

    Always computes the full plan across every category; only mutates the
    filesystem (or invokes docker) when ``args.go`` is true. ``args`` may be
    ``None`` (or any object without ``dir``/``go``/``docker`` attributes) to
    fall back to the defaults, mirroring ``sort.run``'s convenience for
    direct/test callers that don't go through argparse.

    Each category is computed independently: a category whose candidate
    gathering raises is recorded as ``{"error": ...}`` rather than aborting
    the other categories (the orphaned-installers category additionally has
    its own internal ``NotImplementedError`` handling for OSes where that
    concept doesn't exist -- see its docstring). Docker is reported
    separately from the four filesystem categories, both because it is
    gated on an extra flag (``--docker``) and because its reclaimed-bytes
    figure is only knowable *after* running it, unlike every other
    category's pre-computed size.

    ``args.from_queue`` takes an entirely separate path: instead of
    computing/executing a fresh plan across categories, it executes every
    already-*approved* ``delete`` entry in the approval queue whose ``src``
    falls under one of the resolved roots, via ``_run_from_queue`` -- see
    that helper's docstring for the master-path-refusal/staleness/isolation/
    locking details.
    """
    raw_dirs = getattr(args, "dir", None) if args is not None else None
    go = bool(getattr(args, "go", False)) if args is not None else False
    docker_requested = bool(getattr(args, "docker", False)) if args is not None else False
    from_queue = bool(getattr(args, "from_queue", False)) if args is not None else False

    config = config_module.load_config(adapter)
    roots = _resolve_roots(adapter, config, raw_dirs)
    downloads_dir = adapter.resolve_standard_dir("downloads")

    if from_queue:
        from_queue_report = _run_from_queue(adapter, config, roots)
        return {"roots": roots, "go": go, "from_queue": from_queue_report}

    category_builders = (
        ("build_caches", lambda: _build_caches_category(adapter, config, roots)),
        ("os_junk", lambda: _os_junk_category(adapter, config, roots)),
        ("recycle_bin", lambda: _recycle_bin_category(adapter, config, roots)),
        (
            "orphaned_installers",
            lambda: _orphaned_installers_category(adapter, config, roots, downloads_dir),
        ),
    )

    categories: dict[str, dict] = {}
    master_path_refusals: list[dict] = []

    for name, builder in category_builders:
        try:
            category_dict, refusals = builder()
        except Exception as exc:  # noqa: BLE001 - one category's bug must not sink the rest
            category_dict, refusals = {"error": str(exc)}, []
        categories[name] = category_dict
        master_path_refusals.extend(refusals)

    if go:
        _execute_deletions(adapter, categories)

    _finalize_category_bytes(categories, go)

    total_bytes_reclaimed = sum(category["bytes_reclaimed"] for category in categories.values())
    total_gb_reclaimed = round(total_bytes_reclaimed / (1024**3), 4)

    docker_info = _run_docker(adapter, go, docker_requested)

    return {
        "roots": roots,
        "go": go,
        "docker_requested": docker_requested,
        "categories": categories,
        "master_path_refusals": master_path_refusals,
        "total_bytes_reclaimed": total_bytes_reclaimed,
        "total_gb_reclaimed": total_gb_reclaimed,
        "docker": docker_info,
    }
