"""Cross-field sanity rules.

The fixture shipments do not trigger the numeric bounds — a real 48-piece,
12,450 kg shipment is 259 kg per piece, comfortably plausible. So these tests
construct the absurd cases deliberately: each one asserts a specific rule and
that the explanation quotes both the number and the bound.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from app.export import standalone_html
from app.fields import FIELD_KIND, FIELD_LABEL
from app.models import (
    Comparison,
    ComparisonValue,
    DocumentReport,
    Report,
)
from app.orchestrator import Reconciler
from app.config import Settings
from app.rules import MAX_KG_PER_UNIT, readings, sanity_checks
from app.adjudicate import customs_risk

REPO = Path(__file__).resolve().parents[1]


def stated(field: str, docs: list[tuple[str, str, str]], status: str | None = None) -> Comparison:
    """docs: (document_id, document_label, compare_key) — canonical mirrors key.

    majority_compare_key is counted, not taken from the first entry, otherwise
    this helper would not model what the comparator actually produces and the
    rules would be tested against a fiction. `status` is derived from agreement
    unless given, because the real engine also reports single_source and absent,
    which no amount of value-pluralising can produce.
    """
    values = [
        ComparisonValue(
            document_id=did, document_label=label, doc_type="commercial_invoice",
            page_index=1, raw_value=compare_key, canonical=compare_key,
            compare_key=compare_key, confidence=0.9,
        )
        for did, label, compare_key in docs
    ]
    counted = Counter(v.compare_key for v in values)
    majority = counted.most_common(1)[0][0]
    return Comparison(
        field=field, label=FIELD_LABEL[field], kind=FIELD_KIND[field],
        status=status or ("consistent" if len(counted) == 1 else "conflict"),
        reason="test fixture", values=values, majority_compare_key=majority,
    )


def report_with(**comparisons) -> Report:
    report = Report(shipment_id="s1", created_at="now", updated_at="now")
    report.comparisons = list(comparisons.values())
    report.summary.total_fields = len(report.comparisons)
    return report


def codes(report: Report) -> list[str]:
    return [f.code for f in sanity_checks(report)]


# ---------------------------------------------------------------------------
# unit mass
# ---------------------------------------------------------------------------


def test_implausible_unit_mass_is_flagged_and_shows_its_arithmetic():
    report = report_with(
        gross_weight=stated("gross_weight", [("d1", "Invoice", "weight:12450.000")]),
        quantity=stated("quantity", [("d1", "Invoice", "count:2:pallet")]),
    )
    findings = sanity_checks(report)
    assert "unit_mass_high" in codes(report)
    text = next(f.explanation for f in findings if f.code == "unit_mass_high")
    assert "6,225.0 kg per piece" in text, text
    assert f"{MAX_KG_PER_UNIT:,.0f}" in text
    assert next(f for f in findings if f.code == "unit_mass_high").level == "risk"


def test_a_plausible_shipment_is_not_flagged():
    report = report_with(
        gross_weight=stated("gross_weight", [
            ("d1", "Invoice", "weight:12450.000"),
            ("d2", "Packing list", "weight:12450.000")]),
        quantity=stated("quantity", [
            ("d1", "Invoice", "count:48:ea"),
            ("d2", "Packing list", "count:48:ea")]),
    )
    assert "unit_mass_high" not in codes(report)
    assert "unit_mass_low" not in codes(report)


def test_an_implausibly_light_basis_mismatch_is_warned_not_risked():
    report = report_with(
        gross_weight=stated("gross_weight", [("d1", "Invoice", "weight:12.000")]),
        quantity=stated("quantity", [("d1", "Invoice", "count:5000000:ea")]),
    )
    flagged = [f for f in sanity_checks(report) if f.code == "unit_mass_low"]
    assert flagged and flagged[0].level == "warn"


def test_a_zero_count_cannot_divide():
    report = report_with(
        gross_weight=stated("gross_weight", [("d1", "Invoice", "weight:100.000")]),
        quantity=stated("quantity", [("d1", "Invoice", "count:0:ea")]),
    )
    assert "unit_mass_high" not in codes(report)


def test_a_missing_field_skips_the_pair_rule_rather_than_guessing():
    report = report_with(
        gross_weight=stated("gross_weight", [("d1", "Invoice", "weight:12450.000")]),
    )
    assert [c for c in codes(report) if c.startswith("unit_mass")] == []


# ---------------------------------------------------------------------------
# money
# ---------------------------------------------------------------------------


def test_a_value_with_no_currency_is_a_risk_finding():
    """Not converting is the invariant; saying so loudly is the rule."""
    report = report_with(
        invoice_value=stated("invoice_value", [("d1", "Invoice", "money:UNKNOWN:48200.00")]),
    )
    flagged = [f for f in sanity_checks(report) if f.code == "value_without_currency"]
    assert flagged and flagged[0].level == "risk"
    assert "no currency is inferred" in flagged[0].explanation


def test_an_absurd_unit_price_is_warned_in_its_own_currency():
    report = report_with(
        invoice_value=stated("invoice_value", [("d1", "Invoice", "money:EUR:900000.00")]),
        quantity=stated("quantity", [("d1", "Invoice", "count:3:ea")]),
    )
    flagged = [f for f in sanity_checks(report) if f.code == "unit_price_high"]
    assert flagged
    assert "EUR" in flagged[0].explanation
    assert "like for like" in flagged[0].explanation


def test_a_short_hs_code_is_flagged():
    report = report_with(hs_code=stated("hs_code", [("d1", "Invoice", "hs:8517")]))
    assert "hs_code_short" in codes(report)


def test_a_six_digit_hs_code_is_not_flagged():
    report = report_with(hs_code=stated("hs_code", [("d1", "Invoice", "hs:851762")]))
    assert "hs_code_short" not in codes(report)


def test_a_bill_of_lading_with_no_container_number_is_reported():
    report = report_with(consignee_name=stated(
        "consignee_name", [("d1", "Invoice", "party:ACME")]))
    report.documents = [DocumentReport(
        id="d9", label="bol.png", filename="bol.png",
        declared_doc_type="bill_of_lading", status="ok")]
    assert "bill_of_lading_without_container" in codes(report)


def test_coverage_gaps_are_stated_as_info_not_silently_omitted():
    report = report_with(invoice_value=stated(
        "invoice_value", [("d1", "Invoice", "money:USD:10.00")]))
    info = [f for f in sanity_checks(report) if f.code == "single_source_fields"]
    assert info and info[0].level == "info"
    assert "invoice value" in info[0].explanation


def test_a_genuinely_cross_checked_field_is_not_listed_as_unchecked():
    report = report_with(gross_weight=stated("gross_weight", [
        ("d1", "Invoice", "weight:100.000"),
        ("d2", "Packing list", "weight:100.000"),
    ]))
    assert "single_source_fields" not in codes(report)


# ---------------------------------------------------------------------------
# integration
# ---------------------------------------------------------------------------


def test_findings_are_deterministic_across_runs():
    report = report_with(
        gross_weight=stated("gross_weight", [("d1", "Invoice", "weight:9000.000")]),
        quantity=stated("quantity", [("d1", "Invoice", "count:3:crate")]),
    )
    first = json.dumps([f.model_dump() for f in sanity_checks(report)], sort_keys=True)
    second = json.dumps([f.model_dump() for f in sanity_checks(report)], sort_keys=True)
    assert first == second


def test_readings_take_the_majority_value_not_the_first_document():
    report = report_with(gross_weight=stated("gross_weight", [
        ("d1", "Invoice", "weight:50.000"),
        ("d2", "Packing list", "weight:5000.000"),
        ("d3", "B/L", "weight:5000.000"),
    ]))
    assert readings(report)["gross_weight"].compare_key == "weight:5000.000"


def test_a_risk_finding_raises_the_customs_risk_level():
    report = report_with(
        invoice_value=stated("invoice_value", [("d1", "Invoice", "money:UNKNOWN:9.00")]),
    )
    report.sanity = sanity_checks(report)
    level, reasons = customs_risk(report)
    assert level in ("medium", "high")
    assert any("value without currency" in r for r in reasons)


def test_a_clean_report_with_no_findings_stays_low():
    report = report_with(gross_weight=stated("gross_weight", [
        ("d1", "Invoice", "weight:100.000"),
        ("d2", "Packing list", "weight:100.000"),
    ]))
    report.sanity = sanity_checks(report)
    assert customs_risk(report)[0] == "low"


@pytest.mark.asyncio
async def test_a_real_run_carries_sanity_into_the_report_and_export(tmp_path):
    index = json.loads((REPO / "fixtures" / "index.json").read_text(encoding="utf-8"))
    uploads = [(f, d["doc_type"], (REPO / "fixtures" / "images" / f).read_bytes())
               for d in index["shipments"]["shipment_clean"]["documents"] for f in d["files"]]
    rec = Reconciler(Settings(
        _env_file=None, demo_mode=True, runs_dir=tmp_path / "runs",
        cache_dir=REPO / "fixtures" / "responses"))
    sid, _ = await rec.create_shipment(uploads)
    await rec.wait(sid, timeout=60)

    report = rec.get(sid)
    assert report.sanity, "the clean fixture at least has a coverage statement"
    html = standalone_html(report, rec.store.dir(sid))
    assert "sanity" in html
    assert report.sanity[0].explanation[:40] in html
