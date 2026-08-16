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

This module depends only on ``config``/``queue``/``commands``/``adapters``
-- never on ``ui.routes`` -- so ``ui/routes.py`` can safely import FROM this
package (to wire up the new chat routes) without a circular import; see
``config.configured_locations``'s docstring for the same reasoning applied
to where that helper itself now lives.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .. import config as config_module
from .. import queue as queue_module
from ..adapters.base import OSAdapter
from ..commands import sort as sort_module

# Every list-of-files tool caps its output at this many filenames per call,
# with an explicit truncated/total_count signal rather than a silent cut --
# see list_candidate_files' docstring and the design discussion's "flat
# context cost regardless of queue size" principle.
_FILE_LIST_CAP = 50

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
}
