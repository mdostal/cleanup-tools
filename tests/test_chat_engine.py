"""Tests for cleanup_tools.chat.engine's tool-calling loop.

Zero real network calls: every ``anthropic.Anthropic``-shaped client under
test is a fake injected directly into ``run_turn`` (mirroring
test_ai_provider.py's ``_FakeClient``/block_real_network pattern exactly),
and the autouse ``block_real_network`` fixture is a belt-and-suspenders
guard in case that ever changes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cleanup_tools.adapters import MacOSAdapter
from cleanup_tools.chat import engine


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    def _blow_up(*_args, **_kwargs):
        raise RuntimeError("Real network call attempted in test_chat_engine.py -- must be mocked")

    monkeypatch.setattr("socket.socket.connect", _blow_up)


@pytest.fixture
def adapter(tmp_path) -> MacOSAdapter:
    class FakeHomeAdapter(MacOSAdapter):
        def resolve_home(self):
            return tmp_path

    return FakeHomeAdapter()


# ---------------------------------------------------------------------------
# Fake streaming client -- mirrors the real anthropic SDK's
# client.messages.stream(...) shape (a context manager, iterable over
# events, with get_final_message()) closely enough for engine.py's actual
# usage of it, without importing the real SDK's stream classes.
# ---------------------------------------------------------------------------


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(id_: str, name: str, input_: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def _delta_events(text: str) -> list[SimpleNamespace]:
    """One content_block_delta event per character -- exercises genuine
    incremental accumulation, not a single big chunk.
    """
    return [
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=ch))
        for ch in text
    ]


class _FakeStream:
    def __init__(self, events, final_message):
        self._events = events
        self._final_message = final_message

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self):
        return self._final_message


class _FakeMessages:
    """Dispenses queued (events, final_message) pairs from ``.stream()``,
    one per call -- raises IndexError (an unmistakable test failure) if
    called more times than queued, mirroring test_ai_provider.py's
    _FakeMessages discipline.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def stream(self, **_kwargs):
        self.calls += 1
        events, final_message = self._responses.pop(0)
        return _FakeStream(events, final_message)


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


def _text_only_response(text: str, stop_reason: str = "end_turn"):
    final = SimpleNamespace(content=[_text_block(text)], stop_reason=stop_reason)
    return (_delta_events(text), final)


def _tool_use_response(tool_name: str, tool_input: dict, tool_use_id: str = "toolu_1", lead_text: str = ""):
    final = SimpleNamespace(
        content=([_text_block(lead_text)] if lead_text else []) + [_tool_use_block(tool_use_id, tool_name, tool_input)],
        stop_reason="tool_use",
    )
    return (_delta_events(lead_text), final)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_turn_text_only_no_tool_calls_returns_final_text(adapter):
    client = _FakeClient([_text_only_response("Hi there!")])

    result = engine.run_turn(client, adapter, history=[], user_message="hello")

    assert result["text"] == "Hi there!"
    assert result["staged_entry_ids"] == []
    assert client.messages.calls == 1


def test_run_turn_partial_callback_receives_growing_accumulated_text(adapter):
    client = _FakeClient([_text_only_response("Hello")])
    seen = []

    engine.run_turn(client, adapter, history=[], user_message="hi", partial_callback=seen.append)

    # One callback per character, each strictly longer than the last, ending
    # at the full text -- proves genuine incremental accumulation, not a
    # single final callback disguised as streaming.
    assert seen[-1] == "Hello"
    assert len(seen) == len("Hello")
    assert all(len(seen[i]) < len(seen[i + 1]) for i in range(len(seen) - 1))


def test_run_turn_calls_a_tool_and_feeds_the_real_result_back(adapter, tmp_path):
    from cleanup_tools import config as config_module

    root = tmp_path / "my-root"
    config_module.save_config(
        adapter,
        config_module.Config(bucket_rules=config_module.DEFAULT_BUCKET_RULES, search_roots=[str(root)]),
    )

    client = _FakeClient(
        [
            _tool_use_response("list_locations", {}),
            _text_only_response("You have one location configured."),
        ]
    )

    result = engine.run_turn(client, adapter, history=[], user_message="what locations do I have?")

    assert result["text"] == "You have one location configured."
    assert client.messages.calls == 2  # one round to call the tool, one round for final text
    # See test_run_turn_second_round_trip_receives_the_real_tool_result below
    # for the assertion that the SECOND round's messages actually contain
    # the real (not placeholder) tool result.


def test_run_turn_second_round_trip_receives_the_real_tool_result(adapter, tmp_path, monkeypatch):
    from cleanup_tools import config as config_module

    root = tmp_path / "my-root"
    config_module.save_config(
        adapter,
        config_module.Config(bucket_rules=config_module.DEFAULT_BUCKET_RULES, search_roots=[str(root)]),
    )

    captured_messages = []

    class _CapturingMessages(_FakeMessages):
        def stream(self, **kwargs):
            captured_messages.append(kwargs["messages"])
            return super().stream(**kwargs)

    client = _FakeClient([])
    client.messages = _CapturingMessages(
        [
            _tool_use_response("list_locations", {}),
            _text_only_response("done"),
        ]
    )

    engine.run_turn(client, adapter, history=[], user_message="what locations do I have?")

    assert len(captured_messages) == 2
    second_round_messages = captured_messages[1]
    # Last message is the tool_result the engine built from the real
    # list_locations() call -- must contain the real configured root path.
    tool_result_message = second_round_messages[-1]
    assert tool_result_message["role"] == "user"
    tool_result_content = tool_result_message["content"][0]["content"]
    assert str(root.resolve()) in tool_result_content


def test_run_turn_unknown_tool_name_does_not_crash(adapter):
    client = _FakeClient(
        [
            _tool_use_response("not_a_real_tool", {}),
            _text_only_response("I couldn't do that."),
        ]
    )

    result = engine.run_turn(client, adapter, history=[], user_message="do something weird")

    assert result["text"] == "I couldn't do that."


def test_run_turn_respects_max_tool_rounds_per_turn_and_never_spins_forever(adapter, monkeypatch):
    monkeypatch.setattr(engine, "MAX_TOOL_ROUNDS_PER_TURN", 3)
    # The model calls list_locations every single round, forever -- proves
    # the loop terminates on the round cap rather than spinning.
    responses = [_tool_use_response("list_locations", {}) for _ in range(10)]
    client = _FakeClient(responses)

    result = engine.run_turn(client, adapter, history=[], user_message="loop forever")

    assert client.messages.calls == 3
    assert "tool-call limit" in result["text"]


def test_run_turn_includes_prior_history_in_the_messages_payload(adapter):
    captured_messages = []

    class _CapturingMessages(_FakeMessages):
        def stream(self, **kwargs):
            captured_messages.append(kwargs["messages"])
            return super().stream(**kwargs)

    client = _FakeClient([])
    client.messages = _CapturingMessages([_text_only_response("sure")])

    history = [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello!"}]
    engine.run_turn(client, adapter, history=history, user_message="follow-up question")

    first_call_messages = captured_messages[0]
    assert first_call_messages[0] == {"role": "user", "content": "hi"}
    assert first_call_messages[1] == {"role": "assistant", "content": "hello!"}
    assert first_call_messages[2] == {"role": "user", "content": "follow-up question"}


def test_run_turn_passes_system_prompt_and_tool_schemas(adapter):
    captured_kwargs = []

    class _CapturingMessages(_FakeMessages):
        def stream(self, **kwargs):
            captured_kwargs.append(kwargs)
            return super().stream(**kwargs)

    client = _FakeClient([])
    client.messages = _CapturingMessages([_text_only_response("ok")])

    engine.run_turn(client, adapter, history=[], user_message="hi")

    assert "propose" in captured_kwargs[0]["system"].lower()
    assert any(t["name"] == "list_locations" for t in captured_kwargs[0]["tools"])
