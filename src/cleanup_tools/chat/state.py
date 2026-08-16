"""In-memory conversation-state store for the in-app chat agent.

Mirrors ``ui.jobs``'s own module shape exactly (a module-level dict + one
``threading.Lock``, never persisted to disk) -- see that module's docstring
for why: a conversation is ephemeral UI state, like a background job, not
durable data. The only durable output of a conversation is whatever gets
staged into ``queue.yaml`` via ``tools.propose_moves`` (a later story),
which survives independently of this store. If the process restarts
mid-conversation, the conversation simply no longer exists; the client is
expected to start a new one, exactly like a lost background job today.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field


@dataclass
class Message:
    """One message in a conversation's history. ``role`` is ``"user"`` or
    ``"assistant"`` -- tool-call/tool-result internals live only inside one
    turn's own execution (``engine.py``), never persisted into this
    cross-turn history, since the engine reconstructs the tool-call
    exchange fresh each turn from the plain user/assistant text record.
    """

    role: str
    text: str


@dataclass
class Conversation:
    id: str
    messages: list[Message] = field(default_factory=list)


_lock = threading.Lock()
_conversations: dict[str, Conversation] = {}


def create_conversation() -> str:
    """Start a new, empty conversation and return its id."""
    conversation_id = uuid.uuid4().hex
    with _lock:
        _conversations[conversation_id] = Conversation(id=conversation_id)
    return conversation_id


def get_conversation(conversation_id: str) -> Conversation | None:
    """A snapshot copy of ``conversation_id``'s current state, or ``None``
    if unknown -- mirrors ``jobs.get_job``'s snapshot-not-live-reference
    contract, so a caller can never mutate registry state by holding onto
    what this returns.
    """
    with _lock:
        conv = _conversations.get(conversation_id)
        if conv is None:
            return None
        return Conversation(id=conv.id, messages=list(conv.messages))


def append_message(conversation_id: str, role: str, text: str) -> None:
    """Append one message to ``conversation_id``'s history. A no-op
    (silently) if the conversation id is unknown -- mirrors
    ``jobs``'s own "job not found, nothing to update" tolerance for a
    background thread whose conversation could in principle have been
    dropped between when a turn started and when it finished.
    """
    with _lock:
        conv = _conversations.get(conversation_id)
        if conv is not None:
            conv.messages.append(Message(role=role, text=text))
