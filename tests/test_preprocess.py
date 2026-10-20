"""Optional ingest preprocessing: auto-orient, deskew, contrast.

These tests run offline on the bundled fixture image and a rotated copy of it.
The invariant they protect is the one that matters for evidence: the image the
model sees is the image the evidence display and highlight boxes use, so a page
whose text layer yields boxes must come through unrotated — otherwise a box
computed in the original orientation would land on the wrong line.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from app.ingest import ingest

REPO = Path(__file__).resolve().parents[1]
INVOICE = REPO / "fixtures" / "images" / "invoice_clean.png"


def _rotated(angle: int) -> bytes:
    img = Image.open(INVOICE)
    img = img.rotate(angle, expand=True)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_a_sideways_photo_is_auto_oriented():
    page = ingest("sideways.png", _rotated(90), preprocess=True)[0]
    assert page.preprocessed is True
    assert page.rotation in (90, 270), f"expected a 90/270 orient, got {page.rotation}"
    # The invoice is portrait; after a 90° rotation it was landscape, and the
    # auto-orient put it back upright.
    assert page.width < page.height


def test_preprocessing_is_off_by_default():
    page = ingest("sideways.png", _rotated(90))[0]
    assert page.preprocessed is False
    assert page.rotation == 0


def test_an_upright_page_is_not_rotated_so_boxes_stay_put():
    # A page whose text layer yields highlight boxes must come through unrotated.
    page = ingest("invoice_clean.png", INVOICE.read_bytes(), preprocess=True)[0]
    assert page.rotation == 0


def test_preprocessing_still_produces_a_decodable_jpeg():
    page = ingest("sideways.png", _rotated(90), preprocess=True)[0]
    img = Image.open(io.BytesIO(page.jpeg))
    img.load()
    assert img.size == (page.width, page.height)
