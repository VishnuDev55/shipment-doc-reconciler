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
from pathlib import Path

import pytest

from app.config import Settings
from app.models import (
    DocumentReport,
    InternalCheck,
    Report,
    SanityFinding,
    SeverityFinding,
    UnverifiedField,
)
from app.orchestrator import Reconciler, build_summary
from app.scoring import MEDIUM_AT, POINTS, score_report
from app.severity import (
    LEVELS,
    SEVERITY_TABLE,
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


def test_the_four_shipment_defining_fields_are_the_critical_ones():
    """Critical is a short list on purpose: a report where everything is critical
    tells the reviewer to do everything, which is the same as nothing."""
    critical = {key for key, value in SEVERITY_TABLE.items() if value == "critical"}
    assert critical == {
        "comparison:container_number:conflict",
        "comparison:consignee_name:conflict",
        "comparison:hs_code:conflict",
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


def test_a_count_difference_whatever_the_words_is_a_warning():
    report = report_with(quantity=stated("quantity", [
        ("d1", "Invoice", "count:1200:piece"),
        ("d2", "Packing list", "count:1300:carton"),
    ], status="suspicious"))
    assert severity_of(report, "comparison")["comparison:quantity:suspicious"] == "warning"


def test_a_field_only_one_document_stated_is_shown_but_never_blocks():
    report = settled(report_with(po_number=stated(
        "po_number", [("d1", "Invoice", "ref:PO-77")], status="single_source")))
    assert severity_of(report, "comparison")["comparison:po_number:single_source"] == "info"
    assert report.status_report.banner == "ready"
    assert report.status_report.info == 1
    assert "only one document stated" in report.status_report.caveat


def test_absence_is_only_a_finding_when_the_report_already_treats_it_as_one():
    """A packing-list-only shipment prints no invoice value and that is normal.
    An HS code nowhere is the one absence the risk strip scores, so the banner
    agrees with it instead of inventing a second opinion."""
    absent_value = report_with(invoice_value=stated(
        "invoice_value", [("d1", "Packing list", None)], status="absent"))
    assert findings(absent_value) == []

    absent_hs = settled(report_with(hs_code=stated(
        "hs_code", [("d1", "Packing list", None)], status="absent")))
    assert severity_of(absent_hs, "comparison")["comparison:hs_code:absent"] == "warning"
    assert absent_hs.status_report.banner == "review"


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
