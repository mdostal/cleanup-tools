"""Tests for cleanup_tools.chat.state's in-memory conversation store."""

from __future__ import annotations

from cleanup_tools.chat import state


def test_create_conversation_returns_a_fresh_empty_conversation():
    conv_id = state.create_conversation()

    conv = state.get_conversation(conv_id)
    assert conv is not None
    assert conv.id == conv_id
    assert conv.messages == []


def test_get_conversation_unknown_id_returns_none():
    assert state.get_conversation("does-not-exist") is None


def test_append_message_adds_to_history_in_order():
    conv_id = state.create_conversation()

    state.append_message(conv_id, "user", "hello")
    state.append_message(conv_id, "assistant", "hi there")

    conv = state.get_conversation(conv_id)
    assert [(m.role, m.text) for m in conv.messages] == [
        ("user", "hello"),
        ("assistant", "hi there"),
    ]


def test_append_message_unknown_conversation_id_is_a_silent_no_op():
    # Must not raise -- mirrors jobs.py's "job not found, nothing to
    # update" tolerance.
    state.append_message("does-not-exist", "user", "hello")


def test_get_conversation_returns_a_snapshot_not_a_live_reference():
    conv_id = state.create_conversation()
    state.append_message(conv_id, "user", "hello")

    snapshot = state.get_conversation(conv_id)
    snapshot.messages.append(state.Message(role="user", text="mutated"))

    fresh = state.get_conversation(conv_id)
    assert len(fresh.messages) == 1
    assert fresh.messages[0].text == "hello"


def test_create_conversation_ids_are_unique():
    ids = {state.create_conversation() for _ in range(20)}
    assert len(ids) == 20


# ---------------------------------------------------------------------------
# turn_count / staged_file_count: the state chat_turn_cap and the
# per-conversation file-proposal cap are enforced against.
# ---------------------------------------------------------------------------


def test_turn_count_is_zero_for_a_fresh_conversation():
    conv_id = state.create_conversation()
    assert state.turn_count(state.get_conversation(conv_id)) == 0


def test_turn_count_increments_once_per_completed_user_assistant_pair():
    conv_id = state.create_conversation()
    state.append_message(conv_id, "user", "hello")
    state.append_message(conv_id, "assistant", "hi there")

    assert state.turn_count(state.get_conversation(conv_id)) == 1

    state.append_message(conv_id, "user", "and another thing")
    state.append_message(conv_id, "assistant", "sure")

    assert state.turn_count(state.get_conversation(conv_id)) == 2


def test_staged_file_count_starts_at_zero():
    conv_id = state.create_conversation()
    assert state.get_conversation(conv_id).staged_file_count == 0


def test_record_staged_files_accumulates_across_calls():
    conv_id = state.create_conversation()

    state.record_staged_files(conv_id, 3)
    state.record_staged_files(conv_id, 2)

    assert state.get_conversation(conv_id).staged_file_count == 5


def test_record_staged_files_unknown_conversation_id_is_a_silent_no_op():
    state.record_staged_files("does-not-exist", 5)


def test_get_conversation_staged_file_count_is_a_snapshot_not_a_live_reference():
    conv_id = state.create_conversation()
    state.record_staged_files(conv_id, 1)

    snapshot = state.get_conversation(conv_id)
    state.record_staged_files(conv_id, 1)

    assert snapshot.staged_file_count == 1
    assert state.get_conversation(conv_id).staged_file_count == 2
