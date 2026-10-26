"""Severity, and the one property the banner has to keep.

The banner is the first thing an auditor reads and often the only line they act
on, so the interesting question is not what it says on any one shipment but what
it can never say: `ready` beside a verdict or a risk strip that says otherwise.
Everything else in this file exists to hold that one claim up.

Reports here are run through `settled()`, which gives them the state and the
verdict a finished run would have. A half-built report defaults to `state =
"pending"`, and the banner is correct to refuse to judge a run that has not
finished -- so without this the tests would be measuring the wrong question.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.config import Settings
from app.export import VERDICT_HEADLINE
from app.fields import EXPECTED, FIELD_KIND, FIELD_LABEL
from app.models import (
    Comparison,
    ComparisonValue,
    DocumentReport,
    InternalCheck,
    Report,
    SanityFinding,
    SeverityFinding,
    UnverifiedField,
)
from app.orchestrator import Reconciler, build_summary
from app.scoring import HIGH_AT, MEDIUM_AT, POINTS, score_report
from app.severity import (
    LEVELS,
    SEVERITY_TABLE,
    bridge_line,
    derive_banner,
    findings,
    status_report,
)

from .test_rules import report_with, stated

REPO = Path(__file__).resolve().parents[1]

# One disagreeing pair per field, in the format that field's normalizer emits.
DISAGREEMENT = {
    "quantity": ("count:5:piece", "count:9:piece"),
    "gross_weight": ("weight:100.000", "weight:900.000"),
    "net_weight": ("weight:100.000", "weight:900.000"),
    "container_number": ("container:MSKU3063865", "container:MSKU3063866"),
    "hs_code": ("hs:85176100", "hs:85176200"),
    "consignee_name": ("Nordwind Supply GmbH", "Baltic Trading Ltd"),
    "invoice_value": ("money:USD:9000.00", "money:USD:9050.00"),
    "incoterms": ("incoterm:CIF", "incoterm:FOB"),
    "po_number": ("ref:PO-1", "ref:PO-2"),
    "country_of_origin": ("country:CN", "country:DE"),
}
ALL_FIELDS = tuple(DISAGREEMENT)


def settled(report: Report) -> Report:
    """The state, verdict and banner a finished run carries.

    In production `refresh_report` derives all three from the same documents, so
    a test that sets the verdict but not the banner would be asserting against a
    report no run could ever produce.
    """
    if not report.documents:
        report.documents = [DocumentReport(
            id="d1", label="Invoice", filename="invoice.png", status="ok")]
    report.summary = build_summary(report.documents, report.comparisons)
    report.state = "done"
    report.status_report = status_report(report)
    return report


def conflict(field: str, first: str | None = None, second: str | None = None) -> Report:
    keys = DISAGREEMENT[field]
    return report_with(**{field: stated(
        field, [("d1", "A", first or keys[0]), ("d2", "B", second or keys[1])])})


def severity_of(report: Report, source: str) -> dict[str, str]:
    return {f.code: f.severity for f in findings(report) if f.source == source}


# ---------------------------------------------------------------------------
# the table itself
# ---------------------------------------------------------------------------


def test_every_row_names_a_real_level():
    bad = {key: value for key, value in SEVERITY_TABLE.items() if value not in LEVELS}
    assert not bad, f"levels outside {LEVELS} would be silently dropped: {bad}"


def test_the_five_shipment_defining_fields_are_the_critical_ones():
    """Critical is a short list on purpose: a report where everything is critical
    tells the reviewer to do everything, which is the same as nothing.

    Enumerated rather than counted, so adding a sixth row is a decision somebody
    has to make in this file and not a silent widening of the band.
    """
    critical = {key for key, value in SEVERITY_TABLE.items() if value == "critical"}
    assert critical == {
        "comparison:container_number:conflict",
        "comparison:consignee_name:conflict",
        "comparison:hs_code:conflict",
        "comparison:country_of_origin:conflict",
        "comparison:invoice_value:conflict:currency",
    }


# ---------------------------------------------------------------------------
# comparisons
# ---------------------------------------------------------------------------


def test_a_container_mismatch_blocks():
    report = settled(conflict("container_number"))
    picked = severity_of(report, "comparison")
    assert picked["comparison:container_number:conflict"] == "critical"
    assert report.status_report.banner == "blocked"


def test_a_consignee_mismatch_blocks():
    assert settled(conflict("consignee_name")).status_report.banner == "blocked"


def test_a_wrong_hs_heading_blocks():
    assert settled(conflict("hs_code")).status_report.banner == "blocked"


def test_two_countries_of_origin_block():
    """Origin decides the duty rate and FTA eligibility, so an entry naming two
    countries cannot be filed correctly while it stands. This is a judgment
    call, and it is pinned here rather than left as a comment: the risk strip
    already scored origin_conflict at 60 points (above HIGH_AT), so the table
    saying `warning` was the two readers of one report disagreeing about how
    bad the same fact is.
    """
    report = settled(conflict("country_of_origin"))
    picked = severity_of(report, "comparison")
    assert picked["comparison:country_of_origin:conflict"] == "critical"
    assert report.status_report.banner == "blocked"


def test_a_near_miss_on_origin_is_still_only_a_warning():
    """The boundary the row above does not move: one printing read with low
    confidence against another is `suspicious`, which the wildcard row keeps at
    warning. Only an outright conflict between two named countries blocks."""
    report = settled(report_with(country_of_origin=stated(
        "country_of_origin",
        [("d1", "A", "country:CN"), ("d2", "B", "country:CN")],
        status="suspicious")))
    assert severity_of(
        report, "comparison")["comparison:country_of_origin:suspicious"] == "warning"
    assert report.status_report.banner == "review"
    assert report.status_report.critical == 0


def test_the_same_amount_in_two_currencies_blocks_rather_than_warns():
    report = settled(conflict(
        "invoice_value", "money:USD:9000.00", "money:EUR:9000.00"))
    picked = severity_of(report, "comparison")
    assert picked["comparison:invoice_value:conflict:currency"] == "critical"
    assert report.status_report.banner == "blocked"


def test_the_same_amount_over_a_tolerance_gap_warns_instead():
    """Same field, different finding: 9000 USD on one paper and 9050 USD on
    another is a discrepancy to resolve, not a different sale."""
    report = settled(conflict("invoice_value"))
    assert severity_of(report, "comparison")["comparison:invoice_value:conflict"] \
        == "warning"
    assert report.status_report.banner == "review"


def test_a_counting_basis_difference_is_information_not_work():
    """The engine's own words for this are "normal on real trade documents", so
    the banner may not treat it as a reason to hold the shipment."""
    report = report_with(quantity=stated("quantity", [
        ("d1", "Invoice", "count:1200:piece"),
        ("d2", "Packing list", "count:1200:carton"),
    ], status="suspicious"))
    picked = severity_of(settled(report), "comparison")
    assert picked["comparison:quantity:suspicious:basis"] == "info"
    # The verdict still calls a suspicious reading a review, and the banner
    # follows it. Being stricter than the verdict is allowed; the reverse is not.
    assert report.status_report.banner == "review"


def test_a_low_confidence_read_of_a_matching_count_is_not_a_basis_difference():
    """The same `suspicious` status, reached for a different reason. Keying the
    level off the reason text would have called this harmless too."""
    report = report_with(quantity=stated("quantity", [
        ("d1", "Invoice", "count:1200:piece"),
        ("d2", "Packing list", "count:1200:carton"),
    ], status="suspicious"))
    report.comparisons[0].values[1].confidence = 0.2
    assert severity_of(report, "comparison")["comparison:quantity:suspicious"] == "warning"


def test_a_basis_difference_is_informational_even_when_the_counts_differ():
    """1,200 pieces against 130 cartons proves nothing about either document.

    The numbers are not comparable, which is why the engine calls it suspicious
    rather than conflicting -- and a severity that ignored that would turn a
    difference of basis into a warning about the cargo. The banner still says
    review, because the verdict floor holds regardless of this level.
    """
    report = report_with(quantity=stated("quantity", [
        ("d1", "Invoice", "count:1200:piece"),
        ("d2", "Packing list", "count:1300:carton"),
    ], status="suspicious"))
    assert severity_of(report, "comparison")[
        "comparison:quantity:suspicious:basis"] == "info"


def test_two_package_words_counting_the_same_boxes_differently_is_a_warning():
    """Cartons and pallets are both packaging, so 100 of one against 130 of the
    other is a disagreement about the shipment, not about how it is described."""
    report = report_with(quantity=stated("quantity", [
        ("d1", "Invoice", "count:100:carton"),
        ("d2", "Packing list", "count:130:pallet"),
    ], status="suspicious"))
    assert severity_of(report, "comparison")[
        "comparison:quantity:suspicious"] == "warning"


def test_a_field_only_one_document_stated_is_shown_but_never_blocks():
    report = settled(report_with(po_number=stated(
        "po_number", [("d1", "Invoice", "ref:PO-77")], status="single_source")))
    assert severity_of(report, "comparison")["comparison:po_number:single_source"] == "info"
    assert report.status_report.banner == "ready"
    assert report.status_report.info == 1
    assert "only one document stated" in report.status_report.caveat


# ---------------------------------------------------------------------------
# absence: who was asked
# ---------------------------------------------------------------------------

# The five fields a customs entry is filed on. A bill of lading carries none of
# them, so a BL-only run is the shape where "absent" means nobody was asked --
# which is what the live instance showed beside a +55 risk score.
CUSTOMS_FIELDS = ("hs_code", "invoice_value", "incoterms", "po_number",
                  "country_of_origin")
NOT_CARRIED_BY = ("bill_of_lading",)
CARRIED_BY = ("commercial_invoice", "packing_list")

DOC_LABEL = {
    "commercial_invoice": "Invoice",
    "packing_list": "Packing list",
    "bill_of_lading": "Bill of lading",
    "certificate_of_origin": "Certificate of origin",
}


def absent_on(field: str, doc_types: tuple[str, ...],
              failed: tuple[str, ...] = ()) -> Report:
    """A finished run over exactly these documents, in which nobody printed it.

    Shaped the way the engine shapes it: a document that could not be read
    produces no observation at all, so it is in `documents` and not in `values`.
    That is the case the carrier test has to survive -- the invoice was uploaded,
    it just could not be read, and blaming the shipment for that would be
    reporting our own failure as somebody else's paperwork.
    """
    report = Report(shipment_id="s1", created_at="now", updated_at="now")
    report.documents = [
        DocumentReport(id=f"d{i}", label=DOC_LABEL[dt], filename=f"{dt}.png",
                       declared_doc_type=dt,  # type: ignore[arg-type]
                       status="failed" if dt in failed else "ok",
                       error="no text could be recovered" if dt in failed else None)
        for i, dt in enumerate(doc_types, start=1)
    ]
    report.comparisons = [Comparison(
        field=field, label=FIELD_LABEL[field], kind=FIELD_KIND[field],
        status="absent", reason="not found on any document",
        values=[
            ComparisonValue(
                document_id=doc.id, document_label=doc.label,
                doc_type=doc.declared_doc_type,  # type: ignore[arg-type]
                page_index=1, raw_value=None, canonical=None, compare_key=None,
                confidence=0.0,
                expected_here=field in EXPECTED.get(doc.declared_doc_type,  # type: ignore[arg-type]
                                                    frozenset()),
            )
            for doc in report.documents if doc.status != "failed"
        ],
    )]
    report.risk = score_report(report)
    return settled(report)


def comparison_findings(report: Report) -> list[SeverityFinding]:
    return [f for f in findings(report) if f.source == "comparison"]


@pytest.mark.parametrize("field", CUSTOMS_FIELDS)
def test_a_field_no_uploaded_document_carries_is_not_checked_not_flagged(field):
    """The live-testing break: a run with no invoice in it said "HS code stated
    on no document" as a *warning* and added 55 risk points, in the same breath
    as its own reason text saying the absence was expected. Nobody was asked, so
    there is nothing to reconcile and nothing to score."""
    report = absent_on(field, NOT_CARRIED_BY)
    got = comparison_findings(report)
    assert len(got) == 1
    assert got[0].severity == "info"
    assert got[0].code == f"comparison:{field}:absent:not_carried"
    assert got[0].label == f"{FIELD_LABEL[field]} not checked"
    assert "no document that normally carries" in got[0].detail
    assert report.status_report.banner == "ready"
    assert report.risk.contributions == []
    assert report.risk.score == 0


@pytest.mark.parametrize("field", CUSTOMS_FIELDS)
def test_a_field_missing_from_a_document_that_prints_it_still_warns(field):
    report = absent_on(field, CARRIED_BY)
    got = comparison_findings(report)
    assert len(got) == 1
    assert got[0].severity == "warning"
    assert got[0].code == f"comparison:{field}:absent"
    assert got[0].label == f"{FIELD_LABEL[field]} stated on no document"
    assert report.status_report.banner == "review"


def test_only_a_carried_hs_code_costs_the_classification_points():
    """The 55 points are for a classification that cannot be verified. Where no
    uploaded document would have carried it, the field was never in reach, and
    scoring it would put a paperwork gap in the high band on its own."""
    carried = absent_on("hs_code", CARRIED_BY)
    assert [c.code for c in carried.risk.contributions] == ["hs_code_absent"]
    assert carried.risk.score == POINTS["hs_code_absent"] == HIGH_AT

    not_carried = absent_on("hs_code", NOT_CARRIED_BY)
    assert not_carried.risk.contributions == []
    assert not_carried.risk.score == 0


def test_an_unreadable_carrier_is_a_coverage_gap_not_an_expected_absence():
    report = absent_on("hs_code", ("commercial_invoice", "bill_of_lading"),
                       failed=("commercial_invoice",))
    picked = {f.code: f.severity for f in findings(report)}
    assert picked["comparison:hs_code:absent"] == "warning"
    assert picked["document:failed"] == "warning"
    assert report.status_report.banner == "review"


def test_the_ready_caveat_names_the_fields_nobody_was_asked_about():
    """`ready` with items underneath has to say what kind they are, or the
    reader cannot tell "nothing was wrong" from "nothing was looked at"."""
    report = absent_on("hs_code", NOT_CARRIED_BY)
    assert report.status_report.banner == "ready"
    assert "no uploaded document was expected to carry" in report.status_report.caveat
    assert "only one document stated" in report.status_report.caveat


def test_absence_of_an_uncarried_field_leaves_a_clean_run_ready():
    """The whole point of the split, on a report that is otherwise clean: five
    fields nobody printed, five informational notes, and a shipment that still
    moves -- with the caveat saying why the list is not empty."""
    report = absent_on(CUSTOMS_FIELDS[0], NOT_CARRIED_BY)
    report.comparisons = [absent_on(field, NOT_CARRIED_BY).comparisons[0]
                          for field in CUSTOMS_FIELDS]
    report.risk = score_report(report)
    settled(report)
    assert report.status_report.info == len(CUSTOMS_FIELDS)
    assert report.status_report.warning == 0
    assert report.status_report.banner == "ready"
    assert report.risk.score == 0


# ---------------------------------------------------------------------------
# the other stages
# ---------------------------------------------------------------------------


def test_a_document_that_did_not_add_up_warns():
    report = report_with()
    report.internal_checks = [InternalCheck(
        code="line_total_math", document_id="d1", document_label="Invoice",
        status="fail", detail="600 x 9.18 printed as 5,510.00", fields=["invoice_value"])]
    assert severity_of(report, "math")["math:fail"] == "warning"
    assert settled(report).status_report.banner == "review"


def test_a_check_that_never_ran_is_stated_without_blaming_the_shipment():
    """`not_run` has to be visible or an empty list reads as a clean bill of
    health. It must not move the banner, because nothing about the goods changed
    -- only about what this app was asked to look at."""
    report = report_with()
    report.internal_checks = [InternalCheck(
        code="line_total_sum", document_id="d1", document_label="Invoice",
        status="not_run", detail="no goods lines were transcribed")]
    settled(report)
    assert severity_of(report, "math")["math:not_run"] == "info"
    assert report.status_report.banner == "ready"
    assert report.status_report.info == 1


def test_a_value_the_page_does_not_print_warns():
    report = report_with()
    report.unverified = [UnverifiedField(
        document_id="d1", document_label="Invoice", field="po_number",
        label="PO number", value="PO-9981", evidence_text="PO NO. 9981",
        page_index=1, reason="The quote was not found on the page.")]
    assert severity_of(report, "evidence")["evidence:excluded"] == "warning"
    assert settled(report).status_report.banner == "review"


def test_an_unread_document_warns_because_coverage_is_the_finding():
    report = report_with()
    report.documents = [DocumentReport(
        id="d1", filename="scan.jpg", label="Scan", status="failed",
        error="no text could be recovered")]
    settled(report)
    assert severity_of(report, "document")["document:failed"] == "warning"
    assert report.status_report.banner == "review"


def test_the_cross_field_levels_pass_straight_through():
    report = report_with()
    report.sanity = [
        SanityFinding(code="net_above_gross", level="risk", explanation="net exceeds gross"),
        SanityFinding(code="hs_code_short", level="warn", explanation="only 4 digits"),
        SanityFinding(code="single_source_fields", level="info", explanation="one paper only"),
    ]
    picked = severity_of(report, "rule")
    assert (picked["rule:risk"], picked["rule:warn"], picked["rule:info"]) == \
        ("warning", "warning", "info")


def test_consistent_values_and_expected_absences_produce_nothing():
    report = settled(report_with(gross_weight=stated("gross_weight", [
        ("d1", "Invoice", "weight:100.000"), ("d2", "BL", "weight:100.000")])))
    assert findings(report) == []
    assert report.status_report.banner == "ready"


def test_findings_are_ordered_worst_first():
    report = report_with(
        container_number=stated("container_number", [
            ("d1", "BL", "container:MSKU3063865"),
            ("d2", "PL", "container:MSKU3063866")]),
        invoice_value=stated("invoice_value", [
            ("d1", "Invoice", "money:USD:9000.00"),
            ("d2", "Invoice", "money:USD:9050.00")]),
        po_number=stated("po_number", [("d1", "Invoice", "ref:PO-77")],
                         status="single_source"))
    levels = [f.severity for f in findings(report)]
    assert levels == sorted(levels, key=lambda s: LEVELS.index(s))
    assert levels[0] == "critical" and levels[-1] == "info"


# ---------------------------------------------------------------------------
# the banner rule, on its own
# ---------------------------------------------------------------------------


def banner_for(critical=0, warning=0, info=0, verdict="clean", risk=0, state="done"):
    items = []
    for level, count in (("critical", critical), ("warning", warning), ("info", info)):
        items += [SeverityFinding(
            code=f"test:{level}", severity=level, source="comparison",
            label=level, detail="") for _ in range(count)]
    return derive_banner(items=items, verdict=verdict, risk_score=risk, state=state)[0]


def test_any_critical_blocks_and_a_warning_alone_does_not():
    assert banner_for(critical=1, warning=3) == "blocked"
    assert banner_for(warning=1) == "review"
    assert banner_for() == "ready"


def test_informational_items_never_hold_a_shipment_back():
    assert banner_for(info=4) == "ready"


def test_a_running_or_empty_report_claims_nothing():
    assert derive_banner(items=[], verdict="running", risk_score=0, state="running")[0] \
        == "running"
    assert derive_banner(items=[], verdict="empty", risk_score=0, state="pending")[0] \
        == "empty"
    assert derive_banner(items=[], verdict="clean", risk_score=0, state="failed")[0] \
        == "ready", "a run that produced no usable documents has finished; " \
                    "saying it is still working would be the other kind of lie"


# ---------------------------------------------------------------------------
# the one property: never kinder than the verdict or the risk strip
# ---------------------------------------------------------------------------


def test_the_banner_is_never_kinder_than_the_verdict():
    """The severity table can be *stricter* than the verdict on purpose -- that
    is what the evidence and arithmetic stages added. It cannot be kinder: a
    status the engine called a conflict always has a row behind it."""
    assert banner_for(verdict="conflicts") == "review"
    assert banner_for(verdict="review") == "review"
    assert banner_for(verdict="clean") == "ready"


def test_the_banner_is_never_kinder_than_the_risk_strip():
    assert banner_for(verdict="clean", risk=MEDIUM_AT) == "review"
    assert banner_for(verdict="clean", risk=MEDIUM_AT - 1) == "ready"


def test_a_conflict_on_every_field_keeps_the_banner_at_review_or_worse():
    """One row per field rather than a table of expectations: a status the engine
    can reach and the table cannot name would surface here as `ready`."""
    for field in ALL_FIELDS:
        report = settled(conflict(field))
        banner = report.status_report.banner
        assert banner in ("blocked", "review"), f"{field} said {banner}"
        assert report.status_report.critical + report.status_report.warning >= 1, field


def test_a_suspicious_reading_on_every_field_is_at_least_a_warning():
    for field in ALL_FIELDS:
        report = settled(report_with(**{field: stated(
            field, [("d1", "A", "x:1"), ("d2", "B", "x:1")], status="suspicious")}))
        assert report.status_report.banner in ("blocked", "review"), field


def test_every_point_the_risk_strip_awards_has_a_severity_behind_it():
    """The two tables are maintained apart and read the same report, so this is
    the check that they cannot drift: any rule worth the medium band or more must
    also be worth a warning, or the banner could say ready over it.

    Below the band a point may be informational -- a field only one document
    stated is worth five points and is not a reason to hold a truck.
    """
    shapes = [conflict(field) for field in ALL_FIELDS]
    shapes.append(settled(report_with(hs_code=stated(
        "hs_code", [("d1", "A", None)], status="absent"))))
    shapes.append(settled(report_with(po_number=stated(
        "po_number", [("d1", "A", "ref:PO-1")], status="single_source"))))
    unreadable = report_with()
    unreadable.documents = [DocumentReport(
        id="d1", filename="a.jpg", label="A", status="failed", error="unreadable"),
        DocumentReport(id="d2", filename="b.jpg", label="B", status="failed",
                       error="unreadable")]
    shapes.append(settled(unreadable))
    with_sanity = report_with()
    with_sanity.sanity = [
        SanityFinding(code="net_above_gross", level="risk", explanation="x"),
        SanityFinding(code="hs_code_short", level="warn", explanation="x"),
    ]
    shapes.append(settled(with_sanity))

    for report in shapes:
        report.risk = score_report(report)
        note = status_report(report)
        for contribution in report.risk.contributions:
            if contribution.points >= MEDIUM_AT:
                assert note.banner != "ready", (
                    f"{contribution.code} is worth {contribution.points} points "
                    f"and the banner said ready")
        assert note.risk_score == report.risk.score
        assert note.verdict == report.summary.verdict


def test_the_table_backs_every_award_the_score_can_give():
    """Same invariant, from the other side: the point table is enumerated, so a
    scoring rule added without a severity row fails here instead of shipping a
    banner that ignores it."""
    reached: set[str] = set()
    for report in [conflict(field) for field in ALL_FIELDS]:
        reached |= {c.code for c in score_report(settled(report)).contributions}
    for field, status in (("hs_code", "absent"), ("hs_code", "suspicious"),
                          ("container_number", "suspicious"),
                          ("po_number", "single_source")):
        report = settled(report_with(**{field: stated(
            field, [("d1", "A", "x:1"), ("d2", "B", "x:1")], status=status)}))
        reached |= {c.code for c in score_report(report).contributions}

    unreadable = report_with()
    unreadable.documents = [DocumentReport(
        id="d1", filename="a.jpg", label="A", status="failed", error="unreadable")]
    reached |= {c.code for c in score_report(settled(unreadable)).contributions}

    with_sanity = report_with()
    with_sanity.sanity = [SanityFinding(code="net_above_gross", level="risk",
                                        explanation="x")]
    reached |= {c.code for c in score_report(settled(with_sanity)).contributions}

    # The two sanity entries in POINTS are looked up by level, not emitted as
    # contribution codes, so they are not reachable under their own name.
    untested = {c for c in POINTS if not c.startswith("sanity_")} - reached
    assert not untested, f"risk codes no shape reaches, so no severity row is " \
                         f"proven to back them: {sorted(untested)}"


# ---------------------------------------------------------------------------
# determinism, and what the banner echoes
# ---------------------------------------------------------------------------


def test_the_same_report_always_produces_the_same_banner():
    report = settled(report_with(
        hs_code=stated("hs_code", [("d1", "A", "hs:85176100"),
                                   ("d2", "B", "hs:85176200")]),
        po_number=stated("po_number", [("d1", "A", "ref:1")], status="single_source")))
    assert status_report(report).model_dump() == status_report(report).model_dump()


def test_the_banner_shows_the_verdict_and_score_it_sits_beside():
    """`ready` next to a high risk strip is the failure mode this module exists to
    prevent, so the two numbers are carried in the banner's own record -- a
    reader and a test can check they agree without recomputing anything."""
    report = settled(conflict("invoice_value", "money:USD:9000.00", "money:EUR:9000.00"))
    report.risk = score_report(report)
    note = status_report(report)
    assert note.verdict == "conflicts"
    assert note.risk_score == report.risk.score
    assert note.method == "severity-v1"
    assert note.headline.startswith("Blocked")
    assert "1 item(s) must be corrected" in note.headline


# ---------------------------------------------------------------------------
# over the shipped fixtures, through the real pipeline
# ---------------------------------------------------------------------------

DEFAULTS = Settings(_env_file=None)


def _settings(tmp_path: Path, **over):
    base = dict(
        _env_file=None, demo_mode=True, vision_provider="nvidia",
        nvidia_api_key="", nebius_api_key="",
        runs_dir=tmp_path / "runs", cache_dir=REPO / "fixtures" / "responses",
    )
    base.update(over)
    return Settings(**base)


def _uploads(shipment: str):
    index = json.loads((REPO / "fixtures" / "index.json").read_text(encoding="utf-8"))
    out = []
    for doc in index["shipments"][shipment]["documents"]:
        for fname in doc["files"]:
            out.append((fname, doc["doc_type"],
                        (REPO / "fixtures" / "images" / fname).read_bytes()))
    return out


@pytest.mark.asyncio
async def test_a_finished_run_says_the_same_thing_whichever_way_you_read_it(tmp_path):
    """The three fixtures cover a clean shipment, a contradicted one and a messy
    one, so this is where the banner meets real values rather than the ones these
    tests invented. Two claims, both about the dangerous direction: it never
    claims readiness over a verdict that withheld it, and the risk-score backstop
    never has to fire -- the table already covers every point the score awards."""
    for shipment in ("shipment_clean", "shipment_conflict", "shipment_messy"):
        rec = Reconciler(settings=_settings(tmp_path / shipment))
        sid, _ = await rec.create_shipment(_uploads(shipment))
        await rec.wait(sid, timeout=180)
        report = rec.get(sid)
        note = report.status_report

        assert note.verdict == report.summary.verdict
        assert note.risk_score == report.risk.score
        assert (note.critical, note.warning, note.info) == (
            sum(1 for f in note.findings if f.severity == "critical"),
            sum(1 for f in note.findings if f.severity == "warning"),
            sum(1 for f in note.findings if f.severity == "info"),
        )
        if report.summary.verdict in ("conflicts", "review"):
            assert note.banner != "ready", f"{shipment}: verdict " \
                f"{report.summary.verdict} with banner {note.banner}"
        if report.risk.score >= MEDIUM_AT:
            assert note.banner != "ready", f"{shipment}: risk " \
                f"{report.risk.score} with banner {note.banner}"
            fired = not any(f.severity in ("critical", "warning")
                            for f in note.findings)
            assert not fired, f"{shipment}: the backstop carried a score of " \
                f"{report.risk.score} that no table row covered"


@pytest.mark.asyncio
async def test_the_messy_shipment_is_blocked_not_merely_reviewed(tmp_path):
    """A regression pin with a reason attached. This shipment carries a container
    number that fails its own ISO 6346 check digit against another document's,
    which is the definition of the critical band: the papers describe two
    different pieces of equipment."""
    rec = Reconciler(settings=_settings(tmp_path))
    sid, _ = await rec.create_shipment(_uploads("shipment_conflict"))
    await rec.wait(sid, timeout=180)
    note = rec.get(sid).status_report
    assert note.banner == "blocked"
    assert note.critical >= 1
    assert any(f.code.startswith("comparison:") for f in note.findings
               if f.severity == "critical")


# ---------------------------------------------------------------------------
# the artifacts a reader actually files
# ---------------------------------------------------------------------------


def test_the_pdf_prints_the_status_before_anything_it_summarises():
    from app.pdf import report_html

    report = settled(conflict("container_number"))
    report.risk = score_report(report)
    report.status_report = status_report(report)
    page = report_html(report)
    assert "<h2>Status</h2>" in page
    assert "Blocked" in page
    assert page.index("<h2>Status</h2>") < page.index("Customs risk</h2>"), \
        "the banner has to be the first answer on the page, not a footnote to it"
    assert "comparison:container_number:conflict" not in page, \
        "the row keys are internal; a filed PDF should read as findings, not as " \
        "a dump of the table"


def test_the_standalone_page_shows_the_same_banner_as_the_web_app():
    from app.export import standalone_html

    report = settled(conflict("invoice_value"))
    report.risk = score_report(report)
    report.status_report = status_report(report)
    page = standalone_html(report)
    assert 'class="statusbar review"' in page
    assert "Review needed" in page


def test_a_report_written_before_the_banner_existed_still_loads():
    """Runs on disk are data, not a schema migration. An old report.json has no
    status_report at all, and the app must show it rather than fail -- which is
    also why the web layer hides the card instead of inventing a banner."""
    plain = json.loads(Report(
        shipment_id="old", created_at="t", updated_at="t").model_dump_json())
    del plain["status_report"]
    loaded = Report.model_validate(plain)
    assert loaded.status_report.banner == "empty"
    assert loaded.status_report.findings == []


@pytest.mark.asyncio
async def test_the_banner_survives_the_round_trip_through_disk(tmp_path):
    """The banner is stored rather than recomputed per request, so what the UI,
    the PDF and a reopened run show has to be the same record the file holds."""
    rec = Reconciler(settings=_settings(tmp_path))
    sid, _ = await rec.create_shipment(_uploads("shipment_conflict"))
    await rec.wait(sid, timeout=180)
    first = rec.get(sid).status_report.model_dump()

    reopened = Reconciler(settings=_settings(tmp_path)).get(sid)
    assert reopened is not rec.get(sid), "a second reader, from disk only"
    assert reopened.status_report.model_dump() == first
    assert reopened.status_report.banner == "blocked"


# ---------------------------------------------------------------------------
# the banner and the verdict: one story, or one line about the difference
# ---------------------------------------------------------------------------

# Instructions. The banner is the only line in the report allowed to use one,
# because it is the only line that has read every stage. Phrases, not words:
# "Nothing to reconcile yet" describes a state and must keep passing, while
# "reconcile before the goods move" is an order and must not.
IMPERATIVES = ("reconcile before", "before the goods move", "must be corrected",
               "do not ship", "hold this", "check the", "correct the", "resolve ")

# The pairs that read as two different answers, and so owe the reader a
# sentence. Everything else either agrees (blocked beside "conflicts found") or
# cannot happen at all (ready beside a verdict that withheld it).
MISMATCH = {("review", "conflicts"), ("review", "clean"),
            ("blocked", "clean"), ("blocked", "review")}


def test_no_verdict_line_competes_with_the_banner_for_the_instruction():
    """Break caught on the live instance: "Conflicts found — reconcile before
    the goods move" printed directly above a banner saying "Review needed". Two
    lines, two orders, and no way to tell which governs. The verdict describes
    the field table; the banner decides."""
    for verdict, headline in VERDICT_HEADLINE.items():
        low = headline.lower()
        assert not any(word in low for word in IMPERATIVES), \
            f"{verdict}: {headline!r} tells the reader what to do"


def test_the_web_app_and_the_export_word_the_verdict_identically():
    """One sentence in two languages: a Python dict and a JS object. Nothing a
    reader can notice drifting, which is exactly why it is pinned here -- the
    web app and the filed HTML would quietly disagree about the same shipment."""
    js = (REPO / "web" / "app.js").read_text(encoding="utf-8")
    block = js.split("const HEADLINE = {", 1)[1].split("};", 1)[0]
    assert dict(re.findall(r"(\w+):\s*'([^']*)'", block)) == VERDICT_HEADLINE


COMBINATIONS = [
    (critical, warning, verdict, risk)
    for critical in (0, 1)
    for warning in (0, 1)
    for verdict in ("clean", "review", "conflicts")
    for risk in (0, MEDIUM_AT)
]


def _items(critical: int, warning: int) -> list[SeverityFinding]:
    return [
        SeverityFinding(code=f"test:{level}", severity=level,  # type: ignore[arg-type]
                        source="comparison", label=level, detail="")
        for level, count in (("critical", critical), ("warning", warning))
        for _ in range(count)
    ]


@pytest.mark.parametrize("critical,warning,verdict,risk", COMBINATIONS)
def test_every_combination_reads_as_one_story(critical, warning, verdict, risk):
    """All 24 reachable shapes, enumerated rather than sampled: either the two
    lines point the same way, or exactly one sentence says why they do not."""
    banner, headline, _caveat = derive_banner(
        items=_items(critical, warning), verdict=verdict, risk_score=risk,
        state="done")
    bridge = bridge_line(banner, verdict)

    assert headline
    if banner == "ready":
        assert verdict == "clean", "the monotonic rule: never kinder than the verdict"
    if banner == "blocked":
        assert critical == 1
    assert (bridge != "") == ((banner, verdict) in MISMATCH), \
        f"{banner} beside {verdict}: {bridge!r}"
    if bridge:
        assert len(bridge.split()) <= 40, "one line means one line"
        assert bridge.endswith(".")


def test_the_one_line_reaches_the_filed_artifacts():
    """A bridge that only the web app renders is a bridge only the person who
    happened to look at the screen can cross."""
    from app.export import standalone_html
    from app.pdf import report_html

    report = settled(conflict("invoice_value"))
    report.risk = score_report(report)
    report.status_report = status_report(report)
    assert report.status_report.banner == "review"
    assert report.summary.verdict == "conflicts"
    line = report.status_report.bridge
    assert line, "this is the pair the live instance showed"
    assert line in standalone_html(report)
    assert line in report_html(report)


def test_the_web_page_has_somewhere_to_put_the_line():
    page = (REPO / "web" / "index.html").read_text(encoding="utf-8")
    js = (REPO / "web" / "app.js").read_text(encoding="utf-8")
    assert 'id="status-bridge"' in page
    assert "status-bridge" in js
