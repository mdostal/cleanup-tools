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
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, jsonify, redirect, render_template, request, url_for
from PIL import Image, UnidentifiedImageError

from .. import config as config_module
from .. import queue as queue_module
from ..ai import CredentialsError, get_provider
from ..ai.wiring import DEFAULT_CAP as DEFAULT_AI_CAP
from ..ai.wiring import propose_for_other_bucket
from ..commands import corral_screenshots as corral_screenshots_module
from ..commands import reclaim as reclaim_module
from ..commands import sort as sort_module
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


def _is_ai_source(source: str) -> bool:
    """Whether a QueueEntry's ``source`` tag marks it as AI-proposed.

    AI-sourced entries carry ``source=f"ai:<provider>"`` (see
    ``ai.wiring._provider_source``, e.g. ``"ai:anthropic"``). Every other
    source convention currently in use -- the ``QueueEntry`` default
    ``"manual"``, and this module's own ``"ui-plan-sort"``/
    ``"ui-plan-reclaim"`` plan-staging tags -- is a human-triggered source
    and therefore NOT AI-proposed. This is purely a rendering distinction
    (see ``queue.html``'s per-entry badge and ``dashboard.html``'s
    per-group AI count): it never changes approve/reject/undo/bulk
    behavior, which treats every entry identically regardless of source.
    """
    return source.startswith("ai:")


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


def _stage_sort_plan(adapter, queue_path, progress_callback=None) -> list[queue_module.QueueEntry]:
    """Dry-run cleanup_tools.commands.sort against the default target dir,
    convert its plan into QueueEntry "move" proposals, and stage them.

    Reuses sort.run()'s own plan-building (args=None -> dry-run against the
    platform Downloads dir, same default the "sort" CLI subcommand uses
    with no args) rather than walking the filesystem again here.

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
    """
    result = sort_module.run(adapter, args=None)
    plan_items = result.get("plan", [])
    total = len(plan_items)
    new_entries = []
    for current, item in enumerate(plan_items, start=1):
        new_entries.append(
            queue_module.QueueEntry(
                action="move",
                src=str(item["src"]),
                dest=str(item["dest"]),
                source="ui-plan-sort",
                group_key=f"sort:{item['bucket']}",
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


def _stage_reclaim_plan(adapter, queue_path, progress_callback=None) -> list[queue_module.QueueEntry]:
    """Dry-run cleanup_tools.commands.reclaim across its default roots,
    convert every non-refused candidate into a QueueEntry "delete"
    proposal, and stage them.

    Master-path-refused candidates (see reclaim.py's
    ``MASTER_PATH_REFUSAL_REASON``) are never staged -- there is nothing to
    approve for a candidate reclaim.py itself refuses to ever delete, even
    under --go.

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

    result = reclaim_module.run(run_adapter, args=None)
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
    return queue_module.stage_entries(adapter, new_entries, queue_path)


def _stage_corral_screenshots_plan(
    adapter, queue_path, progress_callback=None
) -> list[queue_module.QueueEntry]:
    """Dry-run cleanup_tools.commands.corral_screenshots across its default
    roots, convert its plan into QueueEntry "move" proposals, and stage them.

    Reuses corral_screenshots.run()'s own plan-building (args=None ->
    dry-run against the default resolved roots, same defaults the
    "corral-screenshots" CLI subcommand uses with no args) rather than
    walking the filesystem again here -- structurally identical to
    _stage_sort_plan above, including the same ``queue_path``/
    ``progress_callback`` treatment (see that function's docstring for the
    background-job/no-app-context reasoning and why no proxy-adapter trick
    is needed for progress here). args=None also means
    set_default_location is never true, so this route can never trigger the
    OS-preference change -- only ever a dry-run plan.
    """
    result = corral_screenshots_module.run(adapter, args=None)
    plan_items = result.get("plan", [])
    total = len(plan_items)
    new_entries = []
    for current, item in enumerate(plan_items, start=1):
        new_entries.append(
            queue_module.QueueEntry(
                action="move",
                src=str(item["src"]),
                dest=str(item["dest"]),
                source="ui-plan-corral-screenshots",
                group_key="corral-screenshots",
                plan_snapshot=queue_module.build_plan_snapshot(item["src"]),
            )
        )
        if progress_callback is not None:
            progress_callback(current, total)
    return queue_module.stage_entries(adapter, new_entries, queue_path)


def _sort_job(adapter, queue_path, progress_callback) -> list[queue_module.QueueEntry]:
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
        return _stage_sort_plan(adapter, queue_path, progress_callback)
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
    """
    adapter = _adapter()
    queue_path = _queue_path()
    job_id = jobs.start_job(_sort_job, adapter, queue_path)
    return jsonify({"job_id": job_id})


def _reclaim_job(adapter, queue_path, progress_callback) -> list[queue_module.QueueEntry]:
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
        return _stage_reclaim_plan(adapter, queue_path, progress_callback)
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
    """
    adapter = _adapter()
    queue_path = _queue_path()
    job_id = jobs.start_job(_reclaim_job, adapter, queue_path)
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


def _corral_screenshots_job(adapter, queue_path, progress_callback) -> list[queue_module.QueueEntry]:
    """The ``target_fn`` run on ``/plan/corral-screenshots``'s background job
    thread. Structurally identical to ``_sort_job`` above.
    """
    try:
        return _stage_corral_screenshots_plan(adapter, queue_path, progress_callback)
    except TimeoutError as exc:
        raise TimeoutError(QUEUE_BUSY_MESSAGE) from exc


@bp.route("/plan/corral-screenshots")
def plan_corral_screenshots():
    """Kick off corral-screenshots planning/staging on a background job
    thread and return its ``job_id`` immediately -- mirrors ``plan_sort``/
    ``plan_reclaim`` exactly.
    """
    adapter = _adapter()
    queue_path = _queue_path()
    job_id = jobs.start_job(_corral_screenshots_job, adapter, queue_path)
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


@bp.route("/queue")
def queue_view():
    entries = _pending_entries()
    page_entries, pagination = _paginate(
        entries, request.args.get("page"), request.args.get("per_page")
    )
    # Distinct, truthy group_keys among ALL pending entries (not just the
    # current page) -- feeds the "bulk-approve/reject this whole group"
    # quick-action bar, which must be able to target entries regardless of
    # which page they currently sit on. Entries with no group_key (the
    # "ungrouped" display fallback) are deliberately excluded here: there is
    # no real group_key value that would round-trip back to matching them
    # via the exact-match bulk routes, so offering a fake "ungrouped" button
    # would silently do nothing when clicked.
    group_keys = sorted({e.group_key for e in entries if e.group_key})
    return render_template(
        "queue.html",
        entries=page_entries,
        total_pending=len(entries),
        group_keys=group_keys,
        is_image_entry=is_image_entry,
        is_ai_source=_is_ai_source,
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


def _bulk_target_ids(pending: list, group_key: str | None, entry_ids: list[str] | None) -> list[str]:
    """Resolve the pending-entry ids a bulk action should touch.

    ``group_key`` (if given, non-blank) takes priority and is matched
    exactly against ``entry.group_key`` -- entries with no group_key never
    match a group_key request, even "ungrouped" (the display fallback used
    only in templates, never a real stored value). If ``group_key`` isn't
    given, ``entry_ids`` (if given) is intersected against the pending ids
    so only ids that are both requested and still pending are touched.
    Returns an empty list if neither is given.
    """
    if group_key:
        return [e.id for e in pending if e.group_key == group_key]
    if entry_ids:
        pending_ids = {e.id for e in pending}
        # Preserve request order, dedup, and drop anything not pending.
        seen = set()
        result = []
        for eid in entry_ids:
            if eid in pending_ids and eid not in seen:
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
