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


def _plan(adapter: OSAdapter, config: config_module.Config, target_dir: Path) -> list[dict]:
    """Compute the move plan for every non-dotfile directly inside ``target_dir``.

    ``adapter.list_dir(target_dir, max_depth=0)`` returns files at any depth
    that happen to satisfy its depth-0 walk semantics, but it does not
    exclude dotfiles (e.g. ``.DS_Store``) -- those are filtered out here
    rather than in the adapter, since "skip hidden files" is sort-specific
    policy, not a general filesystem-listing concern.

    Each entry also carries ``dest_exists``: whether ``dest`` already exists
    on disk at plan time. This is informational in dry-run mode (it tells
    the caller a real ``--go`` run would skip that entry rather than
    overwrite something already there) and is what the ``--go`` path below
    uses to decide whether to actually call ``adapter.move()``.
    """
    plan: list[dict] = []
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


def _is_under(child: Path, parent: Path) -> bool:
    """True if resolved ``child`` is ``parent`` itself or somewhere underneath it."""
    return child == parent or parent in child.parents


def _run_from_queue(adapter: OSAdapter, target_dir: Path) -> dict:
    """Execute every approved, queue-sourced ``move`` whose ``src`` is under ``target_dir``.

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
    resolved_target = _resolve_loose(target_dir)

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
            and _is_under(_resolve_loose(Path(entry.src)), resolved_target)
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
    """Sort ``args.dir`` (default: the platform Downloads dir) into buckets.

    Always computes the full plan; only mutates the filesystem when
    ``args.go`` is true. ``args`` may be ``None`` (or any object without
    ``dir``/``go`` attributes) to fall back to the defaults -- mirroring
    ``survey.run``'s ``args=None`` convenience for direct/test callers that
    don't go through argparse.

    Raises ``FileNotFoundError`` if the resolved target directory does not
    exist, rather than silently returning an empty plan -- a typo'd or
    since-deleted path should be a loud error, not indistinguishable from
    "nothing to sort".

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
    falls under ``target_dir``, via ``_run_from_queue``. See that helper's
    docstring for the staleness/isolation/locking details.
    """
    raw_dir = getattr(args, "dir", None) if args is not None else None
    go = bool(getattr(args, "go", False)) if args is not None else False
    from_queue = bool(getattr(args, "from_queue", False)) if args is not None else False

    target_dir = Path(raw_dir) if raw_dir is not None else adapter.resolve_standard_dir("downloads")

    if not target_dir.is_dir():
        raise FileNotFoundError(f"sort target directory does not exist: {target_dir}")

    if from_queue:
        from_queue_report = _run_from_queue(adapter, target_dir)
        return {"dir": target_dir, "go": go, "from_queue": from_queue_report}

    config = config_module.load_config(adapter)
    plan = _plan(adapter, config, target_dir)

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

    return {"dir": target_dir, "go": go, "plan": plan}
