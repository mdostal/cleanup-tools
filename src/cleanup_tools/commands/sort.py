"""Sort a directory's loose top-level files into ``_sorted/<bucket>/`` — the
Python port of ``scripts/sort-downloads.sh``.

Dry-run by default: :func:`run` always computes and returns the full move
plan, and only calls :meth:`~cleanup_tools.adapters.base.OSAdapter.move` for
each entry when explicitly told to via ``--go``. The returned dict's shape
(``{"dir", "go", "plan"}``) is identical either way, so callers can inspect
exactly what would happen (or did happen) without branching on ``go``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from cleanup_tools import config as config_module
from cleanup_tools import queue as queue_module
from cleanup_tools.adapters.base import OSAdapter

SORTED_SUBDIR = "_sorted"


def _resolve_roots(
    adapter: OSAdapter, config: config_module.Config, raw_dirs: list[str] | None
) -> list[Path]:
    """CLI-supplied dirs win; else configured search_roots; else the platform Downloads dir.

    Mirrors ``reclaim.py``/``corral_screenshots.py``'s ``_resolve_roots``
    exactly -- sort.py was the one pipeline still hardcoded to a single
    target dir; this brings it in line with its siblings so ``search_roots``
    (any-and-all configured locations, not a fixed enum) works the same way
    across every pipeline.
    """
    if raw_dirs:
        return [Path(d) for d in raw_dirs]
    if config.search_roots:
        return [Path(r) for r in config.search_roots]
    return [adapter.resolve_standard_dir("downloads")]


def _plan(adapter: OSAdapter, config: config_module.Config, target_dirs: list[Path]) -> list[dict]:
    """Compute the move plan for every non-dotfile directly inside each of ``target_dirs``.

    A root that doesn't exist is skipped rather than raised on here --
    mirroring ``corral_screenshots.py``'s per-root ``if not target_dir.is_dir():
    continue`` guard, since a *configured* root (search_roots or the
    single default) is a best-effort set, not something the user just
    typed. An *explicitly CLI-supplied* missing dir is still a loud error --
    see ``run()``, which checks that case before ever calling this function.

    ``adapter.list_dir(target_dir, max_depth=0)`` returns files at any depth
    that happen to satisfy its depth-0 walk semantics, but it does not
    exclude dotfiles (e.g. ``.DS_Store``) -- those are filtered out here
    rather than in the adapter, since "skip hidden files" is sort-specific
    policy, not a general filesystem-listing concern.

    Each entry also carries ``dest_exists``: whether ``dest`` already exists
    on disk at plan time. This is informational in dry-run mode (it tells
    the caller a real ``--go`` run would skip that entry rather than
    overwrite something already there) and is what the ``--go`` path below
    uses to decide whether to actually call ``adapter.move()``. ``_sorted/``
    lives inside *each* scanned root (not one shared global destination
    like corral-screenshots' ``~/Pictures/Screenshots``), so dest is
    computed per-root, not against a single passed-in destination dir.
    """
    plan: list[dict] = []
    for target_dir in target_dirs:
        if not target_dir.is_dir():
            continue
        for file_path in adapter.list_dir(target_dir, max_depth=0):
            if file_path.name.startswith("."):
                continue
            bucket = config_module.resolve_bucket(file_path.name, config.bucket_rules)
            dest = target_dir / SORTED_SUBDIR / bucket / file_path.name
            plan.append(
                {
                    "src": file_path,
                    "dest": dest,
                    "bucket": bucket,
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
    """Execute every approved, queue-sourced ``move`` whose ``src`` is under any of ``target_dirs``.

    Loads the queue once, filters to qualifying entries, and for each one:
    checks staleness first (a stale entry is left alone -- status stays
    ``approved`` so a future re-plan can pick it up -- and is only noted in
    this function's *returned* report, never persisted back to the queue
    file); then checks whether ``entry.dest`` already exists on disk --
    mirroring the ``--go`` path's ``dest_exists`` guard above exactly, this
    entry is also left alone entirely (no move attempted, ``executed_at``/
    ``execution_error`` untouched, ``status`` untouched) and is only noted
    in the ``skipped`` list of the returned report, so a pre-existing file
    at the destination is never silently clobbered just because the move
    came from the queue instead of a fresh ``--go`` plan; otherwise attempts
    ``adapter.move()``, isolated per-entry via a broad ``try/except
    Exception`` -- deliberately wider than a plain ``OSError`` catch, so
    that even an unexpected non-OSError bug in one entry's move can never
    prevent ``save_queue`` below from running and persisting the outcomes
    already recorded on every other entry -- so one failure never aborts
    the batch. ``entry.executed_at``/``entry.execution_error`` are set on
    every *attempted* entry (success or failure) but ``entry.status`` is
    never touched here -- execution and approval are tracked separately.

    The whole load -> mutate -> save cycle runs inside a single
    ``with_queue_lock`` block: the queue is loaded exactly once (not
    per-entry), and saved exactly once at the end, for the entire
    invocation.
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
    """Sort ``args.dir`` (default: configured search_roots, else the platform
    Downloads dir) into buckets, across every resolved root.

    Always computes the full plan; only mutates the filesystem when
    ``args.go`` is true. ``args`` may be ``None`` (or any object without
    ``dir``/``go`` attributes) to fall back to the defaults -- mirroring
    ``survey.run``'s ``args=None`` convenience for direct/test callers that
    don't go through argparse.

    ``args.dir`` may now name more than one root (any-and-all configured
    locations, not a fixed enum -- see ``_resolve_roots``, mirroring
    ``reclaim.py``/``corral_screenshots.py``). Root-existence handling is
    asymmetric on purpose: an **explicitly CLI-supplied** dir that doesn't
    exist is still a loud ``FileNotFoundError`` (a typo'd path someone just
    typed should never look indistinguishable from "nothing to sort") --
    checked here, before ``_plan`` ever runs. A **configured** root
    (``search_roots``, or the single default when nothing is configured) is
    best-effort instead: ``_plan`` silently skips one that doesn't exist,
    matching ``reclaim``/``corral-screenshots``'s existing precedent, since
    a broad, implicit location set may legitimately not all exist yet (e.g.
    no ``Documents`` folder on a fresh machine).

    In ``--go`` mode, each plan entry is executed independently: a failure
    moving one file (e.g. it was deleted out from under us mid-run) is
    caught and recorded on that entry rather than aborting the whole batch,
    so every other entry still gets its chance to move and the caller can
    see exactly what happened to each one. An entry whose destination
    already exists is never passed to ``adapter.move()`` -- it is recorded
    as skipped instead, so a same-named file already sitting in
    ``_sorted/<bucket>/`` is never silently clobbered.

    ``args.from_queue`` takes an entirely separate path: instead of
    computing/executing a fresh plan, it executes every already-*approved*
    ``move`` entry in the approval queue (see ``queue.py``) whose ``src``
    falls under any resolved root, via ``_run_from_queue``. See that
    helper's docstring for the staleness/isolation/locking details.
    """
    raw_dirs = getattr(args, "dir", None) if args is not None else None
    go = bool(getattr(args, "go", False)) if args is not None else False
    from_queue = bool(getattr(args, "from_queue", False)) if args is not None else False

    config = config_module.load_config(adapter)
    target_dirs = _resolve_roots(adapter, config, raw_dirs)

    if raw_dirs:
        for target_dir in target_dirs:
            if not target_dir.is_dir():
                raise FileNotFoundError(f"sort target directory does not exist: {target_dir}")

    if from_queue:
        from_queue_report = _run_from_queue(adapter, target_dirs)
        return {"roots": target_dirs, "go": go, "from_queue": from_queue_report}

    plan = _plan(adapter, config, target_dirs)

    if go:
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

    return {"roots": target_dirs, "go": go, "plan": plan}
