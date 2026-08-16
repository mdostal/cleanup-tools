"""In-app chat agent: a real conversation, grounded in real queue/config/
filesystem state, that can only ever *propose* into the existing approval
queue -- never execute anything itself. See
``.pHive/epics/chat-agent-plan-builder/docs/design-discussion.md`` for the
full design.

Submodules:

- ``state`` -- in-memory, per-conversation message history (mirrors
  ``ui.jobs``'s own module shape; never persisted).
- ``tools`` -- the plain Python functions the engine can call. Depends only
  on ``config``/``queue``/``commands``/``adapters`` -- never on
  ``ui.routes``, so ``ui/routes.py`` can safely import FROM this package to
  wire up the chat routes without a circular import.
- ``engine`` -- the Anthropic tool-calling loop for one conversation turn.
"""
