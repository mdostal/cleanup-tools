"""Approval queue for AI-proposed filesystem actions.

A ``QueueEntry`` represents a single proposed ``move`` or ``delete`` that is
awaiting (or has received) human approval, plus a ``status_history`` trail
recording every status transition it has gone through. The queue is
persisted as a YAML list at ``~/.config/cleanup-tools/approval_queue.yaml``
(see ``default_queue_path``), written atomically via
``OSAdapter.write_file_atomic`` so a reader never observes a half-written
file.

Because the CLI can be invoked concurrently (e.g. a background approver and
an interactive session touching the same queue file at once), every mutating
operation here (``stage_entries``, ``set_status``, ``undo``) takes the queue
file's lock via ``OSAdapter.file_lock`` for its entire read-modify-write
cycle, mirroring the load/mutate/save shape of ``config.py``'s
``load_config``/``save_config`` pair.
"""

from __future__ import annotations

import contextlib
import hashlib
import uuid
import yaml

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .adapters.base import OSAdapter


@dataclass
class QueueEntry:
    """A single proposed filesystem action, plus its approval lifecycle."""

    action: str
    src: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    dest: str = ""
    status: str = "pending"
    source: str = "manual"
    status_history: list[dict] = field(default_factory=list)
    group_key: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    plan_snapshot: dict = field(default_factory=dict)
    executed_at: str | None = None
    execution_error: str | None = None


def entry_size(entry: QueueEntry) -> int:
    """Best-effort size for dashboard/aggregate totals, from the entry's plan_snapshot.

    Directories and non-file paths don't carry a "size" key in
    plan_snapshot (see ``build_plan_snapshot``), so those contribute 0
    rather than raising or requiring a fresh (possibly slow) du call on
    every dashboard load.
    """
    snapshot = entry.plan_snapshot
    if not isinstance(snapshot, dict):
        return 0
    return snapshot.get("size") or 0


def is_ai_source(source: str) -> bool:
    """Whether a QueueEntry's ``source`` tag marks it as AI-proposed.

    AI-sourced entries carry ``source=f"ai:<provider>"`` (see
    ``ai.wiring._provider_source``, e.g. ``"ai:anthropic"``). Every other
    source convention currently in use -- the ``QueueEntry`` default
    ``"manual"``, and the UI's own ``"ui-plan-sort"``/``"ui-plan-reclaim"``
    plan-staging tags -- is a human-triggered source and therefore NOT
    AI-proposed. This is purely a rendering/reporting distinction: it never
    changes approve/reject/undo/bulk behavior, which treats every entry
    identically regardless of source.
    """
    return source.startswith("ai:")


def parse_group_key(group_key: str | None) -> dict:
    """Defensively split a ``group_key`` into ``{"pipeline", "location", "bucket"}``.

    Not a general-purpose parser -- it branches on the known pipeline
    prefix plus segment count, per the design discussion's simplified
    (non-migration-proven) parsing rule: the real queue resets before this
    schema ships, so this only needs to never raise on every format
    actually ever written, not round-trip arbitrary historical data.

    Formats handled:
      - None / "" -> no pipeline, "other" location, no bucket.
      - New (post-epic), 3 segments: ``sort:<location>:<bucket>``,
        ``reclaim:<location>:<category>``.
      - New (post-epic), 2 segments: ``corral-screenshots:<location>``.
      - Old (pre-epic), 2 segments: ``sort:<bucket>``, ``reclaim:<category>``
        -- no location segment existed yet, so location falls back to "other".
      - Old (pre-epic), 1 segment: ``corral-screenshots`` -- same fallback.
      - Anything else unrecognized -> no pipeline, "other" location, no bucket.
    """
    if not group_key:
        return {"pipeline": None, "location": "other", "bucket": None}
    parts = group_key.split(":")
    pipeline = parts[0]
    if pipeline in ("sort", "reclaim"):
        if len(parts) == 3:
            return {"pipeline": pipeline, "location": parts[1], "bucket": parts[2]}
        if len(parts) == 2:
            return {"pipeline": pipeline, "location": "other", "bucket": parts[1]}
        return {"pipeline": pipeline, "location": "other", "bucket": None}
    if pipeline == "corral-screenshots":
        if len(parts) == 2:
            return {"pipeline": pipeline, "location": parts[1], "bucket": None}
        return {"pipeline": pipeline, "location": "other", "bucket": None}
    return {"pipeline": None, "location": "other", "bucket": None}


def group_entries_hierarchical(entries: list[QueueEntry]) -> list[dict]:
    """Aggregate ``entries`` into a location -> bucket tree, via ``parse_group_key``.

    Relocated here (from ``ui/routes.py``, which still exposes it as
    ``_group_entries_hierarchical`` for backward compatibility) so
    ``chat/tools.py``'s ``list_queue_summary`` tool can reuse the exact
    same aggregate shape the dashboard tree already produces, without
    ``chat/`` importing from ``ui.routes`` -- see ``chat/tools.py``'s
    module docstring and ``config.configured_locations``'s docstring for
    the same reasoning applied elsewhere in this epic.

    Single pass over ``entries``, aggregates only -- never materializes a
    per-entry list anywhere in the returned structure (repeating the
    pre-pagination performance mistake at real, thousands-of-entries scale
    is the single most-cited risk for the dashboard tree story; see that
    story's own risk list and the prior-art research). Each bucket node is
    keyed by the entry's literal ``group_key`` (not just the parsed bucket
    label), so a bucket row's "Approve all"/"Reject all"/"Undo all" action
    -- an exact match against that literal group_key -- reaches every
    entry in that row and nothing outside it, even across the rare case of
    two different literal group_key strings (e.g. an old- and new-format
    group_key) that happen to parse to the same display label.

    Entries whose ``group_key`` doesn't resolve to a real configured
    location (``parse_group_key``'s "other" fallback, including entries
    with no ``group_key`` at all) land under an "other" location branch --
    it appears in the returned list like any other location whenever at
    least one such entry exists, never silently dropped.

    Both levels are sorted by descending total_size, largest first.
    """
    locations: dict[str, dict] = {}

    for entry in entries:
        parsed = parse_group_key(entry.group_key)
        location = parsed["location"]
        size = entry_size(entry)

        loc = locations.setdefault(
            location,
            {"location": location, "count": 0, "total_size": 0, "status_counts": {}, "buckets": {}},
        )
        loc["count"] += 1
        loc["total_size"] += size
        loc["status_counts"][entry.status] = loc["status_counts"].get(entry.status, 0) + 1

        bucket = loc["buckets"].setdefault(
            entry.group_key,
            {
                "group_key": entry.group_key,
                "label": parsed["bucket"] or parsed["pipeline"] or "ungrouped",
                "count": 0,
                "total_size": 0,
                "status_counts": {},
                "ai_count": 0,
            },
        )
        bucket["count"] += 1
        bucket["total_size"] += size
        bucket["status_counts"][entry.status] = bucket["status_counts"].get(entry.status, 0) + 1
        if is_ai_source(entry.source):
            bucket["ai_count"] += 1

    result = []
    for loc in locations.values():
        loc["buckets"] = sorted(loc["buckets"].values(), key=lambda b: b["total_size"], reverse=True)
        result.append(loc)
    return sorted(result, key=lambda loc: loc["total_size"], reverse=True)


QUEUE_FILENAME = "approval_queue.yaml"


def default_queue_path(adapter: OSAdapter) -> Path:
    """Return the default queue file path: ``~/.config/cleanup-tools/approval_queue.yaml``."""
    return adapter.resolve_home() / ".config" / "cleanup-tools" / QUEUE_FILENAME


def _entry_from_dict(data: dict) -> QueueEntry:
    return QueueEntry(
        action=data["action"],
        src=data["src"],
        id=data.get("id") or uuid.uuid4().hex,
        dest=data.get("dest", ""),
        status=data.get("status", "pending"),
        source=data.get("source", "manual"),
        status_history=list(data.get("status_history") or []),
        group_key=data.get("group_key"),
        created_at=data.get("created_at") or datetime.now(timezone.utc).isoformat(),
        plan_snapshot=dict(data.get("plan_snapshot") or {}),
        executed_at=data.get("executed_at"),
        execution_error=data.get("execution_error"),
    )


def _entry_to_dict(entry: QueueEntry) -> dict:
    return {
        "id": entry.id,
        "action": entry.action,
        "src": entry.src,
        "dest": entry.dest,
        "status": entry.status,
        "source": entry.source,
        "status_history": list(entry.status_history),
        "group_key": entry.group_key,
        "created_at": entry.created_at,
        "plan_snapshot": dict(entry.plan_snapshot),
        "executed_at": entry.executed_at,
        "execution_error": entry.execution_error,
    }


def load_queue(adapter: OSAdapter, path: Path | None = None) -> list[QueueEntry]:
    """Load the approval queue from ``path`` (default: ``default_queue_path``).

    Returns an empty list if the file does not exist -- an empty queue is
    the normal starting state, not an error. If the file exists but is not
    valid YAML (or does not contain a list at the top level), raises a
    ``ValueError`` naming the file and the parse problem, mirroring
    ``config.load_config``'s error convention. An entry missing a required
    key (``action``/``src``) raises the same file-naming ``ValueError``
    instead of a raw ``KeyError``.
    """
    if path is None:
        path = default_queue_path(adapter)

    if not path.exists():
        return []

    raw = adapter.read_file(path)
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"Failed to parse queue file {path}: {exc}") from exc

    if parsed is None:
        parsed = []
    if not isinstance(parsed, list):
        raise ValueError(
            f"Failed to parse queue file {path}: expected a YAML list at "
            f"the top level, got {type(parsed).__name__}"
        )

    try:
        return [_entry_from_dict(e) for e in parsed]
    except KeyError as exc:
        raise ValueError(
            f"Failed to parse queue file {path}: entry missing required key {exc}"
        ) from exc


def save_queue(adapter: OSAdapter, entries: list[QueueEntry], path: Path | None = None) -> None:
    """Serialize ``entries`` to YAML and write them via ``adapter.write_file_atomic``."""
    if path is None:
        path = default_queue_path(adapter)

    data = [_entry_to_dict(e) for e in entries]

    path.parent.mkdir(parents=True, exist_ok=True)
    adapter.write_file_atomic(path, yaml.safe_dump(data, sort_keys=False))


@contextlib.contextmanager
def with_queue_lock(adapter: OSAdapter, path: Path):
    """Thin wrapper around ``adapter.file_lock`` scoped to a queue file.

    Exists mainly so callers (and tests) have one obvious name for "the
    lock guarding this queue file" rather than reaching into the adapter
    directly, and so the locking strategy can change in one place later.
    """
    with adapter.file_lock(path):
        yield


def _find_entry(entries: list[QueueEntry], entry_id: str, path: Path) -> QueueEntry:
    for entry in entries:
        if entry.id == entry_id:
            return entry
    raise ValueError(f"No queue entry with id {entry_id!r} found in {path}")


def stage_entries(
    adapter: OSAdapter, new_entries: list[QueueEntry], path: Path | None = None
) -> list[QueueEntry]:
    """Append ``new_entries`` to the queue, skipping duplicates of a pending entry.

    Under the queue file lock: loads the existing queue, drops any new
    entry whose ``src`` matches an already-*pending* existing entry (so
    re-running a planner that would re-propose the same in-flight action is
    a no-op instead of creating a duplicate approval request -- entries
    that were already approved/rejected/etc. don't block a fresh proposal
    for the same ``src``), appends the rest, saves, and returns only the
    entries that were actually added.

    Dedup is tracked with a single "seen" set that starts as the existing
    pending srcs and gains each src as it's accepted, so two entries sharing
    a ``src`` within the *same* ``new_entries`` batch are also deduped (only
    the first is added) -- not just duplicates against pre-existing state.
    """
    if path is None:
        path = default_queue_path(adapter)

    with with_queue_lock(adapter, path):
        existing = load_queue(adapter, path)
        # "seen" starts from pre-existing pending srcs and grows as we accept
        # entries from this batch, so a duplicate src *within* new_entries is
        # caught too -- not just duplicates against state that predates this
        # call. Without folding batch-accepted srcs in as we go, two entries
        # sharing a src in the same new_entries list would both pass (each
        # only checked against the entries-at-load-time snapshot) and both
        # get added.
        seen_srcs = {e.src for e in existing if e.status == "pending"}

        added = []
        for e in new_entries:
            if e.src in seen_srcs:
                continue
            seen_srcs.add(e.src)
            added.append(e)

        existing.extend(added)
        save_queue(adapter, existing, path)

    return added


def set_status(
    adapter: OSAdapter, entry_id: str, new_status: str, path: Path | None = None
) -> QueueEntry:
    """Transition the entry with ``entry_id`` to ``new_status``, recording history.

    Under the queue file lock: loads the queue, finds the entry by id
    (raising ``ValueError`` naming the missing id and the queue file if it
    isn't found), appends a ``{"status", "timestamp"}`` record to its
    ``status_history``, sets ``status`` to ``new_status``, saves, and
    returns the updated entry.

    If ``new_status`` matches the most recent ``status_history`` record
    (i.e. this is a repeat transition to the status the entry is already
    in -- e.g. a fast double-click on "Approve" before the page redirects,
    since there is no client-side debounce), no new history record is
    appended. Without this, two consecutive identical history entries would
    accumulate, and a single ``undo()`` call would only pop the duplicate
    and land back on the same status instead of the true prior one.
    """
    if path is None:
        path = default_queue_path(adapter)

    with with_queue_lock(adapter, path):
        entries = load_queue(adapter, path)
        entry = _find_entry(entries, entry_id, path)

        already_there = (
            entry.status_history and entry.status_history[-1]["status"] == new_status
        )
        if not already_there:
            entry.status_history.append(
                {"status": new_status, "timestamp": datetime.now(timezone.utc).isoformat()}
            )
        entry.status = new_status

        save_queue(adapter, entries, path)

    return entry


def edit_entry(
    adapter: OSAdapter,
    entry_id: str,
    new_dest: str,
    new_group_key: str | None,
    path: Path | None = None,
) -> QueueEntry:
    """Change a *pending* entry's proposed ``dest``/``group_key`` before approval.

    Only ever touches a pending entry: editing an already-approved/
    rejected entry's proposed destination is meaningless (it already has
    -- or, for a rejected entry, never will have -- a real outcome), and
    editing after execution would silently disagree with what actually
    happened on disk. Raises ``ValueError`` (naming the entry id and its
    actual status) if ``entry.status`` isn't ``"pending"``, mirroring
    ``set_status``'s guard-rail style for invalid transitions.

    Under the same queue file lock / load-mutate-save cycle as
    ``set_status``/``undo``: loads the queue, finds the entry (raising
    ``ValueError`` as in ``set_status`` if it isn't found), appends a
    ``status_history`` record carrying the old and new ``dest``/
    ``group_key`` (so the edit is auditable the same way a status
    transition is -- see ``QueueEntry.status_history``), mutates the
    entry, saves, and returns it.
    """
    if path is None:
        path = default_queue_path(adapter)

    with with_queue_lock(adapter, path):
        entries = load_queue(adapter, path)
        entry = _find_entry(entries, entry_id, path)

        if entry.status != "pending":
            raise ValueError(
                f"Cannot edit entry {entry_id!r}: status is {entry.status!r}, not 'pending'"
            )

        entry.status_history.append(
            {
                "status": entry.status,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "edit": {
                    "dest": {"old": entry.dest, "new": new_dest},
                    "group_key": {"old": entry.group_key, "new": new_group_key},
                },
            }
        )
        entry.dest = new_dest
        entry.group_key = new_group_key

        save_queue(adapter, entries, path)

    return entry


def undo(adapter: OSAdapter, entry_id: str, path: Path | None = None) -> QueueEntry:
    """Revert the entry with ``entry_id`` to its previous status.

    Under the queue file lock: loads the queue, finds the entry (raising
    ``ValueError`` as in ``set_status`` if it isn't found), raises
    ``ValueError`` if ``status_history`` has only one entry (there is
    nothing before it to revert to), otherwise pops the most recent history
    record and sets ``status`` back to whatever the new-last record's
    status is, saves, and returns the updated entry.
    """
    if path is None:
        path = default_queue_path(adapter)

    with with_queue_lock(adapter, path):
        entries = load_queue(adapter, path)
        entry = _find_entry(entries, entry_id, path)

        if len(entry.status_history) <= 1:
            raise ValueError(f"Cannot undo entry {entry_id!r}: no prior status to revert to")

        entry.status_history.pop()
        entry.status = entry.status_history[-1]["status"]

        save_queue(adapter, entries, path)

    return entry


# Content hashing is capped at this many bytes so a multi-GB file doesn't
# have to be read in full just to gate a staleness check: files at or under
# this size get a hash of their entire content, so the check is exact; files
# larger than this get a hash of only their first CONTENT_HASH_MAX_BYTES
# bytes (a "prefix hash"), which is a documented, deliberate tradeoff --
# a content change confined entirely to the untouched tail of a huge file
# (with size and mtime *also* coincidentally unchanged) could in theory slip
# past it. That residual gap is far narrower than the size+mtime-only check
# it replaces, and full-file hashing on every staleness check for very large
# files would be prohibitively slow on the delete/move-approval hot path.
CONTENT_HASH_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB


def _content_hash(path: Path) -> str:
    """Return a sha256 hex digest of ``path``'s content (see ``CONTENT_HASH_MAX_BYTES``)."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        hasher.update(f.read(CONTENT_HASH_MAX_BYTES))
    return hasher.hexdigest()


def build_plan_snapshot(path: Path) -> dict:
    """Build the ``plan_snapshot`` dict for ``path`` at entry-creation time.

    Callers that construct a ``QueueEntry`` for a real, existing file should
    use this to populate ``plan_snapshot`` rather than hand-assembling
    ``{"size": ..., "mtime": ...}``, so every entry gets the ``content_hash``
    that ``check_staleness`` needs to catch same-size, restored-mtime content
    swaps (see ``check_staleness``'s docstring for why size/mtime alone
    aren't enough).

    Content-hash staleness detection is file-only: it's gated on
    ``path.is_file()``, not merely "not a directory". This deliberately
    covers more than directories -- hashing every file in a directory tree
    (``node_modules``, an orphaned build dir, ...) on every plan/check would
    be prohibitively expensive on the delete/move-approval hot path, but
    ``is_file()`` also returns False (rather than raising) for FIFOs,
    sockets, device files, and broken symlinks. Opening a FIFO to hash it
    can block indefinitely waiting for a writer, and a directory's "size" as
    reported by ``stat`` is filesystem-dependent metadata, not a meaningful
    measure of its content anyway -- so none of these get hashed. Anything
    that isn't a plain regular file gets the same fallback snapshot: no
    ``"content_hash"`` and no ``"size"``, only ``"mtime"`` plus an
    ``"is_dir"`` marker (``True`` only for an actual directory). At the very
    top, ``path.exists()`` is checked before ``path.stat()`` is ever called
    at all -- ``exists()`` follows symlinks and returns False for a broken
    symlink without raising, whereas ``.stat()`` follows symlinks by default
    and raises ``FileNotFoundError`` on one, so a broken symlink gets the
    same non-file fallback shape (minus ``"mtime"``, since there is nothing
    safe to stat) rather than crashing ``build_plan_snapshot`` outright.

    ``check_staleness`` then falls back to mtime-only (or existence-only, for
    a broken symlink) staleness detection for these entries: a strictly
    weaker guarantee than the file path gets (a change confined to a file
    *inside* a directory doesn't always bump the directory's own mtime on
    every filesystem/operation), but it no longer hangs or crashes, and it's
    still better than nothing.
    """
    path = Path(path)
    if not path.exists():
        # Nothing safe to stat (e.g. a broken symlink) -- fall back to the
        # same non-file snapshot shape used below, just without "mtime".
        return {
            "is_dir": False,
        }

    stat = path.stat()
    if not path.is_file():
        # Directories, FIFOs, sockets, device files, ... -- anything that
        # isn't a plain regular file falls back to the same mtime-only,
        # no-hash snapshot the directory case has always used.
        return {
            "mtime": stat.st_mtime,
            "is_dir": path.is_dir(),
        }
    return {
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "content_hash": _content_hash(path),
        "is_dir": False,
    }


def check_staleness(adapter: OSAdapter, entry: QueueEntry) -> bool:
    """Return whether ``entry.src`` has changed since ``entry.plan_snapshot`` was taken.

    Existence and stat checks go through plain ``pathlib`` rather than
    ``adapter`` -- mirroring the house convention established in
    ``commands/sort.py`` (``dest_exists`` via plain ``dest.exists()``) of
    using pathlib directly for simple filesystem predicates that don't need
    OS-specific handling. ``adapter`` is accepted (and unused) to keep this
    function's signature consistent with the rest of this module and leave
    room for an adapter-backed stat in a future OS where that matters.

    Returns True if ``entry.src`` no longer exists, or if its current size
    or mtime no longer matches what was recorded in ``plan_snapshot`` at
    plan time (whichever of ``"size"``/``"mtime"`` the snapshot recorded).

    Also returns True if the snapshot recorded a ``"content_hash"`` (see
    ``build_plan_snapshot``) and the file's current content hash no longer
    matches it. This closes the gap where size and mtime alone say "not
    stale" for a same-size content replacement whose mtime happens to be
    restored or coincide (``cp -p``, ``rsync -t``, some editors' save-in-place
    behavior, or plain filesystem mtime-resolution collisions) -- since this
    check gates whether an approved delete/move is safe to actually run,
    content is checked whenever a hash was recorded, not just size/mtime.

    Content-hash staleness detection is file-only, gated on
    ``src_path.is_file()`` rather than merely "not a directory". When
    ``entry.src`` currently is not a plain regular file -- a directory, or a
    FIFO/socket/device/broken-symlink that ``entry.src`` came to point at
    after planning -- content is never hashed (see ``build_plan_snapshot``
    for why), so staleness for that entry falls back to the ``"mtime"``-only
    comparison above. That's a weaker guarantee than a file gets, but it
    doesn't crash on ``open(path, "rb")`` raising ``IsADirectoryError`` for a
    directory, and it doesn't hang indefinitely on ``open()`` blocking for a
    writer on a FIFO.

    ``entry.src`` no longer existing is handled before any of this: a
    missing path (including a broken symlink, which ``Path.exists()``
    correctly reports as not existing without raising) returns True above
    before ``.stat()`` is ever called, so ``.stat()`` here only ever runs on
    a path already confirmed to exist.

    Also returns True -- without attempting any stat-field comparison -- if
    whether ``entry.src`` is currently a directory disagrees with whether it
    was a directory when ``plan_snapshot`` was taken (a file replaced by a
    directory of the same name, or vice versa, between planning and this
    check). That's a change worth flagging regardless of what size/mtime/
    content_hash say, and comparing e.g. a recorded file size against a
    directory's stat would be meaningless anyway.
    """
    src_path = Path(entry.src)
    if not src_path.exists():
        return True

    snapshot = entry.plan_snapshot
    stat = src_path.stat()
    is_dir = src_path.is_dir()

    if is_dir != bool(snapshot.get("is_dir", False)):
        return True

    if "size" in snapshot and stat.st_size != snapshot["size"]:
        return True
    if "mtime" in snapshot and stat.st_mtime != snapshot["mtime"]:
        return True
    if (
        src_path.is_file()
        and "content_hash" in snapshot
        and _content_hash(src_path) != snapshot["content_hash"]
    ):
        return True

    return False
