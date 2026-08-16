"""In-memory background-job registry for the approvals UI.

``/plan/reclaim`` can take on the order of two minutes on a real machine
with a lot of ``node_modules`` directories (see
``adapters.base.OSAdapter.dir_size_bytes``'s ``du -sk`` shell-out), because
the Flask dev server this UI runs on is single-threaded and synchronous: a
route that blocks for two minutes leaves the whole UI -- and, once this
project is wrapped in a native desktop shell, the whole app window --
looking frozen with zero feedback.

This module is the plain, non-SSE fix: a route kicks off the slow work on a
background ``threading.Thread`` via :func:`start_job`, gets back a
``job_id`` immediately, and the client polls ``GET /status/<job_id>``
(see ``routes.py``) until the job reaches a terminal state.

The registry is deliberately an in-memory ``dict``, guarded by a single
``threading.Lock``, and is NEVER persisted to disk -- unlike
``queue.yaml``, which must survive a process restart because it represents
durable approval state, a job only matters for the lifetime of one open
browser tab (or, later, one open desktop-app window). If the process
restarts mid-job, the job simply no longer exists; there is nothing to
recover, and the client is expected to re-request the underlying action.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable

# A job's ``status`` is always one of these three strings. "running" is the
# only non-terminal one; a job transitions to "done" or "error" exactly
# once, and never moves again after that.
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"


@dataclass
class JobState:
    """A snapshot of one background job's progress/outcome.

    ``current``/``total`` are plain integers a job body reports via the
    ``progress_callback`` passed to it -- there is no enforced relationship
    between them (a job that can't know its total ahead of time is free to
    report ``total`` equal to ``current`` and grow both together as work is
    discovered), but ``get_job`` never fabricates or interpolates a value
    that wasn't actually reported.

    ``result`` is only meaningful once ``status == "done"``; ``error`` is
    only meaningful once ``status == "error"``.

    ``partial`` is a generalization of the same "report progress while
    still running" idea, for jobs whose meaningful progress is growing
    TEXT rather than a numeric current/total (e.g. an LLM streaming a
    response one token at a time) -- a job body calls the
    ``partial_callback`` passed to it (alongside ``progress_callback``) to
    overwrite this field at any point before completion. ``None`` means
    "nothing reported yet," not "empty string reported." Every existing
    caller that only ever reads ``current``/``total``/``result``/``error``
    is unaffected -- this is purely additive.
    """

    status: str = STATUS_RUNNING
    current: int = 0
    total: int = 0
    result: Any = None
    error: str | None = None
    partial: Any = None


# Module-level registry + the lock guarding every read/write of it (and of
# the JobState objects it holds). One lock for the whole registry is fine
# at this project's scale (one user, one job at a time in practice); it's
# never held across the actual job work, only around the small dict/attr
# mutations below.
_lock = threading.Lock()
_jobs: dict[str, JobState] = {}


def _copy(job: JobState) -> JobState:
    """A snapshot copy, so a caller of ``get_job`` can never mutate registry state."""
    return JobState(
        status=job.status,
        current=job.current,
        total=job.total,
        result=job.result,
        error=job.error,
        partial=job.partial,
    )


def start_job(target_fn: Callable[..., Any], *args: Any, wants_partial: bool = False) -> str:
    """Start ``target_fn(*args, progress_callback[, partial_callback])`` on a
    background thread.

    Registers a fresh ``JobState(status="running")`` under a new random
    ``job_id`` before the thread is even started (so a caller that polls
    immediately after ``start_job`` returns can never get a 404 for a job
    that "doesn't exist yet"), then spawns a daemon thread running
    ``target_fn``.

    ``target_fn`` is always called with ``progress_callback`` as its last
    positional argument, exactly as before (every existing caller's
    signature -- one trailing callback parameter -- is untouched). When
    ``wants_partial=True`` is passed EXPLICITLY by the caller of
    ``start_job`` (never inferred from ``target_fn``'s signature), a second
    trailing ``partial_callback`` is also appended, and ``target_fn`` must
    declare a parameter to receive it. This is an opt-in the caller states,
    not something ``start_job`` guesses -- appending an extra positional
    argument to a call whose callee doesn't expect it would be a
    ``TypeError``, so no existing ``target_fn`` (``_sort_job``,
    ``_reclaim_job``, ``_corral_screenshots_job``, ...) is ever affected
    unless it opts in by also being updated to accept the second
    parameter.

    ``progress_callback(current, total)`` updates that job's
    ``current``/``total`` in the registry (numeric progress -- e.g. once
    per candidate directory sized). ``partial_callback(value)`` (only
    passed when ``wants_partial=True``) overwrites that job's ``partial``
    (growing non-numeric progress, e.g. accumulated streamed text -- see
    ``JobState.partial``'s docstring).

    The ENTIRE call to ``target_fn`` runs inside a ``try/except Exception``:
    whatever it raises -- including a bare ``TimeoutError`` from a queue
    lock, matching the pattern the rest of this UI's routes already use --
    is caught and recorded as a terminal ``"error"`` status with
    ``str(exc)`` as the message. This is the guarantee that a job can never
    get stuck at ``"running"`` forever: every path out of ``target_fn``,
    including an uncaught bug, reaches a terminal status.
    """
    job_id = uuid.uuid4().hex
    with _lock:
        _jobs[job_id] = JobState(status=STATUS_RUNNING, current=0, total=0)

    def _progress_callback(current: int, total: int) -> None:
        with _lock:
            job = _jobs.get(job_id)
            if job is not None:
                job.current = current
                job.total = total

    def _partial_callback(value: Any) -> None:
        with _lock:
            job = _jobs.get(job_id)
            if job is not None:
                job.partial = value

    def _run() -> None:
        try:
            if wants_partial:
                result = target_fn(*args, _progress_callback, _partial_callback)
            else:
                result = target_fn(*args, _progress_callback)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
            with _lock:
                job = _jobs.get(job_id)
                if job is not None:
                    job.status = STATUS_ERROR
                    job.error = str(exc)
        else:
            with _lock:
                job = _jobs.get(job_id)
                if job is not None:
                    job.status = STATUS_DONE
                    job.result = result

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return job_id


def get_job(job_id: str) -> JobState | None:
    """Return a snapshot of ``job_id``'s current state, or ``None`` if unknown."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        return _copy(job)
