"""Sort a directory's loose top-level files into ``_sorted/<bucket>/`` — the
Python port of ``scripts/sort-downloads.sh``.

Dry-run by default: :func:`run` always computes and returns the full move
plan, and only calls :meth:`~cleanup_tools.adapters.base.OSAdapter.move` for
each entry when explicitly told to via ``--go``. The returned dict's shape
(``{"dir", "go", "plan"}``) is identical either way, so callers can inspect
exactly what would happen (or did happen) without branching on ``go``.
"""

from __future__ import annotations

from pathlib import Path

from cleanup_tools import config as config_module
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
    """
    raw_dir = getattr(args, "dir", None) if args is not None else None
    go = bool(getattr(args, "go", False)) if args is not None else False

    target_dir = Path(raw_dir) if raw_dir is not None else adapter.resolve_standard_dir("downloads")

    if not target_dir.is_dir():
        raise FileNotFoundError(f"sort target directory does not exist: {target_dir}")

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
