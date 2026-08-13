"""Anthropic-backed implementation of :class:`AIProvider`.

SDK telemetry research (2026-08-10/11, against ``anthropic`` 0.121.0
installed fresh into a throwaway venv and read directly from
``site-packages/anthropic/``)
-----------------------------------------------------------------------
This project has a hard "no ambient network calls / no telemetry" rule, so
before writing any request code we checked what the SDK itself does beyond
the one API call this module explicitly makes:

* ``_base_client.py`` / ``_client.py`` -- no update-checks, no analytics
  pings, no "phone home" of any kind. Grepping the entire package for
  telemetry/analytics/tracking vendor names (statsig, posthog, segment,
  mixpanel, amplitude) and for update-check patterns (latest-version
  lookups, PyPI/npm registry calls) returns nothing.
* The only "telemetry" the package contains is ``lib/_stainless_helpers.py``,
  which defines the ``x-stainless-helper`` / ``x-stainless-helper-method``
  headers. These are metadata attached to *the request the caller already
  made* (naming which SDK helper -- e.g. the tool runner -- produced it),
  not a separate outbound call. Every SDK from every vendor sends a
  User-Agent-equivalent; this is that, scoped to which SDK code path was
  used. ``anthropic._base_client.py`` also sets ``User-Agent`` and several
  ``x-stainless-*`` diagnostic headers (SDK version, platform, Python
  version, retry count) -- again, all riding on the one request, never a
  standalone call.
* No background threads, no ``atexit`` hooks, no ``asyncio.create_task``
  anywhere in the package outside of type definitions for an unrelated
  "scheduled deployment" API feature (Managed Agents cron scheduling, which
  this module does not use and which only fires from an explicit API call
  the caller makes, not from importing or constructing the client).
* Credential resolution (``Anthropic.__init__``) can, if no explicit
  ``api_key=``/``auth_token=`` is passed and the env vars are unset, fall
  back to reading an OAuth profile from disk
  (``~/.config/anthropic/...``) or Workload Identity Federation env vars.
  That's a local filesystem/env read, not a network call by itself -- but
  it means an un-parameterized ``anthropic.Anthropic()`` can silently pick
  up credentials this project's own ``get_provider()`` factory doesn't
  know about. We avoid that entirely below by *always* constructing the
  client with an explicit ``api_key=``, so ``ai/__init__.py`` stays the
  single source of truth for which credential is used.

Conclusion: no default telemetry/analytics exists to disable. Nothing here
needed a settings flag turned off. The one deliberate hardening we still
apply is unrelated to telemetry: we pass ``max_retries=0`` to the SDK
client and implement our own explicit, exactly-bounded retry loop (see
below), so "at most one extra network call" is legible directly in this
file's code rather than living inside the SDK's own (harmless, but
opaque-from-here) exponential-backoff retry logic.
"""

from __future__ import annotations

from typing import Any

import anthropic

from .base import AIProvider, ProposalResult

DEFAULT_MODEL = "claude-haiku-4-5"
"""Cheap/fast model, chosen deliberately for this task.

Classifying one file's bucket from a filename + a little metadata is a
narrow, high-volume, low-stakes classification task -- not the kind of
open-ended reasoning that calls for a frontier model. ``model=`` is
overridable at construction time for callers who want a stronger model.
"""

DEFAULT_MAX_TOKENS = 300
DEFAULT_TIMEOUT_SECONDS = 20.0

_PROPOSE_BUCKET_TOOL: dict[str, Any] = {
    "name": "propose_bucket",
    "description": (
        "Propose which cleanup bucket a file belongs in, given its filename and "
        "metadata. Call this exactly once with your best judgment."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "bucket": {
                "type": "string",
                "description": (
                    "Short bucket label, e.g. 'screenshots', 'invoices', "
                    "'installers', 'trash'."
                ),
            },
            "confidence": {
                "type": "number",
                "description": "Confidence in this proposal, from 0.0 (guessing) to 1.0 (certain).",
            },
            "rationale": {
                "type": "string",
                "description": "One-sentence explanation for the proposed bucket.",
            },
        },
        "required": ["bucket", "confidence", "rationale"],
    },
}


def _build_prompt(filename: str, metadata: dict) -> str:
    lines = [
        "You are helping a personal file-cleanup tool decide what to do with an "
        "ambiguous file. You will never see the file's contents -- only the "
        "filename and metadata below.",
        f"Filename: {filename}",
    ]
    if metadata:
        lines.append("Metadata:")
        for key in sorted(metadata):
            lines.append(f"  {key}: {metadata[key]}")
    lines.append(
        "Call the propose_bucket tool with your best-guess bucket, a confidence "
        "between 0 and 1, and a one-sentence rationale."
    )
    return "\n".join(lines)


def _extract_tool_input(response: Any) -> Any:
    """Return the ``propose_bucket`` tool_use block's ``input``, or ``None``."""
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "propose_bucket":
            return getattr(block, "input", None)
    return None


def _parse_response(response: Any) -> ProposalResult:
    """Turn a raw SDK response into a :class:`ProposalResult`.

    Degrades to ``kind="unparseable"`` for every shape the model might
    return that isn't the expected tool call -- never raises.

    The SDK client defaults to ``_strict_response_validation=False``, so a
    malformed/non-conformant response is not guaranteed to have the shape
    the rest of this function assumes -- ``response.content`` might not be
    a list (or even iterable), and a
    tool-supplied ``confidence`` might not be a value ``float()`` can
    handle (e.g. an absurdly large int from a malformed tool call). The
    whole body below is therefore wrapped in a single
    try/except that catches exactly the exceptions that mean "this
    response's shape doesn't match what we expect" -- ``TypeError``
    (non-iterable/wrong-type access), ``AttributeError`` (missing
    attribute on an unexpected object), ``ValueError``, and
    ``OverflowError`` (``float()`` on a huge int) -- and converts them to
    the same ``kind="unparseable"`` degradation used for every other
    malformed-response case here. Anything else is a real bug and is left
    to propagate.
    """
    try:
        if getattr(response, "stop_reason", None) == "refusal":
            return ProposalResult(kind="unparseable", error="model declined to respond (stop_reason=refusal)")

        tool_input = _extract_tool_input(response)
        if not isinstance(tool_input, dict):
            return ProposalResult(kind="unparseable", error="no propose_bucket tool_use block in response")

        bucket = tool_input.get("bucket")
        confidence = tool_input.get("confidence")
        rationale = tool_input.get("rationale")

        if not isinstance(bucket, str) or not bucket.strip():
            return ProposalResult(kind="unparseable", error=f"missing or invalid 'bucket': {bucket!r}")

        # bool is a subclass of int; reject it explicitly so `confidence: true`
        # doesn't silently parse as 1.0.
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            return ProposalResult(kind="unparseable", error=f"missing or invalid 'confidence': {confidence!r}")
        confidence = float(confidence)
        if not (0.0 <= confidence <= 1.0):
            return ProposalResult(kind="unparseable", error=f"'confidence' out of range [0, 1]: {confidence!r}")

        if rationale is not None and not isinstance(rationale, str):
            return ProposalResult(kind="unparseable", error=f"invalid 'rationale': {rationale!r}")

        return ProposalResult(
            kind="success",
            bucket=bucket,
            confidence=confidence,
            rationale=rationale or "",
        )
    except (TypeError, AttributeError, ValueError, OverflowError) as exc:
        return ProposalResult(kind="unparseable", error=f"malformed response shape: {exc!r}")


class AnthropicProvider(AIProvider):
    """Proposes cleanup buckets via the Anthropic Messages API.

    Retry policy: **at most one retry, only on timeout or rate-limit.**
    Implemented as a plain ``for attempt in range(2)`` loop rather than a
    backoff library specifically so "at most one extra network call" is
    obvious from reading this file -- ``range(2)`` is a hard structural cap
    on total attempts, independent of which exception fires on each pass.

    Any other SDK exception (bad auth, a 4xx/5xx we don't special-case, a
    raw connection failure) is caught and translated to a
    :class:`ProposalResult` immediately, with no retry -- never allowed to
    escape to the caller.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = DEFAULT_MODEL,
        client: Any | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Construct the provider.

        ``client`` is an injection point for tests (or callers with their
        own pre-configured ``anthropic.Anthropic`` instance) -- when given,
        ``api_key``/``timeout`` are ignored and no SDK client is
        constructed here. When omitted, ``api_key`` is required and a real
        ``anthropic.Anthropic`` client is built with ``max_retries=0`` (see
        the module docstring for why: this class owns retries, not the
        SDK) and an explicit ``timeout``.
        """
        self._model = model
        if client is not None:
            self._client = client
            return
        if not api_key:
            raise ValueError("AnthropicProvider requires api_key (or an injected client)")
        self._client = anthropic.Anthropic(
            api_key=api_key,
            max_retries=0,
            timeout=timeout,
        )

    def propose_bucket(self, filename: str, metadata: dict) -> ProposalResult:
        messages = [{"role": "user", "content": _build_prompt(filename, metadata)}]

        # Exactly two passes max: attempt 0, and (only on timeout/rate-limit)
        # one retry at attempt 1. No other exception continues the loop.
        for attempt in range(2):
            try:
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=DEFAULT_MAX_TOKENS,
                    tools=[_PROPOSE_BUCKET_TOOL],
                    tool_choice={"type": "tool", "name": "propose_bucket"},
                    messages=messages,
                )
            except anthropic.APITimeoutError as exc:
                if attempt == 0:
                    continue
                return ProposalResult(kind="timeout", error=str(exc))
            except anthropic.RateLimitError as exc:
                if attempt == 0:
                    continue
                return ProposalResult(kind="rate_limited", error=str(exc))
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
                # Never retried: retrying with the same bad credential can't
                # succeed.
                return ProposalResult(kind="auth_failure", error=str(exc))
            except anthropic.APIError as exc:
                # Catch-all for every other SDK exception (APIConnectionError,
                # other APIStatusError subclasses like BadRequestError /
                # InternalServerError, etc.) -- translated, never re-raised,
                # never retried.
                return ProposalResult(kind="error", error=str(exc))
            else:
                return _parse_response(response)

        # Unreachable (both loop iterations either `continue` once then hit
        # the `return` on the second pass, or `return` immediately) but
        # keeps this function total rather than implicitly returning None.
        return ProposalResult(kind="timeout", error="retry loop exhausted with no response")
