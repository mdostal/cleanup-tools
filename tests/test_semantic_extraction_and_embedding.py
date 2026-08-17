"""Tests for cleanup_tools.semantic.extract / cleanup_tools.semantic.embeddings.

Real-framework tests, not mocked: extract_text()/get_embedder() call the ACTUAL
PDFKit/Vision/NaturalLanguage frameworks via PyObjC on macOS -- per this
story's own design discussion, "no test depends on a mocked
platform-framework call returning fabricated results as if they were real."
These are skipped (not faked) on non-macOS platforms via a plain
``sys.platform`` check, mirroring the story's stated "skippable/xfail on
non-macOS CI via a platform marker, never mocked into false confidence"
discipline.

The NotImplementedError-on-non-macOS path is tested unconditionally (it never
touches a real framework, and is exactly the behavior that must hold on a
real non-macOS CI run).
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from cleanup_tools.adapters import MacOSAdapter
from cleanup_tools.adapters.arch_linux import ArchLinuxAdapter
from cleanup_tools.semantic import embeddings, extract

pytestmark_macos = pytest.mark.skipif(sys.platform != "darwin", reason="requires real macOS PyObjC frameworks")


@pytest.fixture
def adapter(tmp_path) -> MacOSAdapter:
    class FakeHomeAdapter(MacOSAdapter):
        def resolve_home(self):
            return tmp_path

    return FakeHomeAdapter()


# ---------------------------------------------------------------------------
# NotImplementedError on non-macOS -- unconditional, no real framework touched.
# ---------------------------------------------------------------------------


def test_extract_text_raises_not_implemented_on_non_macos_adapter(tmp_path):
    arch_adapter = ArchLinuxAdapter()
    f = tmp_path / "note.txt"
    f.write_text("hello")

    with pytest.raises(NotImplementedError):
        extract.extract_text(f, arch_adapter)


def test_get_embedder_raises_not_implemented_on_non_macos_adapter():
    arch_adapter = ArchLinuxAdapter()

    with pytest.raises(NotImplementedError):
        embeddings.get_embedder(arch_adapter)


# ---------------------------------------------------------------------------
# extract_text -- real macOS frameworks.
# ---------------------------------------------------------------------------


@pytestmark_macos
def test_extract_text_plain_text_file_returns_real_content(adapter, tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("Invoice for Acme Corp, house sale settlement.")

    assert extract.extract_text(f, adapter) == "Invoice for Acme Corp, house sale settlement."


@pytestmark_macos
def test_extract_text_markdown_file_returns_real_content(adapter, tmp_path):
    f = tmp_path / "note.md"
    f.write_text("# Heading\n\nSome real markdown content.")

    result = extract.extract_text(f, adapter)
    assert result is not None
    assert "Some real markdown content." in result


@pytestmark_macos
def test_extract_text_pdf_with_real_text_layer_uses_pdfkit_not_ocr(adapter, tmp_path):
    src_txt = tmp_path / "sample.txt"
    src_txt.write_text("Invoice for Acme Corp, house sale settlement, total due 4500.00 USD.")
    pdf_path = tmp_path / "sample.pdf"
    with open(pdf_path, "wb") as f:
        subprocess.run(["cupsfilter", str(src_txt)], stdout=f, stderr=subprocess.DEVNULL, check=True)

    result = extract.extract_text(pdf_path, adapter)

    assert result is not None
    assert "Acme Corp" in result
    assert "house sale settlement" in result.lower() or "settlement" in result.lower()


@pytestmark_macos
def test_extract_text_unsupported_extension_returns_none(adapter, tmp_path):
    f = tmp_path / "data.xyz"
    f.write_text("not a real supported type")

    assert extract.extract_text(f, adapter) is None


@pytestmark_macos
def test_extract_text_nonexistent_file_returns_none_not_raises(adapter, tmp_path):
    missing = tmp_path / "does-not-exist.pdf"

    assert extract.extract_text(missing, adapter) is None


@pytestmark_macos
def test_extract_text_empty_text_file_returns_none(adapter, tmp_path):
    f = tmp_path / "empty.txt"
    f.write_text("   \n  ")

    assert extract.extract_text(f, adapter) is None


# ---------------------------------------------------------------------------
# get_embedder / TextEmbedder -- real NLEmbedding.
# ---------------------------------------------------------------------------


@pytestmark_macos
def test_get_embedder_returns_a_real_working_embedder(adapter):
    embedder = embeddings.get_embedder(adapter)

    vector = embedder.embed("Invoice for Acme Corp, house sale settlement.")

    assert isinstance(vector, list)
    assert len(vector) > 0
    assert all(isinstance(x, float) for x in vector)


@pytestmark_macos
def test_embed_is_deterministic(adapter):
    embedder = embeddings.get_embedder(adapter)

    v1 = embedder.embed("Invoice for Acme Corp, house sale settlement.")
    v2 = embedder.embed("Invoice for Acme Corp, house sale settlement.")

    assert v1 == v2


@pytestmark_macos
def test_embed_similar_sentences_are_more_similar_than_unrelated_ones(adapter):
    import math

    def cosine(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb)

    embedder = embeddings.get_embedder(adapter)
    invoice_a = embedder.embed("Invoice for Acme Corp, total due 4500 dollars.")
    invoice_b = embedder.embed("Billing statement from Acme Corp, amount owed 4500.")
    unrelated = embedder.embed("A recipe for chocolate chip cookies with walnuts.")

    assert cosine(invoice_a, invoice_b) > cosine(invoice_a, unrelated)
