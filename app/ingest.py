"""Document ingestion: bytes -> normalized page images.

Everything downstream (the model call, the cache key, the evidence images)
works on these canonical JPEGs, never on the raw upload. That matters for the
cache: the key hashes what we actually send, so a re-encoded or resized upload
of the same page still hits.

PDFs are rasterised with pypdfium2 (pure wheel, no poppler) because Token
Factory's Files endpoint is batch-JSONL only — there is no document-upload
path into chat completions.

Two things are true of every byte sequence that arrives here: it is checked
against what the filename claimed to be, and it is decoded with a ceiling on
how much memory it is allowed to expand into. A document upload is untrusted
input by definition, and "the decoder chose to allocate 4 GB" is not a reason.
"""

from __future__ import annotations

import base64
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageOps

# Raised when a file's declared pixel count would expand past the
# ceiling below. Named once because it can surface in two places:
# open() sees it from the header, load() from the decode.
_BOMB = Image.DecompressionBombError

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}
PDF_SUFFIXES = {".pdf"}

# Pillow warns above ~89 MP and raises above ~178 MP by default. Both numbers
# are far more than a page of text needs: 50 MP is already a 7000×7000 image,
# and every page is resized to a 1600 px long edge moments later. Making the
# ceiling explicit (and turning the warning into an error) means a
# decompression bomb is refused rather than survived.
MAX_IMAGE_PIXELS = 50_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

# Magic numbers, checked before any decoder is handed the bytes.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "pdf"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"RIFF", "riff"),        # webp: RIFF....WEBP
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
    (b"BM", "bmp"),
)


def sniff(raw: bytes) -> str | None:
    """What these bytes actually are, by their first bytes, or None."""
    for magic, kind in _SIGNATURES:
        if raw.startswith(magic):
            if kind == "riff":
                return "webp" if raw[8:12] == b"WEBP" else None
            return kind
    return None


def _reject_unless_kind_matches(filename: str, raw: bytes) -> str:
    """Return the kind to parse, refusing a file that is not what it claims."""
    suffix = Path(filename).suffix.lower()
    claimed = "pdf" if suffix in PDF_SUFFIXES else (
        "image" if suffix in IMAGE_SUFFIXES else None
    )
    if claimed is None:
        raise ValueError(
            f"unsupported file type {suffix!r}; allowed: .pdf and "
            f"{sorted(IMAGE_SUFFIXES)}"
        )

    actual = sniff(raw)
    if actual is None:
        raise ValueError(f"{filename}: unrecognised file signature, refusing to parse")
    if claimed == "pdf" and actual != "pdf":
        raise ValueError(f"{filename}: claims to be a PDF but starts with a "
                         f"{actual!r} signature")
    if claimed == "image" and actual == "pdf":
        raise ValueError(f"{filename}: claims to be an image but is a PDF")
    if claimed == "image" and suffix in {".jpg", ".jpeg"} and actual != "jpeg":
        raise ValueError(f"{filename}: claims to be a JPEG but is {actual}")
    return claimed


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
            Image.Resampling.LANCZOS,
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
    try:
        handle = Image.open(io.BytesIO(raw))
    except _BOMB as exc:
        raise ValueError(f"image too large to decode safely: {exc}") from exc

    # Pillow only *raises* at twice MAX_IMAGE_PIXELS and merely warns in
    # between, and a warning is not a refusal: an 81-megapixel PNG was measured
    # sailing through a 50-megapixel ceiling. The declared size is in the header,
    # so the check happens before any pixel is decoded.
    if handle.width * handle.height > MAX_IMAGE_PIXELS:
        handle.close()
        raise ValueError(
            f"image declares {handle.width}x{handle.height} "
            f"({handle.width * handle.height:,} px), above the "
            f"{MAX_IMAGE_PIXELS:,} px ceiling"
        )

    with handle as im:
        # Ask the JPEG/PNG decoder to downscale while parsing rather than after:
        # a 12000×16000 camera photo becomes a 1600 px page without ever being
        # materialised at full size, which is the difference between a slow
        # request and an exhausted worker.
        if long_edge:
            im.draft("RGB", (long_edge, long_edge))
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
    """Dispatch on what the bytes are, having checked what they claimed to be."""
    if not raw:
        raise ValueError("empty file")
    claimed = _reject_unless_kind_matches(filename, raw)
    if claimed == "pdf":
        return rasterize_pdf(raw, long_edge=long_edge, quality=quality, max_pages=max_pages)
    return rasterize_image(raw, long_edge=long_edge, quality=quality, max_pages=max_pages)
