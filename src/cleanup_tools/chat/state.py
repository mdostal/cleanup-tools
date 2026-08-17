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
    # Running total of files this conversation has staged via propose_moves
    # across every turn so far -- the state chat-cost-control-and-settings'
    # per-conversation file cap is checked against (see
    # tools.propose_moves and its ``_CONVERSATION_FILE_CAP`` docstring).
    # Grows monotonically; never reset except by starting a new conversation.
    staged_file_count: int = 0


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
        return Conversation(
            id=conv.id, messages=list(conv.messages), staged_file_count=conv.staged_file_count
        )


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


def turn_count(conversation: Conversation) -> int:
    """How many complete turns ``conversation`` has had so far.

    A pure function of ``conversation.messages``, not a separate counter
    that could drift from it: ``append_message`` is only ever called twice
    per completed turn (once "user", once "assistant" -- see
    ``ui/routes.py``'s ``_chat_turn_job``, which appends both only AFTER
    ``chat.engine.run_turn`` returns successfully, never on a failed/errored
    turn), so ``len(messages) // 2`` is always exact.
    """
    return len(conversation.messages) // 2


def record_staged_files(conversation_id: str, count: int) -> None:
    """Add ``count`` to ``conversation_id``'s running staged-file total --
    see ``Conversation.staged_file_count``'s docstring. Silently a no-op if
    the conversation id is unknown, mirroring ``append_message``'s own
    tolerance for the same reason.
    """
    with _lock:
        conv = _conversations.get(conversation_id)
        if conv is not None:
            conv.staged_file_count += count
