"""One severity per finding, and one banner that follows from it by rule.

Three panels already comment on the same shipment: the verdict (does the
paperwork agree with itself), the risk strip (how exposed is a customs entry),
and the cross-field rules. Each answers a different question, and an auditor has
to read all three to get one answer. This module gives them the one answer --
not by averaging them, but by looking at every finding the other stages produced
and assigning each a severity from a single table.

The table is the only place a level is decided. Nothing here asks a model
anything, and nothing here re-reads a document: the inputs are the engine's own
statuses, so a severity can disagree with a verdict only if the verdict itself
changes.

Monotonic in one direction, deliberately and only in that direction. The banner
can be stricter than the verdict, because Task 1 and Task 2 introduced findings
the comparison never sees -- a value whose quoted line is not on the page, an
invoice whose own rows do not add up. It can never be kinder than the verdict or
the risk strip: `ready` is not reachable while either of them says otherwise.
That asymmetry is the whole claim the banner makes, and it is tested.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .compare import LOW_CONFIDENCE, carrier_uploaded, package_basis
from .fields import TYPICALLY_IN
from .models import (
    Comparison,
    InternalCheck,
    Report,
    SeverityFinding,
    StatusReport,
    effective_doc_type,
)
from .scoring import MEDIUM_AT

# The levels, and how they read on screen. Three is the most a reviewer can hold
# at once: below "warning" a finding is context, not work.
LEVELS = ("critical", "warning", "info")
LEVEL_ORDER = {"critical": 0, "warning": 1, "info": 2}

BANNER_HEADLINE = {
    "blocked": "Blocked",
    "review": "Review needed",
    "ready": "Ready to ship",
    "running": "Still reconciling",
    "empty": "Nothing to report yet",
}

# ---------------------------------------------------------------------------
# THE table. One row per (source, field, status) and nothing else decides a
# level. Tuning severity means editing this dict and nothing else -- which is
# the point of keeping it in one piece rather than inline in the builders.
#
# A row is only here if a real stage can produce it. Rows nobody can reach would
# silently become the answer when a status is added elsewhere, so the lookup
# refuses to invent a level instead: an unmapped finding produces no finding at
# all, and the completeness test in tests/test_severity.py is what keeps that
# from happening quietly.
# ---------------------------------------------------------------------------

SEVERITY_TABLE: dict[str, str] = {
    # --- cross-document comparisons -------------------------------------
    # Critical is reserved for the five things that make the shipment a
    # different shipment in the eyes of whoever clears it: the equipment it
    # moves on, who it is delivered to, what it is classified as, where it
    # comes from, and what it was sold for in which currency.
    "comparison:container_number:conflict": "critical",
    "comparison:consignee_name:conflict": "critical",
    "comparison:hs_code:conflict": "critical",
    # Two currencies on one invoice value is not a rounding gap. The engine
    # refuses to convert, so the documents are making two different claims
    # about the sale, and neither can be reconciled against the other.
    "comparison:invoice_value:conflict:currency": "critical",
    # Origin decides the duty rate and whether an FTA preference applies at
    # all, so two documents naming two countries is not a paperwork nit to
    # resolve later -- the entry cannot be filed correctly while it stands.
    # The risk strip already scored it that way (POINTS["origin_conflict"] is
    # 60, above HIGH_AT); the table said warning, which meant the banner was
    # the only reader of this report that treated it as survivable.
    "comparison:country_of_origin:conflict": "critical",
    "comparison:invoice_value:conflict": "warning",
    "comparison:gross_weight:conflict": "warning",
    "comparison:net_weight:conflict": "warning",
    "comparison:quantity:conflict": "warning",
    "comparison:incoterms:conflict": "warning",
    "comparison:po_number:conflict": "warning",
    # A near-miss is worth a look, not a block. Any field can land here,
    # including from a low-confidence read of an otherwise matching value.
    "comparison:*:suspicious": "warning",
    # Counting individual items against counting cartons is normal paperwork,
    # and the engine already says so; a difference of basis is context.
    "comparison:quantity:suspicious:basis": "info",
    "comparison:*:single_source": "info",
    # Absent splits on one question: was a document uploaded that normally
    # prints this field? If yes, a value that should have been there is missing
    # and a human has to go and look -- for the HS code the risk strip scores
    # that as a classification it cannot verify, and for the rest it is the same
    # kind of gap on a smaller scale. If no, nothing was ever going to state it:
    # a bill-of-lading-only run carries no invoice value, and calling that a
    # warning would flag every upload that is missing a document type it never
    # claimed to have. The wording of the two has to differ, because "not
    # printed here" and "printed nowhere" are different instructions to a reader.
    "comparison:*:absent": "warning",
    "comparison:*:absent:not_carried": "info",
    # --- documents ------------------------------------------------------
    # "We could not read it" is not "it agrees". Coverage gaps are the finding
    # most likely to be mistaken for a clean run.
    "document:failed": "warning",
    # --- cross-field rules (app/rules.py levels, passed through) ---------
    "rule:risk": "warning",
    "rule:warn": "warning",
    "rule:info": "info",
    # --- arithmetic inside one document ---------------------------------
    "math:fail": "warning",
    # A rule that did not run is stated so an empty findings list cannot be
    # read as "the paperwork adds up". It is information about this app, not
    # about the shipment, so it never moves the banner.
    "math:not_run": "info",
    # --- goods lines ----------------------------------------------------
    "line_item:quantity_conflict": "warning",
    "line_item:unit_differs": "info",
    "line_item:only_here": "info",
    # --- evidence verification (app/verify.py) --------------------------
    # The model quoted a line that is not on the page. Removed from the
    # comparison rather than trusted, which is a warning about the read even
    # though it cannot create a conflict.
    "evidence:excluded": "warning",
}


_MONEY_RE = re.compile(r"^money:([A-Z]{3}|UNKNOWN):")
_COUNT_RE = re.compile(r"^count:(-?\d+(?:\.\d+)?):(.*)$")


def _lookup(key: str) -> str | None:
    """Exact row first, then the wildcard row for the same status."""
    return SEVERITY_TABLE.get(key)


def _stated_currencies(comp: Comparison) -> set[str]:
    out: set[str] = set()
    for value in comp.values:
        if not value.compare_key:
            continue
        match = _MONEY_RE.match(value.compare_key)
        if match:
            out.add(match.group(1))
    return out


def _difference_of_basis(comp: Comparison) -> bool:
    """True when the counts are not a real disagreement about the goods.

    Two shapes qualify, and both are the engine's own "worth a glance" cases:
    the same number printed with different package words, and one document
    counting items while another counts the boxes they ship in -- which is a
    difference of basis, not of fact, whatever the two numbers are.

    Both halves are read from the compare keys and the confidence the model
    reported, never from the reason text: a reason is prose we format, and keying
    a severity off a sentence would break the day someone improves the sentence.
    """
    counts, bases = set(), set()
    for value in comp.values:
        if not value.compare_key:
            continue
        if value.confidence < LOW_CONFIDENCE:
            return False                 # a doubtful read is not a formatting quirk
        match = _COUNT_RE.match(value.compare_key)
        if match is None:
            return False
        counts.add(match.group(1))
        bases.add(package_basis(match.group(2)))
    if len(counts) == 1:
        return True
    return bases == {"item", "package"}


def _comparison_finding(
    comp: Comparison, doc_types: Sequence[str]
) -> SeverityFinding | None:
    if comp.status == "consistent":
        return None

    # `code` names the finding; `row` names the table entry that decided its
    # level. They differ for the wildcard rows, and a reader wants the field --
    # "comparison:*:suspicious" tells you nothing about which value to go look at.
    if comp.field == "invoice_value" and comp.status == "conflict":
        if len(_stated_currencies(comp)) > 1:
            row = code = "comparison:invoice_value:conflict:currency"
        else:
            row = code = "comparison:invoice_value:conflict"
    elif comp.field == "quantity" and comp.status == "suspicious" \
            and _difference_of_basis(comp):
        row = code = "comparison:quantity:suspicious:basis"
    else:
        code = f"comparison:{comp.field}:{comp.status}"
        if comp.status == "absent" and not carrier_uploaded(comp, doc_types):
            code += ":not_carried"
            row = "comparison:*:absent:not_carried"
        else:
            row = code if code in SEVERITY_TABLE else f"comparison:*:{comp.status}"

    severity = _lookup(row)
    if severity is None:
        return None

    documents = sorted({v.document_label for v in comp.values if v.compare_key})
    label, detail = comp.label, comp.reason
    if comp.status == "single_source":
        label = f"{comp.label} checked by nobody"
    elif comp.status == "absent":
        if row.endswith(":not_carried"):
            # Says what was not done, not what is wrong with the paperwork: the
            # engine's own reason already calls this absence expected, and a
            # finding that reads as a defect would contradict the row it sits in.
            label = f"{comp.label} not checked"
            detail = (f"Not checked: no document that normally carries "
                      f"{comp.label.lower()} was uploaded -- it is usually found "
                      f"on {TYPICALLY_IN[comp.field]}. Nothing disagreed, because "
                      f"nothing was asked. Upload one of those documents and this "
                      f"field is compared too.")
        else:
            label = f"{comp.label} stated on no document"

    return SeverityFinding(
        code=code,
        severity=severity,  # type: ignore[arg-type]
        source="comparison",
        label=label,
        detail=detail,
        fields=[comp.field],
        documents=documents,
    )


def findings(report: Report) -> list[SeverityFinding]:
    """Every finding on the report, one severity each, worst first.

    Pure over the report, and it reads only fields the earlier stages already
    wrote: no document is re-opened, no value is re-decided.
    """
    out: list[SeverityFinding] = []
    doc_types = [effective_doc_type(d) for d in report.documents]

    for comp in report.comparisons:
        finding = _comparison_finding(comp, doc_types)
        if finding is not None:
            out.append(finding)

    for doc in report.documents:
        if doc.status != "failed":
            continue
        out.append(SeverityFinding(
            code="document:failed", severity="warning", source="document",
            label=f"{doc.label} could not be read",
            detail=doc.error or "No values were recovered from this document, "
                 "so nothing on it was cross-checked.",
            documents=[doc.label],
        ))

    for finding_src in report.sanity:
        severity = _lookup(f"rule:{finding_src.level}")
        if severity is None:
            continue
        out.append(SeverityFinding(
            code=f"rule:{finding_src.level}", severity=severity,  # type: ignore[arg-type]
            source="rule",
            label=f"Cross-field: {finding_src.code.replace('_', ' ')}",
            detail=finding_src.explanation,
            fields=list(finding_src.fields),
        ))

    for check in report.internal_checks:
        if check.status == "not_run":
            continue  # grouped below
        severity = _lookup(f"math:{check.status}")
        if severity is None:
            continue
        out.append(SeverityFinding(
            code=f"math:{check.status}", severity=severity,  # type: ignore[arg-type]
            source="math",
            label=f"{check.document_label}: {check.code.replace('_', ' ')}",
            detail=check.detail,
            fields=list(check.fields),
            documents=[check.document_label],
        ))

    # A check that did not run is per rule, not per document: on a four-document
    # shipment with goods lines switched off, twelve identical notes would bury
    # the two findings that are actually about the cargo.
    skipped: dict[str, list[InternalCheck]] = {}
    for check in report.internal_checks:
        if check.status == "not_run":
            skipped.setdefault(check.code, []).append(check)
    for code in sorted(skipped):
        group = skipped[code]
        documents = sorted({g.document_label for g in group})
        reasons = sorted({g.detail for g in group})
        out.append(SeverityFinding(
            code="math:not_run", severity="info", source="math",
            label=f"{code.replace('_', ' ')} not checked",
            detail=f"{len(group)} document(s) -- {', '.join(documents)}: "
                   + "; ".join(reasons),
            fields=sorted({f for g in group for f in g.fields}),
            documents=documents,
        ))

    for item in report.unverified:
        quoted = item.value if item.value else "was claimed present"
        out.append(SeverityFinding(
            code="evidence:excluded", severity="warning", source="evidence",
            label=f"{item.document_label}: {item.label} withdrawn",
            detail=(f"The quoted line \"{item.evidence_text}\" was not found on "
                    f"page {item.page_index} of {item.document_label}, so "
                    f"{quoted} was removed from the comparison rather than "
                    f"trusted. {item.reason}"),
            fields=[item.field],
            documents=[item.document_label],
        ))

    section = report.line_items
    if section.enabled and section.status in ("ok", "partial"):
        for match in section.matches:
            key = f"line_item:{match.status}"
            severity = _lookup(key)
            if severity is None:
                continue
            out.append(SeverityFinding(
                code=key, severity=severity,  # type: ignore[arg-type]
                source="line_item",
                label=f"Goods line: {match.label}",
                detail=match.detail,
                documents=list(match.documents),
            ))

    out.sort(key=lambda f: (LEVEL_ORDER.get(f.severity, 9), f.code, f.label))
    return out


def derive_banner(
    *,
    items: list[SeverityFinding],
    verdict: str,
    risk_score: int,
    state: str,
) -> tuple[str, str, str]:
    """The banner, its headline, and a note about what it cannot see.

    `ready` requires all three of: no critical, no warning, and a verdict that
    was already clean. The last condition is what makes this monotonic -- it is
    not derivable from the first two, because the severity table looks at
    findings the comparison never considered.

    The risk-score floor is a backstop, not a second ranking. Nothing in the
    table should need it today, and tests/test_severity.py asserts that it does
    not fire on any shipped fixture; it is here so that a rule added later
    without a table row degrades to "review needed" instead of "ready to ship".
    """
    critical = sum(1 for f in items if f.severity == "critical")
    warning = sum(1 for f in items if f.severity == "warning")

    if verdict == "empty" or state in ("pending", "running") or verdict == "running":
        banner = "running" if verdict != "empty" else "empty"
        return banner, BANNER_HEADLINE[banner], ""

    if critical:
        banner = "blocked"
    elif warning or verdict in ("conflicts", "review") or risk_score >= MEDIUM_AT:
        # Three floors, any one of which holds the banner back: a warning-level
        # finding, a verdict that already withheld readiness, and the risk
        # strip's own score. The last two are why this is not simply `if warning`
        # -- see the module docstring.
        banner = "review"
    else:
        banner = "ready"

    unchecked = sum(1 for f in items if f.severity == "info")
    caveat = ""
    if banner == "ready" and unchecked:
        caveat = (f"{unchecked} informational item(s) sit below this. Some are "
                  f"fields no uploaded document was expected to carry, so nothing "
                  f"could compare them; some are fields only one document stated; "
                  f"others are arithmetic checks that were not enabled. Ready "
                  f"means nothing was found to block, not that everything was "
                  f"checked.")
    return banner, BANNER_HEADLINE[banner], caveat


def status_report(report: Report) -> StatusReport:
    """Reduce the whole report to one banner, with every finding shown beneath it."""
    items = findings(report)
    counts = {level: sum(1 for f in items if f.severity == level) for level in LEVELS}
    banner, headline, caveat = derive_banner(
        items=items,
        verdict=report.summary.verdict,
        risk_score=report.risk.score,
        state=report.state,
    )

    if counts["critical"]:
        headline = (f"{headline} — {counts['critical']} item(s) must be "
                    f"corrected before this shipment moves")
    elif counts["warning"]:
        headline = (f"{headline} — {counts['warning']} item(s) need a human "
                    f"decision")
    elif banner in ("ready", "running", "empty"):
        headline = BANNER_HEADLINE[banner]

    return StatusReport(
        banner=banner,  # type: ignore[arg-type]
        headline=headline,
        caveat=caveat,
        critical=counts["critical"],
        warning=counts["warning"],
        info=counts["info"],
        findings=items,
        verdict=report.summary.verdict,
        risk_score=report.risk.score,
    )


def apply_status(report: Report) -> Report:
    """Set the banner and the drafts that follow from it, in one place.

    The drafts are derived from the findings, so they are written here rather
    than at a call site that might read a stale banner -- an email about a
    discrepancy the report no longer reports is the exact artifact this pairing
    exists to prevent.
    """
    from .correction_email import draft_emails

    report.status_report = status_report(report)
    report.correction_emails = draft_emails(report)
    return report


__all__ = ["SEVERITY_TABLE", "apply_status", "derive_banner", "findings",
           "status_report"]
