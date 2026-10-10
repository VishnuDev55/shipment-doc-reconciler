"""The printed text of a page, obtained without asking the model.

The whole product rests on one claim: "here is the line it came from". Nothing
so far checked that the line exists. This module is what makes the check
possible -- it recovers what a page actually says from a source the vision model
never touched.

Two sources, and only two:

  * **the PDF's own text layer.** A PDF exported from an ERP or a mail client
    carries real, positioned text, placed there by the software that made the
    document, not by anyone reading it. That makes it genuinely independent
    evidence about what the page says.
  * **a sidecar written when a synthetic fixture was generated.** Our fixture
    pages come from HTML rendered through xhtml2pdf, which produces a text
    layer; the rasteriser then throws it away to make a phone-photo-looking PNG.
    Keeping a copy costs nothing and gives the fixture set a text source.

Everything else -- a JPEG, a photo, a scanned image -- has no text source here,
and the honest answer for those is "cannot check", not "checked and passed".
No OCR is attempted: tesseract is a system package, and the container is
deliberately single-stage with no `apt-get` (see `app/ingest.py`), so an OCR
dependency would be a worse trade than the gap it closes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .ingest import PDF_SUFFIXES

# A page with fewer than this many characters of text is treated as having no
# text layer at all. Real vector PDFs produce hundreds; a scanned-image-in-a-PDF
# wrapper produces nothing but the odd stray glyph.
MIN_PAGE_CHARS = 8


@dataclass(frozen=True)
class PageText:
    """What one page says, and where that came from."""

    index: int                      # 1-based, matching PageImage.index
    lines: tuple[str, ...]          # as the text layer emits them, in reading order
    source: str                     # 'pdf_text_layer' | 'fixture_sidecar'

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def usable(self) -> bool:
        return len(self.text.strip()) >= MIN_PAGE_CHARS


def page_texts_from_pdf(raw: bytes, *, max_pages: int = 6) -> list[PageText]:
    """Per-page text of a PDF, or [] when it carries no text layer.

    A scanned PDF wrapped around page images is the common case for [], and
    returning an empty list rather than raising is what lets the caller say
    "not checkable" instead of failing somebody's upload.
    """
    import pypdfium2 as pdfium

    try:
        doc = pdfium.PdfDocument(raw)
    except Exception:                      # unreadable here is ingest's problem, not ours
        return []
    pages: list[PageText] = []
    try:
        for i in range(min(len(doc), max_pages)):
            try:
                body = doc[i].get_textpage().get_text_bounded()
            except Exception:
                body = ""
            lines = tuple(ln for ln in body.replace("\r\n", "\n").replace("\r", "\n")
                          .split("\n"))
            pages.append(PageText(index=i + 1, lines=lines, source="pdf_text_layer"))
    finally:
        doc.close()
    if not any(p.usable for p in pages):
        return []
    return pages


def page_texts_for(filename: str, raw: bytes, *, max_pages: int = 6) -> list[PageText]:
    """Text source for an upload, if one exists. Images never do."""
    if not raw:
        return []
    suffix = Path(filename).suffix.lower()
    if suffix in PDF_SUFFIXES:
        return page_texts_from_pdf(raw, max_pages=max_pages)
    return []


def save_sidecar(path: Path, pages: list[PageText]) -> Path:
    """Persist page text beside a run so a retry re-verifies without re-uploading."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        [{"index": p.index, "lines": list(p.lines), "source": p.source} for p in pages],
        indent=2,
    ), encoding="utf-8")
    return path


def load_sidecar(path: Path) -> list[PageText]:
    """Read back what `save_sidecar` wrote. Missing or corrupt is simply no text."""
    path = Path(path)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(payload, list):
        return []
    out: list[PageText] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        lines = entry.get("lines", [])
        if not isinstance(lines, list):
            # A string here would otherwise be read as a sequence of one-
            # character lines, producing a page of noise that matches nothing.
            continue
        try:
            out.append(PageText(
                index=int(entry.get("index", len(out) + 1)),
                lines=tuple(str(ln) for ln in lines),
                source=str(entry.get("source", "unknown")),
            ))
        except (TypeError, ValueError):
            continue
    return out
