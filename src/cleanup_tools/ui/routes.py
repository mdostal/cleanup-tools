"""Routes for the approvals UI.

Scope for this story: a dashboard overview, plan-staging routes that reuse
the existing sort/reclaim planning logic (never re-scanning the filesystem
themselves), a pending-entry review queue, per-entry approve/reject/undo,
and an image thumbnail route that NEVER serves an original full-resolution
file over HTTP.

This module deliberately never calls ``sort.run``/``reclaim.run`` with
``go=True`` and never touches ``--from-queue`` execution -- the UI only
ever moves entries between queue *statuses* (via ``queue.set_status``/
``queue.undo``), never the files themselves. Execution stays a separate,
deliberate CLI step.

A follow-up story adds bulk actions / keyboard shortcuts / pagination. To
keep that addable without a rewrite:

- Single-entry status changes go through the small ``_transition`` helper
  below; a future bulk route can loop over ids calling the same helper
  (or wrap ``queue.set_status`` directly the same way) instead of
  duplicating the approve/reject logic.
- ``queue.html``'s per-entry card is a self-contained block with the entry
  id available as both a ``data-entry-id`` attribute and each form's URL,
  so a bulk-select checkbox can be dropped into the existing card markup
  without restructuring it (a disabled placeholder checkbox is already
  there, see the template).
- ``/queue`` lists all pending entries as a plain Python list assembled by
  ``_pending_entries``; pagination can slice that list without changing
  how entries are fetched or rendered.
"""

from __future__ import annotations

import io
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, redirect, render_template, request, url_for
from PIL import Image, UnidentifiedImageError

from .. import queue as queue_module
from ..commands import reclaim as reclaim_module
from ..commands import sort as sort_module

bp = Blueprint("ui", __name__, template_folder="templates")

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp"}

# Message shown when a queue mutation can't acquire the queue file's lock
# (adapter.file_lock's default 5s timeout) because another process -- e.g. a
# `cleanup sort --from-queue` or `cleanup reclaim --from-queue` run -- is
# legitimately holding it for the duration of its execution. This is a
# transient condition, not a real failure, so every route that touches the
# queue lock surfaces the same wording rather than leaking the raw
# TimeoutError message.
QUEUE_BUSY_MESSAGE = "Queue is busy -- try again in a moment."

# Thumbnails are capped to this many pixels on the long edge. Pillow's
# Image.thumbnail() only ever shrinks (never enlarges), and the thumbnail
# route below always re-encodes to JPEG regardless of the source format --
# so what goes out over HTTP is never byte-identical to the original file,
# even for an already-small source image.
THUMBNAIL_MAX_PX = 256


# ---------------------------------------------------------------------------
# Small helpers shared by multiple routes.
# ---------------------------------------------------------------------------


def _adapter():
    return current_app.config["CLEANUP_ADAPTER"]


def _queue_path():
    """The queue path this app was configured with, or the adapter's default.

    Resolved fresh on every call (rather than cached at app-creation time)
    so a fake-home adapter's default path is picked up correctly by every
    request in tests.
    """
    configured = current_app.config.get("CLEANUP_QUEUE_PATH")
    if configured is not None:
        return configured
    return queue_module.default_queue_path(_adapter())


def _load_entries() -> list[queue_module.QueueEntry]:
    return queue_module.load_queue(_adapter(), _queue_path())


def _find_entry(entry_id: str) -> queue_module.QueueEntry | None:
    for entry in _load_entries():
        if entry.id == entry_id:
            return entry
    return None


def is_image_entry(entry: queue_module.QueueEntry) -> bool:
    """Whether ``entry.src``'s extension is one the thumbnail route can render."""
    return Path(entry.src).suffix.lower() in IMAGE_EXTENSIONS


def _pending_entries() -> list[queue_module.QueueEntry]:
    return [e for e in _load_entries() if e.status == "pending"]


def _entry_size(entry: queue_module.QueueEntry) -> int:
    """Best-effort size for dashboard totals, from the entry's plan_snapshot.

    Directories and non-file paths don't carry a "size" key in
    plan_snapshot (see ``queue.build_plan_snapshot``), so those contribute
    0 rather than raising or requiring a fresh (possibly slow) du call on
    every dashboard load.
    """
    snapshot = entry.plan_snapshot
    if not isinstance(snapshot, dict):
        return 0
    return snapshot.get("size") or 0


def _group_entries(entries: list[queue_module.QueueEntry]) -> list[dict]:
    """Group ``entries`` by ``group_key`` (falling back to "ungrouped").

    Each group dict carries an entry count, a summed size (see
    ``_entry_size``), and a per-status breakdown -- so the dashboard can
    show "12 items, 340 MB, 8 pending / 3 approved / 1 rejected" per group,
    not just a flat count. Groups are returned sorted by total size,
    largest first (DiskDrill-style "what's using the most space" ordering).
    """
    groups: dict[str, dict] = {}
    for entry in entries:
        key = entry.group_key or "ungrouped"
        group = groups.setdefault(
            key, {"group_key": key, "count": 0, "total_size": 0, "status_counts": {}}
        )
        group["count"] += 1
        group["total_size"] += _entry_size(entry)
        group["status_counts"][entry.status] = group["status_counts"].get(entry.status, 0) + 1

    return sorted(groups.values(), key=lambda g: g["total_size"], reverse=True)


def _status_counts(entries: list[queue_module.QueueEntry]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.status] = counts.get(entry.status, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Dashboard.
# ---------------------------------------------------------------------------


@bp.route("/")
def dashboard():
    entries = _load_entries()
    return render_template(
        "dashboard.html",
        groups=_group_entries(entries),
        overall_status_counts=_status_counts(entries),
        total_entries=len(entries),
        staged=request.args.get("staged"),
        plan_error=request.args.get("plan_error"),
    )


# ---------------------------------------------------------------------------
# Plan staging: reuse sort.run()/reclaim.run()'s existing dry-run planning
# logic (never re-implement scanning here), then stage_entries() every
# proposed action into the queue. stage_entries() is what makes hitting
# these routes twice in a row idempotent -- it drops any new entry whose src
# already matches a *pending* entry, so re-planning against an unchanged
# filesystem produces zero new entries the second time.
# ---------------------------------------------------------------------------


def _stage_sort_plan(adapter) -> list[queue_module.QueueEntry]:
    """Dry-run cleanup_tools.commands.sort against the default target dir,
    convert its plan into QueueEntry "move" proposals, and stage them.

    Reuses sort.run()'s own plan-building (args=None -> dry-run against the
    platform Downloads dir, same default the "sort" CLI subcommand uses
    with no args) rather than walking the filesystem again here.
    """
    result = sort_module.run(adapter, args=None)
    new_entries = [
        queue_module.QueueEntry(
            action="move",
            src=str(item["src"]),
            dest=str(item["dest"]),
            source="ui-plan-sort",
            group_key=f"sort:{item['bucket']}",
            plan_snapshot=queue_module.build_plan_snapshot(item["src"]),
        )
        for item in result.get("plan", [])
    ]
    return queue_module.stage_entries(adapter, new_entries, _queue_path())


def _stage_reclaim_plan(adapter) -> list[queue_module.QueueEntry]:
    """Dry-run cleanup_tools.commands.reclaim across its default roots,
    convert every non-refused candidate into a QueueEntry "delete"
    proposal, and stage them.

    Master-path-refused candidates (see reclaim.py's
    ``MASTER_PATH_REFUSAL_REASON``) are never staged -- there is nothing to
    approve for a candidate reclaim.py itself refuses to ever delete, even
    under --go.
    """
    result = reclaim_module.run(adapter, args=None)
    new_entries = []
    for category_name, category in result.get("categories", {}).items():
        for item in category.get("entries", []):
            if item.get("master_path_refused"):
                continue
            new_entries.append(
                queue_module.QueueEntry(
                    action="delete",
                    src=str(item["path"]),
                    dest="",
                    source="ui-plan-reclaim",
                    group_key=f"reclaim:{category_name}",
                    plan_snapshot=queue_module.build_plan_snapshot(item["path"]),
                )
            )
    return queue_module.stage_entries(adapter, new_entries, _queue_path())


@bp.route("/plan/sort")
def plan_sort():
    adapter = _adapter()
    try:
        added = _stage_sort_plan(adapter)
    except FileNotFoundError as exc:
        return redirect(url_for("ui.dashboard", plan_error=str(exc)))
    except TimeoutError:
        return redirect(url_for("ui.dashboard", plan_error=QUEUE_BUSY_MESSAGE))
    return redirect(url_for("ui.dashboard", staged=len(added)))


@bp.route("/plan/reclaim")
def plan_reclaim():
    adapter = _adapter()
    try:
        added = _stage_reclaim_plan(adapter)
    except TimeoutError:
        return redirect(url_for("ui.dashboard", plan_error=QUEUE_BUSY_MESSAGE))
    return redirect(url_for("ui.dashboard", staged=len(added)))


# ---------------------------------------------------------------------------
# Review queue.
# ---------------------------------------------------------------------------


@bp.route("/queue")
def queue_view():
    return render_template("queue.html", entries=_pending_entries(), is_image_entry=is_image_entry)


@bp.route("/queue/<entry_id>/approve", methods=["POST"])
def approve_entry(entry_id):
    try:
        queue_module.set_status(_adapter(), entry_id, "approved", _queue_path())
    except ValueError as exc:
        abort(404, description=str(exc))
    except TimeoutError:
        abort(503, description=QUEUE_BUSY_MESSAGE)
    return redirect(url_for("ui.queue_view"))


@bp.route("/queue/<entry_id>/reject", methods=["POST"])
def reject_entry(entry_id):
    try:
        queue_module.set_status(_adapter(), entry_id, "rejected", _queue_path())
    except ValueError as exc:
        abort(404, description=str(exc))
    except TimeoutError:
        abort(503, description=QUEUE_BUSY_MESSAGE)
    return redirect(url_for("ui.queue_view"))


@bp.route("/queue/<entry_id>/undo", methods=["POST"])
def undo_entry(entry_id):
    # queue.undo() raises ValueError for both "no such entry" and "found,
    # but nothing to revert to" -- looking the entry up here first lets
    # those two cases map to the right status code (404 vs 400) instead of
    # collapsing both into one, and keeps "unknown id" consistent with
    # approve/reject above.
    if _find_entry(entry_id) is None:
        abort(404)
    try:
        queue_module.undo(_adapter(), entry_id, _queue_path())
    except ValueError as exc:
        return str(exc), 400
    except TimeoutError:
        abort(503, description=QUEUE_BUSY_MESSAGE)
    return redirect(url_for("ui.queue_view"))


# ---------------------------------------------------------------------------
# Thumbnails. This is the ONLY route that ever reads image bytes off disk
# for HTTP serving, and it never returns the original file: the source is
# always opened, downscaled to at most THUMBNAIL_MAX_PX on its long edge,
# and re-encoded to JPEG before being written to the response body. There is
# no other route (and never should be) that streams a queue entry's src
# path's raw bytes.
# ---------------------------------------------------------------------------


@bp.route("/thumbnail/<entry_id>")
def thumbnail(entry_id):
    entry = _find_entry(entry_id)
    if entry is None:
        abort(404)

    src_path = Path(entry.src)
    if src_path.suffix.lower() not in IMAGE_EXTENSIONS:
        abort(404)
    if not src_path.is_file():
        abort(404)

    try:
        with Image.open(src_path) as img:
            img.load()
            img.thumbnail((THUMBNAIL_MAX_PX, THUMBNAIL_MAX_PX))
            if img.mode != "RGB":
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        # DecompressionBombError is a plain Exception subclass (NOT an
        # OSError), raised by Pillow for images whose declared pixel count
        # exceeds its default safety threshold (~179M px) -- a real,
        # legitimately-large image (e.g. a stitched panorama or scan) hits
        # this just as readily as a malicious file, so it gets the same
        # graceful 404 as an unreadable/unidentifiable image, not a 500.
        abort(404)

    return Response(buf.getvalue(), mimetype="image/jpeg")
