"""Document ingestion: bytes -> normalized page images.

Everything downstream (the model call, the cache key, the evidence images)
works on these canonical JPEGs, never on the raw upload. That matters for the
cache: the key hashes what we actually send, so a re-encoded or resized upload
of the same page still hits.

PDFs are rasterised with pypdfium2 (pure wheel, no poppler) because Token
Factory's Files endpoint is batch-JSONL only — there is no document-upload
path into chat completions.
"""

from __future__ import annotations

import base64
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageOps

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}
PDF_SUFFIXES = {".pdf"}


@dataclass(frozen=True)
class PageImage:
    index: int  # 1-based page number within its document
    jpeg: bytes
    sha256: str
    width: int
    height: int

    @property
    def data_uri(self) -> str:
        return f"data:image/jpeg;base64,{base64.b64encode(self.jpeg).decode()}"


def _encode(img: Image.Image, index: int, long_edge: int, quality: int) -> PageImage:
    """EXIF-orient, RGB-convert, resize to long edge, encode JPEG."""
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")

    if long_edge and max(img.size) > long_edge:
        scale = long_edge / max(img.size)
        img = img.resize(
            (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
            Image.LANCZOS,
        )

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    data = buf.getvalue()
    return PageImage(
        index=index,
        jpeg=data,
        sha256=hashlib.sha256(data).hexdigest(),
        width=img.width,
        height=img.height,
    )


def rasterize_image(
    raw: bytes, *, long_edge: int = 1600, quality: int = 85, max_pages: int = 6
) -> list[PageImage]:
    """One image file. Multi-frame TIFFs are capped at max_pages."""
    pages: list[PageImage] = []
    with Image.open(io.BytesIO(raw)) as im:
        frames = getattr(im, "n_frames", 1)
        for i in range(min(frames, max_pages)):
            if frames > 1:
                im.seek(i)
            pages.append(_encode(im, len(pages) + 1, long_edge, quality))
    if not pages:
        raise ValueError("image produced no pages")
    return pages


def rasterize_pdf(
    raw: bytes,
    *,
    scale: float = 2.0,
    long_edge: int = 1600,
    quality: int = 85,
    max_pages: int = 6,
) -> list[PageImage]:
    try:
        doc = pdfium.PdfDocument(raw)
    except Exception as exc:
        raise ValueError(f"unreadable PDF: {exc}") from exc

    pages: list[PageImage] = []
    try:
        for i in range(min(len(doc), max_pages)):
            pil = doc[i].render(scale=scale).to_pil()
            pages.append(_encode(pil, len(pages) + 1, long_edge, quality))
    finally:
        doc.close()

    if not pages:
        raise ValueError("PDF produced no renderable pages")
    return pages


def ingest(
    filename: str,
    raw: bytes,
    *,
    long_edge: int = 1600,
    quality: int = 85,
    max_pages: int = 6,
) -> list[PageImage]:
    """Dispatch on file suffix. Raises ValueError on unsupported or empty input."""
    if not raw:
        raise ValueError("empty file")
    suffix = Path(filename).suffix.lower()
    if suffix in PDF_SUFFIXES:
        return rasterize_pdf(raw, long_edge=long_edge, quality=quality, max_pages=max_pages)
    if suffix in IMAGE_SUFFIXES:
        return rasterize_image(raw, long_edge=long_edge, quality=quality, max_pages=max_pages)
    raise ValueError(
        f"unsupported file type {suffix!r}; allowed: .pdf and {sorted(IMAGE_SUFFIXES)}"
    )
