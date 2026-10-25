"""One definition of the report's sections, shared by its two layouts.

`export.standalone_html` and `pdf.report_html` are two views of the same
report, styled for a browser and for paper respectively. They keep their own
markup on purpose — the print renderer implements a fraction of what a browser
can — but *what* each section shows, and the data it is built from, is decided
once here. A field, severity threshold or panel change lands in this file
instead of having to stay in step across two renderers.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Report, SeverityFinding
from .transparency import model_steps, totals


@dataclass(frozen=True)
class Section:
    """A named section of the report, in filing order."""

    key: str
    title: str


# The canonical inventory of sections, in the order the printable report files
# them. The standalone HTML shows a browser-friendly subset and its own order;
# both renderers read from this list, so the two cannot drift apart on which
# sections exist when a panel is added or removed.
SECTIONS: tuple[Section, ...] = (
    Section("status", "Status"),
    Section("risk", "Customs risk"),
    Section("comparisons", "Field comparisons"),
    Section("unverified", "Unverified evidence"),
    Section("internal_checks", "Arithmetic inside each document"),
    Section("sanity", "Cross-field checks"),
    Section("line_items", "Goods lines"),
    Section("drafts", "Correction drafts"),
    Section("corrections", "Human corrections"),
    Section("model_prose", "Model prose"),
    Section("transparency", "Model steps"),
    Section("documents", "Documents read"),
)


def visible_findings(report: Report) -> list[SeverityFinding]:
    """The findings shown as items: everything above the informational line.

    Shared by both layouts, so raising or lowering that bar is a one-line change
    here rather than an edit that has to stay in step in two renderers.
    """
    return [f for f in report.status_report.findings if f.severity != "info"]


def transparency(report: Report) -> tuple[list, int, int]:
    """The model-step table rows plus their token and latency totals."""
    steps = model_steps(report)
    tokens, ms = totals(steps)
    return steps, tokens, ms


def not_run_groups(report: Report) -> list[tuple[str, str, list[str]]]:
    """(rule, shared reason, documents) for every check that did not run.

    Grouped by (code, reason) rather than by code alone: if a second cause ever
    appears, it gets its own row instead of being hidden under the first one's
    explanation. Both renderers read this, so the PDF's grouped rows and the
    HTML's list cannot disagree about which documents were never checked.
    """
    groups: dict[tuple[str, str], list[str]] = {}
    for check in report.internal_checks:
        if check.status == "not_run":
            groups.setdefault((check.code, check.detail), []).append(check.document_label)
    return [(code, reason, documents) for (code, reason), documents in groups.items()]


def check_counts(report: Report) -> tuple[int, int, int]:
    """(passed, failed, not_run) as *checks*, not as table rows."""
    passed = sum(1 for c in report.internal_checks if c.status == "pass")
    failed = sum(1 for c in report.internal_checks if c.status == "fail")
    skipped = sum(1 for c in report.internal_checks if c.status == "not_run")
    return passed, failed, skipped
