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

# Where one line sits on the page, as fractions of the page: (x0, y0, x1, y1),
# origin top-left, increasing downward. Normalized rather than in points because
# the image a reader looks at is this page re-encoded at a different size, and
# the only thing that survives that trip is the proportion.
Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class PageText:
    """What one page says, where it sits, and where that came from.

    `boxes` is parallel to `lines` -- same length, same order, `None` for a line
    we could not place. It is `None` for every line the moment the two
    derivations disagree about what the lines even are, because a highlight drawn
    beside the wrong words is worse than no highlight: it looks like evidence.
    """

    index: int                      # 1-based, matching PageImage.index
    lines: tuple[str, ...]          # as the text layer emits them, in reading order
    source: str                     # 'pdf_text_layer' | 'fixture_sidecar'
    boxes: tuple[Box | None, ...] = ()

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def usable(self) -> bool:
        return len(self.text.strip()) >= MIN_PAGE_CHARS

    @property
    def placeable(self) -> bool:
        """True when at least one line has coordinates worth drawing."""
        return len(self.boxes) == len(self.lines) and any(b is not None for b in self.boxes)

    def box_for(self, line_index: int) -> Box | None:
        if not self.placeable or line_index < 0 or line_index >= len(self.boxes):
            return None
        return self.boxes[line_index]


def _split_lines(body: str) -> tuple[str, ...]:
    return tuple(body.replace("\r\n", "\n").replace("\r", "\n").split("\n"))


def _lines_and_boxes(textpage, width: float, height: float):
    """Walk the character array once: the text it spells, and where each line is.

    The char walk is what ties a line to a rectangle -- `get_text_bounded()`
    hands back the text with no positions, and the search API only answers for a
    string you already know. Walking costs about 6 ms for a 1,800-character page,
    measured, which is cheap next to the vision call that reads the same page.

    Coordinates are flipped here (PDF measures up from the bottom, an image
    measures down from the top) and divided by the page size, so nothing
    downstream has to know either fact again.
    """
    try:
        count = textpage.count_chars()
    except Exception:
        return None
    if not count:
        return None

    chars: list[str] = []
    boxes: list[tuple[float, float, float, float]] = []
    try:
        for i in range(count):
            chars.append(textpage.get_text_range(i, 1))
            boxes.append(textpage.get_charbox(i))
    except Exception:
        return None

    lines: list[str] = []
    rects: list[Box | None] = []
    current: list[str] = []
    span: list[tuple[float, float, float, float]] = []

    def flush() -> None:
        lines.append("".join(current))
        if span:
            left = min(b[0] for b in span) / width
            bottom = min(b[1] for b in span)
            right = max(b[2] for b in span) / width
            top = max(b[3] for b in span)
            rects.append((
                round(max(left, 0.0), 5),
                round(max((height - top) / height, 0.0), 5),
                round(min(right, 1.0), 5),
                round(min((height - bottom) / height, 1.0), 5),
            ))
        else:
            rects.append(None)
        current.clear()
        span.clear()

    for ch, box in zip(chars, boxes, strict=True):
        if ch == "\n":
            flush()
        elif ch == "\r":
            continue                       # paired with the \n that follows
        else:
            current.append(ch)
            span.append(box)
    if current:
        flush()

    return tuple(lines), tuple(rects)


def page_texts_from_pdf(raw: bytes, *, max_pages: int = 6) -> list[PageText]:
    """Per-page text and line positions of a PDF, or [] when it has no text layer.

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
            page = doc[i]
            try:
                textpage = page.get_textpage()
                body = textpage.get_text_bounded()
            except Exception:
                body = ""
                textpage = None
            lines = _split_lines(body)

            boxes: tuple[Box | None, ...] = ()
            if textpage is not None:
                try:
                    width, height = page.get_size()
                except Exception:
                    width = height = 0.0
                walked = _lines_and_boxes(textpage, width, height) if width and height else None
                # Only trust the positions when both readings agree about the
                # page's line count. A rotated, cropped or re-encoded page can
                # make them disagree, and then the honest answer is "no box".
                if walked is not None and len(walked[0]) == len(lines):
                    boxes = walked[1]
            pages.append(PageText(index=i + 1, lines=lines,
                                  source="pdf_text_layer", boxes=boxes))
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
        [{"index": p.index, "lines": list(p.lines), "source": p.source,
          "boxes": [list(b) if b else None for b in p.boxes]} for p in pages],
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
        raw_boxes = entry.get("boxes")
        boxes = _read_boxes(raw_boxes, len(lines))
        try:
            out.append(PageText(
                index=int(entry.get("index", len(out) + 1)),
                lines=tuple(str(ln) for ln in lines),
                source=str(entry.get("source", "unknown")),
                boxes=boxes,
            ))
        except (TypeError, ValueError):
            continue
    return out


def _read_boxes(raw_boxes: object, line_count: int) -> tuple[Box | None, ...]:
    """Boxes as written, or nothing at all.

    A sidecar from before positions existed has no `boxes` key, and one whose
    list does not line up with its lines cannot be trusted to place a highlight;
    both cases mean the same thing downstream -- page and quoted line, and say so.
    """
    if not isinstance(raw_boxes, list) or len(raw_boxes) != line_count:
        return ()
    out: list[Box | None] = []
    for item in raw_boxes:
        if item is None:
            out.append(None)
            continue
        if not isinstance(item, (list, tuple)) or len(item) != 4:
            return ()
        try:
            values = tuple(float(v) for v in item)
        except (TypeError, ValueError):
            return ()
        out.append(values)  # type: ignore[arg-type]
    return tuple(out)
