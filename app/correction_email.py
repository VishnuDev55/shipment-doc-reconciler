"""Correction emails written by rule, from findings that already exist.

The report ends with somebody having to fix paperwork, and a reviewer who has to
retype the discrepancy into an email will retype it wrong. These drafts are
generated from the severity findings the engine already produced -- no model is
asked, so nothing can be added, softened or invented between the verdict and the
message. The existing AI briefing (`Report.briefing`, written by the optional
adjudication step) stays where it is and is labelled for what it is; this is the
default, that one is the opt-in.

Three rules the drafts are built around:

  * **Only critical and warning findings are written up.** An informational item
    -- a field one document stated alone, a difference of counting basis -- is not
    a request to change anything, and asking for it would train the recipient to
    skim.
  * **No expected value is invented.** A draft says what each document printed and
    quotes the line it came from. Where a number genuinely is derivable -- the
    product of a quantity and a price -- it is stated as *our calculation*, never
    as a fact from a document.
  * **Nobody is named who was not named on the paperwork.** Recipients are roles
    ("the seller's documentation desk"), not people, and each draft says plainly
    that no address is on file. The app reads trade documents, not contact lists.
"""

from __future__ import annotations

from collections import Counter

from .fields import DocType, FieldKey
from .models import Comparison, CorrectionEmail, Report, SeverityFinding

# Who is answerable for a document. A role, because a role is what the document
# type actually establishes; the name of a person is not on any of them.
RECIPIENT_BY_DOC: dict[DocType, str] = {
    "commercial_invoice": "the seller's documentation desk",
    "packing_list": "the shipper's packing department",
    "bill_of_lading": "the carrier's documentation desk",
    "certificate_of_origin": "the chamber of commerce that issued the certificate",
    "other": "whoever issued the document",
}

# What a recipient is being asked to do, per finding source. Phrased as a
# question about the paperwork, because that is all the report can establish: it
# has never seen the cargo.
ASK_BY_SOURCE = {
    "comparison": "Please confirm which printing is correct and reissue the "
                  "document that is wrong.",
    "math": "Please check the arithmetic on your document and reissue it. The "
            "figures quoted above are our calculation from the numbers your own "
            "document prints, not a value taken from any other paper.",
    "evidence": "Please re-send the document. A value was read from it that its "
                "page does not print, so the reading has been set aside rather "
                "than trusted.",
    "document": "Please re-send the document in a legible form; nothing could be "
                "read from it, so nothing on it was cross-checked.",
    "rule": "Please confirm the two figures against each other. They are printed "
            "on the same shipment's paperwork and cannot both be right.",
    "line_item": "Please confirm the goods lines against your own packing.",
}

WRITER = ("Shipment Doc Reconciler (written by rule from the report's findings; "
          "no language model was asked)")

NO_ADDRESS = ("(No contact address is on file. This report reads trade documents, "
              "not address books, so the recipient is a role rather than a person.)")


def _document(report: Report, label: str) -> str | None:
    for doc in report.documents:
        if doc.label == label:
            return doc.id
    return None


def _comparison_for(report: Report, finding: SeverityFinding) -> Comparison | None:
    if not finding.fields:
        return None
    field = finding.fields[0]
    return next((c for c in report.comparisons if c.field == field), None)


def _evidence_line(report: Report, document_id: str, field: FieldKey) -> str:
    doc = next((d for d in report.documents if d.id == document_id), None)
    if doc is None or doc.extraction is None:
        return ""
    extracted = doc.extraction.fields.get(field)
    if extracted is None or not extracted.evidence_text:
        return ""
    return f'"{extracted.evidence_text}" (page {extracted.page_index})'


def _winning_keys(stated: list) -> set[str]:
    """The printings that outvoted the others, if any outvoted anything.

    A strict majority is the only basis for naming one document as the odd one
    out. When two documents disagree 1-1, `majority_compare_key` resolves to
    whichever the comparator happened to see first, and a draft written on that
    would tell the wrong party their paperwork is the suspect one -- so on a tie
    everyone named in the disagreement gets the same question, and is told the
    report cannot decide between them.
    """
    counts = Counter(v.compare_key for v in stated)
    if len(counts) < 2:
        return set(counts)
    top = max(counts.values())
    leaders = {key for key, count in counts.items() if count == top}
    return leaders if top > 1 else set()


def _recipients(report: Report, finding: SeverityFinding) -> set[str]:
    """Which documents owe an answer to this finding."""
    if finding.source == "comparison":
        comp = _comparison_for(report, finding)
        if comp is None:
            return set()
        stated = [v for v in comp.values if v.compare_key]
        winners = _winning_keys(stated)
        return {v.document_id for v in stated if v.compare_key not in winners}

    out: set[str] = set()
    for label in finding.documents:
        key = _document(report, label)
        if key:
            out.add(key)
    if finding.source == "rule":
        # A cross-field finding names fields rather than papers, and a pair can be
        # impossible no matter which single document is wrong. Everyone who stated
        # one of the fields is asked.
        for comp in report.comparisons:
            if comp.field in finding.fields:
                out.update(v.document_id for v in comp.values if v.compare_key)
    return out


def _lines_for(report: Report, finding: SeverityFinding,
               recipient_id: str) -> list[str]:
    """One finding, phrased for one recipient. Never names a winner it cannot prove."""
    if finding.source == "comparison":
        comp = _comparison_for(report, finding)
        if comp is None:
            return []
        stated = [v for v in comp.values if v.compare_key]
        lines = [comp.label]
        for value in stated:
            label = "your document" if value.document_id == recipient_id \
                else value.document_label
            lines.append(f"    {label} printed {value.raw_value}")
        quote = _evidence_line(report, recipient_id, comp.field)
        if quote:
            lines.append(f"  Line quoted from your document: {quote}")
        lines.append(f"  The report's finding: {comp.reason}")
        if not _winning_keys(stated) and comp.arbitrated_by is None:
            lines.append("  The report cannot establish which printing is the "
                         "correct one: the documents split evenly and nothing on "
                         "the papers settles it. If yours agrees with the rest, no "
                         "change is needed on it.")
        return lines

    lines = [finding.label]
    for detail in (finding.detail or "").splitlines():
        if detail.strip():
            lines.append(f"  {detail.strip()}")
    if finding.source == "evidence" and finding.fields:
        quote = _evidence_line(report, recipient_id, finding.fields[0])
        if quote:
            lines.append(f"  Line the value was attributed to: {quote}")
    return lines


def _subject(count: int, label: str) -> str:
    word = "discrepancies" if count != 1 else "discrepancy"
    return f"{count} {word} on {label} -- please review and reissue"


def draft_emails(report: Report) -> list[CorrectionEmail]:
    """One draft per implicated document, worst findings first.

    Pure over the report: no model, no clock, no randomness, so the same shipment
    returns byte-identical text and two runs can be diffed against each other.
    """
    loud = [f for f in report.status_report.findings
            if f.severity in ("critical", "warning")]

    per_doc: dict[str, list[tuple[SeverityFinding, list[str]]]] = {}
    for finding in loud:
        for document_id in sorted(_recipients(report, finding)):
            lines = _lines_for(report, finding, document_id)
            if lines:
                per_doc.setdefault(document_id, []).append((finding, lines))

    out: list[CorrectionEmail] = []
    for doc in report.documents:
        entries = per_doc.get(doc.id)
        if not entries:
            continue
        entries.sort(key=lambda pair: (pair[0].severity != "critical",
                                       pair[0].code, pair[0].label))

        role = RECIPIENT_BY_DOC.get(
            doc.detected_doc_type or doc.declared_doc_type or "other",
            RECIPIENT_BY_DOC["other"])
        total = len(entries)
        body: list[str] = [
            f"To: {role}",
            NO_ADDRESS,
            "",
            f'Re: shipment {report.shipment_id}, document "{doc.label}"',
            "",
            "We reconciled the paperwork for this shipment against itself and "
            f"found {total} {'discrepancies' if total != 1 else 'discrepancy'} "
            "involving your document.",
        ]
        for index, (finding, lines) in enumerate(entries, 1):
            body.append("")
            body.append(f"{index}. [{finding.severity.upper()}] {lines[0]}")
            body.extend(lines[1:])
            if index == total:
                body.append("")
                body.append(ASK_BY_SOURCE.get(finding.source,
                                              ASK_BY_SOURCE["comparison"]))
        body += [
            "",
            "Nothing has been altered on our side. Every value above is printed on "
            "the document named beside it, and the quoted line is the line it was "
            "read from.",
            "",
            f"-- {WRITER}",
        ]

        out.append(CorrectionEmail(
            document_id=doc.id,
            document_label=doc.label,
            recipient_role=role,
            subject=_subject(total, doc.label),
            body="\n".join(body),
            findings=[f.code for f, _ in entries],
            critical=sum(1 for f, _ in entries if f.severity == "critical"),
            warning=sum(1 for f, _ in entries if f.severity == "warning"),
        ))
    return out
