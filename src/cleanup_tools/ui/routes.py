"""Routes for the approvals UI.

Scope: a dashboard overview, plan-staging routes that reuse the existing
sort/reclaim planning logic (never re-scanning the filesystem themselves),
a pending-entry review queue with pagination, per-entry approve/reject/undo,
bulk approve/reject (by group_key or explicit id list), and an image
thumbnail route that NEVER serves an original full-resolution file over
HTTP.

This module deliberately never calls ``sort.run``/``reclaim.run`` with
``go=True`` and never touches ``--from-queue`` execution -- the UI only
ever moves entries between queue *statuses* (via ``queue.set_status``/
``queue.undo``), never the files themselves. Execution stays a separate,
deliberate CLI step.

Bulk actions (``/queue/bulk-approve``, ``/queue/bulk-reject``) reuse
``queue.set_status`` per-id exactly the way the single-entry routes do --
see ``_bulk_transition`` -- rather than duplicating the transition logic.
Keyboard shortcuts (``static/keyboard.js``) work entirely client-side
against the existing per-entry approve/reject forms (each tagged with a
``data-action`` attribute) and the bulk-select checkboxes rendered in
``queue.html``; no new server-side state is needed for focus tracking.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

from flask import Blueprint, Response, abort, current_app, jsonify, redirect, render_template, request, url_for
from PIL import Image, UnidentifiedImageError

from .. import config as config_module
from .. import queue as queue_module
from ..ai import CredentialsError, default_credentials_path, get_provider, get_raw_client
from ..ai.wiring import DEFAULT_CAP as DEFAULT_AI_CAP
from ..ai.wiring import propose_for_other_bucket
from ..chat import engine as chat_engine
from ..chat import state as chat_state
from ..commands import corral_screenshots as corral_screenshots_module
from ..commands import reclaim as reclaim_module
from ..commands import sort as sort_module
from ..semantic import index as semantic_index
from ..semantic import pipeline as semantic_pipeline
from . import jobs

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

# Relocated to queue.py (as PROTECTED_PATH_ROOTS/is_protected_path) so
# chat/tools.py's propose_moves can reuse this exact guard without chat/
# importing from ui.routes -- see queue.py's PROTECTED_PATH_ROOTS docstring.
# Kept as module-level aliases here (not re-defined) so every existing call
# site/import in this module and in tests/test_ui_routes.py keeps working
# unchanged -- including monkeypatching: tests that need to override the
# protected roots now patch queue_module.PROTECTED_PATH_ROOTS (the real
# module ``_is_protected_path``/``is_protected_path`` reads from), not this
# alias.
PROTECTED_PATH_ROOTS = queue_module.PROTECTED_PATH_ROOTS
_is_protected_path = queue_module.is_protected_path


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


# Relocated to queue.py (as is_ai_source/entry_size/parse_group_key/
# group_entries_hierarchical) so chat/tools.py can reuse the exact same
# aggregate shapes without importing from ui.routes -- see queue.py's
# group_entries_hierarchical docstring. Kept as module-level aliases here
# (not re-defined) so every existing call site/import in this module and
# in tests/test_ui_routes.py keeps working unchanged.
_is_ai_source = queue_module.is_ai_source
_entry_size = queue_module.entry_size
parse_group_key = queue_module.parse_group_key
_group_entries_hierarchical = queue_module.group_entries_hierarchical


def _pending_entries() -> list[queue_module.QueueEntry]:
    return [e for e in _load_entries() if e.status == "pending"]


# Relocated to config.py (alongside configured_locations, for the same
# reason -- see that function's docstring) so chat/tools.py's propose_moves
# can build a proposed entry's dest/group_key identically to every other
# staging path, without chat/ importing from ui.routes.
_location_for_src = config_module.location_for_src


BUCKET_ICONS = {
    "photos": "📷",
    "screenshots": "🖼️",
    "pdfs": "📄",
    "docs": "📝",
    "archives": "🗜️",
    "videos": "🎬",
    "installers": "💿",
    "data": "📊",
}
DEFAULT_BUCKET_ICON = "📁"


def bucket_icon(bucket: str | None) -> str:
    """A small emoji icon for a bucket name -- Guided mode's (config.ui_mode)
    per-entry visual scan aid. Always paired with the bucket's text label
    wherever it's used (never icon-alone), per the same "icon + text,
    never color/icon alone" principle the status badges follow.
    """
    return BUCKET_ICONS.get(bucket or "", DEFAULT_BUCKET_ICON)


def short_path(path: str) -> str:
    """Shorten an absolute path for display: ``~``-relativize against the
    real home directory, then cap to at most the last two path segments,
    marked with a leading ``"…/"`` when something was actually cut.

    A pure display transform -- it never invents a name and never hides
    the real path from the user permanently; every call site wraps this
    in a ``<details>/<summary>`` disclosure (keyboard/focus-operable, not
    mouse-hover-only -- see base.html's ``.path-disclosure``) so the full,
    real path is always one keystroke or click away. This directly
    answers the loudest, most-repeated finding from this project's UI
    design review: raw 60-100+ character paths repeated verbatim 3-4
    times per screen were the single biggest complaint across every
    persona that reviewed it.
    """
    p = Path(path)
    parts = p.parts
    home_parts = Path.home().parts

    if parts[: len(home_parts)] == home_parts:
        rel_parts = parts[len(home_parts) :]
        if not rel_parts:
            return "~"
        if len(rel_parts) <= 2:
            return "~/" + "/".join(rel_parts)
        return "…/" + "/".join(rel_parts[-2:])

    if len(parts) <= 3:
        return str(p)
    return "…/" + "/".join(parts[-2:])


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
            key,
            {"group_key": key, "count": 0, "total_size": 0, "status_counts": {}, "ai_count": 0},
        )
        group["count"] += 1
        group["total_size"] += _entry_size(entry)
        group["status_counts"][entry.status] = group["status_counts"].get(entry.status, 0) + 1
        if _is_ai_source(entry.source):
            group["ai_count"] += 1

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
    adapter = _adapter()
    config = config_module.load_config(adapter)
    return render_template(
        "dashboard.html",
        groups=_group_entries(entries),
        location_tree=_group_entries_hierarchical(entries),
        configured_locations=config_module.configured_locations(config, adapter),
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


def _stage_sort_plan(
    adapter, queue_path, progress_callback=None, dirs: list[str] | None = None
) -> list[queue_module.QueueEntry]:
    """Dry-run cleanup_tools.commands.sort against the target dir(s), convert
    its plan into QueueEntry "move" proposals, and stage them.

    Reuses sort.run()'s own plan-building rather than walking the
    filesystem again here. ``dirs``, if given (a subset of configured
    search_roots picked from the dashboard's location multi-select),
    scopes the scan to exactly those roots -- passed through as
    ``args.dir``, the same shape ``cli.py``'s real argparse Namespace uses.
    ``dirs=None`` (the default) falls back to sort.run()'s own default
    resolution (configured search_roots, else the platform Downloads dir),
    same as the "sort" CLI subcommand with no args.

    The slow part of this route is NOT sort.run() itself (a plain
    filesystem walk) -- it's the per-proposed-entry
    ``queue_module.build_plan_snapshot`` call below, which reads up to 8MiB
    of each file to compute a real content hash (see that function's
    docstring). With a real Downloads folder containing thousands of files,
    that loop alone is what used to make this route block for minutes.

    ``queue_path`` is taken explicitly (rather than resolved via
    ``_queue_path()`` internally) because this is called from a background
    job thread (see ``_sort_job``/``plan_sort`` below) that has no Flask
    request/app context to resolve ``current_app`` against -- the caller
    must resolve it while still inside the request and pass it in, exactly
    like ``_stage_reclaim_plan``.

    If ``progress_callback`` is given, it's called once per proposed entry
    as its ``build_plan_snapshot`` is computed -- unlike reclaim's
    ``_DirSizeProgressAdapter`` proxy trick (needed because reclaim.py makes
    the slow calls internally, one per candidate directory, with no fixed
    total known upfront), this loop is directly under this function's own
    control and its total (``len(plan)``) is known before the loop even
    starts, so no proxy is needed here -- just call the callback once per
    iteration.

    Any proposed item under a hard-blocked protected path (see
    ``_is_protected_path``) is dropped here, before a ``QueueEntry`` is
    ever constructed for it -- refused structurally, not merely flagged.
    """
    run_args = SimpleNamespace(dir=dirs, go=False) if dirs else None
    result = sort_module.run(adapter, args=run_args)
    plan_items = result.get("plan", [])
    total = len(plan_items)
    config = config_module.load_config(adapter)
    new_entries = []
    for current, item in enumerate(plan_items, start=1):
        if not _is_protected_path(item["src"], adapter):
            location = _location_for_src(item["src"], config, adapter)
            new_entries.append(
                queue_module.QueueEntry(
                    action="move",
                    src=str(item["src"]),
                    dest=str(item["dest"]),
                    source="ui-plan-sort",
                    group_key=f"sort:{location}:{item['bucket']}",
                    plan_snapshot=queue_module.build_plan_snapshot(item["src"]),
                )
            )
        if progress_callback is not None:
            progress_callback(current, total)
    return queue_module.stage_entries(adapter, new_entries, queue_path)


class _DirSizeProgressAdapter:
    """Thin proxy around an ``OSAdapter`` that reports progress per call to
    ``dir_size_bytes`` -- the one call reclaim.py's category builders make
    per candidate directory, and the slow (``du -sk`` shell-out) part of
    ``/plan/reclaim``'s ~2-minute worst case.

    Every other attribute access (``find_dirs``, ``list_dir``,
    ``file_lock``, ...) is forwarded unchanged to the wrapped adapter via
    ``__getattr__``, so this can stand in for a real adapter anywhere
    reclaim.py or queue.py expects one -- it only ever intercepts
    ``dir_size_bytes``.

    There is no way to know the *total* number of ``dir_size_bytes`` calls
    a given ``reclaim.run()`` invocation will make ahead of time (candidate
    directories are discovered and sized category-by-category, not all
    upfront) -- so ``total`` is reported equal to ``current`` on every call,
    i.e. "N of at least N directories sized so far". This is real,
    monotonically-increasing progress driven entirely by actual
    ``dir_size_bytes`` calls, not a fixed/fake placeholder; it just means
    the fraction isn't meaningful as a percentage until the job finishes.
    """

    def __init__(self, wrapped, progress_callback):
        self._wrapped = wrapped
        self._progress_callback = progress_callback
        self._count = 0

    def __getattr__(self, name):
        return getattr(self._wrapped, name)

    def dir_size_bytes(self, path):
        size = self._wrapped.dir_size_bytes(path)
        self._count += 1
        self._progress_callback(self._count, self._count)
        return size


def _stage_reclaim_plan(
    adapter, queue_path, progress_callback=None, dirs: list[str] | None = None
) -> list[queue_module.QueueEntry]:
    """Dry-run cleanup_tools.commands.reclaim across its roots, convert every
    non-refused candidate into a QueueEntry "delete" proposal, and stage
    them.

    Master-path-refused candidates (see reclaim.py's
    ``MASTER_PATH_REFUSAL_REASON``) are never staged -- there is nothing to
    approve for a candidate reclaim.py itself refuses to ever delete, even
    under --go. Candidates under a hard-blocked protected path (see
    ``_is_protected_path``) are refused the same way, structurally, before
    a ``QueueEntry`` ever exists for them -- distinct from master_paths
    (user-configured, opt-in) but handled with the same "never even stage
    it" discipline.

    ``dirs``, if given (a subset of configured search_roots picked from
    the dashboard's location multi-select), scopes the scan to exactly
    those roots via ``args.dir``. ``dirs=None`` falls back to
    ``reclaim.run()``'s own default resolution.

    ``queue_path`` is taken explicitly (rather than resolved via
    ``_queue_path()`` internally) because this is called from a background
    job thread (see ``plan_reclaim`` below) that has no Flask request/app
    context to resolve ``current_app`` against -- the caller must resolve
    it while still inside the request and pass it in.

    If ``progress_callback`` is given, ``reclaim_module.run`` is called
    against a ``_DirSizeProgressAdapter`` wrapping ``adapter`` instead of
    ``adapter`` directly, so every ``dir_size_bytes`` call along the way
    reports real progress. ``stage_entries`` at the end always uses the
    real ``adapter`` (staging is fast queue-file I/O, not worth wrapping).
    """
    run_adapter = adapter
    if progress_callback is not None:
        run_adapter = _DirSizeProgressAdapter(adapter, progress_callback)

    run_args = SimpleNamespace(dir=dirs, go=False) if dirs else None
    result = reclaim_module.run(run_adapter, args=run_args)
    config = config_module.load_config(adapter)
    new_entries = []
    for category_name, category in result.get("categories", {}).items():
        for item in category.get("entries", []):
            if item.get("master_path_refused"):
                continue
            if _is_protected_path(item["path"], adapter):
                continue
            location = _location_for_src(item["path"], config, adapter)
            new_entries.append(
                queue_module.QueueEntry(
                    action="delete",
                    src=str(item["path"]),
                    dest="",
                    source="ui-plan-reclaim",
                    group_key=f"reclaim:{location}:{category_name}",
                    plan_snapshot=queue_module.build_plan_snapshot(item["path"]),
                )
            )
    return queue_module.stage_entries(adapter, new_entries, queue_path)


def _stage_corral_screenshots_plan(
    adapter, queue_path, progress_callback=None, dirs: list[str] | None = None
) -> list[queue_module.QueueEntry]:
    """Dry-run cleanup_tools.commands.corral_screenshots across its roots,
    convert its plan into QueueEntry "move" proposals, and stage them.

    Reuses corral_screenshots.run()'s own plan-building rather than walking
    the filesystem again here -- structurally identical to _stage_sort_plan
    above, including the same ``queue_path``/``progress_callback``/``dirs``
    treatment (see that function's docstring for the background-job/
    no-app-context reasoning, why no proxy-adapter trick is needed for
    progress here, and the location-subset scoping). The constructed
    ``args`` never sets ``set_default_location``, so this route can never
    trigger the OS-preference change -- only ever a dry-run plan.

    Any proposed item under a hard-blocked protected path (see
    ``_is_protected_path``) is dropped here, before a ``QueueEntry`` is
    ever constructed for it.
    """
    run_args = SimpleNamespace(dir=dirs, go=False) if dirs else None
    result = corral_screenshots_module.run(adapter, args=run_args)
    plan_items = result.get("plan", [])
    total = len(plan_items)
    config = config_module.load_config(adapter)
    new_entries = []
    for current, item in enumerate(plan_items, start=1):
        if not _is_protected_path(item["src"], adapter):
            location = _location_for_src(item["src"], config, adapter)
            new_entries.append(
                queue_module.QueueEntry(
                    action="move",
                    src=str(item["src"]),
                    dest=str(item["dest"]),
                    source="ui-plan-corral-screenshots",
                    group_key=f"corral-screenshots:{location}",
                    plan_snapshot=queue_module.build_plan_snapshot(item["src"]),
                )
            )
        if progress_callback is not None:
            progress_callback(current, total)
    return queue_module.stage_entries(adapter, new_entries, queue_path)


def _sort_job(adapter, queue_path, dirs, progress_callback) -> list[queue_module.QueueEntry]:
    """The ``target_fn`` run on ``/plan/sort``'s background job thread.

    Just ``_stage_sort_plan`` with progress reporting wired up, mirroring
    ``_reclaim_job`` exactly: a ``TimeoutError`` from the queue lock is
    re-raised carrying ``QUEUE_BUSY_MESSAGE``. Anything else (notably a
    ``FileNotFoundError`` for a missing Downloads dir, which the OLD
    synchronous route caught separately) is left to propagate as-is --
    ``jobs.py``'s generic ``except Exception`` in ``start_job`` records
    ``str(exc)`` verbatim as the job's terminal error either way, and
    ``FileNotFoundError``'s message ("sort target directory does not
    exist: <path>") is already friendly/non-leaky, so no special-casing is
    needed here the way it is for the queue lock's raw TimeoutError text.
    """
    try:
        return _stage_sort_plan(adapter, queue_path, progress_callback, dirs=dirs)
    except TimeoutError as exc:
        raise TimeoutError(QUEUE_BUSY_MESSAGE) from exc


@bp.route("/plan/sort")
def plan_sort():
    """Kick off sort planning/staging on a background job thread and return
    its ``job_id`` immediately (HTTP 200 JSON), instead of blocking the
    request on ``_stage_sort_plan``'s per-entry ``build_plan_snapshot``
    content-hashing loop -- mirrors ``plan_reclaim`` exactly (see that
    route's docstring for the full "why" of the background-job pattern;
    this route used to be the last of the three "Plan: X" actions still
    blocking synchronously).

    Accepts an optional repeated ``?dirs=`` query param -- the dashboard's
    location multi-select posting a subset of configured search_roots to
    scope this run to, instead of every configured location (see
    ``_stage_sort_plan``'s ``dirs`` param). Omitted entirely -> the usual
    full default set.
    """
    adapter = _adapter()
    queue_path = _queue_path()
    dirs = request.args.getlist("dirs") or None
    job_id = jobs.start_job(_sort_job, adapter, queue_path, dirs)
    return jsonify({"job_id": job_id})


def _cluster_documents_job(adapter, queue_path, dirs, threshold, progress_callback) -> list[queue_module.QueueEntry]:
    """The ``target_fn`` run on ``/plan/cluster-documents``'s background job thread.

    ``semantic.pipeline.run()``'s own return contract is a summary dict
    (``{"staged_entry_ids", "clusters_found", "files_scanned",
    "files_indexed"}``) -- deliberately not UI-route-shaped, since
    ``pipeline.py`` has its own tests/contract independent of this route.
    This function is the adaptation layer: it re-loads the actual staged
    ``QueueEntry`` objects by id, so the result matches ``_sort_job``/
    ``_reclaim_job``'s existing ``list[QueueEntry]`` shape exactly and the
    EXISTING generic ``GET /status/<job_id>`` route (``_serialize_staged_entries``)
    and ``plan-trigger.js`` work completely unchanged -- no new status route
    needed, unlike chat's turn results, which really do have a different
    shape.
    """
    try:
        result = semantic_pipeline.run(
            adapter, queue_path=queue_path, dirs=dirs, threshold=threshold, progress_callback=progress_callback
        )
    except TimeoutError as exc:
        raise TimeoutError(QUEUE_BUSY_MESSAGE) from exc

    staged_ids = set(result["staged_entry_ids"])
    if not staged_ids:
        return []
    resolved_queue_path = queue_path or queue_module.default_queue_path(adapter)
    entries = queue_module.load_queue(adapter, resolved_queue_path)
    return [e for e in entries if e.id in staged_ids]


@bp.route("/plan/cluster-documents")
def plan_cluster_documents():
    """Kick off local document-topic clustering on a background job thread
    and return its ``job_id`` immediately -- mirrors ``plan_sort`` exactly
    (see ``_cluster_documents_job``'s docstring for how its differently-
    shaped pipeline result gets adapted to fit the same generic
    ``/status/<job_id>`` contract every other "Plan: X" trigger already
    uses).

    Entirely local, zero network calls -- see
    ``.pHive/epics/document-topic-clustering/docs/design-discussion.md``.
    Same optional repeated ``?dirs=`` query param as every other "Plan: X"
    trigger; the similarity threshold comes from
    ``config.semantic_cluster_threshold`` (Settings > Semantic Clustering),
    not a query param.
    """
    adapter = _adapter()
    queue_path = _queue_path()
    dirs = request.args.getlist("dirs") or None
    config = config_module.load_config(adapter)
    job_id = jobs.start_job(_cluster_documents_job, adapter, queue_path, dirs, config.semantic_cluster_threshold)
    return jsonify({"job_id": job_id})


def _reclaim_job(adapter, queue_path, dirs, progress_callback) -> list[queue_module.QueueEntry]:
    """The ``target_fn`` run on ``/plan/reclaim``'s background job thread.

    Just ``_stage_reclaim_plan`` with progress reporting wired up, except a
    ``TimeoutError`` from the queue lock is re-raised carrying
    ``QUEUE_BUSY_MESSAGE`` -- mirroring every other route in this file that
    catches a bare ``TimeoutError`` and substitutes the friendly, non-leaky
    wording -- rather than whatever raw "Timed out after 5.0s waiting for
    lock on ..." message ``adapter.file_lock`` itself raises. ``jobs.py``'s
    generic ``except Exception`` in ``start_job`` then records this message
    verbatim as the job's terminal ``error``.
    """
    try:
        return _stage_reclaim_plan(adapter, queue_path, progress_callback, dirs=dirs)
    except TimeoutError as exc:
        raise TimeoutError(QUEUE_BUSY_MESSAGE) from exc


def _serialize_staged_entries(entries: list[queue_module.QueueEntry]) -> dict:
    """JSON-safe shape for a list of staged ``QueueEntry``s, for ``/status``'s
    ``result`` field -- the same list ``_stage_reclaim_plan`` would have
    returned synchronously, just turned into plain dicts via
    ``dataclasses.asdict`` so Flask's ``jsonify`` can serialize it.
    """
    return {
        "count": len(entries),
        "entries": [dataclasses.asdict(e) for e in entries],
    }


@bp.route("/plan/reclaim")
def plan_reclaim():
    """Kick off reclaim planning/staging on a background job thread and
    return its ``job_id`` immediately (HTTP 200 JSON), instead of blocking
    the request -- and the whole single-threaded dev server -- for as long
    as ``_stage_reclaim_plan`` takes (up to ~2 minutes on a real machine
    with a lot of ``node_modules`` directories; see
    ``adapters.base.OSAdapter.dir_size_bytes``).

    ``adapter``/``queue_path`` are resolved here, while still inside the
    request's app context, and passed explicitly into the job -- the
    background thread has no request/app context of its own, so it can't
    call ``_adapter()``/``_queue_path()`` (both read ``current_app``).

    Callers poll ``GET /status/<job_id>`` (below) for progress/outcome.
    ``plan_sort``/``plan_corral_screenshots`` follow this exact same
    async-job pattern -- none of the three ``/plan/*`` routes redirect
    synchronously or block until the plan finishes.

    Accepts the same optional repeated ``?dirs=`` query param as
    ``plan_sort`` -- see that route's docstring.
    """
    adapter = _adapter()
    queue_path = _queue_path()
    dirs = request.args.getlist("dirs") or None
    job_id = jobs.start_job(_reclaim_job, adapter, queue_path, dirs)
    return jsonify({"job_id": job_id})


@bp.route("/status/<job_id>")
def job_status(job_id):
    """Poll a background job's current state.

    Unknown ``job_id`` -> HTTP 404 (never a 500, never a misleading
    running/done payload) -- a job that never existed, or that existed in a
    now-restarted process (this registry is in-memory only, see jobs.py),
    looks identical from here: "no such job".
    """
    job = jobs.get_job(job_id)
    if job is None:
        abort(404, description=f"No such job: {job_id!r}")

    payload: dict = {"status": job.status, "current": job.current, "total": job.total}
    if job.status == jobs.STATUS_DONE:
        payload["result"] = _serialize_staged_entries(job.result)
    elif job.status == jobs.STATUS_ERROR:
        payload["error"] = job.error
    return jsonify(payload)


# ---------------------------------------------------------------------------
# Chat: an in-app conversation with an agent that can read real queue/
# config/filesystem state and propose moves -- never execute anything
# itself (see chat/engine.py and chat/tools.py's module docstrings, and
# .pHive/epics/chat-agent-plan-builder/docs/design-discussion.md). A chat
# turn reuses the exact same background-job/polling pattern every other
# long-running action in this app already uses (jobs.start_job,
# wants_partial=True for the growing streamed-text case) -- see
# JobState.partial's docstring -- rather than a new transport.
# ---------------------------------------------------------------------------


@bp.route("/chat")
def chat_view():
    return render_template("chat.html")


@bp.route("/chat/new", methods=["POST"])
def chat_new():
    """Start a fresh, empty conversation and return its id.

    Conversation state is in-memory only (see chat/state.py's module
    docstring) -- nothing here touches queue.yaml or config.yaml. Also
    returns the conversation's turn cap (``config.chat_turn_cap``) so the
    client can render a running "Turn N of Cap" usage indicator without a
    separate round trip -- see the chat-cost-control-and-settings design
    discussion §2.6.
    """
    conversation_id = chat_state.create_conversation()
    config = config_module.load_config(_adapter())
    return jsonify({"conversation_id": conversation_id, "turn_cap": config.chat_turn_cap})


def _chat_turn_job(
    adapter, conversation_id: str, user_message: str, chat_model: str, progress_callback, partial_callback
) -> dict:
    """The ``target_fn`` run on a chat turn's background job thread.

    Resolves the conversation's prior history, runs the engine's
    tool-calling loop for exactly one turn (see chat/engine.py's ``run_turn``
    docstring for what "one turn" means), then appends both the user's
    message and the assistant's final response to that conversation's
    history -- so the NEXT turn (a separate job, separate call to this
    function) sees the full prior exchange.

    ``chat_model`` (resolved from ``config.chat_model`` by the caller,
    while still inside the request -- this function runs on a background
    thread with no Flask request context to resolve it against) is passed
    through to ``run_turn`` unchanged.

    Deliberately does NOT catch/translate any exception (credential
    resolution failure, a raw ``anthropic.APIError``, ...) -- jobs.py's
    ``start_job`` already catches anything a target_fn raises and records
    it as a terminal job error; translating here too would just be
    redundant, less-informative error handling (mirrors ``run_turn``'s own
    documented reasoning for the same choice).
    """
    conv = chat_state.get_conversation(conversation_id)
    if conv is None:
        raise ValueError(f"unknown conversation: {conversation_id!r}")

    history = [{"role": m.role, "text": m.text} for m in conv.messages]
    client = get_raw_client()
    result = chat_engine.run_turn(
        client,
        adapter,
        history,
        user_message,
        conversation_id=conversation_id,
        model=chat_model,
        partial_callback=partial_callback,
    )

    chat_state.append_message(conversation_id, "user", user_message)
    chat_state.append_message(conversation_id, "assistant", result["text"])
    return result


CHAT_TURN_CAP_MESSAGE = "Conversation limit reached -- start a new one."


@bp.route("/chat/<conversation_id>/message", methods=["POST"])
def chat_message(conversation_id):
    """Send one message in an existing conversation; kicks off the turn as
    a background job (streamed via ``partial``) and returns its ``job_id``
    immediately, mirroring ``plan_sort``/``plan_reclaim``'s async pattern.

    Unknown ``conversation_id`` -> 404, checked here (before a job is even
    started) rather than surfacing as a job error -- an unknown
    conversation is a request-shape problem, not something that happened
    while doing real work.

    The conversation's turn cap (``config.chat_turn_cap``) is ALSO checked
    here, before ``jobs.start_job`` is ever called -- a pre-call gate,
    mirroring ``ai/wiring.py``'s own "slice before calling, never
    call-then-discard" cap discipline applied to a new surface. A
    conversation already at its cap gets a 409 with an explicit message
    instead of a new turn silently starting; the turn already in flight
    (if any) when a cap is reached is never touched by this check, since it
    only runs before a NEW turn's job starts.
    """
    data = request.get_json(silent=True) or {}
    user_message = (data.get("message") or "").strip()
    if not user_message:
        return jsonify({"error": "message is required"}), 400

    conv = chat_state.get_conversation(conversation_id)
    if conv is None:
        abort(404, description=f"No such conversation: {conversation_id!r}")

    config = config_module.load_config(_adapter())
    if chat_state.turn_count(conv) >= config.chat_turn_cap:
        return jsonify({"error": CHAT_TURN_CAP_MESSAGE}), 409

    adapter = _adapter()
    job_id = jobs.start_job(
        _chat_turn_job, adapter, conversation_id, user_message, config.chat_model, wants_partial=True
    )
    return jsonify({"job_id": job_id})


@bp.route("/chat/status/<job_id>")
def chat_job_status(job_id):
    """Poll a chat turn's background job -- a dedicated route rather than
    reusing ``GET /status/<job_id>`` above, because that route's "done"
    handling always calls ``_serialize_staged_entries`` (a list of
    ``QueueEntry`` objects); a chat job's result is
    ``{"text", "staged_entry_ids"}``, a different shape entirely. Same
    unknown-``job_id`` -> 404 contract as the other route.
    """
    job = jobs.get_job(job_id)
    if job is None:
        abort(404, description=f"No such job: {job_id!r}")

    payload: dict = {"status": job.status, "partial": job.partial}
    if job.status == jobs.STATUS_DONE:
        payload["result"] = job.result
    elif job.status == jobs.STATUS_ERROR:
        payload["error"] = job.error
    return jsonify(payload)


@bp.route("/healthz")
def healthz():
    """A deliberately cheap liveness check -- NOT a repurposing of ``/``
    (which does real queue-loading/grouping work via ``_load_entries``).

    Makes ZERO queue or filesystem I/O calls: no ``queue_module.load_queue``
    (or anything that reaches it, like ``_load_entries``/``_adapter``'s
    config lookups), just a static JSON body. This exists so a native
    desktop-app shell (or anything else) can cheaply poll "is the server up
    and responsive" without that poll itself being slow enough to look like
    the thing it's trying to detect.
    """
    return jsonify({"status": "ok"}), 200


def _corral_screenshots_job(
    adapter, queue_path, dirs, progress_callback
) -> list[queue_module.QueueEntry]:
    """The ``target_fn`` run on ``/plan/corral-screenshots``'s background job
    thread. Structurally identical to ``_sort_job`` above.
    """
    try:
        return _stage_corral_screenshots_plan(adapter, queue_path, progress_callback, dirs=dirs)
    except TimeoutError as exc:
        raise TimeoutError(QUEUE_BUSY_MESSAGE) from exc


@bp.route("/plan/corral-screenshots")
def plan_corral_screenshots():
    """Kick off corral-screenshots planning/staging on a background job
    thread and return its ``job_id`` immediately -- mirrors ``plan_sort``/
    ``plan_reclaim`` exactly, including the same optional repeated
    ``?dirs=`` query param.
    """
    adapter = _adapter()
    queue_path = _queue_path()
    dirs = request.args.getlist("dirs") or None
    job_id = jobs.start_job(_corral_screenshots_job, adapter, queue_path, dirs)
    return jsonify({"job_id": job_id})


@bp.route("/propose-ai", methods=["POST"])
def propose_ai():
    """Ask the configured AI provider to bucket up to ``DEFAULT_AI_CAP`` of
    the files sort.py's dry-run plan would otherwise leave in ``'other'``,
    and stage every successful proposal into the SAME approval queue manual
    entries use (see ``ai.wiring.propose_for_other_bucket``).

    ``get_provider()`` raising ``CredentialsError`` (no API key configured
    anywhere it looks) is a routine, expected condition for a tool that
    doesn't require AI to be configured at all -- handled the same way
    ``plan_sort``/``plan_reclaim`` handle their own routine failure modes
    above: a redirect back to the dashboard carrying a ``plan_error``
    message, never a 500.
    """
    adapter = _adapter()
    try:
        provider = get_provider()
    except CredentialsError as exc:
        return redirect(url_for("ui.dashboard", plan_error=str(exc)))

    config = config_module.load_config(adapter)
    try:
        result = propose_for_other_bucket(
            adapter, config, provider, DEFAULT_AI_CAP, queue_path=_queue_path()
        )
    except FileNotFoundError as exc:
        return redirect(url_for("ui.dashboard", plan_error=str(exc)))
    except TimeoutError:
        return redirect(url_for("ui.dashboard", plan_error=QUEUE_BUSY_MESSAGE))

    staged = len(result["proposed"])
    failed = len(result["failures"])
    if failed:
        return redirect(url_for("ui.dashboard", staged=staged, plan_error=f"{failed} AI proposal(s) failed"))
    return redirect(url_for("ui.dashboard", staged=staged))


# ---------------------------------------------------------------------------
# Review queue.
# ---------------------------------------------------------------------------

# Pagination defaults for /queue. DEFAULT_PER_PAGE is used whenever
# ``per_page`` is absent/invalid; MAX_PER_PAGE caps a caller-supplied value
# so a malicious or accidental ``?per_page=999999999`` can't force the
# route to render (or even just slice) an enormous list in one request.
DEFAULT_PER_PAGE = 25
MAX_PER_PAGE = 200


def _paginate(entries: list, page_arg, per_page_arg) -> tuple[list, dict]:
    """Slice ``entries`` for the requested page, clamping out-of-range input.

    ``page`` and ``per_page`` are parsed defensively -- a missing, blank, or
    non-integer value falls back to the default rather than raising (so a
    malformed query string degrades to "page 1 at the default size" instead
    of a 500). ``per_page`` is clamped to ``[1, MAX_PER_PAGE]`` and ``page``
    is clamped so it never goes below 1, and never above the last real page
    (once ``total`` is known) -- a page number past the end returns the
    *last* page's worth of entries when the queue is non-empty, and an empty
    slice when the queue is empty entirely, rather than 500ing or silently
    returning page 1's entries for a request that asked for page 40.

    Returns ``(page_entries, pagination_info)`` where ``pagination_info``
    carries everything the template needs to render prev/next controls and
    a "page X of Y" indicator.
    """

    def _to_int(value, default):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return default
        return value

    per_page = _to_int(per_page_arg, DEFAULT_PER_PAGE)
    if per_page < 1:
        per_page = DEFAULT_PER_PAGE
    per_page = min(per_page, MAX_PER_PAGE)

    total = len(entries)
    total_pages = max(1, -(-total // per_page))  # ceil division

    page = _to_int(page_arg, 1)
    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages

    start = (page - 1) * per_page
    end = start + per_page
    page_entries = entries[start:end]

    info = {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_page": page - 1,
        "next_page": page + 1,
        "start_index": start + 1 if page_entries else 0,
        "end_index": start + len(page_entries),
    }
    return page_entries, info


_CLUSTER_SNIPPET_MAX_CHARS = 120


def _cluster_snippet(entry: queue_module.QueueEntry) -> str | None:
    """Short "why was this grouped here" excerpt for a ``source="local:cluster"``
    entry -- looked up from the semantic index by content hash (never
    re-extracted; see ``semantic.index.get_text``), capped to
    ``_CLUSTER_SNIPPET_MAX_CHARS`` in every render path -- never the full
    document. ``None`` for a non-cluster-sourced entry, or one with no
    indexed text (e.g. the index was cleared since staging).
    """
    if entry.source != "local:cluster":
        return None
    if not isinstance(entry.plan_snapshot, dict):
        return None
    content_hash = entry.plan_snapshot.get("content_hash")
    if not content_hash:
        return None
    text = semantic_index.get_text(_adapter(), content_hash)
    if not text:
        return None
    snippet = " ".join(text.split())  # collapse all whitespace/newlines
    if len(snippet) > _CLUSTER_SNIPPET_MAX_CHARS:
        return snippet[:_CLUSTER_SNIPPET_MAX_CHARS].rstrip() + "…"
    return snippet


@bp.route("/queue")
def queue_view():
    entries = _pending_entries()
    page_entries, pagination = _paginate(
        entries, request.args.get("page"), request.args.get("per_page")
    )
    # Distinct, truthy group_keys among ALL pending entries (not just the
    # current page), each with its real pending count -- feeds the
    # "bulk-approve/reject this whole group" quick-action bar, which must
    # be able to target entries regardless of which page they currently
    # sit on. The count is what lets that bar's confirm-before-firing
    # dialog (see static/confirm-actions.js) name a real number instead of
    # a vague "are you sure?" -- a direct requirement from the UI design
    # review. Entries with no group_key (the "ungrouped" display fallback)
    # are deliberately excluded here: there is no real group_key value
    # that would round-trip back to matching them via the exact-match bulk
    # routes, so offering a fake "ungrouped" button would silently do
    # nothing when clicked.
    group_counts: dict[str, int] = {}
    for e in entries:
        if e.group_key:
            group_counts[e.group_key] = group_counts.get(e.group_key, 0) + 1
    group_keys = [
        {"group_key": gk, "count": group_counts[gk]} for gk in sorted(group_counts)
    ]
    return render_template(
        "queue.html",
        entries=page_entries,
        total_pending=len(entries),
        group_keys=group_keys,
        is_image_entry=is_image_entry,
        is_ai_source=_is_ai_source,
        cluster_snippet=_cluster_snippet,
        pagination=pagination,
    )


# ---------------------------------------------------------------------------
# History: a reverse-chronological feed built entirely from existing
# QueueEntry.status_history data (no new storage). The single safety-
# critical property here -- self-caught during this epic's design review,
# not by an earlier grill pass -- is that Undo must never be offered where
# it would lie: queue.undo() only ever reverts the queue's *status* field,
# it never touches the filesystem, so a row for an entry whose --from-queue
# execution already ran (entry.executed_at set -- the move/delete actually
# happened on disk) must not show a plain, working-looking Undo button.
# ---------------------------------------------------------------------------


def _history_rows(entries: list[queue_module.QueueEntry]) -> list[dict]:
    """Flatten every entry's ``status_history`` into one reverse-chronological
    row list -- what happened, when, to what, via which rule/pipeline
    (``group_key``).

    ``queue.undo()`` only ever reverts the single most recent transition
    (it pops the *last* history record), so only the LATEST row for a
    given entry can accurately offer a real Undo action -- an older row
    would visually claim to undo its own transition while actually
    undoing whatever happened *after* it. Every row still carries
    ``is_latest`` so the template can gate on it, but ``can_undo`` folds
    in every other precondition (latest, not yet executed on disk, and
    something real to revert to -- mirroring ``queue.undo()``'s own
    ``len(status_history) <= 1`` guard) so the template only ever needs
    one boolean, never re-deriving this safety property itself.
    """
    rows: list[dict] = []
    for entry in entries:
        history = entry.status_history
        last_index = len(history) - 1
        for index, record in enumerate(history):
            is_latest = index == last_index
            rows.append(
                {
                    "entry_id": entry.id,
                    "action": entry.action,
                    "src": entry.src,
                    "dest": entry.dest,
                    "group_key": entry.group_key,
                    "source": entry.source,
                    "status": record.get("status"),
                    "timestamp": record.get("timestamp"),
                    "edit": record.get("edit"),
                    "is_latest": is_latest,
                    "executed_at": entry.executed_at,
                    "can_undo": is_latest and entry.executed_at is None and len(history) > 1,
                }
            )
    rows.sort(key=lambda r: r["timestamp"] or "", reverse=True)
    return rows


@bp.route("/history")
def history_view():
    rows = _history_rows(_load_entries())
    page_rows, pagination = _paginate(
        rows, request.args.get("page"), request.args.get("per_page")
    )
    return render_template(
        "history.html",
        rows=page_rows,
        total_rows=len(rows),
        pagination=pagination,
    )


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


# ---------------------------------------------------------------------------
# Bulk actions. Both routes accept EITHER a ``group_key`` (the primary use
# case -- "approve everything in the screenshots group") OR an explicit list
# of entry ids (``entry_ids``, repeated form/query keys or a JSON body list),
# scoped to pending entries only:
#
# - group_key scoping is an EXACT match against ``entry.group_key`` -- never
#   a substring/prefix match -- so a group_key of "sort:screenshots" cannot
#   accidentally also sweep up "sort:screenshots_old" or anything else that
#   merely contains the string.
# - id-list scoping only ever touches ids that are both present in the
#   supplied list AND currently pending; an id for an already-approved/
#   rejected entry, or an id not in the queue at all, is silently ignored
#   rather than erroring, since the point is "act on whichever of these are
#   still actionable", not "fail the whole batch over one stale id".
#
# Neither route ever reaches entries outside the specified scope: both
# build their target id set from ``_pending_entries()`` first, then call
# ``queue.set_status`` only for ids in that intersection.
# ---------------------------------------------------------------------------


def _bulk_target_ids(
    candidates: list, group_key: str | None, entry_ids: list[str] | None
) -> list[str]:
    """Resolve the ids among ``candidates`` a bulk action should touch.

    ``group_key`` (if given, non-blank) takes priority. A ``group_key``
    ending in ``":"`` is a **prefix** match -- every candidate whose
    ``group_key`` starts with it (e.g. ``"sort:downloads:"`` matches
    ``"sort:downloads:photos"``) -- so a tree-branch identifier can target
    every leaf beneath it. Because a real stored ``group_key`` value never
    itself ends in ``":"``, this is unambiguous and can never accidentally
    prefix-match a sibling branch (``"sort:downloads:"`` never matches
    ``"sort:downloads2:photos"`` -- the trailing ``":"`` is the segment
    boundary). Anything NOT ending in ``":"`` is matched exactly, byte-for-
    byte identical to the original exact-match-only behavior -- entries
    with no group_key never match a group_key request, even "ungrouped"
    (the display fallback used only in templates, never a real stored
    value). If ``group_key`` isn't given, ``entry_ids`` (if given) is
    intersected against ``candidates``' ids so only ids that are both
    requested and present in ``candidates`` are touched. Returns an empty
    list if neither is given.
    """
    if group_key:
        if group_key.endswith(":"):
            return [e.id for e in candidates if e.group_key and e.group_key.startswith(group_key)]
        return [e.id for e in candidates if e.group_key == group_key]
    if entry_ids:
        candidate_ids = {e.id for e in candidates}
        # Preserve request order, dedup, and drop anything not a candidate.
        seen = set()
        result = []
        for eid in entry_ids:
            if eid in candidate_ids and eid not in seen:
                seen.add(eid)
                result.append(eid)
        return result
    return []


def _extract_bulk_request() -> tuple[str | None, list[str]]:
    """Pull ``group_key``/``entry_ids`` out of a bulk-action POST.

    Accepts either an HTML form POST (``group_key`` field, and/or
    ``entry_ids`` as a repeated form field) or a JSON body with the same
    two keys -- so this is usable both from the bulk-action-bar form in
    queue.html and from a future/scripted JSON caller.

    ``entry_ids`` is parsed defensively, the same way ``_paginate``'s
    ``_to_int`` degrades malformed pagination input rather than raising: a
    bare scalar (e.g. ``{"entry_ids": 12345}``) is neither a list nor a
    string, so it can't be iterated into individual ids and is treated as
    "no ids given" instead of blowing up ``list()`` with a 500.
    """
    if request.is_json:
        data = request.get_json(silent=True) or {}
        group_key = data.get("group_key") or None
        entry_ids = data.get("entry_ids") or []
        if isinstance(entry_ids, str):
            entry_ids = [entry_ids]
        elif not isinstance(entry_ids, list):
            entry_ids = []
        return group_key, list(entry_ids)

    group_key = request.form.get("group_key") or None
    entry_ids = request.form.getlist("entry_ids")
    return group_key, entry_ids


def _bulk_transition(new_status: str):
    group_key, entry_ids = _extract_bulk_request()
    pending = _pending_entries()
    target_ids = _bulk_target_ids(pending, group_key, entry_ids)

    updated = []
    try:
        for entry_id in target_ids:
            updated.append(queue_module.set_status(_adapter(), entry_id, new_status, _queue_path()))
    except TimeoutError:
        abort(503, description=QUEUE_BUSY_MESSAGE)

    if request.is_json:
        return {
            "status": new_status,
            "updated_ids": [e.id for e in updated],
            "count": len(updated),
        }
    return redirect(url_for("ui.queue_view"))


@bp.route("/queue/bulk-approve", methods=["POST"])
def bulk_approve():
    return _bulk_transition("approved")


@bp.route("/queue/bulk-reject", methods=["POST"])
def bulk_reject():
    return _bulk_transition("rejected")


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


@bp.route("/queue/bulk-undo", methods=["POST"])
def bulk_undo():
    """Undo every targeted entry back to its previous status.

    Unlike bulk-approve/bulk-reject (which only ever touch *pending*
    entries -- there's nothing to approve/reject otherwise), bulk-undo's
    targets are resolved against the WHOLE queue: an approved or rejected
    entry is exactly what undo is for. ``group_key``/``entry_ids``
    resolution (including the ":"-prefix tree-branch match) is the same
    ``_bulk_target_ids`` every other bulk route uses.

    Mirrors ``undo_entry``'s per-id semantics but batched: an id with
    nothing to revert to (``queue.undo``'s ``ValueError``) is skipped
    rather than failing the whole batch -- same "act on whichever of these
    are still actionable" philosophy as id-list scoping in
    ``_bulk_target_ids``.
    """
    group_key, entry_ids = _extract_bulk_request()
    target_ids = _bulk_target_ids(_load_entries(), group_key, entry_ids)

    updated = []
    skipped_ids = []
    try:
        for entry_id in target_ids:
            try:
                updated.append(queue_module.undo(_adapter(), entry_id, _queue_path()))
            except ValueError:
                skipped_ids.append(entry_id)
    except TimeoutError:
        abort(503, description=QUEUE_BUSY_MESSAGE)

    if request.is_json:
        return {
            "updated_ids": [e.id for e in updated],
            "count": len(updated),
            "skipped_ids": skipped_ids,
        }
    return redirect(url_for("ui.queue_view"))


@bp.route("/queue/<entry_id>/edit", methods=["POST"])
def edit_entry(entry_id):
    """Change a pending entry's proposed ``dest``/``group_key`` before approval.

    Accepts either an HTML form POST (``dest``, ``group_key`` fields) or a
    JSON body with the same two keys, mirroring ``_extract_bulk_request``'s
    dual-input convention. ``group_key`` is optional (omitted/blank clears
    it, matching ``QueueEntry.group_key``'s ``None``-able type) -- ``dest``
    is required, since a move/delete proposal with no destination isn't a
    coherent edit.
    """
    if request.is_json:
        data = request.get_json(silent=True) or {}
        new_dest = data.get("dest")
        new_group_key = data.get("group_key") or None
    else:
        new_dest = request.form.get("dest")
        new_group_key = request.form.get("group_key") or None

    if not new_dest:
        return "dest is required", 400

    try:
        queue_module.edit_entry(_adapter(), entry_id, new_dest, new_group_key, _queue_path())
    except ValueError as exc:
        message = str(exc)
        if "not 'pending'" in message:
            return message, 400
        abort(404, description=message)
    except TimeoutError:
        abort(503, description=QUEUE_BUSY_MESSAGE)
    return redirect(url_for("ui.queue_view"))


# ---------------------------------------------------------------------------
# Settings. Currently just the desktop-app icon picker. Persisted via
# config.py's ``icon_choice`` field (``~/.config/cleanup-tools/config.yaml``)
# rather than the theme picker's plain browser ``localStorage`` -- the
# Tauri/Rust shell also needs to read this choice (to re-apply it as the
# live Dock/taskbar icon at every launch, and to swap the installed .app
# bundle's Finder icon on macOS), which localStorage can't cross into.
# ``ICON_CHOICES`` here is the single place this route module treats as
# authoritative for the allowed set; ``src-tauri/src/lib.rs``'s own
# ``ICON_CHOICES`` mirrors the same four slugs by hand -- same
# cross-language "must match" convention as that file's ``SIDECAR_PORT``.
# ---------------------------------------------------------------------------

ICON_CHOICES = {
    "broom-folder": "Broom & Folder",
    "broom-sparkle": "Broom & Sparkle",
    "tidy-folder-check": "Tidy Folder",
    "recycle-folder": "Recycle Folder",
}
DEFAULT_ICON_CHOICE = "broom-folder"

# ---------------------------------------------------------------------------
# UI mode: an interaction-density/copy-style preference, orthogonal to the
# Ledger/Sonar/Tide color theme (see config.Config.ui_mode's docstring for
# why this is config-persisted rather than the theme's localStorage). This
# module owns the allowed set and its labels/descriptions, same convention
# as ICON_CHOICES above. Applied globally via a data-ui-mode attribute on
# <html> (see app.py's context processor and base.html) -- CSS attribute
# selectors handle Console mode's density entirely; Guided mode's
# plain-language copy needs real template branching at the specific spots
# where the copy differs (queue.html, dashboard.html), gated on this same
# value.
# ---------------------------------------------------------------------------

UI_MODES = {
    "standard": {
        "label": "Standard",
        "description": "The default: dense, direct, technical labels visible at a glance.",
    },
    "guided": {
        "label": "Guided",
        "description": "Plain-language confirmations, bucket icons, technical details tucked behind a toggle. Good for occasional use.",
    },
    "console": {
        "label": "Console",
        "description": "Denser rows for triaging hundreds of entries in one sitting. Same data, tighter layout.",
    },
}
DEFAULT_UI_MODE = "standard"


def _user_bucket_rules(config: config_module.Config) -> list[config_module.BucketRule]:
    """The user-added bucket rules within ``config.bucket_rules`` -- everything
    BEFORE the trailing ``DEFAULT_BUCKET_RULES`` block ``load_config`` always
    appends (see that function's docstring). Mirrors its own trailing-match
    slicing exactly, so the CRUD routes below only ever add/edit/remove/
    reorder user rules -- never the defaults, which stay a fixed, always-
    applied-last fallback. Editing a default in place here would also break
    ``load_config``'s round-trip detection (it recognizes "ends with the
    real default block" by exact list equality), so keeping this boundary
    exact is a correctness requirement, not just a UI nicety.
    """
    n = len(config_module.DEFAULT_BUCKET_RULES)
    if len(config.bucket_rules) >= n and config.bucket_rules[len(config.bucket_rules) - n :] == config_module.DEFAULT_BUCKET_RULES:
        return list(config.bucket_rules[: len(config.bucket_rules) - n])
    return list(config.bucket_rules)


def _save_user_bucket_rules(adapter, config: config_module.Config, user_rules: list[config_module.BucketRule]) -> None:
    """Persist ``user_rules`` (see ``_user_bucket_rules``) followed by the
    fixed defaults -- the exact shape ``load_config`` expects to read back
    and correctly re-split on the next load.
    """
    new_config = dataclasses.replace(
        config, bucket_rules=user_rules + config_module.DEFAULT_BUCKET_RULES
    )
    config_module.save_config(adapter, new_config)


def _ai_credentials_status() -> dict:
    """Whether an Anthropic API key is configured, and from where -- for the
    AI Provider settings pane. NEVER reads or exposes the key value itself,
    only which of ``ai/__init__.py``'s two resolution sources (env var,
    else the credentials file) is actually active, mirroring that module's
    own precedence without duplicating its file-reading/permission logic.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return {"configured": True, "source": "ANTHROPIC_API_KEY environment variable"}
    path = default_credentials_path()
    if path.exists():
        return {"configured": True, "source": str(path)}
    return {"configured": False, "source": None}


@bp.route("/settings")
def settings():
    config = config_module.load_config(_adapter())
    return render_template(
        "settings.html",
        icon_choices=ICON_CHOICES,
        current_icon_choice=config.icon_choice,
        ui_modes=UI_MODES,
        current_ui_mode=config.ui_mode,
        user_bucket_rules=_user_bucket_rules(config),
        default_bucket_rules=config_module.DEFAULT_BUCKET_RULES,
        search_roots=config.search_roots,
        master_paths=config.master_paths,
        ai_status=_ai_credentials_status(),
        current_chat_turn_cap=config.chat_turn_cap,
        current_chat_model=config.chat_model,
        current_semantic_cluster_threshold=config.semantic_cluster_threshold,
        # Advanced pane: read-only, formatted JSON of the SAME dict
        # config.py's own save_config() persists (config_to_dict is the
        # single source of truth both go through) -- so this can never
        # drift from what a change made via any other pane actually wrote.
        # Read-only by design this story; validated in-place editing of
        # arbitrary pasted JSON is real, separable risk, deferred on
        # purpose (see the design discussion's open question 1).
        config_json=json.dumps(config_module.config_to_dict(config), indent=2, sort_keys=False),
    )


@bp.route("/settings/icon", methods=["POST"])
def set_icon_choice():
    """Persist the chosen icon slug to ``config.yaml``.

    JSON in/out (not a form POST) so the client can apply the live
    Tauri-side icon swap in the same interaction without a page reload --
    see ``static/settings.js``. Runs identically whether or not a Tauri
    shell is actually wrapping this page (the plain-browser ``cleanup
    approve`` case): the config write always happens here; the live
    in-process icon swap is a separate step the client only attempts when
    ``window.__TAURI__`` is present.
    """
    data = request.get_json(silent=True) or {}
    choice = data.get("choice")
    if choice not in ICON_CHOICES:
        return jsonify({"error": f"unknown icon choice: {choice!r}"}), 400

    adapter = _adapter()
    # dataclasses.replace, not an in-place `config.icon_choice = choice`
    # mutation: when no config.yaml exists yet, load_config() returns the
    # module-level DEFAULT_CONFIG singleton BY REFERENCE (see config.py) --
    # mutating it in place would corrupt that shared "constant" for every
    # other request/test in the process, not just this one.
    config = dataclasses.replace(config_module.load_config(adapter), icon_choice=choice)
    config_module.save_config(adapter, config)
    return jsonify({"choice": choice})


@bp.route("/settings/ui-mode", methods=["POST"])
def set_ui_mode():
    """Persist the chosen UI mode (Standard/Guided/Console) to ``config.yaml``.

    Plain form POST + redirect, matching every other Settings CRUD route
    in this file (unlike ``set_icon_choice``, there's no live in-process
    side effect to apply beyond the config write -- the mode takes effect
    on next render via app.py's context processor, no Tauri-side call
    needed).
    """
    mode = request.form.get("ui_mode")
    if mode not in UI_MODES:
        return f"unknown UI mode: {mode!r}", 400

    adapter = _adapter()
    config = dataclasses.replace(config_module.load_config(adapter), ui_mode=mode)
    config_module.save_config(adapter, config)
    return redirect(url_for("ui.settings") + "#general")


@bp.route("/settings/chat-options", methods=["POST"])
def set_chat_options():
    """Persist ``chat_turn_cap``/``chat_model`` to ``config.yaml`` -- the
    two chat cost-control knobs surfaced on the AI Provider pane (see
    config.Config's own docstring on these fields). Plain form POST +
    redirect, same convention as ``set_ui_mode``.

    ``chat_turn_cap`` must be a positive integer -- a cap of 0 or less
    would make every conversation unusable, and a non-numeric value can't
    be compared against a turn count at all; both are rejected with 400
    rather than silently coerced or persisted. ``chat_model`` just needs to
    be non-blank -- unlike ``ui_mode``/``icon_choice``, there's no fixed
    enum to validate against here (see the design discussion §2.6: BYOK
    already means the user can point this at any model string their key
    can call, not just a hardcoded shortlist).
    """
    raw_cap = request.form.get("chat_turn_cap", "")
    try:
        chat_turn_cap = int(raw_cap)
    except ValueError:
        return f"chat_turn_cap must be a whole number, got {raw_cap!r}", 400
    if chat_turn_cap < 1:
        return f"chat_turn_cap must be at least 1, got {chat_turn_cap}", 400

    chat_model = (request.form.get("chat_model") or "").strip()
    if not chat_model:
        return "chat_model must not be blank", 400

    adapter = _adapter()
    config = dataclasses.replace(
        config_module.load_config(adapter), chat_turn_cap=chat_turn_cap, chat_model=chat_model
    )
    config_module.save_config(adapter, config)
    return redirect(url_for("ui.settings") + "#ai-provider")


@bp.route("/settings/semantic-options", methods=["POST"])
def set_semantic_options():
    """Persist ``semantic_cluster_threshold`` to ``config.yaml`` -- the
    cosine-similarity threshold ``semantic/cluster.py``'s threshold+
    union-find grouping uses (see config.Config's own docstring on this
    field). Plain form POST + redirect, same convention as
    ``set_chat_options``.

    Must be a real number in ``[0.0, 1.0]`` -- cosine similarity's valid
    range -- rejected with 400 (and not persisted) otherwise, mirroring
    ``set_chat_options``' validation discipline.
    """
    raw_threshold = request.form.get("semantic_cluster_threshold", "")
    try:
        threshold = float(raw_threshold)
    except ValueError:
        return f"semantic_cluster_threshold must be a number, got {raw_threshold!r}", 400
    if not (0.0 <= threshold <= 1.0):
        return f"semantic_cluster_threshold must be between 0.0 and 1.0, got {threshold}", 400

    adapter = _adapter()
    config = dataclasses.replace(config_module.load_config(adapter), semantic_cluster_threshold=threshold)
    config_module.save_config(adapter, config)
    return redirect(url_for("ui.settings") + "#semantic-clustering")


# ---------------------------------------------------------------------------
# Bucket Rules CRUD (settings pane). Index-addressed within the USER rules
# sublist only (see _user_bucket_rules) -- the defaults are a fixed,
# read-only, always-applied-last fallback, never edited here. This is a
# single-user local config file with no concurrent-writer story the way the
# approval queue has, so plain index addressing (rather than a generated id
# per rule) is enough -- consistent with there being no id scheme for
# bucket rules anywhere else in this codebase.
# ---------------------------------------------------------------------------


def _parse_extensions(raw: str) -> frozenset[str]:
    """Comma/whitespace-separated extensions -> a lowercased frozenset,
    stripping any leading "." a user might type out of habit (e.g. ".jpg").
    """
    parts = [p.strip().lower().lstrip(".") for p in raw.replace(",", " ").split()]
    return frozenset(p for p in parts if p)


@bp.route("/settings/bucket-rules/add", methods=["POST"])
def add_bucket_rule():
    extensions = _parse_extensions(request.form.get("extensions", ""))
    bucket = (request.form.get("bucket") or "").strip()
    filename_pattern = (request.form.get("filename_pattern") or "").strip() or None

    if not extensions or not bucket:
        return "extensions and bucket are required", 400

    adapter = _adapter()
    config = config_module.load_config(adapter)
    user_rules = _user_bucket_rules(config)
    user_rules.append(
        config_module.BucketRule(extensions=extensions, bucket=bucket, filename_pattern=filename_pattern)
    )
    _save_user_bucket_rules(adapter, config, user_rules)
    return redirect(url_for("ui.settings") + "#bucket-rules")


@bp.route("/settings/bucket-rules/<int:index>/edit", methods=["POST"])
def edit_bucket_rule(index):
    extensions = _parse_extensions(request.form.get("extensions", ""))
    bucket = (request.form.get("bucket") or "").strip()
    filename_pattern = (request.form.get("filename_pattern") or "").strip() or None

    if not extensions or not bucket:
        return "extensions and bucket are required", 400

    adapter = _adapter()
    config = config_module.load_config(adapter)
    user_rules = _user_bucket_rules(config)
    if not 0 <= index < len(user_rules):
        abort(404, description=f"No user bucket rule at index {index}")

    user_rules[index] = config_module.BucketRule(
        extensions=extensions, bucket=bucket, filename_pattern=filename_pattern
    )
    _save_user_bucket_rules(adapter, config, user_rules)
    return redirect(url_for("ui.settings") + "#bucket-rules")


@bp.route("/settings/bucket-rules/<int:index>/remove", methods=["POST"])
def remove_bucket_rule(index):
    adapter = _adapter()
    config = config_module.load_config(adapter)
    user_rules = _user_bucket_rules(config)
    if not 0 <= index < len(user_rules):
        abort(404, description=f"No user bucket rule at index {index}")

    del user_rules[index]
    _save_user_bucket_rules(adapter, config, user_rules)
    return redirect(url_for("ui.settings") + "#bucket-rules")


@bp.route("/settings/bucket-rules/<int:index>/move", methods=["POST"])
def move_bucket_rule(index):
    """Swap the rule at ``index`` with its neighbor in ``direction`` ("up" or
    "down") -- up/down buttons, not drag-and-drop (see this story's design
    decision: no drag-and-drop exists anywhere in this codebase yet, and
    up/down buttons are far lower implementation risk for a first version).
    Order changing here is exactly what makes ``resolve_bucket``'s
    first-match-wins behavior change on the next sort run -- see this
    story's reorder-then-sort integration test.
    """
    direction = request.form.get("direction")
    if direction not in ("up", "down"):
        return "direction must be 'up' or 'down'", 400

    adapter = _adapter()
    config = config_module.load_config(adapter)
    user_rules = _user_bucket_rules(config)
    if not 0 <= index < len(user_rules):
        abort(404, description=f"No user bucket rule at index {index}")

    target = index - 1 if direction == "up" else index + 1
    if 0 <= target < len(user_rules):
        user_rules[index], user_rules[target] = user_rules[target], user_rules[index]
        _save_user_bucket_rules(adapter, config, user_rules)
    return redirect(url_for("ui.settings") + "#bucket-rules")


# ---------------------------------------------------------------------------
# Search Roots CRUD (settings pane) -- the natural home for
# guided-sort-and-cluster's deferred "manage search_roots from the UI" open
# question. Value-addressed (not index) since paths are naturally unique
# and removal-by-value avoids any index-drift concern.
# ---------------------------------------------------------------------------


@bp.route("/settings/search-roots/add", methods=["POST"])
def add_search_root():
    path = (request.form.get("path") or "").strip()
    if not path:
        return "path is required", 400

    adapter = _adapter()
    config = config_module.load_config(adapter)
    if path not in config.search_roots:
        new_config = dataclasses.replace(config, search_roots=config.search_roots + [path])
        config_module.save_config(adapter, new_config)
    return redirect(url_for("ui.settings") + "#search-roots")


@bp.route("/settings/search-roots/remove", methods=["POST"])
def remove_search_root():
    path = request.form.get("path")
    adapter = _adapter()
    config = config_module.load_config(adapter)
    new_roots = [r for r in config.search_roots if r != path]
    if new_roots != config.search_roots:
        new_config = dataclasses.replace(config, search_roots=new_roots)
        config_module.save_config(adapter, new_config)
    return redirect(url_for("ui.settings") + "#search-roots")


# ---------------------------------------------------------------------------
# Master Paths CRUD (settings pane). ``backed_up`` gates reclaim.py's
# delete-refusal (see MASTER_PATH_REFUSAL_REASON) -- flipping it true->false
# REMOVES that protection for the path, so toggling requires an explicit
# confirmation click (settings.html's per-row form, not a bare checkbox
# auto-submitting on change) with warning copy naming exactly what changes,
# not generic "are you sure?" text. See this story's review-step callout.
# ---------------------------------------------------------------------------


@bp.route("/settings/master-paths/add", methods=["POST"])
def add_master_path():
    path = (request.form.get("path") or "").strip()
    backed_up = request.form.get("backed_up") == "on"
    if not path:
        return "path is required", 400

    adapter = _adapter()
    config = config_module.load_config(adapter)
    if not any(mp.path == path for mp in config.master_paths):
        new_config = dataclasses.replace(
            config, master_paths=config.master_paths + [config_module.MasterPath(path=path, backed_up=backed_up)]
        )
        config_module.save_config(adapter, new_config)
    return redirect(url_for("ui.settings") + "#master-paths")


@bp.route("/settings/master-paths/remove", methods=["POST"])
def remove_master_path():
    path = request.form.get("path")
    adapter = _adapter()
    config = config_module.load_config(adapter)
    new_paths = [mp for mp in config.master_paths if mp.path != path]
    if len(new_paths) != len(config.master_paths):
        new_config = dataclasses.replace(config, master_paths=new_paths)
        config_module.save_config(adapter, new_config)
    return redirect(url_for("ui.settings") + "#master-paths")


@bp.route("/settings/master-paths/toggle-backed-up", methods=["POST"])
def toggle_master_path_backed_up():
    path = request.form.get("path")
    adapter = _adapter()
    config = config_module.load_config(adapter)

    new_paths = []
    found = False
    for mp in config.master_paths:
        if mp.path == path:
            found = True
            new_paths.append(dataclasses.replace(mp, backed_up=not mp.backed_up))
        else:
            new_paths.append(mp)

    if not found:
        abort(404, description=f"No master path {path!r}")

    new_config = dataclasses.replace(config, master_paths=new_paths)
    config_module.save_config(adapter, new_config)
    return redirect(url_for("ui.settings") + "#master-paths")


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
