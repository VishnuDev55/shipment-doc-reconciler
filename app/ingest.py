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
import math
import time
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

# Wall clock one document may hold the renderer for. Deliberately generous: a
# real six-page scan draws in a second or two, so this only ever trips on a
# document that is working at the renderer rather than being read by it.
PDF_RENDER_DEADLINE_SECONDS = 30.0

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
    # Preprocessing provenance: whether the page was auto-oriented/deskewed, and
    # the orient rotation (degrees) applied, so the report can say what the
    # model actually saw.
    preprocessed: bool = False
    rotation: int = 0

    @property
    def data_uri(self) -> str:
        return f"data:image/jpeg;base64,{base64.b64encode(self.jpeg).decode()}"


def _orientation_score(img: Image.Image) -> float:
    """How much this image looks like upright text, by horizontal projection.

    Upright text forms strong horizontal bands — rows of dark pixels separated by
    white gaps — so the variance of the row-sums of dark pixels is high. A page
    rotated 90° spreads the same dark pixels evenly across every row and scores
    low. Cheap, deterministic, and OCR-free: it needs no model and no text layer.
    """
    gray = img.convert("L")
    small = gray.resize((max(1, gray.width // 4), max(1, gray.height // 4)))
    data = small.tobytes()  # L mode: one byte per pixel, row-major
    width, height = small.size
    hist = [0] * 256
    for byte in data:
        hist[byte] += 1
    total = sum(hist[:200])
    if total == 0:
        return 0.0
    acc = 0
    threshold = 128
    for value in range(200):
        acc += hist[value]
        if acc >= total // 2:
            threshold = value
            break
    rows = [0] * height
    for y in range(height):
        start = y * width
        rows[y] = sum(1 for b in data[start:start + width] if b < threshold)
    mean = sum(rows) / len(rows)
    return sum((r - mean) ** 2 for r in rows) / len(rows)


def _best_rotation(img: Image.Image) -> int:
    """The clockwise rotation (0/90/180/270) that best uprights the page."""
    best, best_score = 0, _orientation_score(img)
    for angle in (90, 180, 270):
        score = _orientation_score(img.rotate(angle, expand=True))
        if score > best_score:
            best, best_score = angle, score
    return best


def _deskew(img: Image.Image) -> Image.Image:
    """Straighten a small tilt by sweeping angles and keeping the best score.

    Bounded to ±6°, which is the tilt a hand-held photo produces; larger
    rotations are the orient step's job, not this one's.
    """
    best, best_score = img, _orientation_score(img)
    for tenths in range(-60, 61, 3):  # -6.0° .. +6.0° in 0.3° steps
        angle = tenths / 10.0
        candidate = img.rotate(angle, expand=True, fillcolor=(255, 255, 255))
        score = _orientation_score(candidate)
        if score > best_score:
            best, best_score = candidate, score
    return best


def _preprocess(img: Image.Image) -> tuple[Image.Image, int]:
    """Auto-orient + deskew + contrast. Returns (image, rotation applied)."""
    rotation = _best_rotation(img)
    if rotation:
        img = img.rotate(rotation, expand=True)
    img = _deskew(img)
    img = ImageOps.autocontrast(img)
    return img, rotation


def _encode(img: Image.Image, index: int, long_edge: int, quality: int,
            preprocess: bool = False) -> PageImage:
    """EXIF-orient, optionally preprocess, RGB-convert, resize, encode JPEG."""
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")

    rotation = 0
    if preprocess:
        img, rotation = _preprocess(img)

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
        preprocessed=preprocess,
        rotation=rotation,
    )


def rasterize_image(
    raw: bytes, *, long_edge: int = 1600, quality: int = 85,
    max_pages: int = 6, preprocess: bool = False,
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
            pages.append(_encode(im, len(pages) + 1, long_edge, quality, preprocess))
    if not pages:
        raise ValueError("image produced no pages")
    return pages


def _render_scale(width: float, height: float, *, scale: float,
                  long_edge: int) -> float:
    """The scale to draw a page at, given the size that page declares.

    A PDF states its page size in points and the renderer allocates
    `width*scale x height*scale` pixels for it, so the number that decides the
    allocation arrives in the file. `MAX_IMAGE_PIXELS` already refuses an image
    that declares itself that big, and guarding only the Pillow path left the
    PDF path open: a sub-MiB document declaring 14400x14400 pt asked for ~3 GB.

    The page is drawn smaller rather than refused. `_encode` resizes every page
    to `long_edge` moments after the render, so drawing it at more than twice
    that is wasted memory even for a document nobody is attacking.
    """
    if width <= 0 or height <= 0 or scale <= 0:
        return scale
    wanted = (width * scale) * (height * scale)
    if wanted <= MAX_IMAGE_PIXELS:
        return scale
    longest = max(width, height)
    fit = math.sqrt(MAX_IMAGE_PIXELS / wanted)
    useful = (long_edge * 2) / longest if long_edge else scale
    # A floor, so a page declared absurd enough to need a scale of zero still
    # produces a readable page instead of an empty bitmap.
    return max(min(scale * fit, useful), 0.01)


def rasterize_pdf(
    raw: bytes,
    *,
    scale: float = 2.0,
    long_edge: int = 1600,
    quality: int = 85,
    max_pages: int = 6,
    preprocess: bool = False,
    deadline_seconds: float = PDF_RENDER_DEADLINE_SECONDS,
) -> list[PageImage]:
    try:
        doc = pdfium.PdfDocument(raw)
    except Exception as exc:
        raise ValueError(f"unreadable PDF: {exc}") from exc

    pages: list[PageImage] = []
    started = time.monotonic()
    try:
        for i in range(min(len(doc), max_pages)):
            # Checked per page rather than once up front: the cost of drawing a
            # page is not knowable from the file's size, and a document that
            # keeps the instance's CPU for a minute is the same denial of
            # service whether it does it on page 1 or page 6.
            if time.monotonic() - started > deadline_seconds:
                raise ValueError(
                    f"PDF took too long to render ({len(pages)} of "
                    f"{min(len(doc), max_pages)} pages in "
                    f"{deadline_seconds:.0f} s); it was not read further"
                )
            page = doc[i]
            width, height = page.get_size()
            pil = page.render(
                scale=_render_scale(width, height, scale=scale, long_edge=long_edge)
            ).to_pil()
            pages.append(_encode(pil, len(pages) + 1, long_edge, quality, preprocess))
    finally:
        doc.close()

    if not pages:
        raise ValueError("PDF produced no renderable pages")
    return pages


def page_count(filename: str, raw: bytes) -> int:
    """How many pages the file declares, without rendering any of them.

    `rasterize_*` caps at `max_pages` and returns the pages it produced, which is
    all a run needs -- but a caller that wants to say "four of five pages were
    read" has to ask what the file actually contained. This opens the document
    and counts: pypdfium2 parses the cross-reference table, Pillow reads a header
    frame count, and neither decodes a pixel.
    """
    if not raw:
        raise ValueError("empty file")
    claimed = _reject_unless_kind_matches(filename, raw)
    if claimed == "pdf":
        try:
            doc = pdfium.PdfDocument(raw)
        except Exception as exc:
            raise ValueError(f"unreadable PDF: {exc}") from exc
        try:
            return len(doc)
        finally:
            doc.close()
    try:
        handle = Image.open(io.BytesIO(raw))
    except _BOMB as exc:
        raise ValueError(f"image too large to decode safely: {exc}") from exc
    try:
        return int(getattr(handle, "n_frames", 1))
    finally:
        handle.close()


def ingest(
    filename: str,
    raw: bytes,
    *,
    long_edge: int = 1600,
    quality: int = 85,
    max_pages: int = 6,
    preprocess: bool = False,
) -> list[PageImage]:
    """Dispatch on what the bytes are, having checked what they claimed to be."""
    if not raw:
        raise ValueError("empty file")
    claimed = _reject_unless_kind_matches(filename, raw)
    if claimed == "pdf":
        return rasterize_pdf(raw, long_edge=long_edge, quality=quality,
                              max_pages=max_pages, preprocess=preprocess)
    return rasterize_image(raw, long_edge=long_edge, quality=quality,
                           max_pages=max_pages, preprocess=preprocess)
