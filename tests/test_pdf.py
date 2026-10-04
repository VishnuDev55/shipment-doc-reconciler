"""The PDF export: a real document, with the same honesty rules as the screen.

Rendering is slow enough that the suite builds one PDF per scenario rather than
one per assertion, and reads its text layer back with the PDF library already in
the image pipeline. What is checked is not pixel layout but the claims the file
makes: that it names every contracted field, shows why each verdict was reached,
records where the numbers came from, and cannot be talked into executing text
that came out of a document.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, configure
from app.fields import FIELD_ORDER
from app.models import (Comparison, ComparisonValue, DocumentReport, ExtractedField,
                        ExtractionResponse, Report, RunInfo, Summary)
from app.normalize import normalize_field
from app.orchestrator import Reconciler
from app.pdf import render_pdf, report_html

REPO = Path(__file__).resolve().parents[1]
FAKE_NVIDIA = "nvapi-TEST-ONLY-0000-1234"


def settings_for(tmp_path: Path, **over) -> Settings:
    base = dict(
        _env_file=None, demo_mode=True,
        nvidia_api_key=FAKE_NVIDIA, nebius_api_key="sk-test-nebius-0000-9876",
        runs_dir=tmp_path / "runs", cache_dir=REPO / "fixtures" / "responses",
    )
    base.update(over)
    return Settings(**base)


def pdf_text(pdf: bytes) -> str:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf)
    try:
        return "\n".join(doc[i].get_textpage().get_text_bounded()
                         for i in range(len(doc)))
    finally:
        doc.close()


@pytest.fixture(scope="module")
def conflict_pdf(tmp_path_factory) -> bytes:
    """One real report, rendered once, reused by every content assertion."""
    import asyncio

    tmp = tmp_path_factory.mktemp("pdf")
    configure(Reconciler(settings_for(tmp)))
    index = json.loads((REPO / "fixtures" / "index.json").read_text())
    uploads = [(f, d["doc_type"], (REPO / "fixtures" / "images" / f).read_bytes())
               for d in index["shipments"]["shipment_conflict"]["documents"]
               for f in d["files"]]

    async def run():
        rec = Reconciler(settings_for(tmp))
        sid, _ = await rec.create_shipment(uploads)
        await rec.wait(sid, timeout=60)
        return rec.get(sid)

    report = asyncio.run(run())
    assert report.summary.verdict == "conflicts", "the fixture stopped meaning this"
    return render_pdf(report)


# ---------------------------------------------------------------------------
# the artifact itself
# ---------------------------------------------------------------------------


def test_the_pdf_is_a_pdf(conflict_pdf):
    assert conflict_pdf[:5] == b"%PDF-"
    assert len(conflict_pdf) > 5000, "suspiciously small: probably a blank page"


def test_the_text_layer_is_readable_and_complete(conflict_pdf):
    text = pdf_text(conflict_pdf)
    assert len(text) > 3000
    # Every contracted field must appear by its human label, not only by key:
    # this is the document an auditor files, and `country_of_origin` in a
    # printed table is a bug report waiting to happen.
    for label in ("Quantity", "Gross weight", "Net weight", "Container no.",
                  "HS code", "Consignee", "Invoice value", "Incoterms",
                  "PO number", "Country of origin"):
        assert label in text, label


def test_the_verdict_and_its_reason_travel_together(conflict_pdf):
    text = pdf_text(conflict_pdf)
    assert "Verdict: CONFLICTS" in text
    assert "5.68%" in text, "the weight conflict must state its magnitude"
    assert "ISO 6346" in text and "ARBITRATES" in text


def test_the_risk_score_shows_its_arithmetic(conflict_pdf):
    text = pdf_text(conflict_pdf)
    assert "HIGH — 100/100" in text
    for code in ("container_conflict", "weight_conflict", "single_source_field"):
        assert code in text, code
    assert "+60" in text and "+40" in text


def test_no_provider_key_appears_anywhere_in_the_file(conflict_pdf):
    """The report is the one artifact designed to leave the building."""
    assert FAKE_NVIDIA.encode() not in conflict_pdf
    assert b"sk-test-nebius" not in conflict_pdf


# ---------------------------------------------------------------------------
# provenance: the claim a printed report must not overreach
# ---------------------------------------------------------------------------


def test_a_replayed_report_says_it_made_no_call(conflict_pdf):
    text = pdf_text(conflict_pdf)
    assert "No API call was made for" in text
    assert "captured in an earlier run and replayed" in text
    assert "during this run" not in text, (
        "a cached replay must not read like a live extraction"
    )


def test_the_versions_that_produced_it_are_printed(conflict_pdf):
    """The version string is read from the config, not written out here.

    An earlier version of this test hardcoded "extract-v3" and failed the day
    the contract moved to v4 -- which is the correct outcome for the product and
    the wrong one for a test, since the point is that the PDF carries whatever
    version produced the report, not that it carries a particular number.
    """
    from app.config import PROMPT_VERSION

    text = pdf_text(conflict_pdf)
    assert f"prompt {PROMPT_VERSION}" in text
    assert "normalizer" in text and "comparator" in text


# ---------------------------------------------------------------------------
# the endpoint
# ---------------------------------------------------------------------------


def test_the_route_returns_an_attachment(tmp_path):
    configure(Reconciler(settings_for(tmp_path)))
    client = TestClient(app)
    sid = client.get("/api/fixture/shipment_messy").json()["shipment_id"]
    res = client.get(f"/api/shipments/{sid}/report.pdf")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/pdf"
    assert f'reconciliation_{sid}.pdf' in res.headers["content-disposition"]
    assert res.content[:5] == b"%PDF-"


def test_an_unknown_shipment_is_404_not_a_blank_page(tmp_path):
    configure(Reconciler(settings_for(tmp_path)))
    client = TestClient(app)
    assert client.get("/api/shipments/nosuchid/report.pdf").status_code == 404


# ---------------------------------------------------------------------------
# text from a document is data
# ---------------------------------------------------------------------------


def _crafted(value: str) -> Report:
    """A one-conflict report carrying an attacker-supplied HS code.

    ExtractionResponse requires all ten keys, which is the contract doing its job:
    a hand-built report in a test has to be as complete as a real one.
    """
    report = Report(shipment_id="s", created_at="now", updated_at="now")
    report.run = RunInfo(prompt_version="t", normalizer_version="t",
                         comparator_version="t", model="m", vision_provider="nvidia",
                         summary_model="s", summary_provider="nebius",
                         demo_mode=True, json_schema_mode="strict",
                         result_source="authored", live_calls=0, usd_per_mtok=0.0)
    field = ExtractedField(present=True, value=value, evidence_text=value,
                           page_index=1, confidence=0.9)
    blank = ExtractedField(present=False, value=None, evidence_text="",
                           page_index=1, confidence=0.0)
    fields = {key: (field if key == "hs_code" else blank) for key in FIELD_ORDER}
    norm = normalize_field("hs_code", field)
    report.comparisons = [Comparison(
        field="hs_code", label="HS code", kind="hs_code", status="conflict",
        reason="values differ",
        values=[ComparisonValue(document_id="d", document_label="doc.png",
                                doc_type="other", page_index=1, raw_value=value,
                                canonical=norm.canonical, compare_key=norm.compare_key,
                                confidence=0.9)],
    )]
    report.summary = Summary(total_fields=1, consistent=0, suspicious=0, conflict=1,
                             single_source=0, absent=0, documents_ok=1,
                             documents_failed=0, verdict="conflicts")
    report.documents = [DocumentReport(id="d", label="doc.png", filename="doc.png",
                                       status="ok", attempts=1, pages=[],
                                       extraction=ExtractionResponse(
                                           doc_type="other", doc_type_reason="x",
                                           fields=fields),
                                       normalized={"hs_code": norm},
                                       timings_ms=1, used_cache=False,
                                       used_fallback_schema=False, source="authored",
                                       provider="nvidia", model="m")]
    return report


@pytest.mark.parametrize("value", [
    "<script>alert(1)</script>",
    "</td></tr><script>document.cookie</script>",
    "]]>&<xmp>-",
])
def test_document_text_cannot_mark_up_the_report(value):
    """A shipment document is untrusted input, and the PDF is the artifact most
    likely to be opened in a viewer that executes markup. The value must reach
    the page as text, never as an element."""
    import html

    page = report_html(_crafted(value))
    assert html.escape(value, quote=False) in page, "the value must survive as text"
    assert "<script" not in page.lower(), "and must not arrive as an element"


def test_a_failing_renderer_says_so_instead_of_shipping_silence(monkeypatch):
    """An empty or truncated PDF would be the worst possible evidence artifact:
    it looks like a report."""
    import xhtml2pdf.pisa as pisa_module

    def boom(*a, **k):
        class Result:
            err = 1
        return Result()

    monkeypatch.setattr(pisa_module, "CreatePDF", boom)
    with pytest.raises(RuntimeError, match="PDF rendering failed"):
        render_pdf(_crafted("8517.62.00"))


# ---------------------------------------------------------------------------
# The goods-lines section, which only exists when it was asked for
# ---------------------------------------------------------------------------


def _with_lines(**over) -> Report:
    from app.models import LineItemMatch, LineItemsSection

    report = _crafted("8517.62.00")
    report.line_items = LineItemsSection(
        enabled=True, status="ok", source="cached_authored",
        documents=["invoice.png", "packing.png"],
        matches=[
            LineItemMatch(key="sku:H40", label="H-40",
                          documents=["invoice.png", "packing.png"],
                          quantities={"invoice.png": "60 CTNS",
                                      "packing.png": "55 CTNS"},
                          status="quantity_conflict",
                          detail="the same line is counted differently"),
            LineItemMatch(key="sku:B7", label="B-7",
                          documents=["invoice.png", "packing.png"],
                          quantities={"invoice.png": "10 CTNS",
                                      "packing.png": "10 CTNS"}),
        ],
        matched=1, conflicts=1, uncheckable=0, **over)
    return report


def test_goods_lines_are_absent_from_a_report_that_never_asked_for_them():
    """Off by default, and the paper must not mention a check that did not run."""
    assert "Goods lines" not in report_html(_crafted("8517.62.00"))


def test_the_goods_lines_print_with_both_documents_quoted_verbatim():
    page = report_html(_with_lines())
    assert "Goods lines" in page
    assert "H-40" in page and "60 CTNS" in page and "55 CTNS" in page
    # The status reaches the paper as words, not as the enum's snake_case.
    assert "quantity conflict" in page and "quantity_conflict" not in page
    assert "matched" in page
    assert "nothing is scored on similarity" in page


def test_the_section_says_which_documents_were_not_read():
    page = report_html(_with_lines(note="bol.png returned no goods lines"))
    assert "bol.png returned no goods lines" in page


def test_a_goods_line_cannot_mark_up_the_report():
    page = report_html(_with_lines())
    assert "<script" not in page.lower()
    hostile = _crafted("x")
    from app.models import LineItemMatch, LineItemsSection

    hostile.line_items = LineItemsSection(
        enabled=True, status="ok", source="live_api", documents=["a"],
        matches=[LineItemMatch(key="k", label="<script>alert(1)</script>",
                               documents=["a"], quantities={"a": "</td><b>"})])
    page = report_html(hostile)
    assert "&lt;script&gt;" in page and "<script" not in page.lower()
