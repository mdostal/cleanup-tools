"""Unit tests for cleanup_tools.ui.jobs's in-memory job registry, in isolation
from Flask/routes.py entirely -- these drive start_job()/get_job() directly.
"""

from __future__ import annotations

import threading
import time

import pytest

from cleanup_tools.ui import jobs


def test_get_job_unknown_id_returns_none():
    assert jobs.get_job("does-not-exist") is None


def test_start_job_returns_a_job_id_immediately_in_running_state():
    started = threading.Event()
    release = threading.Event()

    def target(progress_callback):
        started.set()
        release.wait(timeout=5)
        return "ok"

    job_id = jobs.start_job(target)
    assert isinstance(job_id, str) and job_id

    started.wait(timeout=5)
    state = jobs.get_job(job_id)
    assert state is not None
    assert state.status == "running"

    release.set()


def test_job_reaches_done_status_with_result():
    def target(progress_callback):
        progress_callback(1, 2)
        progress_callback(2, 2)
        return {"staged": 3}

    job_id = jobs.start_job(target)

    deadline = time.time() + 5
    state = jobs.get_job(job_id)
    while state.status == "running" and time.time() < deadline:
        time.sleep(0.01)
        state = jobs.get_job(job_id)

    assert state.status == "done"
    assert state.result == {"staged": 3}
    assert state.current == 2
    assert state.total == 2
    assert state.error is None


def test_job_progress_updates_are_visible_while_running():
    gate = threading.Event()

    def target(progress_callback):
        progress_callback(1, 5)
        gate.wait(timeout=5)
        progress_callback(5, 5)
        return "done-result"

    job_id = jobs.start_job(target)

    deadline = time.time() + 5
    state = jobs.get_job(job_id)
    while state.current < 1 and time.time() < deadline:
        time.sleep(0.01)
        state = jobs.get_job(job_id)

    assert state.status == "running"
    assert state.current == 1
    assert state.total == 5

    gate.set()


def test_job_uncaught_exception_still_reaches_terminal_error_status():
    def target(progress_callback):
        raise RuntimeError("boom")

    job_id = jobs.start_job(target)

    deadline = time.time() + 5
    state = jobs.get_job(job_id)
    while state.status == "running" and time.time() < deadline:
        time.sleep(0.01)
        state = jobs.get_job(job_id)

    assert state.status == "error"
    assert state.error == "boom"
    assert state.result is None


def test_job_timeout_error_from_body_also_lands_as_terminal_error_status():
    # Mirrors the QUEUE_BUSY_MESSAGE / TimeoutError pattern used throughout
    # routes.py -- a bare TimeoutError raised inside the job body must be
    # caught just like any other exception, not left uncaught.
    def target(progress_callback):
        raise TimeoutError("queue lock busy")

    job_id = jobs.start_job(target)

    deadline = time.time() + 5
    state = jobs.get_job(job_id)
    while state.status == "running" and time.time() < deadline:
        time.sleep(0.01)
        state = jobs.get_job(job_id)

    assert state.status == "error"
    assert state.error == "queue lock busy"


def test_start_job_passes_through_extra_args_before_progress_callback():
    received = {}

    def target(a, b, progress_callback):
        received["a"] = a
        received["b"] = b
        received["callback_is_callable"] = callable(progress_callback)
        return "ok"

    job_id = jobs.start_job(target, "x", "y")

    deadline = time.time() + 5
    state = jobs.get_job(job_id)
    while state.status == "running" and time.time() < deadline:
        time.sleep(0.01)
        state = jobs.get_job(job_id)

    assert state.status == "done"
    assert received == {"a": "x", "b": "y", "callback_is_callable": True}


# ---------------------------------------------------------------------------
# partial/wants_partial: the streaming-text generalization added for the
# chat-agent-plan-builder epic. Opt-in ONLY (wants_partial=True), so every
# existing single-callback target_fn above stays completely unaffected --
# these tests specifically pin down the opt-in boundary, not just the happy
# path.
# ---------------------------------------------------------------------------


def test_wants_partial_false_by_default_partial_stays_none():
    def target(progress_callback):
        progress_callback(1, 1)
        return "ok"

    job_id = jobs.start_job(target)

    deadline = time.time() + 5
    state = jobs.get_job(job_id)
    while state.status == "running" and time.time() < deadline:
        time.sleep(0.01)
        state = jobs.get_job(job_id)

    assert state.status == "done"
    assert state.partial is None


def test_wants_partial_true_passes_a_second_callback_and_updates_partial():
    gate = threading.Event()

    def target(progress_callback, partial_callback):
        partial_callback("Hello")
        gate.wait(timeout=5)
        partial_callback("Hello, world")
        return "final text"

    job_id = jobs.start_job(target, wants_partial=True)

    deadline = time.time() + 5
    state = jobs.get_job(job_id)
    while state.partial is None and time.time() < deadline:
        time.sleep(0.01)
        state = jobs.get_job(job_id)

    assert state.status == "running"
    assert state.partial == "Hello"

    gate.set()

    deadline = time.time() + 5
    state = jobs.get_job(job_id)
    while state.status == "running" and time.time() < deadline:
        time.sleep(0.01)
        state = jobs.get_job(job_id)

    assert state.status == "done"
    assert state.partial == "Hello, world"
    assert state.result == "final text"


def test_wants_partial_true_with_extra_args_still_passes_them_before_both_callbacks():
    received = {}

    def target(a, progress_callback, partial_callback):
        received["a"] = a
        received["both_callable"] = callable(progress_callback) and callable(partial_callback)
        return "ok"

    job_id = jobs.start_job(target, "x", wants_partial=True)

    deadline = time.time() + 5
    state = jobs.get_job(job_id)
    while state.status == "running" and time.time() < deadline:
        time.sleep(0.01)
        state = jobs.get_job(job_id)

    assert state.status == "done"
    assert received == {"a": "x", "both_callable": True}


def test_existing_single_callback_target_fn_unaffected_by_wants_partial_feature_existing():
    """Regression guard: a target_fn with the OLD one-callback signature must
    never receive a second positional argument just because the partial
    feature now exists in this module -- wants_partial defaults to False.
    """

    def old_style_target(progress_callback):
        return "still works"

    job_id = jobs.start_job(old_style_target)  # no wants_partial at all

    deadline = time.time() + 5
    state = jobs.get_job(job_id)
    while state.status == "running" and time.time() < deadline:
        time.sleep(0.01)
        state = jobs.get_job(job_id)

    assert state.status == "done"
    assert state.result == "still works"


def test_get_job_returns_a_snapshot_not_a_live_reference():
    def target(progress_callback):
        progress_callback(1, 1)
        return "ok"

    job_id = jobs.start_job(target)

    deadline = time.time() + 5
    state = jobs.get_job(job_id)
    while state.status == "running" and time.time() < deadline:
        time.sleep(0.01)
        state = jobs.get_job(job_id)

    snapshot = jobs.get_job(job_id)
    snapshot.status = "mutated"
    snapshot.current = 999

    fresh = jobs.get_job(job_id)
    assert fresh.status == "done"
    assert fresh.current == 1
