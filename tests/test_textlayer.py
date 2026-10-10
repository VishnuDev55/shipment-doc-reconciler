"""Recovering what a page says without asking the model that read it.

Two of these tests render a document to get a real text layer, because the
behaviour worth pinning is the interaction with actual PDF producers: a vector
PDF gives text, a scanned PDF wrapped around page images gives nothing, and the
difference between those two decides whether the app says "verified",
"unverified" or "cannot check".
"""

from __future__ import annotations

import io

from PIL import Image

from app.textlayer import (
    MIN_PAGE_CHARS,
    PageText,
    load_sidecar,
    page_texts_for,
    page_texts_from_pdf,
    save_sidecar,
)

HTML = """<html><body><h1>PACKING LIST</h1>
<p>Gross weight 12,450.00 KGS</p><p>Marks NORDWIND/2026/EU</p></body></html>"""


def _text_pdf() -> bytes:
    from xhtml2pdf import pisa

    buf = io.BytesIO()
    pisa.CreatePDF(io.StringIO(HTML), dest=buf)
    return buf.getvalue()


def _image_only_pdf() -> bytes:
    """A PDF built by wrapping rasters -- what a scanner produces."""
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), "white").save(buf, format="PDF")
    return buf.getvalue()


# ---------------------------------------------------------------------------


def test_a_text_pdf_yields_its_own_words():
    pages = page_texts_from_pdf(_text_pdf())
    assert pages, "a vector PDF must produce text"
    body = pages[0].text
    assert "PACKING LIST" in body
    assert "12,450.00 KGS" in body
    assert pages[0].index == 1
    assert pages[0].source == "pdf_text_layer"
    assert pages[0].usable


def test_a_scanned_pdf_yields_nothing_rather_than_a_lie():
    """Empty means "no text source", which becomes not_checkable downstream.

    Returning blank lines and letting the caller treat them as text would mark
    every field of every scan as invented.
    """
    assert page_texts_from_pdf(_image_only_pdf()) == []


def test_an_image_upload_has_no_text_source():
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), "white").save(buf, format="PNG")
    assert page_texts_for("photo.png", buf.getvalue()) == []


def test_dispatch_only_reads_text_from_pdfs():
    pdf = _text_pdf()
    assert page_texts_for("doc.pdf", pdf)
    assert page_texts_for("doc.png", pdf) == []      # right bytes, wrong suffix
    assert page_texts_for("doc.pdf", b"") == []


def test_page_count_is_capped_like_ingestion():
    """An unbounded text pass over a 900-page PDF is a denial of service with a
    PDF reader attached, and ingest caps pages for the same reason."""
    body = "".join(
        f"<p>Page {i} marker text for counting</p>"
        "<p style='page-break-after: always'></p>" for i in range(4)
    )
    from xhtml2pdf import pisa

    buf = io.BytesIO()
    pisa.CreatePDF(io.StringIO(f"<html><body>{body}</body></html>"), dest=buf)
    pdf = buf.getvalue()

    uncapped = page_texts_from_pdf(pdf, max_pages=99)
    assert len(uncapped) >= 2, "the multi-page render did not produce multiple pages"
    assert len(page_texts_from_pdf(pdf, max_pages=2)) == 2


def test_a_single_glyph_page_is_treated_as_no_text():
    page = PageText(index=1, lines=("x",), source="pdf_text_layer")
    assert not page.usable
    assert MIN_PAGE_CHARS > 1, "a one-character page is noise, not a text layer"


# ---------------------------------------------------------------------------


def test_sidecar_round_trip(tmp_path):
    pages = [
        PageText(index=1, lines=("ALPHA", "BETA"), source="fixture_sidecar"),
        PageText(index=2, lines=("GAMMA",), source="fixture_sidecar"),
    ]
    path = save_sidecar(tmp_path / "text" / "doc1.json", pages)
    assert load_sidecar(path) == pages


def test_a_missing_sidecar_is_no_text(tmp_path):
    assert load_sidecar(tmp_path / "absent.json") == []


def test_a_corrupt_sidecar_is_no_text_and_does_not_raise(tmp_path):
    """It sits in the run directory beside untrusted uploads; a half-written or
    hand-edited file must degrade to 'cannot check', not to a 500 on retry."""
    path = tmp_path / "bad.json"
    for body in ("{not json", "[]", "{}", "[1, 2]",
                 '[{"index": 1, "lines": "not-a-list"}]',
                 '[{"index": "abc", "lines": ["ok"]}]'):
        path.write_text(body, encoding="utf-8")
        assert load_sidecar(path) == [], body


def test_an_empty_sidecar_list_round_trips(tmp_path):
    path = save_sidecar(tmp_path / "none.json", [])
    assert load_sidecar(path) == []
