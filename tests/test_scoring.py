"""The 0-100 score, and the rule behind every point in it.

These tests exist so the number cannot quietly become a vibe: the level must be
read off the score, the contributions must account for it, and the same report
must produce the same figure twice.
"""

from __future__ import annotations

import json

from app.adjudicate import customs_risk
from app.export import standalone_html
from app.models import DocumentReport
from app.scoring import (
    HIGH_AT,
    MAX_SANITY,
    MAX_SINGLE_SOURCE,
    POINTS,
    score_report,
)
from app.rules import SanityFinding

from .test_rules import report_with, stated


def contrib(note, code: str):
    return next((c for c in note.contributions if c.code == code), None)


# ---------------------------------------------------------------------------
# bands and derivation
# ---------------------------------------------------------------------------


def test_a_clean_shipment_scores_zero_and_says_why_there_is_nothing_to_say():
    report = report_with(gross_weight=stated("gross_weight", [
        ("d1", "Invoice", "weight:100.000"),
        ("d2", "Packing list", "weight:100.000"),
    ]))
    note = score_report(report)
    assert (note.score, note.level) == (0, "low")
    assert note.contributions == []
    assert note.reasons == ["no classification-relevant disagreement detected"]


def test_an_hs_code_conflict_clears_the_high_band_on_its_own():
    note = score_report(report_with(hs_code=stated(
        "hs_code", [("d1", "Invoice", "hs:85176100"),
                    ("d2", "Packing list", "hs:85176200")])))
    assert note.score == POINTS["hs_code_conflict"] >= HIGH_AT
    assert note.level == "high"


def test_a_weight_conflict_is_serious_but_not_classification_high():
    note = score_report(report_with(gross_weight=stated("gross_weight", [
        ("d1", "Invoice", "weight:1000.000"),
        ("d2", "Packing list", "weight:9000.000"),
    ])))
    assert POINTS["weight_conflict"] < HIGH_AT
    assert note.level == "medium"


def test_the_level_is_read_off_the_score_not_decided_separately():
    """The whole point of routing severity through one number: the strip and the
    score cannot disagree about the same shipment."""
    for report in (
        report_with(hs_code=stated("hs_code", [("d1", "A", "hs:85"),
                                               ("d2", "B", "hs:86")])),
        report_with(gross_weight=stated("gross_weight", [("d1", "A", "weight:5.000"),
                                                         ("d2", "B", "weight:9.000")])),
        report_with(consignee_name=stated("consignee_name", [("d1", "A", "party:X")])),
    ):
        note = score_report(report)
        expected = ("high" if note.score >= note.high_at
                    else "medium" if note.score >= note.medium_at else "low")
        assert note.level == expected, note


def test_the_thresholds_are_reported_so_a_reader_can_re_derive_the_band():
    note = score_report(report_with(gross_weight=stated("gross_weight", [
        ("d1", "A", "weight:5.000"), ("d2", "B", "weight:9.000")])))
    assert (note.high_at, note.medium_at) == (HIGH_AT, 25)
    assert note.method == "deterministic-v1"
    assert note.source == "deterministic"


# ---------------------------------------------------------------------------
# accounting
# ---------------------------------------------------------------------------


def test_contributions_add_up_to_the_score():
    report = report_with(
        hs_code=stated("hs_code", [("d1", "A", "hs:851761"), ("d2", "B", "hs:851762")]),
        gross_weight=stated("gross_weight", [("d1", "A", "weight:10.000"),
                                             ("d2", "B", "weight:99.000")]),
        invoice_value=stated("invoice_value", [("d1", "A", "money:USD:1.00"),
                                               ("d2", "B", "money:EUR:2.00")]),
    )
    note = score_report(report)
    # The cap is the only thing that can make the total differ from the sum, so
    # the accounting is exact below 100 and pinned above it.
    assert note.score == min(100, sum(c.points for c in note.contributions))
    assert note.score == 100, "three conflicts should saturate"
    assert {c.code for c in note.contributions} >= {
        "hs_code_conflict", "weight_conflict", "value_conflict"}


def test_the_score_is_capped_at_100():
    report = report_with(
        hs_code=stated("hs_code", [("d1", "A", "hs:851761"), ("d2", "B", "hs:851762")]),
        container_number=stated("container_number", [
            ("d1", "A", "cn:MSKU1234561"), ("d2", "B", "cn:MSKU1234565")]),
        invoice_value=stated("invoice_value", [("d1", "A", "money:USD:1.00"),
                                               ("d2", "B", "money:EUR:2.00")]),
    )
    note = score_report(report)
    assert note.score == 100
    assert sum(c.points for c in note.contributions) > 100, "raw awards exceed the cap"


def test_volume_of_light_findings_cannot_outscore_one_bad_hs_code():
    """Caps exist so that five barely-checked fields do not read as a
    classification problem."""
    report = report_with(
        quantity=stated("quantity", [("d1", "Only doc", "count:10:ea")], "single_source"),
        gross_weight=stated("gross_weight", [("d1", "Only doc", "weight:80.000")],
                            "single_source"),
        consignee_name=stated("consignee_name", [("d1", "Only doc", "party:ACME")],
                              "single_source"),
    )
    note = score_report(report)
    single_entry = contrib(note, "single_source_field")
    assert single_entry.points == min(MAX_SINGLE_SOURCE, 5 * 3)
    assert single_entry.points < POINTS["hs_code_conflict"]


def test_sanity_findings_are_carried_as_contributions_with_their_own_detail():
    report = report_with(invoice_value=stated(
        "invoice_value", [("d1", "Invoice", "money:UNKNOWN:48200.00")]))
    report.sanity = [SanityFinding(code="value_without_currency", level="risk",
                                   fields=["invoice_value"], explanation="no currency given")]
    note = score_report(report)
    entry = contrib(note, "sanity_value_without_currency")
    assert entry and entry.points == POINTS["sanity_risk"]
    assert entry.detail == "no currency given"
    assert entry.fields == ["invoice_value"]


def test_scaling_the_sanity_block_keeps_every_finding_visible():
    report = report_with(invoice_value=stated(
        "invoice_value", [("d1", "Invoice", "money:USD:10.00")]))
    report.sanity = [
        SanityFinding(code=f"code_{i}", level="risk", fields=["invoice_value"],
                      explanation=f"finding {i}")
        for i in range(5)
    ]
    note = score_report(report)
    sanity = [c for c in note.contributions if c.code.startswith("sanity_")]
    assert len(sanity) == 5, "scaling must not drop findings"
    assert sum(c.points for c in sanity) <= MAX_SANITY


# ---------------------------------------------------------------------------
# properties
# ---------------------------------------------------------------------------


def test_contributions_add_exactly_to_the_score_below_the_cap():
    report = report_with(gross_weight=stated("gross_weight", [
        ("d1", "Invoice", "weight:1000.000"),
        ("d2", "Packing list", "weight:9000.000"),
    ]))
    note = score_report(report)
    assert note.score == sum(c.points for c in note.contributions) < 100


def test_the_same_report_always_scores_the_same():
    report = report_with(hs_code=stated("hs_code", [("d1", "A", "hs:8517"),
                                                    ("d2", "B", "hs:8517620000")]))
    first = score_report(report).model_dump()
    second = score_report(report).model_dump()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_unreadable_documents_are_an_input_not_a_verdict():
    report = report_with(consignee_name=stated("consignee_name", [
        ("d1", "Invoice", "party:ACME"), ("d2", "Packing list", "party:ACME")]))
    report.documents = [DocumentReport(id="d9", label="blurry.jpg",
                                       filename="blurry.jpg", status="failed")]
    note = score_report(report)
    assert contrib(note, "unreadable_document").points == POINTS["unreadable_document"]
    assert note.level == "medium"
    assert "coverage is incomplete" in " ".join(note.reasons)


def test_customs_risk_is_just_a_view_over_the_score():
    """Two implementations of severity is how a strip and a number diverge."""
    report = report_with(hs_code=stated("hs_code", [("d1", "A", "hs:851761"),
                                                    ("d2", "B", "hs:851762")]))
    note = score_report(report)
    level, reasons = customs_risk(report)
    assert (level, reasons) == (note.level, note.reasons)


def test_the_score_and_its_breakdown_reach_the_standalone_report(tmp_path):
    from .test_rules import REPO
    from app.config import Settings
    from app.orchestrator import Reconciler

    index = json.loads((REPO / "fixtures" / "index.json").read_text(encoding="utf-8"))
    uploads = [(f, d["doc_type"], (REPO / "fixtures" / "images" / f).read_bytes())
               for d in index["shipments"]["shipment_conflict"]["documents"]
               for f in d["files"]]
    rec = Reconciler(Settings(
        _env_file=None, demo_mode=True, runs_dir=tmp_path / "runs",
        cache_dir=REPO / "fixtures" / "responses"))
    import asyncio

    async def go():
        sid, _ = await rec.create_shipment(uploads)
        await rec.wait(sid, timeout=60)
        return rec.get(sid)

    report = asyncio.run(go())
    assert report.risk.score > 0
    assert report.risk.contributions
    html = standalone_html(report, rec.store.dir(report.shipment_id))
    assert f"{report.risk.score}" in html
    assert report.risk.contributions[0].code.replace("_", " ") in html
