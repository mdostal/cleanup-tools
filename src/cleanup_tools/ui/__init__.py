"""Localhost-only Flask "approvals UI" for reviewing AI-proposed filesystem
actions staged in the approval queue (see :mod:`cleanup_tools.queue`).

This package only ever *reviews* queue entries (approve/reject/undo,
dashboard, staging new plans) -- it never executes a move or delete itself.
Execution stays a deliberate, separate ``cleanup sort --from-queue --go`` /
``cleanup reclaim --from-queue --go`` CLI step.
"""

from __future__ import annotations
