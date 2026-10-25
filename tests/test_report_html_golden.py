"""The PDF and the standalone HTML are two layouts of one report.

They keep separate markup on purpose -- xhtml2pdf implements a fraction of what a
browser does -- but they must not disagree about *what* the report says. These
tests pin the shared section model and the one list that is easiest to let drift:
which documents a check did not run on.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from app.export import standalone_html
from app.pdf import report_html
from app.sections import SECTIONS, not_run_groups

REPO = Path(__file__).resolve().parents[1]


def messy_report():
    from tests.test_pdf_layout import _build

    return asyncio.run(_build("shipment_messy"))


def _h2(html: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", m).strip() for m in re.findall(r"<h2[^>]*>(.*?)</h2>", html, re.S)]


def test_every_section_has_a_renderer_in_the_pdf():
    """Break caught: a section added to the shared list but not wired into the
    printable layout, so it silently disappears from the filed artifact."""
    from app.pdf import _SECTION_RENDERERS

    assert tuple(_SECTION_RENDERERS) == tuple(s.key for s in SECTIONS)


def test_both_layouts_use_only_sections_from_the_shared_model():
    """Break caught: a renderer inventing a heading that the shared inventory does
    not contain, which is how the two views start describing different reports.

    Aliases are listed explicitly: the browser layout titles some sections for a
    reader rather than for a filer, and the test says so instead of pretending the
    names are identical.
    """
    report = messy_report()
    aliases = {
        "Field comparison": "comparisons",
        "Source documents and evidence": "documents",
        "Executive summary": "model_prose",
        "Draft discrepancy email": "model_prose",
    }
    known = {s.title for s in SECTIONS} | set(aliases)
    for label, html in (("pdf", report_html(report)),
                        ("html", standalone_html(report, None))):
        unknown = [h for h in _h2(html) if h and h not in known]
        assert not unknown, f"{label} has headings outside the section model: {unknown}"


def test_both_layouts_list_the_same_documents_as_not_run():
    """Break caught: the PDF groups a document into a rule's row while the HTML
    omits it (or vice versa), so one artifact reads as if it had been checked."""
    report = messy_report()
    groups = not_run_groups(report)
    assert groups, "the messy sample is meant to have not-run checks"

    html = standalone_html(report, None)
    for code, _reason, documents in groups:
        for document in documents:
            assert document in html, f"{code}: {document} missing from report.html"

    # And the HTML must not claim a rule ran on a document the PDF excluded.
    html_rules = re.findall(r"<code>([a-z ]+)</code> — not run on (.*?):", html)
    assert html_rules, "the HTML not-run list did not render"
    for rule, documents in html_rules:
        listed = {d.strip() for d in documents.split(",")}
        matching = [docs for code, _reason, docs in groups
                    if code.replace("_", " ") == rule]
        assert matching, f"{rule!r} is not a rule the shared model knows"
        assert listed == set(matching[0]), (
            f"{rule}: HTML lists {listed}, model says {set(matching[0])}")
