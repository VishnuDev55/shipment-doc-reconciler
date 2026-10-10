"""Arithmetic inside one document: pass, fail, and within tolerance.

The rule these tests are built around is that a document can be wrong while
agreeing with every other document, because the other documents copied it.
Cross-document comparison cannot see that at all.

Money tolerances here are absolute and deliberately tiny. The case that shaped
them is real: an invoice row in this project's own fixture data printed 600 x
9.18 = 5,510.00, where the product is 5,508.00. A relative tolerance of even
0.5 % would have passed that as rounding.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.fields import FIELD_ORDER
from app.lineitems import LineItem
from app.mathcheck import LINE_DEPENDENT, internal_checks
from app.models import DocumentReport, ExtractedField, ExtractionResponse, Report
from app.orchestrator import Reconciler

REPO = Path(__file__).resolve().parents[1]
DEFAULTS = Settings(_env_file=None)


def stated(label: str, **values: str) -> DocumentReport:
    """A document that prints the given values for the named fields."""
    fields = {}
    for key in FIELD_ORDER:
        raw = values.get(key)
        fields[key] = (
            ExtractedField(present=True, value=raw, evidence_text=f"{key} {raw}",
                           page_index=1, confidence=0.95)
            if raw is not None else
            ExtractedField(present=False, value=None, evidence_text="",
                           page_index=1, confidence=0.0))
    return DocumentReport(
        id=label, label=label, filename=f"{label}.png", status="ok",
        extraction=ExtractionResponse(doc_type="commercial_invoice",
                                      doc_type_reason="t", fields=fields),
    )


def row(desc, qty, price="", amount="", gross="", net="", sku=""):
    return LineItem(raw_description=desc, raw_sku=sku, raw_quantity=qty,
                    raw_unit_price=price, raw_line_total=amount,
                    raw_gross_weight=gross, raw_net_weight=net,
                    raw_evidence_text=f"{desc} {qty} {price} {amount}")


def run(doc, lines, settings=None):
    report = Report(shipment_id="x", created_at="t", updated_at="t")
    report.documents = [doc] if isinstance(doc, DocumentReport) else list(doc)
    by_doc = {d.id: lines for d in report.documents}
    return internal_checks(report, by_doc, settings=settings or DEFAULTS)


# ---------------------------------------------------------------------------
# quantity x unit price = line amount
# ---------------------------------------------------------------------------


def test_a_row_that_multiplies_out_passes():
    doc = stated("invoice.png", invoice_value="USD 8,420.00")
    found = run(doc, [row("Housing", "600 pieces", "4.85", "2,910.00")])
    math = next(c for c in found if c.code == "line_total_math")
    assert math.status == "pass"
    assert "multiply out" in math.detail


def test_the_defect_this_project_found_in_its_own_data_fails():
    doc = stated("invoice.png", invoice_value="USD 8,420.00")
    math = next(c for c in run(doc, [row("Fasteners", "600 pieces", "9.18", "5,510.00")])
                if c.code == "line_total_math")
    assert math.status == "fail"
    assert "5,508.00" in math.detail and "5,510.00" in math.detail
    assert math.evidence, "a failing row has to name the line it came from"


def test_a_one_cent_rounding_difference_is_not_reported_as_an_error():
    """Invoices round per line, so a printed cent off the exact product is not a
    finding. Two cents is the allowance; a dollar still is a finding."""
    doc = stated("invoice.png", invoice_value="USD 2,910.01")
    math = next(c for c in run(doc, [row("Housing", "600 pieces", "4.85", "2,910.01")])
                if c.code == "line_total_math")
    assert math.status == "pass", math.detail

    big = stated("invoice.png", invoice_value="USD 2,911.00")
    math = next(c for c in run(big, [row("Housing", "600 pieces", "4.85", "2,911.00")])
                if c.code == "line_total_math")
    assert math.status == "fail", math.detail


def test_a_price_with_an_ambiguous_separator_is_set_aside_not_judged():
    """normalize_money reads a bare "2.675" as 2675, because that is the
    documented resolution of an ambiguous separator. Multiplying that out and
    calling the document wrong would report a reading limit as an arithmetic
    error, so the row is named and skipped instead."""
    doc = stated("invoice.png", invoice_value="USD 8,420.00")
    math = next(c for c in run(doc, [row("Pin", "3", "2.675", "8.03")])
                if c.code == "line_total_math")
    assert math.status == "not_run"
    assert "ambiguous" in math.detail


def test_a_row_without_a_price_is_not_checked_and_not_invented():
    """A packing-list row has weights, not prices. Missing cells stay missing --
    filling them with zero would turn 'not printed' into 'printed as nothing'."""
    doc = stated("packing.png", gross_weight="100.00 KGS", net_weight="90.00 KGS")
    found = run(doc, [row("Carton A", "10 CTNS", gross="60.00 KGS", net="55.00 KGS")])
    assert not [c for c in found if c.code == "line_total_math"]


def test_money_tolerance_is_absolute_not_proportional():
    """On a large invoice a percentage is not a rounding error."""
    doc = stated("invoice.png", invoice_value="USD 1,000,000.00")
    math = next(c for c in run(doc, [row("Line", "1000", "950.00", "950,001.00")])
                if c.code == "line_total_math")
    assert math.status == "fail", "one dollar on 950k is 0.0001 %, and still wrong"


# ---------------------------------------------------------------------------
# the lines add up to the stated total
# ---------------------------------------------------------------------------


def test_lines_that_sum_to_the_stated_total_pass():
    doc = stated("invoice.png", invoice_value="USD 8,420.00")
    lines = [row("Housing", "600", "4.85", "2,910.00"),
             row("Fasteners", "500", "11.02", "5,510.00")]
    total = next(c for c in run(doc, lines) if c.code == "line_total_sum")
    assert total.status == "pass"
    assert "8,420.00" in total.detail


def test_lines_that_do_not_sum_to_the_stated_total_fail():
    doc = stated("invoice.png", invoice_value="USD 9,000.00")
    lines = [row("Housing", "600", "4.85", "2,910.00"),
             row("Fasteners", "500", "11.02", "5,510.00")]
    total = next(c for c in run(doc, lines) if c.code == "line_total_sum")
    assert total.status == "fail"
    assert "8,420.00" in total.detail and "9,000.00" in total.detail


def test_a_mixed_currency_sum_fails_rather_than_adding_columns():
    """Adding a USD line to an EUR line is not a sum, and the app never
    converts, so the check refuses rather than producing a number that looks
    like it means something."""
    doc = stated("invoice.png", invoice_value="USD 8,420.00")
    lines = [row("Housing", "600", "4.85", "USD 2,910.00"),
             row("Fasteners", "500", "11.02", "EUR 5,510.00")]
    total = next(c for c in run(doc, lines) if c.code == "line_total_sum")
    assert total.status == "fail"
    assert "more than one currency" in total.detail
    assert "never converts" in total.detail


# ---------------------------------------------------------------------------
# per-row weights against the stated total weight
# ---------------------------------------------------------------------------


def test_carton_weights_that_add_up_pass():
    doc = stated("packing.png", gross_weight="12,450.00 KGS", net_weight="11,600.00 KGS")
    lines = [row("A", "60 CTNS", gross="8,000.00 KGS", net="7,500.00 KGS"),
             row("B", "40 CTNS", gross="4,450.00 KGS", net="4,100.00 KGS")]
    weights = [c for c in run(doc, lines) if c.code == "row_weight_sum"]
    assert len(weights) == 2, "gross and net are checked separately"
    assert all(c.status == "pass" for c in weights), [c.detail for c in weights]


def test_carton_weights_that_do_not_add_up_fail():
    doc = stated("packing.png", gross_weight="12,450.00 KGS", net_weight="11,600.00 KGS")
    lines = [row("A", "60 CTNS", gross="8,000.00 KGS", net="7,500.00 KGS"),
             row("B", "40 CTNS", gross="3,000.00 KGS", net="4,100.00 KGS")]
    weights = [c for c in run(doc, lines) if c.code == "row_weight_sum"]
    failed = [c for c in weights if c.status == "fail"]
    assert len(failed) == 1 and "gross" in failed[0].detail


def test_weight_tolerance_absorbs_a_rounding_difference_only():
    doc = stated("packing.png", gross_weight="12,450.00 KGS", net_weight="11,600.00 KGS")
    lines = [row("A", "1 CTN", gross="12,449.20 KGS", net="11,600.00 KGS")]
    gross = next(c for c in run(doc, lines) if c.code == "row_weight_sum"
                 and "gross" in c.detail)
    assert gross.status == "pass", gross.detail

    big = stated("packing.png", gross_weight="12,450.00 KGS", net_weight="11,600.00 KGS")
    lines = [row("A", "1 CTN", gross="12,000.00 KGS", net="11,600.00 KGS")]
    gross = next(c for c in run(big, lines) if c.code == "row_weight_sum"
                 and "gross" in c.detail)
    assert gross.status == "fail"


def test_weights_written_in_pounds_still_add_up():
    """The check is on normalized kilograms, so a page mixing KGS and LBS is
    added correctly rather than numerically."""
    doc = stated("packing.png", gross_weight="12,450.00 KGS", net_weight="11,600.00 KGS")
    lines = [row("A", "60 CTNS", gross="8,000.00 KGS", net="7,500.00 KGS"),
             row("B", "40 CTNS", gross="9,877.88 LBS", net="4,100.00 KGS")]
    gross = next(c for c in run(doc, lines) if c.code == "row_weight_sum"
                 and "gross" in c.detail)
    assert gross.status == "pass", gross.detail


# ---------------------------------------------------------------------------
# net against gross, inside one document
# ---------------------------------------------------------------------------


def test_a_document_stating_more_net_than_gross_fails_on_its_own():
    doc = stated("invoice.png", gross_weight="11,000.00 KGS", net_weight="11,600.00 KGS")
    found = next(c for c in run(doc, []) if c.code == "net_above_gross_in_document")
    assert found.status == "fail"
    assert "packaging has to weigh something" in found.detail


def test_this_is_not_the_same_check_as_the_cross_document_one():
    """The existing rules.net_above_gross compares majority values ACROSS
    documents. Every paper can repeat the same impossible pair and pass it,
    which is exactly why this one reads each document on its own."""
    a = stated("invoice.png", gross_weight="11,000.00 KGS", net_weight="11,600.00 KGS")
    b = stated("packing.png", gross_weight="11,000.00 KGS", net_weight="11,600.00 KGS")
    found = [c for c in run([a, b], []) if c.code == "net_above_gross_in_document"]
    assert len(found) == 2 and all(c.status == "fail" for c in found)


def test_the_gross_net_check_runs_without_any_goods_rows():
    """It needs only two fields every document already states, so it must not be
    gated behind the opt-in line read."""
    doc = stated("invoice.png", gross_weight="12,450.00 KGS", net_weight="11,600.00 KGS")
    codes = {c.code: c.status for c in run(doc, [])}
    assert codes["net_above_gross_in_document"] == "pass"


# ---------------------------------------------------------------------------
# not_run: the state that stops an empty list meaning "all clear"
# ---------------------------------------------------------------------------


def test_no_rows_records_not_run_for_every_line_dependent_rule():
    doc = stated("invoice.png", invoice_value="USD 8,420.00")
    found = {c.code: c for c in run(doc, [])}
    for code in LINE_DEPENDENT:
        assert found[code].status == "not_run", code
        assert "not the same as passing" in found[code].detail


def test_not_run_is_never_counted_as_a_pass_by_a_caller_that_only_counts_fails():
    doc = stated("invoice.png", invoice_value="USD 8,420.00")
    results = run(doc, [])
    assert [c for c in results if c.status == "fail"] == []
    assert len([c for c in results if c.status == "not_run"]) == len(LINE_DEPENDENT)
    # A caller that treats "no failures" as "checked" is the bug this guards.
    assert any(c.status != "pass" for c in results)


def test_failures_are_listed_before_passes():
    doc = stated("invoice.png", gross_weight="100.00 KGS", net_weight="120.00 KGS")
    lines = [row("A", "10", "5.00", "50.00", gross="100.00 KGS", net="120.00 KGS")]
    results = run(doc, lines)
    assert results[0].status == "fail", [c.status for c in results]


def test_the_same_input_always_produces_the_same_records():
    doc = lambda: stated("invoice.png", invoice_value="USD 9,000.00",   # noqa: E731
                         gross_weight="10.00 KGS", net_weight="9.00 KGS")
    lines = lambda: [row("A", "600", "4.85", "2,910.00")]                # noqa: E731
    first = [(c.code, c.status, c.detail) for c in run(doc(), lines())]
    second = [(c.code, c.status, c.detail) for c in run(doc(), lines())]
    assert first == second


# ---------------------------------------------------------------------------
# through the real pipeline
# ---------------------------------------------------------------------------


def _settings(tmp_path, **over):
    base = dict(
        _env_file=None, demo_mode=True, vision_provider="nvidia",
        nvidia_api_key="", nebius_api_key="",
        runs_dir=tmp_path / "runs", cache_dir=REPO / "fixtures" / "responses",
    )
    base.update(over)
    return Settings(**base)


def _uploads():
    index = json.loads((REPO / "fixtures" / "index.json").read_text())
    out = []
    for doc in index["shipments"]["shipment_clean"]["documents"]:
        for fname in doc["files"]:
            out.append((fname, doc["doc_type"],
                        (REPO / "fixtures" / "images" / fname).read_bytes()))
    return out


@pytest.mark.asyncio
async def test_a_real_shipment_reports_what_was_never_checked(tmp_path):
    """Line items are off by default, so three of the four rules cannot run --
    and the report has to say that rather than present a short list of passes."""
    rec = Reconciler(settings=_settings(tmp_path))
    sid, _ = await rec.create_shipment(_uploads())
    await rec.wait(sid, timeout=120)
    report = rec.get(sid)

    codes = {(c.code, c.status) for c in report.internal_checks}
    for code in LINE_DEPENDENT:
        assert (code, "not_run") in codes, sorted(codes)
    assert ("net_above_gross_in_document", "pass") in codes
    assert not [c for c in report.internal_checks if c.status == "fail"]


@pytest.mark.asyncio
async def test_the_checks_cannot_move_a_verdict(tmp_path):
    """Additive by construction, like the goods lines and the advisory prose: the
    arithmetic findings sit beside the comparison, never inside it."""
    off = Reconciler(settings=_settings(tmp_path))
    sid_a, _ = await off.create_shipment(_uploads())
    await off.wait(sid_a, timeout=120)
    base = off.get(sid_a)

    on = Reconciler(settings=_settings(tmp_path, enable_line_items=True))
    sid_b, _ = await on.create_shipment(_uploads())
    await on.wait(sid_b, timeout=180)
    other = on.get(sid_b)

    assert [(c.field, c.status) for c in base.comparisons] == \
           [(c.field, c.status) for c in other.comparisons]
    assert base.summary.model_dump() == other.summary.model_dump()
    assert base.risk.model_dump() == other.risk.model_dump()
    assert base.state == other.state


def test_the_pdf_prints_the_checks_that_did_not_run():
    from app.pdf import report_html

    report = Report(shipment_id="x", created_at="t", updated_at="t")
    report.documents = [stated("invoice.png", gross_weight="10 KGS", net_weight="9 KGS")]
    report.internal_checks = internal_checks(report, {}, settings=DEFAULTS)
    page = report_html(report)
    assert "Arithmetic inside each document" in page
    assert "not run" in page
    assert "is not a check that passed" in page
