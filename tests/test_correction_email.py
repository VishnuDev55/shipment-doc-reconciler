"""Correction drafts: written by rule, and never by a model.

These drafts are the one place the report produces language a human forwards to
somebody else, which makes them the place where an invented detail does the most
damage. The tests are arranged around that: what may not appear, who may be
addressed, and what has to be true before a document is called the wrong one.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app.config import Settings
from app.fields import FIELD_LABEL, FIELD_ORDER
from app.correction_email import RECIPIENT_BY_DOC, draft_emails
from app.models import (
    CorrectionEmail,
    DocumentReport,
    ExtractedField,
    ExtractionResponse,
    Report,
    SeverityFinding,
    StatusReport,
)
from app.orchestrator import Reconciler
from app.textlayer import load_sidecar

REPO = Path(__file__).resolve().parents[1]


def _field(value: str, quote: str) -> ExtractedField:
    return ExtractedField(present=True, value=value, evidence_text=quote,
                          page_index=1, confidence=0.95)


def _doc(doc_id: str, label: str, doc_type: str, values: dict) -> DocumentReport:
    """A document whose response carries all ten contracted fields.

    The contract requires every key, with absent ones marked rather than omitted,
    so a test that left them out would be validating a shape no run can produce.
    """
    fields: dict = dict.fromkeys(FIELD_ORDER)
    fields.update({k: (v, f"{FIELD_LABEL[k]} {v}") for k, v in values.items()})
    return DocumentReport(
        id=doc_id, label=label, filename=label, declared_doc_type=doc_type,
        detected_doc_type=doc_type, status="ok",
        extraction=ExtractionResponse(
            doc_type=doc_type, doc_type_reason="test",
            fields={k: (_field(pair[0], pair[1]) if pair else ExtractedField(
                present=False, value=None, evidence_text="", page_index=1,
                confidence=0.0)) for k, pair in fields.items()}),
    )


def _report(findings: list[SeverityFinding], docs: list[DocumentReport],
            comparisons: list | None = None) -> Report:
    report = Report(shipment_id="SHP-1", created_at="t", updated_at="t",
                    documents=docs)
    report.comparisons = comparisons or []
    report.status_report = StatusReport(
        banner="review" if findings else "ready", findings=findings,
        warning=sum(1 for f in findings if f.severity == "warning"),
        critical=sum(1 for f in findings if f.severity == "critical"))
    return report


def _finding(code: str, severity: str, source: str, label: str, detail: str,
             *, documents: tuple[str, ...] = (), fields: tuple[str, ...] = ()):
    return SeverityFinding(code=code, severity=severity, source=source,
                           label=label, detail=detail, documents=list(documents),
                           fields=list(fields))


# ---------------------------------------------------------------------------
# what may not appear
# ---------------------------------------------------------------------------


def test_informational_findings_never_become_a_demand():
    """A field only one document stated, or a difference of counting basis, is not
    a request to change anything. Writing to somebody about it would teach them to
    skim the letters that matter."""
    report = _report([
        _finding("comparison:*:single_source", "info", "comparison",
                 "PO number checked by nobody", "appears on one document",
                 documents=("Invoice",), fields=("po_number",)),
        _finding("math:not_run", "info", "math", "line total math not checked",
                 "no goods rows were read", documents=("Invoice",)),
    ], [_doc("d1", "Invoice", "commercial_invoice", {"po_number": "PO-1"})])
    assert draft_emails(report) == []


def test_every_value_a_draft_quotes_is_a_value_a_document_printed():
    """The invariant, checked rather than asserted in a docstring.

    A draft that contained a number no document printed would be the exact thing
    the rest of the app refuses to produce, and it would leave the building in an
    email.
    """
    docs = [
        _doc("d1", "Invoice", "commercial_invoice", {"gross_weight": "12,450.00 KGS"}),
        _doc("d2", "Packing list", "packing_list", {"gross_weight": "14,250.00 KGS"}),
    ]
    from tests.test_rules import stated

    comp = stated("gross_weight", [("d1", "Invoice", "weight:12450.000"),
                                   ("d2", "Packing list", "weight:14250.000")])
    # The helper mirrors raw_value off the compare key; a real run carries what the
    # page printed, and that is the string a draft is allowed to quote.
    for value, printed_raw in zip(comp.values, ("12,450.00 KGS", "14,250.00 KGS"),
                                 strict=True):
        value.raw_value = printed_raw
    report = _report(
        [_finding("comparison:gross_weight:conflict", "warning", "comparison",
                  "Gross weight", "values differ", documents=("Invoice",),
                  fields=("gross_weight",))],
        docs, [comp])
    printed = {value.raw_value for value in comp.values if value.raw_value}
    assert printed == {"12,450.00 KGS", "14,250.00 KGS"}

    # A 1-1 split, so both parties are asked; every draft must quote only
    # printings that appear on a document.
    drafts = draft_emails(report)
    assert len(drafts) == 2
    for mail in drafts:
        for token in ("12,450.00 KGS", "14,250.00 KGS"):
            assert token in mail.body
            assert token in printed, f"{token} appears in a draft and on no document"
        assert "expected" not in mail.body.lower(), (
            "a draft may not claim an expected value; it can only report printings")


def test_no_person_is_named_and_the_absence_is_stated():
    docs = [_doc("d1", "Invoice", "commercial_invoice", {"po_number": "PO-1"})]
    report = _report([_finding("document:failed", "warning", "document",
                               "Scan could not be read", "no text recovered",
                               documents=("Invoice",))], docs)
    [mail] = draft_emails(report)
    assert "No contact address is on file" in mail.body
    assert mail.recipient_role == RECIPIENT_BY_DOC["commercial_invoice"]


def test_the_draft_says_no_model_wrote_it():
    docs = [_doc("d1", "Invoice", "commercial_invoice", {"po_number": "PO-1"})]
    report = _report([_finding("document:failed", "warning", "document",
                               "Scan could not be read", "nothing", documents=("Invoice",))],
                     docs)
    [mail] = draft_emails(report)
    assert "no language model was asked" in mail.body


# ---------------------------------------------------------------------------
# who gets written to
# ---------------------------------------------------------------------------


def _weight_conflict(majority_docs: int) -> Report:
    from tests.test_rules import stated

    values = [("d1", "Invoice", "weight:12450.000"),
              ("d2", "Packing list", "weight:12450.000"),
              ("d3", "Bill of lading", "weight:14250.000")][:majority_docs + 1]
    comp = stated("gross_weight", values)
    docs = [_doc(did, label, "commercial_invoice" if did == "d1" else "packing_list",
                 {"gross_weight": raw})
            for did, label, _key in values
            for raw in [f"{label} weight"]]
    report = _report(
        [_finding("comparison:gross_weight:conflict", "warning", "comparison",
                  "Gross weight", "values differ", documents=tuple(
                      label for _did, label, _k in values),
                  fields=("gross_weight",))],
        docs, [comp])
    return report


def test_a_strict_majority_means_only_the_odd_document_is_written_to():
    report = _weight_conflict(majority_docs=2)
    addressed = {m.document_label for m in draft_emails(report)}
    assert addressed == {"Bill of lading"}, addressed


def test_an_even_split_asks_everyone_and_says_the_report_cannot_decide():
    """Two documents, two answers, nothing on the papers that settles it. Picking
    a side would mean trusting whichever value the comparator happened to see
    first -- `majority_compare_key` resolves ties by order, not by evidence."""
    from tests.test_rules import stated

    comp = stated("gross_weight", [("d1", "Invoice", "weight:12450.000"),
                                   ("d2", "Packing list", "weight:14250.000")])
    docs = [_doc("d1", "Invoice", "commercial_invoice",
                 {"gross_weight": "12,450.00 KGS"}),
            _doc("d2", "Packing list", "packing_list",
                 {"gross_weight": "14,250.00 KGS"})]
    report = _report(
        [_finding("comparison:gross_weight:conflict", "warning", "comparison",
                  "Gross weight", "values differ", documents=("Invoice",),
                  fields=("gross_weight",))],
        docs, [comp])
    drafts = draft_emails(report)
    assert {m.document_label for m in drafts} == {"Invoice", "Packing list"}
    assert all("cannot establish which printing" in m.body for m in drafts)


def test_a_checksum_arbitration_suppresses_the_cannot_decide_line():
    """A container number that fails its own ISO 6346 check digit is inconsistent
    whatever the other paper says, so a 1-1 split is not an unresolvable tie --
    and a draft that said it was would contradict the report beside it."""
    from tests.test_rules import stated

    comp = stated("container_number", [
        ("d1", "Packing list", "container:MSKU3063865"),
        ("d2", "Bill of lading", "container:MSKU3063861")])
    comp.arbitrated_by = "iso_6346_check_digit"
    docs = [_doc("d1", "Packing list", "packing_list",
                 {"container_number": "MSKU3063865"}),
            _doc("d2", "Bill of lading", "bill_of_lading",
                 {"container_number": "MSKU3063861"})]
    report = _report(
        [_finding("comparison:container_number:conflict", "critical", "comparison",
                  "Container no.", "container numbers disagree",
                  documents=("Packing list",), fields=("container_number",))],
        docs, [comp])
    drafts = draft_emails(report)
    assert drafts
    assert all("cannot establish which printing" not in m.body for m in drafts)


def test_the_quoted_line_travels_with_the_draft():
    docs = [_doc("d1", "Invoice", "commercial_invoice",
                 {"gross_weight": "12,450.00 KGS"})]
    from tests.test_rules import stated

    comp = stated("gross_weight", [("d1", "Invoice", "weight:12450.000"),
                                   ("d2", "Packing list", "weight:14250.000")])
    report = _report(
        [_finding("comparison:gross_weight:conflict", "warning", "comparison",
                  "Gross weight", "values differ", documents=("Invoice",),
                  fields=("gross_weight",))],
        docs, [comp])
    [mail] = [m for m in draft_emails(report) if m.document_id == "d1"]
    assert 'Line quoted from your document: "Gross weight 12,450.00 KGS" (page 1)' \
        in mail.body


def test_an_arithmetic_finding_asks_for_the_arithmetic_and_owns_its_math():
    docs = [_doc("d1", "Invoice", "commercial_invoice", {"invoice_value": "USD 9.00"})]
    report = _report(
        [_finding("math:fail", "warning", "math", "Invoice: line total math",
                  "row 1: 600 x 4.85 = 2,910.00, printed 2,915.00",
                  documents=("Invoice",), fields=("invoice_value",))], docs)
    [mail] = draft_emails(report)
    assert "our calculation" in mail.body
    assert "2,915.00" in mail.body


# ---------------------------------------------------------------------------
# shape and stability
# ---------------------------------------------------------------------------


def test_the_subject_line_agrees_with_the_count():
    docs = [_doc("d1", "Invoice", "commercial_invoice", {"po_number": "PO-1"})]

    def one(count: int) -> Report:
        findings = [
            _finding("math:fail", "warning", "math", f"Invoice: rule {i}",
                     f"detail {i}", documents=("Invoice",))
            for i in range(count)]
        return _report(findings, docs)

    assert draft_emails(one(1))[0].subject.startswith("1 discrepancy ")
    assert draft_emails(one(2))[0].subject.startswith("2 discrepancies ")
    assert "1 discrepancy involving your document" in draft_emails(one(1))[0].body
    assert "discrepancy" not in draft_emails(one(2))[0].body.split("found")[1][:60]


def test_critical_items_come_first_and_the_same_input_gives_the_same_bytes():
    from tests.test_rules import stated

    docs = [_doc("d1", "Invoice", "commercial_invoice", {"po_number": "PO-1"})]
    report = _report([
        _finding("math:fail", "warning", "math", "Invoice: b rule", "b",
                 documents=("Invoice",)),
        _finding("comparison:hs_code:conflict", "critical", "comparison",
                 "HS code", "a", documents=("Invoice",), fields=("hs_code",)),
    ], docs, [stated("hs_code", [("d1", "Invoice", "hs:85176200"),
                                 ("d2", "Other", "hs:84713000")])])
    first = draft_emails(report)
    second = draft_emails(report)
    assert [m.model_dump() for m in first] == [m.model_dump() for m in second]
    assert first[0].body.index("[CRITICAL]") < first[0].body.index("[WARNING]")


def test_a_draft_round_trips_through_the_report_json():
    mail = CorrectionEmail(document_id="d1", document_label="Invoice",
                           recipient_role="the seller's desk", subject="s",
                           body="b", findings=["math:fail"], critical=0, warning=1)
    report = Report(shipment_id="s", created_at="t", updated_at="t",
                    correction_emails=[mail])
    back = Report.model_validate(json.loads(report.model_dump_json()))
    assert back.correction_emails[0].body == "b"


# ---------------------------------------------------------------------------
# through the pipeline, into the artifacts
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, demo_mode=True, vision_provider="nvidia",
                    nvidia_api_key="", nebius_api_key="",
                    runs_dir=tmp_path / "runs",
                    cache_dir=REPO / "fixtures" / "responses")


async def _conflict_run(tmp_path: Path) -> Report:
    index = json.loads((REPO / "fixtures" / "index.json").read_text(encoding="utf-8"))
    uploads, texts = [], {}
    for doc in index["shipments"]["shipment_conflict"]["documents"]:
        for fname in doc["files"]:
            uploads.append((fname, doc["doc_type"],
                            (REPO / "fixtures" / "images" / fname).read_bytes()))
            texts[fname] = load_sidecar(
                REPO / "fixtures" / "text" / f"{Path(fname).stem}.json")
    rec = Reconciler(settings=_settings(tmp_path))
    sid, _ = await rec.create_shipment(uploads, page_texts=texts)
    await rec.wait(sid, timeout=180)
    return rec.get(sid)


@pytest.mark.asyncio
async def test_a_real_conflict_writes_to_the_party_whose_paper_is_wrong(tmp_path):
    report = await _conflict_run(tmp_path)
    drafts = report.correction_emails
    assert drafts, "the conflict fixture should generate at least one draft"

    packing = [m for m in drafts if "packing_list" in m.document_label]
    assert packing, [m.document_label for m in drafts]
    body = packing[0].body
    assert "MSKU3067328" in body, body
    assert "[CRITICAL] Container no." in body
    assert "Line quoted from your document" in body
    # The arbitration is real, so the draft must not pretend it is undecided.
    assert "cannot establish which printing" not in body

    for mail in drafts:
        assert mail.critical + mail.warning == len(mail.findings)
        assert "no language model was asked" in mail.body


@pytest.mark.asyncio
async def test_a_clean_shipment_writes_no_letters(tmp_path):
    index = json.loads((REPO / "fixtures" / "index.json").read_text(encoding="utf-8"))
    uploads, texts = [], {}
    for doc in index["shipments"]["shipment_clean"]["documents"]:
        for fname in doc["files"]:
            uploads.append((fname, doc["doc_type"],
                            (REPO / "fixtures" / "images" / fname).read_bytes()))
            texts[fname] = load_sidecar(
                REPO / "fixtures" / "text" / f"{Path(fname).stem}.json")
    rec = Reconciler(settings=_settings(tmp_path))
    sid, _ = await rec.create_shipment(uploads, page_texts=texts)
    await rec.wait(sid, timeout=180)
    report = rec.get(sid)
    assert report.status_report.banner == "ready"
    assert report.correction_emails == []


@pytest.mark.asyncio
async def test_the_drafts_reach_every_artifact_a_reviewer_keeps(tmp_path):
    from app.export import build_zip, standalone_html
    from app.pdf import report_html

    report = await _conflict_run(tmp_path)
    assert report.correction_emails

    page = standalone_html(report, tmp_path / "runs" / report.shipment_id)
    assert "Correction drafts" in page
    assert "no language model was asked" in page

    printed = report_html(report)
    assert "<h2>Correction drafts</h2>" in printed

    archive = zipfile.ZipFile(__import__("io").BytesIO(
        build_zip(report, tmp_path / "runs" / report.shipment_id)))
    names = archive.namelist()
    drafts = [n for n in names if n.startswith("corrections/")]
    assert len(drafts) == len(report.correction_emails)
    readme = archive.read("README.txt").decode()
    assert f"drafts      : {len(drafts)} written by rule" in readme
    assert "corrections/" in readme
