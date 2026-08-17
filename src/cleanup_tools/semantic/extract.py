"""Local text extraction for the document-topic-clustering pipeline.

**Apple-native only, phase 1** -- see ``embeddings.py``'s module docstring and
``.pHive/epics/document-topic-clustering/docs/design-discussion.md`` §2.3 for
the full dependency-footprint reasoning. ``extract_text()`` gates on macOS for
the WHOLE function (including the plain ``.txt``/``.md`` path, which itself
needs no PyObjC at all) rather than partially supporting some file types on
Arch and not others -- a single clean platform boundary, matching this epic's
explicit "the whole feature is macOS-only in phase 1" scoping rather than a
confusing partial-support story.

Three real extraction paths, confirmed working against real files during this
story's implementation spike (not assumed):

- ``.pdf`` -- PDFKit's ``PDFDocument.string()`` (the real text layer), with a
  Vision-OCR-per-page fallback when that layer is empty/near-empty (a scanned
  document with no embedded text).
- Image files -- Vision's ``VNRecognizeTextRequest`` directly.
- ``.txt``/``.md`` -- a plain read, no framework call needed.

``extract_text()`` never raises on a file it can't meaningfully handle
(unsupported type, corrupt file, OCR failure) -- it returns ``None``,
mirroring ``queue.build_plan_snapshot``'s own "never raise, return a fallback"
contract for the same class of "can't do anything useful with this real but
unusual file" situation.
"""

from __future__ import annotations

from pathlib import Path

from ..adapters.base import OSAdapter
from ..adapters.macos import MacOSAdapter

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".heic", ".webp"}
PLAIN_TEXT_EXTENSIONS = {".txt", ".md"}

# A PDF page's own text layer shorter than this (after stripping whitespace)
# is treated as "no real text layer" -- a scanned page with no embedded text
# typically returns an empty or near-empty string, versus a real typed page
# which is virtually always far longer than this. Deliberately conservative
# (a real page with just a short heading would still clear this) since a
# false "needs OCR" is cheap (OCR just re-derives the same short text) while
# a false "has real text" would silently skip OCR on a genuinely scanned page.
_MIN_REAL_TEXT_LAYER_CHARS = 20


def extract_text(path: str | Path, adapter: OSAdapter) -> str | None:
    """Extract text from ``path`` for embedding, or ``None`` if it can't.

    Raises ``NotImplementedError`` on any non-macOS adapter -- see this
    module's docstring for why the gate covers every file type uniformly,
    not just the ones that technically need a PyObjC call.
    """
    if not isinstance(adapter, MacOSAdapter):
        raise NotImplementedError(
            "Local text extraction is macOS-only in this phase (PDFKit/Vision via "
            "PyObjC) -- no cross-platform fallback exists yet. See "
            "document-topic-clustering's design discussion §2.3."
        )

    path = Path(path)
    suffix = path.suffix.lower()

    try:
        if suffix in PLAIN_TEXT_EXTENSIONS:
            return _extract_plain_text(path)
        if suffix == ".pdf":
            return _extract_pdf_text(path)
        if suffix in IMAGE_EXTENSIONS:
            return _extract_image_text(path)
    except Exception:
        # Real but unusual files (corrupt, unreadable, OCR failure, ...)
        # never crash the pipeline -- see this module's docstring.
        return None

    return None


def _extract_plain_text(path: Path) -> str | None:
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return None
    return text if text.strip() else None


def _extract_pdf_text(path: Path) -> str | None:
    import Quartz
    from Foundation import NSURL

    url = NSURL.fileURLWithPath_(str(path))
    doc = Quartz.PDFDocument.alloc().initWithURL_(url)
    if doc is None:
        return None

    text = str(doc.string() or "")
    if len(text.strip()) >= _MIN_REAL_TEXT_LAYER_CHARS:
        return text

    # Empty/near-empty text layer -- likely a scanned document. Fall back to
    # OCR, one page at a time, concatenating whatever real text each page
    # yields.
    page_count = doc.pageCount()
    ocr_parts: list[str] = []
    for index in range(page_count):
        page = doc.pageAtIndex_(index)
        if page is None:
            continue
        page_image = _render_pdf_page_to_cgimage(page)
        if page_image is None:
            continue
        page_text = _ocr_cgimage(page_image)
        if page_text:
            ocr_parts.append(page_text)

    combined = "\n".join(ocr_parts)
    return combined if combined.strip() else None


def _render_pdf_page_to_cgimage(page):
    import Quartz

    bounds = page.boundsForBox_(Quartz.kPDFDisplayBoxMediaBox)
    scale = 2.0  # supersample for better OCR accuracy on small text
    size = (bounds.size.width * scale, bounds.size.height * scale)
    ns_image = page.thumbnailOfSize_forBox_(size, Quartz.kPDFDisplayBoxMediaBox)
    if ns_image is None:
        return None
    return ns_image.CGImageForProposedRect_context_hints_(None, None, None)[0]


def _extract_image_text(path: Path) -> str | None:
    import Quartz
    from Foundation import NSURL

    url = NSURL.fileURLWithPath_(str(path))
    image_source = Quartz.CGImageSourceCreateWithURL(url, None)
    if image_source is None:
        return None
    cg_image = Quartz.CGImageSourceCreateImageAtIndex(image_source, 0, None)
    if cg_image is None:
        return None
    return _ocr_cgimage(cg_image)


def _ocr_cgimage(cg_image) -> str | None:
    import Vision

    results: dict = {}

    def handler(request, error):
        if error is not None:
            results["error"] = error
            return
        texts = []
        for observation in request.results():
            candidates = observation.topCandidates_(1)
            if candidates:
                texts.append(str(candidates[0].string()))
        results["text"] = "\n".join(texts)

    request = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(handler)
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)

    req_handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, None)
    success, _error = req_handler.performRequests_error_([request], None)
    if not success:
        return None
    return results.get("text") or None
