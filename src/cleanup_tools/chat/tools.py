"""Tools the chat engine can call -- see the design discussion's §2.2 (tool
architecture) and §2.8 (prompt-injection posture: every tool result is
data to classify, never an instruction to follow -- enforced by the
engine's system prompt, not by anything in this file).

Every tool is either **read-only** or **stage-only**. None execute, delete,
or touch ``master_paths``/config directly -- see the design discussion's
§2.1 for why this bound is the epic's core safety property: a chat-proposed
entry is just an ordinary pending ``QueueEntry``, and only a human clicking
Approve (in the queue, on the dashboard tree, or via the chat's own
Approve-these-N action) can make anything actually happen on disk.

This module depends only on ``config``/``queue``/``commands``/``adapters``/
``chat.state`` -- never on ``ui.routes`` -- so ``ui/routes.py`` can safely
import FROM this package (to wire up the new chat routes) without a
circular import; see ``config.configured_locations``'s docstring for the
same reasoning applied to where that helper itself now lives.
``chat.state`` is a safe sibling dependency here (it doesn't import this
module or ``chat.engine``), used only to read/update a conversation's
running staged-file total for :func:`propose_moves`'s per-conversation cap.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from .. import config as config_module
from .. import queue as queue_module
from ..adapters.base import OSAdapter
from ..commands import sort as sort_module
from . import state as chat_state

# Every list-of-files tool caps its output at this many filenames per call,
# with an explicit truncated/total_count signal rather than a silent cut --
# see list_candidate_files' docstring and the design discussion's "flat
# context cost regardless of queue size" principle.
_FILE_LIST_CAP = 50

# propose_moves' hard, per-conversation ceiling on TOTAL files staged across
# every propose_moves call in a conversation's lifetime -- stops one runaway
# turn (or several) from staging an enormous plan with no review checkpoint
# (design discussion §2.6). Unlike chat_turn_cap, this is NOT a Config field
# / not user-configurable -- a fixed safety ceiling, not a preference.
_CONVERSATION_FILE_CAP = 500

# propose_moves' second mandatory guard: a bare bucket-name identifier only
# -- no path separator, no "..", no leading "/". This is checked BEFORE
# dest_bucket is ever interpolated into a Path, so a model-supplied value
# like "../../etc" or "/System" can never reach path construction at all.
_BUCKET_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Every tool's Anthropic tool-use schema, in the shape the Messages API's
# `tools=[...]` parameter expects -- mirrors ai/anthropic_provider.py's
# `_PROPOSE_BUCKET_TOOL` shape exactly, just one entry per tool here instead
# of a single forced tool choice (this engine lets the model choose among
# several tools across a conversation, not force exactly one per call).
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "list_locations",
        "description": (
            "List every location this app would scan by default (the user's "
            "configured search_roots, or a downloads/desktop/documents "
            "fallback if none are configured). Call this to ground yourself "
            "in what the user has actually set up before proposing anything."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_queue_summary",
        "description": (
            "Get aggregate counts (never per-entry detail) of everything "
            "currently in the approval queue, grouped by location then "
            "bucket -- the exact same shape the dashboard's location tree "
            "shows. Call this to ground yourself in what's already staged "
            "before discussing or proposing anything further."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "scan_location",
        "description": (
            "Run a fresh, read-only dry-run sort plan for one location and "
            "return bucket counts (e.g. {'photos': 12, 'pdfs': 4}) -- real, "
            "current filesystem state, never cached or stale, and never "
            "writes anything. Optionally scope the result to a subset of "
            "bucket names."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "Absolute path to one configured location (see list_locations).",
                },
                "buckets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: only return counts for these bucket names.",
                },
            },
            "required": ["location"],
        },
    },
    {
        "name": "list_candidate_files",
        "description": (
            "List actual filenames a fresh dry-run sort plan would place in "
            "one bucket at one location, capped at 50 -- if more exist, the "
            "result says so explicitly rather than silently truncating. "
            "Never writes anything."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "Absolute path to one configured location (see list_locations).",
                },
                "bucket": {
                    "type": "string",
                    "description": "Bucket name to list candidate files for (see scan_location).",
                },
            },
            "required": ["location", "bucket"],
        },
    },
    {
        "name": "propose_moves",
        "description": (
            "Stage one or more proposed file moves as ordinary pending queue "
            "entries for the user to review -- this is the ONLY tool that "
            "writes anything, and it never moves or deletes a real file "
            "itself. Every candidate is checked against the same protected-"
            "path and bucket-name guards every other part of this app "
            "already uses; a candidate that fails either guard is refused "
            "(never staged) and reported back with a reason. Staged entries "
            "show up in the Review Queue / dashboard exactly like any other "
            "proposal, waiting for the user to click Approve."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "moves": {
                    "type": "array",
                    "description": "One or more {src, dest_bucket} pairs to propose.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "src": {
                                "type": "string",
                                "description": "Absolute path to the real file to propose moving.",
                            },
                            "dest_bucket": {
                                "type": "string",
                                "description": (
                                    "Bare bucket name (e.g. 'photos', 'pdfs') -- no path "
                                    "separators, no '..'."
                                ),
                            },
                        },
                        "required": ["src", "dest_bucket"],
                    },
                },
            },
            "required": ["moves"],
        },
    },
]


def list_locations(adapter: OSAdapter, **_kwargs: Any) -> dict:
    """Every location sort/reclaim/corral-screenshots would scan by
    default -- the exact same resolution the dashboard's kickoff-bar
    location picker already uses (``config.configured_locations``), so the
    agent's answer can never disagree with what the UI itself shows.
    """
    config = config_module.load_config(adapter)
    locations = config_module.configured_locations(config, adapter)
    return {"locations": locations}


def list_queue_summary(adapter: OSAdapter, **_kwargs: Any) -> dict:
    """Aggregate location -> bucket counts for the current queue.

    Reuses ``queue.group_entries_hierarchical`` directly (the exact
    aggregation the dashboard tree renders) rather than re-deriving a
    similar-but-different shape here -- see that function's docstring for
    why it lives in ``queue.py`` and not ``ui/routes.py``. Aggregates only:
    never returns a per-entry list, so this tool's context cost is flat
    regardless of how many entries are actually queued.
    """
    path = queue_module.default_queue_path(adapter)
    entries = queue_module.load_queue(adapter, path)
    return {"locations": queue_module.group_entries_hierarchical(entries)}


def scan_location(adapter: OSAdapter, location: str, buckets: list[str] | None = None, **_kwargs: Any) -> dict:
    """Fresh, read-only bucket counts for one location.

    Calls straight into ``sort._plan`` -- the same dry-run planning logic
    ``/plan/sort`` and ``cleanup sort`` (without ``--go``) already use --
    rather than re-implementing bucket resolution here. ``_plan`` only ever
    reads the filesystem (``adapter.list_dir``); nothing in this call path
    can write or move a file, matching this tool's schema description and
    the chat-agent-plan-builder design discussion's read-only/stage-only
    tool bound.
    """
    config = config_module.load_config(adapter)
    plan = sort_module._plan(adapter, config, [Path(location)])

    counts: dict[str, int] = {}
    for item in plan:
        bucket = item["bucket"]
        if buckets is not None and bucket not in buckets:
            continue
        counts[bucket] = counts.get(bucket, 0) + 1

    bucket_list = [
        {"bucket": bucket, "count": count}
        for bucket, count in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    ]
    return {"location": location, "buckets": bucket_list}


def list_candidate_files(adapter: OSAdapter, location: str, bucket: str, **_kwargs: Any) -> dict:
    """Actual filenames one bucket at one location would sort, capped at
    ``_FILE_LIST_CAP``, with an explicit ``truncated``/``total_count`` so a
    caller (the model, and anyone reading its answer) never mistakes a
    capped list for the complete one. Read-only, same reasoning as
    :func:`scan_location`.
    """
    config = config_module.load_config(adapter)
    plan = sort_module._plan(adapter, config, [Path(location)])

    matches = sorted(item["src"].name for item in plan if item["bucket"] == bucket)
    return {
        "location": location,
        "bucket": bucket,
        "files": matches[:_FILE_LIST_CAP],
        "total_count": len(matches),
        "truncated": len(matches) > _FILE_LIST_CAP,
    }


def propose_moves(
    adapter: OSAdapter, moves: list[dict], conversation_id: str | None = None, **_kwargs: Any
) -> dict:
    """The one write tool this agent has: stage proposed ``{src, dest_bucket}``
    pairs as ordinary pending ``QueueEntry`` "move" proposals. Never moves,
    deletes, or executes anything itself -- see this module's docstring.

    Every candidate passes two mandatory guards, in this order, BEFORE
    ``queue.stage_entries`` is ever called for it:

    1. A bare bucket-name shape check on ``dest_bucket`` (``_BUCKET_NAME_RE``)
       -- rejected before ``dest_bucket`` is ever interpolated into a
       ``Path``, since it is untrusted, model-supplied input. This alone
       blocks path separators, ``".."``, and absolute paths.
    2. ``queue.is_protected_path`` on ``src`` -- the exact same hard-blocked
       system-path check every other staging path in this app already
       enforces (see ``queue.PROTECTED_PATH_ROOTS``'s docstring). Reused
       unmodified, not reimplemented, so this write path can never disagree
       with any other.

    A candidate missing/malformed input, or pointing at a src that doesn't
    exist, is refused the same way (never staged). Every refusal is
    reported back in ``refused`` with a ``reason`` -- never a silent drop,
    so the agent's response to the user can say what happened and why.

    ``dest``/``group_key`` are built identically to every other staging
    path (``config.location_for_src`` + ``sort.SORTED_SUBDIR`` + the
    3-segment ``sort:<location>:<bucket>`` scheme -- see
    ``config.location_for_src``'s docstring) so a chat-proposed entry is
    structurally indistinguishable from a manually-planned one, except its
    ``source`` tag: ``"ai:chat"`` (distinct from the existing single-shot
    batch proposal's ``"ai:anthropic"``), purely for display -- it never
    changes approve/reject/undo/bulk behavior.

    ``conversation_id`` -- injected by the engine (never something the
    model supplies as tool-call input; it isn't in this tool's
    ``input_schema``) -- is used to enforce ``_CONVERSATION_FILE_CAP``: once
    every candidate has passed both guards above, only as many of the
    survivors as fit in the conversation's REMAINING budget are actually
    staged (the rest are refused with reason ``"conversation_file_cap_reached"``)
    -- a pre-call gate exactly like ``ai/wiring.py``'s own cap discipline
    (slice BEFORE calling ``stage_entries``, never call-then-discard). If
    ``conversation_id`` is ``None`` or unknown (e.g. a direct/test call),
    the cap is enforced against a fresh ``_CONVERSATION_FILE_CAP`` budget
    with no persisted running total.
    """
    config = config_module.load_config(adapter)
    queue_path = queue_module.default_queue_path(adapter)

    candidates: list[queue_module.QueueEntry] = []
    candidate_moves: list[tuple[str, str]] = []
    refused: list[dict] = []

    for move in moves:
        src = move.get("src")
        dest_bucket = move.get("dest_bucket")

        if not isinstance(src, str) or not src or not isinstance(dest_bucket, str) or not dest_bucket:
            refused.append({"src": src, "dest_bucket": dest_bucket, "reason": "missing_or_invalid_input"})
            continue
        if not _BUCKET_NAME_RE.match(dest_bucket):
            refused.append({"src": src, "dest_bucket": dest_bucket, "reason": "invalid_bucket_name"})
            continue
        if queue_module.is_protected_path(src, adapter):
            refused.append({"src": src, "dest_bucket": dest_bucket, "reason": "protected_path"})
            continue
        if not Path(src).exists():
            refused.append({"src": src, "dest_bucket": dest_bucket, "reason": "does_not_exist"})
            continue

        resolved_src = Path(src).resolve()
        location = config_module.location_for_src(src, config, adapter)
        dest = Path(location) / sort_module.SORTED_SUBDIR / dest_bucket / resolved_src.name
        candidates.append(
            queue_module.QueueEntry(
                action="move",
                src=str(resolved_src),
                dest=str(dest),
                source="ai:chat",
                group_key=f"sort:{location}:{dest_bucket}",
                plan_snapshot=queue_module.build_plan_snapshot(resolved_src),
            )
        )
        candidate_moves.append((src, dest_bucket))

    remaining_budget = _CONVERSATION_FILE_CAP
    if conversation_id is not None:
        conv = chat_state.get_conversation(conversation_id)
        if conv is not None:
            remaining_budget = max(0, _CONVERSATION_FILE_CAP - conv.staged_file_count)

    to_stage = candidates[:remaining_budget]
    for over_cap_src, over_cap_bucket in candidate_moves[len(to_stage) :]:
        refused.append(
            {"src": over_cap_src, "dest_bucket": over_cap_bucket, "reason": "conversation_file_cap_reached"}
        )

    staged = queue_module.stage_entries(adapter, to_stage, queue_path) if to_stage else []
    if conversation_id is not None and staged:
        chat_state.record_staged_files(conversation_id, len(staged))

    return {"staged_entry_ids": [entry.id for entry in staged], "refused": refused}


# Dispatch table the engine uses to call a tool by the name the model chose.
# **kwargs always includes every input_schema property the model supplied
# (validated against the schema by the API itself before this ever runs) --
# individual tool functions accept **_kwargs for ones with no properties
# yet, and named parameters once a tool takes real arguments.
TOOL_FUNCTIONS: dict[str, Callable[..., dict]] = {
    "list_locations": list_locations,
    "list_queue_summary": list_queue_summary,
    "scan_location": scan_location,
    "list_candidate_files": list_candidate_files,
    "propose_moves": propose_moves,
}
