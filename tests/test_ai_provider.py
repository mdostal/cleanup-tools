"""Tests for cleanup_tools.ai: the standalone AI-provider interface.

Zero real network calls are permitted anywhere in this file. Enforced two
ways:

1. Structurally -- every ``AnthropicProvider`` under test is constructed
   with an injected fake client (see ``_FakeClient`` below), so the real
   ``anthropic.Anthropic`` HTTP machinery is never invoked by
   ``propose_bucket``.
2. Defensively -- the autouse ``block_real_network`` fixture monkeypatches
   ``socket.socket.connect`` to raise loudly. This also covers the
   ``get_provider()`` tests, which *do* construct a real
   ``anthropic.Anthropic`` client (construction alone performs no I/O), as a
   belt-and-suspenders guard in case that ever changes.

``_FakeClient`` additionally fails loudly (``IndexError`` from popping an
empty list) if ``messages.create`` is called more times than the test
queued responses for -- this is what makes the exact-retry-count assertions
below trustworthy: a bug that retried an extra time would blow up the test
with an unrelated-looking ``IndexError``, not silently pass.
"""

from __future__ import annotations

import stat
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from cleanup_tools.ai import (
    AnthropicProvider,
    CredentialsError,
    ProposalResult,
    get_provider,
)
from cleanup_tools.ai.anthropic_provider import _parse_response


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    """Fail loudly if anything in this file attempts a real socket connection."""

    def _blow_up(*_args, **_kwargs):
        raise RuntimeError("Real network call attempted in test_ai_provider.py -- must be mocked")

    monkeypatch.setattr("socket.socket.connect", _blow_up)


class _FakeMessages:
    """Dispenses queued responses/exceptions from ``messages.create``.

    Raises ``IndexError`` (an unmistakable test failure) if called more
    times than there are queued items -- see module docstring.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


def _tool_use_response(
    bucket="screenshots",
    confidence=0.9,
    rationale="filename matches screenshot naming convention",
    stop_reason="tool_use",
):
    block = SimpleNamespace(
        type="tool_use",
        name="propose_bucket",
        input={"bucket": bucket, "confidence": confidence, "rationale": rationale},
    )
    return SimpleNamespace(content=[block], stop_reason=stop_reason)


def _text_only_response(text="I'm not sure what to do with this file."):
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(content=[block], stop_reason="end_turn")


def _refusal_response():
    return SimpleNamespace(content=[], stop_reason="refusal")


def _fake_httpx_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _fake_httpx_response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code, request=_fake_httpx_request())


def _auth_error() -> anthropic.AuthenticationError:
    return anthropic.AuthenticationError("invalid x-api-key", response=_fake_httpx_response(401), body=None)


def _permission_error() -> anthropic.PermissionDeniedError:
    return anthropic.PermissionDeniedError(
        "key lacks permission", response=_fake_httpx_response(403), body=None
    )


def _rate_limit_error() -> anthropic.RateLimitError:
    return anthropic.RateLimitError("rate limited", response=_fake_httpx_response(429), body=None)


def _timeout_error() -> anthropic.APITimeoutError:
    return anthropic.APITimeoutError(request=_fake_httpx_request())


def _bad_request_error() -> anthropic.BadRequestError:
    return anthropic.BadRequestError("malformed request", response=_fake_httpx_response(400), body=None)


def _connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=_fake_httpx_request())


# ---------------------------------------------------------------------------
# ProposalResult
# ---------------------------------------------------------------------------


def test_proposal_result_success_requires_bucket():
    with pytest.raises(ValueError):
        ProposalResult(kind="success", bucket=None)


def test_proposal_result_non_success_kinds_dont_require_bucket():
    # Every non-success kind must construct fine with no bucket/confidence.
    for kind in ("auth_failure", "rate_limited", "timeout", "unparseable", "error"):
        result = ProposalResult(kind=kind, error="details")
        assert result.bucket is None
        assert result.kind == kind


# ---------------------------------------------------------------------------
# AnthropicProvider.propose_bucket -- one outcome per ProposalResult.kind
# ---------------------------------------------------------------------------


def test_success():
    client = _FakeClient([_tool_use_response(bucket="screenshots", confidence=0.87)])
    provider = AnthropicProvider(client=client)

    result = provider.propose_bucket("Screenshot 2026-08-10 at 9.14.02 AM.png", {"ext": "png"})

    assert result.kind == "success"
    assert result.bucket == "screenshots"
    assert result.confidence == 0.87
    assert result.rationale
    assert client.messages.calls == 1


def test_auth_failure_not_retried():
    client = _FakeClient([_auth_error()])
    provider = AnthropicProvider(client=client)

    result = provider.propose_bucket("file.dat", {})

    assert result.kind == "auth_failure"
    assert result.error
    assert client.messages.calls == 1


def test_permission_denied_maps_to_auth_failure():
    client = _FakeClient([_permission_error()])
    provider = AnthropicProvider(client=client)

    result = provider.propose_bucket("file.dat", {})

    assert result.kind == "auth_failure"
    assert client.messages.calls == 1


def test_rate_limited_after_exhausting_retry():
    client = _FakeClient([_rate_limit_error(), _rate_limit_error()])
    provider = AnthropicProvider(client=client)

    result = provider.propose_bucket("file.dat", {})

    assert result.kind == "rate_limited"
    assert result.error
    assert client.messages.calls == 2


def test_timeout_after_exhausting_retry():
    client = _FakeClient([_timeout_error(), _timeout_error()])
    provider = AnthropicProvider(client=client)

    result = provider.propose_bucket("file.dat", {})

    assert result.kind == "timeout"
    assert result.error
    assert client.messages.calls == 2


def test_unparseable_no_tool_use_block():
    client = _FakeClient([_text_only_response()])
    provider = AnthropicProvider(client=client)

    result = provider.propose_bucket("file.dat", {})

    assert result.kind == "unparseable"
    assert result.error


def test_unparseable_refusal_stop_reason():
    client = _FakeClient([_refusal_response()])
    provider = AnthropicProvider(client=client)

    result = provider.propose_bucket("file.dat", {})

    assert result.kind == "unparseable"


def test_unparseable_bad_confidence_type():
    client = _FakeClient([_tool_use_response(confidence="very confident")])
    provider = AnthropicProvider(client=client)

    result = provider.propose_bucket("file.dat", {})

    assert result.kind == "unparseable"


def test_unparseable_confidence_out_of_range():
    client = _FakeClient([_tool_use_response(confidence=1.5)])
    provider = AnthropicProvider(client=client)

    result = provider.propose_bucket("file.dat", {})

    assert result.kind == "unparseable"


def test_unparseable_missing_bucket():
    block = SimpleNamespace(
        type="tool_use",
        name="propose_bucket",
        input={"confidence": 0.5, "rationale": "no bucket field"},
    )
    response = SimpleNamespace(content=[block], stop_reason="tool_use")
    client = _FakeClient([response])
    provider = AnthropicProvider(client=client)

    result = provider.propose_bucket("file.dat", {})

    assert result.kind == "unparseable"


def test_generic_provider_error_not_retried():
    """A non-timeout, non-rate-limit, non-auth SDK error (e.g. 400) maps to
    'error' and, per the retry policy, is never retried."""
    client = _FakeClient([_bad_request_error()])
    provider = AnthropicProvider(client=client)

    result = provider.propose_bucket("file.dat", {})

    assert result.kind == "error"
    assert client.messages.calls == 1


def test_connection_error_not_retried():
    client = _FakeClient([_connection_error()])
    provider = AnthropicProvider(client=client)

    result = provider.propose_bucket("file.dat", {})

    assert result.kind == "error"
    assert client.messages.calls == 1


# ---------------------------------------------------------------------------
# Exact retry-count assertions
# ---------------------------------------------------------------------------


def test_timeout_then_success_retries_exactly_once():
    client = _FakeClient([_timeout_error(), _tool_use_response(bucket="pdfs")])
    provider = AnthropicProvider(client=client)

    result = provider.propose_bucket("statement.pdf", {"ext": "pdf"})

    assert result.kind == "success"
    assert result.bucket == "pdfs"
    assert client.messages.calls == 2  # one original attempt + exactly one retry


def test_timeout_then_timeout_makes_exactly_two_attempts():
    # Only two responses queued: a third `create()` call would pop from an
    # empty list and raise IndexError, failing this test loudly if the
    # retry loop ever attempted a third call.
    client = _FakeClient([_timeout_error(), _timeout_error()])
    provider = AnthropicProvider(client=client)

    result = provider.propose_bucket("file.dat", {})

    assert result.kind == "timeout"
    assert client.messages.calls == 2


def test_rate_limited_then_success_retries_exactly_once():
    client = _FakeClient([_rate_limit_error(), _tool_use_response(bucket="videos")])
    provider = AnthropicProvider(client=client)

    result = provider.propose_bucket("clip.mov", {"ext": "mov"})

    assert result.kind == "success"
    assert result.bucket == "videos"
    assert client.messages.calls == 2


def test_auth_failure_never_retries_even_once():
    # Only one response queued -- a retry would raise IndexError.
    client = _FakeClient([_auth_error()])
    provider = AnthropicProvider(client=client)

    result = provider.propose_bucket("file.dat", {})

    assert result.kind == "auth_failure"
    assert client.messages.calls == 1


def test_unparseable_response_never_retries():
    # Only one response queued -- a retry would raise IndexError.
    client = _FakeClient([_text_only_response()])
    provider = AnthropicProvider(client=client)

    result = provider.propose_bucket("file.dat", {})

    assert result.kind == "unparseable"
    assert client.messages.calls == 1


def test_unparseable_non_iterable_content_never_raises():
    """``response.content`` a non-iterable truthy value (e.g. an int) must
    degrade to ``unparseable``, not raise a raw ``TypeError``, since the
    SDK client's non-strict response validation doesn't guarantee
    ``content`` is a proper list -- see ``_parse_response``'s docstring."""
    client = _FakeClient([SimpleNamespace(content=42, stop_reason="tool_use")])
    provider = AnthropicProvider(client=client)

    result = provider.propose_bucket("file.dat", {})

    assert result.kind == "unparseable"
    assert result.error
    assert client.messages.calls == 1


def test_unparseable_absurdly_large_confidence_never_raises():
    """A ``confidence`` so large that ``float()`` raises ``OverflowError``
    (e.g. a malformed tool call emitting a huge JSON integer) must degrade
    to ``unparseable``, not raise a raw ``OverflowError``."""
    client = _FakeClient([_tool_use_response(confidence=10**400)])
    provider = AnthropicProvider(client=client)

    result = provider.propose_bucket("file.dat", {})

    assert result.kind == "unparseable"
    assert result.error
    assert client.messages.calls == 1


# ---------------------------------------------------------------------------
# _parse_response unit coverage (direct, no client involved)
# ---------------------------------------------------------------------------


def test_parse_response_rationale_defaults_to_empty_string_when_none():
    block = SimpleNamespace(
        type="tool_use",
        name="propose_bucket",
        input={"bucket": "docs", "confidence": 0.4, "rationale": None},
    )
    response = SimpleNamespace(content=[block], stop_reason="tool_use")

    result = _parse_response(response)

    assert result.kind == "success"
    assert result.rationale == ""


# ---------------------------------------------------------------------------
# AnthropicProvider construction requires either api_key or an injected client
# ---------------------------------------------------------------------------


def test_provider_requires_api_key_when_no_client_injected():
    with pytest.raises(ValueError):
        AnthropicProvider(api_key=None)


# ---------------------------------------------------------------------------
# get_provider() -- key resolution order
# ---------------------------------------------------------------------------


def test_env_var_wins_when_set(tmp_path, monkeypatch):
    creds_path = tmp_path / "credentials"
    creds_path.write_text("file-key-should-not-be-used")
    creds_path.chmod(0o600)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")

    provider = get_provider(credentials_path=creds_path)

    assert provider._client.api_key == "env-key"


def test_empty_env_var_is_treated_as_unset(tmp_path, monkeypatch):
    creds_path = tmp_path / "credentials"
    creds_path.write_text("file-key")
    creds_path.chmod(0o600)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    provider = get_provider(credentials_path=creds_path)

    assert provider._client.api_key == "file-key"


def test_falls_back_to_credentials_file_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    creds_path = tmp_path / "credentials"
    creds_path.write_text("file-key\n")
    creds_path.chmod(0o600)

    provider = get_provider(credentials_path=creds_path)

    assert provider._client.api_key == "file-key"


def test_missing_env_and_missing_file_raises_credentials_error(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    missing_path = tmp_path / "credentials"

    with pytest.raises(CredentialsError):
        get_provider(credentials_path=missing_path)


def test_empty_credentials_file_raises_credentials_error(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    creds_path = tmp_path / "credentials"
    creds_path.write_text("   \n")
    creds_path.chmod(0o600)

    with pytest.raises(CredentialsError):
        get_provider(credentials_path=creds_path)


def test_model_override_is_passed_through(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    creds_path = tmp_path / "credentials"

    provider = get_provider(model="claude-opus-5", credentials_path=creds_path)

    assert provider._model == "claude-opus-5"


# ---------------------------------------------------------------------------
# Credentials-file permission enforcement
# ---------------------------------------------------------------------------


def test_loose_permissions_are_corrected_and_warned(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    creds_path = tmp_path / "credentials"
    creds_path.write_text("loose-key")
    creds_path.chmod(0o644)  # deliberately loose -- world/group readable

    with pytest.warns(UserWarning, match="corrected to 0o600"):
        provider = get_provider(credentials_path=creds_path)

    # The real behavior under test: the mode on disk was actually corrected,
    # not just that a warning fired.
    mode = stat.S_IMODE(creds_path.stat().st_mode)
    assert mode == 0o600
    assert provider._client.api_key == "loose-key"


def test_correct_permissions_do_not_warn(tmp_path, monkeypatch, recwarn):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    creds_path = tmp_path / "credentials"
    creds_path.write_text("key")
    creds_path.chmod(0o600)

    get_provider(credentials_path=creds_path)

    assert len(recwarn) == 0


@pytest.mark.parametrize("loose_mode", [0o644, 0o664, 0o777, 0o604])
def test_various_loose_permissions_all_get_corrected(tmp_path, monkeypatch, loose_mode):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    creds_path = tmp_path / "credentials"
    creds_path.write_text("key")
    creds_path.chmod(loose_mode)

    get_provider(credentials_path=creds_path)

    assert stat.S_IMODE(creds_path.stat().st_mode) == 0o600
