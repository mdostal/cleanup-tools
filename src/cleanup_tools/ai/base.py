"""AI-provider interface for proposing cleanup buckets.

This is intentionally narrow: "given a filename + metadata, propose a
bucket" -- not a general chat interface. It is the ONE sanctioned exception
to this project's "no network, no telemetry" hard rule (see
``.pHive/CONTEXT.md`` Conventions), and only because it is an explicit,
user-triggered call -- never ambient.

This module is standalone. It is NOT wired into the approval queue, the UI,
or any CLI command yet -- that is a separate, future story. Nothing here is
imported by ``queue.py``, ``ui/``, or ``commands/``.

``ProposalResult`` deliberately returns a typed result rather than raising:
callers (eventually the queue/UI layer) need to branch on *why* a proposal
didn't come back, and Python exceptions are a poor fit for "the model
declined to authenticate" vs. "the model responded but not in the expected
shape" -- both are routine, expected outcomes for a network-backed call
touching a third-party service, not exceptional control flow.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

# "error" is a catch-all for provider failures that aren't one of the four
# named failure modes below (e.g. a 400/404/5xx from the API, a raw
# connection failure). It exists so AnthropicProvider never has to let an
# unmapped SDK exception escape -- every code path ends in some ProposalResult.
ProposalKind = Literal[
    "success",
    "auth_failure",
    "rate_limited",
    "timeout",
    "unparseable",
    "error",
]


@dataclass(frozen=True)
class ProposalResult:
    """The outcome of one :meth:`AIProvider.propose_bucket` call.

    ``bucket``, ``confidence``, and ``rationale`` are only populated when
    ``kind == "success"``. Every other field is optional context for
    diagnostics/logging, never something a caller should have to inspect to
    know what happened -- branch on ``kind``.
    """

    kind: ProposalKind
    bucket: str | None = None
    confidence: float | None = None
    rationale: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "success" and not self.bucket:
            raise ValueError("ProposalResult(kind='success') requires a non-empty bucket")


class AIProvider(ABC):
    """Interface for AI providers that can propose a cleanup bucket for a file.

    Structural precedent: :class:`cleanup_tools.adapters.base.OSAdapter` (an
    ABC + factory pattern). The error/retry/security model here is
    different on purpose -- this deals with network calls and secrets,
    OSAdapter does not.
    """

    @abstractmethod
    def propose_bucket(self, filename: str, metadata: dict) -> ProposalResult:
        """Propose a cleanup bucket for ``filename`` given ``metadata``.

        Must never raise on a routine provider failure (bad credentials,
        rate limiting, timeout, an unparseable response) -- those all come
        back as a :class:`ProposalResult` with the matching ``kind``. This
        method is explicitly user-triggered; implementations must not be
        called on any ambient/background schedule.
        """
        raise NotImplementedError
