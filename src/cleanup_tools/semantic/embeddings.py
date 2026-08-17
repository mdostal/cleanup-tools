"""Local text embedding -- an ABC + factory pattern mirroring
``adapters.base.OSAdapter``'s own shape.

**Apple-native only, phase 1** (see
``.pHive/epics/document-topic-clustering/docs/design-discussion.md`` §2.3 for
the full reasoning): ``AppleTextEmbedder`` wraps the NaturalLanguage
framework's ``NLEmbedding`` sentence-embedding API via PyObjC -- zero
bundled model weights, zero network calls, the model ships with the OS.

``get_embedder()`` raises ``NotImplementedError`` on any non-macOS adapter,
matching ``OSAdapter.set_screenshot_save_location``'s existing precedent
(an explicit, documented platform gap rather than a silent degradation).

**A real finding from this module's own implementation spike, not an
assumption**: the design discussion originally named ``NLContextualEmbedding``
(NaturalLanguage's newer, BERT-like embedder) as the target API. Empirically,
that class's constructors (``initWithLanguage:``, `embeddingModelsForLanguage:`)
are not reachable through the installed PyObjC binding (12.2.2) -- neither
selector exists on the bridged class, most likely because that specific
initializer surface is Swift-only and was never exposed to the classic
Objective-C runtime PyObjC bridges against. ``NLEmbedding``'s
``initSentenceEmbeddingWithLocale:`` -- older, always-ObjC-bridged -- is the
real, working alternative used here instead: confirmed via direct testing to
return deterministic 512-dimensional vectors with sensible similarity
behavior (self-similarity ~1.0, unrelated-sentence similarity ~0.11). The
design goal (a real on-device sentence embedder, zero network, zero bundle
cost) is fully met either way -- this is an implementation-level
substitution, not a scope change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..adapters.base import OSAdapter
from ..adapters.macos import MacOSAdapter

DEFAULT_LOCALE_IDENTIFIER = "en_US"


class TextEmbedder(ABC):
    """Produces a fixed-length embedding vector for a piece of text."""

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Return a real embedding vector for ``text``.

        Implementations never return ``None``/empty on valid, non-empty
        input -- they raise if the underlying platform API can't produce a
        vector (e.g. an unsupported locale at construction time), since a
        silently-empty vector would be a much worse failure mode for
        clustering math than a loud error.
        """
        raise NotImplementedError


class AppleTextEmbedder(TextEmbedder):
    """``NLEmbedding``-backed sentence embedder -- see this module's docstring."""

    def __init__(self, locale_identifier: str = DEFAULT_LOCALE_IDENTIFIER) -> None:
        import NaturalLanguage
        from Foundation import NSLocale

        locale = NSLocale.localeWithLocaleIdentifier_(locale_identifier)
        embedding = NaturalLanguage.NLEmbedding.alloc().initSentenceEmbeddingWithLocale_(locale)
        if embedding is None:
            raise RuntimeError(
                f"NLEmbedding has no sentence embedding available for locale {locale_identifier!r}"
            )
        self._embedding = embedding

    def embed(self, text: str) -> list[float]:
        vector = self._embedding.vectorForString_(text)
        if vector is None:
            raise RuntimeError("NLEmbedding returned no vector for the given text")
        return [float(x) for x in vector]


def get_embedder(adapter: OSAdapter) -> TextEmbedder:
    """Return the local text embedder for this platform.

    Raises ``NotImplementedError`` on any non-macOS adapter -- see this
    module's docstring and ``OSAdapter.set_screenshot_save_location``'s
    existing precedent for the same platform-gap pattern.
    """
    if not isinstance(adapter, MacOSAdapter):
        raise NotImplementedError(
            "Local text embedding is macOS-only in this phase (Apple NaturalLanguage "
            "framework via PyObjC) -- no cross-platform fallback exists yet. See "
            "document-topic-clustering's design discussion §2.3."
        )
    return AppleTextEmbedder()
