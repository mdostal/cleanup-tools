"""Read-only disk/clutter snapshot — the Python port of ``scripts/survey.sh``.

Every section here mirrors one ``echo "=== ... ==="`` block from the shell
script, using :class:`~cleanup_tools.adapters.base.OSAdapter` methods instead
of shelling out to ``du``/``find``. Nothing in this module writes, moves, or
deletes anything.
"""

from __future__ import annotations

from cleanup_tools.adapters.base import OSAdapter

DOWNLOADS_BY_EXTENSION_TOP_N = 12
DOCUMENTS_WORK_BIGGEST_REPOS_TOP_N = 15


def _dir_size(adapter: OSAdapter, path) -> int:
    """Return the recursive size of ``path`` in bytes, or 0 if it doesn't exist.

    Several call sites below query directories that may legitimately not
    exist on a given machine (e.g. ``~/Movies``, a standard dir that was
    never created) -- for a read-only snapshot tool, "doesn't exist" should
    contribute 0 rather than raise. Delegates the actual size computation to
    :meth:`OSAdapter.dir_size_bytes`, which shells out to ``du -sk`` instead
    of walking the tree file-by-file in Python (see that method's docstring
    for why: a Python walk over a real home directory's worth of files can
    take minutes, while ``du`` finishes in seconds).
    """
    if not path.is_dir():
        return 0
    return adapter.dir_size_bytes(path)


def _sorted_desc(items: dict) -> dict:
    return dict(sorted(items.items(), key=lambda kv: kv[1], reverse=True))


def _disk(adapter: OSAdapter) -> dict:
    return adapter.disk_usage("/")


def _home_top_level(adapter: OSAdapter) -> dict:
    home = adapter.resolve_home()
    dirs = [
        adapter.resolve_standard_dir("documents"),
        adapter.resolve_standard_dir("downloads"),
        adapter.resolve_standard_dir("desktop"),
        adapter.resolve_standard_dir("pictures"),
        home / "Movies",
        home / "Library",
    ]
    sizes = {d.name: _dir_size(adapter, d) for d in dirs}
    return _sorted_desc(sizes)


def _documents_work_biggest_repos(adapter: OSAdapter) -> dict:
    work_dir = adapter.resolve_standard_dir("documents") / "work"
    if not work_dir.is_dir():
        return {}

    sizes = {d.name: _dir_size(adapter, d) for d in adapter.list_subdirs(work_dir)}
    sorted_sizes = _sorted_desc(sizes)
    return dict(list(sorted_sizes.items())[:DOCUMENTS_WORK_BIGGEST_REPOS_TOP_N])


def _node_modules(adapter: OSAdapter) -> dict:
    roots = [
        adapter.resolve_standard_dir("documents"),
        adapter.resolve_standard_dir("desktop"),
    ]
    dirs = []
    for root in roots:
        dirs.extend(adapter.find_dirs(root, "node_modules"))

    total_bytes = sum(_dir_size(adapter, d) for d in dirs)
    return {"total_bytes": total_bytes, "dir_count": len(dirs)}


def _downloads_by_extension(adapter: OSAdapter) -> dict:
    downloads = adapter.resolve_standard_dir("downloads")
    tally: dict[str, int] = {}
    for file_path in adapter.list_dir(downloads, max_depth=0):
        ext = file_path.suffix.lower().lstrip(".")
        tally[ext] = tally.get(ext, 0) + 1

    sorted_tally = _sorted_desc(tally)
    return dict(list(sorted_tally.items())[:DOWNLOADS_BY_EXTENSION_TOP_N])


def _screenshot_count(adapter: OSAdapter) -> int:
    dirs = [
        adapter.resolve_standard_dir("desktop"),
        adapter.resolve_standard_dir("downloads"),
        adapter.resolve_standard_dir("documents"),
        adapter.resolve_standard_dir("pictures"),
    ]
    return sum(
        len(adapter.list_dir(d, max_depth=2, pattern="screenshot*")) for d in dirs
    )


def run(adapter: OSAdapter, args=None) -> dict:
    """Produce all 6 survey sections as a single JSON-serializable dict.

    ``args`` is accepted (and ignored) for signature parity with other
    commands' ``run(adapter, args)`` -- survey is read-only and needs no
    CLI-supplied options, but ``cli.main`` dispatches every command
    uniformly as ``command_fn(adapter, args)``.
    """
    return {
        "disk": _disk(adapter),
        "home_top_level": _home_top_level(adapter),
        "documents_work_biggest_repos": _documents_work_biggest_repos(adapter),
        "node_modules": _node_modules(adapter),
        "downloads_by_extension": _downloads_by_extension(adapter),
        "screenshot_count": _screenshot_count(adapter),
    }
