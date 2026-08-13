"""Move loose screenshot files into ``~/Pictures/Screenshots`` and (optionally)
flip the macOS screenshot save-location preference -- the Python port of
``scripts/corral-screenshots.sh``.

Dry-run by default, exactly like :mod:`cleanup_tools.commands.sort`:
:func:`run` always computes the full move plan and only calls
:meth:`~cleanup_tools.adapters.base.OSAdapter.move` for each entry when
explicitly told to via ``--go``. The returned dict's shape is identical
either way, so callers can inspect exactly what would happen (or did happen)
without branching on ``go``.

``--set-default-location`` is a THIRD, entirely independent flag -- not a
modifier of ``--go`` or ``--from-queue``. It is the single highest-stakes
behavior in this module (it changes a system-wide macOS preference, not just
files under the roots this command otherwise scans), so its gating is kept
structurally simple: exactly one ``if args.set_default_location`` check in
:func:`run`, entirely separate from the ``go``/``from_queue`` branches above
it, calling :meth:`~cleanup_tools.adapters.base.OSAdapter.set_screenshot_save_location`
which is itself unreachable from anywhere else in this module. Passing
``--go`` alone (with or without ``--from-queue``) can therefore never reach
it, under any code path -- see that function's docstring for the full
argument.

This module deliberately follows ``sort.py``'s shape, not ``reclaim.py``'s
master-path-refusal machinery: moving a stray screenshot into
``Pictures/Screenshots`` is not the kind of destructive, master-path-adjacent
operation that refusal machinery exists to guard against, so nothing here
imports or calls into it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from cleanup_tools import config as config_module
from cleanup_tools import queue as queue_module
from cleanup_tools.adapters.base import OSAdapter

SCREENSHOT_PATTERN = "screenshot*"
SCREENSHOT_MAX_DEPTH = 2
SCREENSHOTS_SUBDIR = "Screenshots"


def _resolve_roots(
    adapter: OSAdapter, config: config_module.Config, raw_dirs: list[str] | None
) -> list[Path]:
    """CLI-supplied dirs win; else configured search_roots; else Desktop+Downloads+Documents.

    Mirrors ``reclaim.py``'s ``_resolve_roots`` exactly. Matches
    ``scripts/corral-screenshots.sh``'s hardcoded ``~/Desktop ~/Downloads
    ~/Documents`` targets when nothing more specific has been configured or
    passed on the command line.
    """
    if raw_dirs:
        return [Path(d) for d in raw_dirs]
    if config.search_roots:
        return [Path(r) for r in config.search_roots]
    return [
        adapter.resolve_standard_dir("desktop"),
        adapter.resolve_standard_dir("downloads"),
        adapter.resolve_standard_dir("documents"),
    ]


def _plan(
    adapter: OSAdapter, target_dirs: list[Path], pictures_screenshots_dir: Path
) -> list[dict]:
    """Compute the move plan for every ``screenshot*`` match (depth <= 2) under any root.

    Reuses ``survey.py``'s ``_screenshot_count`` precedent verbatim:
    ``adapter.list_dir(d, max_depth=2, pattern="screenshot*")``. A root that
    doesn't exist is skipped rather than raised on -- unlike sort.py's single
    required target dir, corral-screenshots' roots are a best-effort set
    (mirroring reclaim.py's per-root ``if not root.is_dir(): continue``
    guard), since e.g. a fresh machine may simply not have a ``Documents``
    dir yet.

    Each entry carries ``dest_exists``, same as sort.py's plan entries: the
    dest already existing is informational in dry-run mode, and is what the
    ``--go``/``--from-queue`` paths use to decide whether to actually move
    (never overwriting a pre-existing file at the destination).
    """
    plan: list[dict] = []
    for target_dir in target_dirs:
        if not target_dir.is_dir():
            continue
        for file_path in adapter.list_dir(
            target_dir, max_depth=SCREENSHOT_MAX_DEPTH, pattern=SCREENSHOT_PATTERN
        ):
            dest = pictures_screenshots_dir / file_path.name
            plan.append(
                {
                    "src": file_path,
                    "dest": dest,
                    "dest_exists": dest.exists(),
                }
            )
    return plan


def _resolve_loose(path: Path) -> Path:
    """Canonicalize ``path`` without requiring it to exist (see reclaim.py's twin)."""
    return path.resolve()


def _is_under_any(child: Path, roots: list[Path]) -> bool:
    """True if resolved ``child`` is any resolved root itself or somewhere underneath it."""
    return any(child == root or root in child.parents for root in roots)


def _run_from_queue(adapter: OSAdapter, target_dirs: list[Path]) -> dict:
    """Execute every approved, queue-sourced ``move`` whose ``src`` is under a resolved root.

    Structurally identical to ``sort.py``'s ``_run_from_queue`` (staleness
    re-checked immediately before acting, per-entry ``dest_exists``
    overwrite guard, broad ``try/except Exception`` isolation so one entry's
    unexpected bug never prevents ``save_queue`` from persisting every other
    entry's outcome, single load/mutate/save cycle under one
    ``with_queue_lock`` block) -- generalized only in that "under the
    target" is checked against a *list* of resolved roots rather than a
    single directory, mirroring how ``reclaim.py``'s ``_run_from_queue``
    generalizes the same check for its own multi-root scan.
    """
    path = queue_module.default_queue_path(adapter)
    resolved_roots = [_resolve_loose(d) for d in target_dirs]

    executed: list[dict] = []
    stale: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []

    with queue_module.with_queue_lock(adapter, path):
        entries = queue_module.load_queue(adapter, path)

        qualifying = [
            entry
            for entry in entries
            if entry.status == "approved"
            and entry.action == "move"
            and _is_under_any(_resolve_loose(Path(entry.src)), resolved_roots)
        ]

        for entry in qualifying:
            if queue_module.check_staleness(adapter, entry):
                stale.append({"id": entry.id, "src": entry.src, "reason": "stale, re-plan"})
                continue

            if Path(entry.dest).exists():
                skipped.append(
                    {
                        "id": entry.id,
                        "src": entry.src,
                        "dest": entry.dest,
                        "reason": "destination already exists, skipped to avoid overwrite",
                    }
                )
                continue

            now = datetime.now(timezone.utc).isoformat()
            try:
                adapter.move(Path(entry.src), Path(entry.dest))
            except Exception as exc:  # noqa: BLE001 - one entry's bug must not sink the rest
                entry.executed_at = now
                entry.execution_error = str(exc)
                failed.append({"id": entry.id, "src": entry.src, "error": str(exc)})
            else:
                entry.executed_at = now
                entry.execution_error = None
                executed.append({"id": entry.id, "src": entry.src, "dest": entry.dest})

        queue_module.save_queue(adapter, entries, path)

    return {
        "queue_path": path,
        "qualifying_count": len(qualifying),
        "executed": executed,
        "stale": stale,
        "skipped": skipped,
        "failed": failed,
    }


def run(adapter: OSAdapter, args=None) -> dict:
    """Corral screenshots from ``args.dir`` (default: resolved roots) into
    ``~/Pictures/Screenshots``, and optionally flip the OS default location.

    Always computes the full plan across every resolved root; only mutates
    the filesystem when ``args.go`` is true. ``args`` may be ``None`` (or any
    object without ``dir``/``go``/``from_queue``/``set_default_location``
    attributes) to fall back to the defaults, mirroring ``sort.run``'s
    ``args=None`` convenience for direct/test callers that don't go through
    argparse.

    In ``--go`` mode, each plan entry is executed independently: a failure
    moving one file is caught and recorded on that entry (``moved: False``,
    ``error: <message>``) rather than aborting the whole batch, matching
    sort.py's per-entry ``try/except OSError`` isolation exactly. An entry
    whose destination already exists is never passed to ``adapter.move()``
    -- it is recorded as ``moved: False`` with a clear reason instead, so a
    same-named file already sitting in ``Pictures/Screenshots`` is never
    silently clobbered. The plan's ``moved_count`` (present only in ``--go``
    mode) is an exact tally of entries that actually moved, not an
    approximation of what a scan found beforehand (unlike the bash script's
    own ``echo "moved ~$n screenshots"``, which counts matches *before*
    attempting the moves and can therefore be wrong).

    ``args.from_queue`` takes an entirely separate path: instead of
    computing/executing a fresh plan, it executes every already-*approved*
    ``move`` entry in the approval queue whose ``src`` falls under one of the
    resolved roots, via ``_run_from_queue``. See that helper's docstring for
    the staleness/isolation/locking details.

    ``args.set_default_location`` is checked LAST, completely independently
    of ``go``/``from_queue``: it is the only code path in this module that
    can ever reach
    :meth:`~cleanup_tools.adapters.base.OSAdapter.set_screenshot_save_location`,
    and reaching it depends on nothing but this one flag. A failure there
    (macOS-only concept -- see ``ArchLinuxAdapter``) is caught narrowly as
    ``except NotImplementedError`` and recorded as
    ``{"skipped": True, "reason": "not supported on this OS"}`` rather than
    raised, so a corral-screenshots run on Arch degrades gracefully instead
    of crashing.
    """
    raw_dirs = getattr(args, "dir", None) if args is not None else None
    go = bool(getattr(args, "go", False)) if args is not None else False
    from_queue = bool(getattr(args, "from_queue", False)) if args is not None else False
    set_default_location = (
        bool(getattr(args, "set_default_location", False)) if args is not None else False
    )

    config = config_module.load_config(adapter)
    target_dirs = _resolve_roots(adapter, config, raw_dirs)
    pictures_screenshots_dir = adapter.resolve_standard_dir("pictures") / SCREENSHOTS_SUBDIR

    if from_queue:
        from_queue_report = _run_from_queue(adapter, target_dirs)
        result: dict = {"roots": target_dirs, "go": go, "from_queue": from_queue_report}
    else:
        plan = _plan(adapter, target_dirs, pictures_screenshots_dir)

        if go:
            moved_count = 0
            for entry in plan:
                if entry["dest_exists"]:
                    entry["moved"] = False
                    entry["error"] = "destination already exists, skipped to avoid overwrite"
                    continue
                try:
                    adapter.move(entry["src"], entry["dest"])
                except OSError as exc:
                    entry["moved"] = False
                    entry["error"] = str(exc)
                else:
                    entry["moved"] = True
                    entry["error"] = None
                    moved_count += 1
            result = {"roots": target_dirs, "go": go, "plan": plan, "moved_count": moved_count}
        else:
            result = {"roots": target_dirs, "go": go, "plan": plan}

    # --set-default-location: structurally independent of everything above.
    # This is the ONLY line in the whole module that can call
    # set_screenshot_save_location, and it is gated on nothing but this one
    # flag -- never on `go`, never on `from_queue`.
    if set_default_location:
        try:
            adapter.set_screenshot_save_location(pictures_screenshots_dir)
        except NotImplementedError:
            result["set_default_location"] = {
                "skipped": True,
                "reason": "not supported on this OS",
            }
        else:
            result["set_default_location"] = {
                "skipped": False,
                "path": pictures_screenshots_dir,
            }

    return result
