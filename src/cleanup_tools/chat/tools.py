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

from typing import Any, Callable

from .. import config as config_module
from ..adapters.base import OSAdapter

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


# Dispatch table the engine uses to call a tool by the name the model chose.
# **kwargs always includes every input_schema property the model supplied
# (validated against the schema by the API itself before this ever runs) --
# individual tool functions accept **_kwargs for ones with no properties
# yet, and named parameters once a tool takes real arguments.
TOOL_FUNCTIONS: dict[str, Callable[..., dict]] = {
    "list_locations": list_locations,
}
