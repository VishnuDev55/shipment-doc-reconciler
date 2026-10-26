"""A printable PDF of one reconciliation.

Built from the Report model rather than converted from the web page on purpose:
the UI leans on CSS xhtml2pdf does not implement (grid, flex, custom properties),
so "print the HTML" would produce a mangled document that happens to contain the
right words. This layout is boring, tables all the way down, and it is the one an
auditor can file.

Two rules hold here as they do everywhere else in the app. Every value drawn
from a document is escaped -- text inside a shipment document is data, and it
must not be able to mark up this report. And nothing is described as live model
output unless the report says it was: the provenance line reads
``run.result_source`` and ``run.live_calls``, and each document lists the source
recorded for it, so a replayed fixture cannot pose as a call made now.
"""

from __future__ import annotations

import io
from html import escape as esc

from reportlab.pdfbase.pdfmetrics import stringWidth

from .export import STATUS_LABEL, VERDICT_HEADLINE
from .models import Report
from .sections import (SECTIONS, check_counts, not_run_groups,
                       transparency, visible_findings)

# xhtml2pdf resolves these; anything fancier is silently dropped by the renderer,
# which is exactly the failure mode that made converting the UI HTML a dead end.
CSS = """
@page { size: A4; margin: 15mm 13mm; }
body { font-family: Helvetica; font-size: 8.6pt; color: #111; }
h1 { font-size: 15pt; margin: 0 0 1mm 0; }
h2 { font-size: 10pt; margin: 6mm 0 1.5mm 0; border-bottom: 0.6pt solid #999;
     padding-bottom: 1mm; }
.sub { color: #555; font-size: 8pt; margin-bottom: 4mm; }
.banner { border: 0.8pt solid #333; padding: 2mm 2.5mm; font-size: 8.4pt;
          margin-bottom: 4mm; }
table { width: 100%; border-collapse: collapse; }
th { background: #eee; font-size: 7.2pt; text-transform: uppercase;
     letter-spacing: 0.4pt; text-align: left; padding: 1.4mm 1.8mm;
     border: 0.5pt solid #aaa; }
td { padding: 1.4mm 1.8mm; border: 0.5pt solid #bbb; font-size: 8.2pt;
     vertical-align: top; }
td.num { text-align: right; }
.st { font-weight: bold; }
.st-conflict, .st-absent { color: #a11; }
.st-suspicious { color: #9a5b00; }
.st-consistent { color: #176b2f; }
.st-single_source { color: #555; }
.mono { font-family: Courier; font-size: 7.6pt; }
/* Dense tables: many columns, long tokens. Smaller type buys the width the
   numeric columns need without forcing the identifiers to wrap. */
.dense td, .dense th { font-size: 7pt; padding: 1.1mm 1.3mm; }
.why { color: #333; }
.val { font-family: Courier; font-size: 7.4pt; }
.small { font-size: 7.6pt; color: #444; }
.risk { border: 1pt solid #333; padding: 2.5mm; margin-bottom: 3mm; }
.risk-high { border-color: #a11; }
.risk-medium { border-color: #9a5b00; }
.status { border: 1.4pt solid #333; padding: 3mm; margin-bottom: 4mm;
          font-size: 9pt; }
.status-blocked { border-color: #a11; background: #fbeeee; }
.status-review { border-color: #9a5b00; background: #fbf4e6; }
.status-ready { border-color: #176b2f; background: #eef6f0; }
.sev { font-size: 7pt; font-weight: bold; text-transform: uppercase;
       width: 16mm; }
.sev-critical { color: #a11; }
.sev-warning { color: #9a5b00; }
.draft { border: 0.8pt solid #999; padding: 2.5mm; margin-bottom: 3mm; }
.dsub { font-weight: bold; font-size: 8.6pt; margin-top: 1mm; }
/* pre-line, not pre-wrap: this renderer keeps the newlines of a <pre> under
   both, but only pre-line leaves its spaces breakable. Under pre-wrap every
   space is unbreakable, so one long letter line ran off the sheet edge. */
.draft pre { font-family: Helvetica; font-size: 8pt; white-space: pre-line;
             margin: 1.5mm 0 0 0; }
ul { margin: 1mm 0 0 4mm; padding: 0; }
li { margin-bottom: 0.8mm; }
.quote { border-left: 2pt solid #999; padding: 1mm 0 1mm 2.5mm; margin: 1.5mm 0;
         font-size: 8.2pt; }
#footer { font-size: 7pt; color: #666; }
"""

SOURCE_TEXT = {
    "live": "genuine model output",
    "live+authored": "part genuine model output, part hand-authored fixture",
    "authored": "hand-authored fixture values, produced by no model at all",
    "none": "nothing was extracted",
}

DOC_SOURCE_TEXT = {
    "live_api": "live this run",
    "cached_live": "real model output, captured earlier and replayed",
    "cached_authored": "hand-authored fixture",
    "authored": "hand-authored fixture",
    "none": "—",
}

# ---------------------------------------------------------------------------
# fitting text into columns
#
# This renderer wraps at spaces and nowhere else, so a single token wider than
# its column -- a file name, a model id like
# nvidia/nemotron-3-nano-omni-30b-a3b-reasoning -- does not wrap; it draws over
# the neighbouring cell. Every cell text therefore goes through _soft, which
# measures with the same metrics the renderer uses and inserts <br/> *only*
# inside a token too wide for its column, preferring to break after a separator
# so ids break where a reader expects. Ordinary words are never touched.
#
# The character-boundary wrapping tried earlier (-pdf-word-wrap: CJK, the only
# word-wrap this renderer implements) is deliberately not used: it breaks
# ordinary words mid-word ("Quantit/y", "Industr/ial") and its line splitter
# swallows every <br/>, which ran the per-document entries of the comparison
# table and the lines of the correction drafts together into one blob.
_CONTENT_PT = (210 - 26) * 72 / 25.4  # A4 width minus the 13mm page margins
_BREAK_AFTER = "/-_.,;:"


def _col(pct: float, pad_mm: float = 1.8) -> float:
    """Printable width in points of a column ``pct`` percent of the content box."""
    return pct / 100 * _CONTENT_PT - 2 * pad_mm * 72 / 25.4 - 1


def _split_token(token: str, avail: float, font: str, size: float) -> list[str]:
    """Break one space-free token into pieces no wider than ``avail`` points."""
    if stringWidth(token, font, size) <= avail:
        return [token]
    pieces: list[str] = []
    while token and stringWidth(token, font, size) > avail:
        lo, hi = 1, len(token)
        while lo < hi:  # longest prefix that still fits
            mid = (lo + hi + 1) // 2
            if stringWidth(token[:mid], font, size) <= avail:
                lo = mid
            else:
                hi = mid - 1
        cut = max((token.rfind(s, 0, lo) for s in _BREAK_AFTER), default=-1)
        take = cut + 1 if cut >= lo // 2 else lo
        pieces.append(token[:take])
        token = token[take:]
    if token:
        pieces.append(token)
    return pieces


def _soft(text: str, avail: float, font: str = "Helvetica", size: float = 8.2) -> str:
    """Escaped cell text, broken only where a token cannot fit its column."""
    return " ".join(
        "<br/>".join(esc(piece) for piece in _split_token(word, avail, font, size))
        for word in text.split()
    )


def _soft_pre(text: str, avail: float, font: str = "Helvetica", size: float = 8) -> str:
    """Like _soft but keeps the letter's own line breaks.

    A correction draft is filed as the letter it will be sent as, so its newlines
    are content, not layout: each is kept (the renderer turns a newline in a
    pre-line <pre> into a break), while a token too wide for the sheet -- a file
    name quoted in the letter -- is still broken rather than run off the edge.
    """
    return "\n".join(
        " ".join("<br/>".join(esc(piece)
                              for piece in _split_token(word, avail, font, size))
                 for word in line.split())
        for line in text.split("\n")
    )


def _provenance(report: Report) -> str:
    """One honest sentence about where these numbers came from.

    `result_source == "live"` means *model-produced*, not *model-produced now*:
    DEMO_MODE replays captured output, and a report that said "extracted during
    this run" while making no call would be the exact overclaim this app exists
    to avoid. So the sentence is built from the source and the call count
    together, and the per-document table carries the detail.
    """
    calls = report.run.live_calls
    made = (f"{calls} live API call(s) were made while producing it."
            if calls else "No API call was made for it.")
    source = report.run.result_source
    if source == "live" and not calls:
        origin = ("The values are genuine model output, captured in an earlier "
                  "run and replayed from this bundle.")
    else:
        origin = f"The values are {SOURCE_TEXT.get(source, source)}."
    return (f"<b>Provenance.</b> {origin} {made} "
            f"Model: {esc(report.run.model)} via {esc(report.run.vision_provider)}. "
            "Normalization and comparison are deterministic Python, not model "
            "output, and the model performs no arithmetic anywhere.")


def _header(report: Report) -> str:
    verdict = report.summary.verdict
    headline = VERDICT_HEADLINE.get(verdict, "")
    s = report.summary
    return f"""<h1>Shipment reconciliation — {esc(report.shipment_id)}</h1>
<div class="sub">Created {esc(report.created_at)} · updated {esc(report.updated_at)}
· state {esc(report.state)}</div>
<div class="banner">{_provenance(report)}</div>
<div class="banner"><b>Verdict: {esc(verdict.upper())}</b> — {esc(headline)}<br/>
<span class="small">{s.documents_ok}/{len(report.documents)} documents read ·
{s.total_fields} fields compared · {s.consistent} consistent ·
{s.suspicious} suspicious · {s.conflict} conflicting ·
{s.single_source} single-source · {s.absent} absent</span></div>"""


def _status(report: Report) -> str:
    """The banner, as the first thing on the page.

    Printed with its counts and its two neighbours (verdict, risk score) even
    though that is redundant on screen: a PDF gets forwarded to someone who will
    never see the other panels, and the whole claim of this section is that the
    three cannot disagree.
    """
    note = report.status_report
    if not note.findings and note.banner in ("empty", "running"):
        return ""
    wide = _col(91)  # the severity column is a fixed 16mm; the rest is text
    rows = "".join(
        f"<tr><td class='sev sev-{esc(f.severity)}'>{esc(f.severity)}</td>"
        f"<td><b>{_soft(f.label, wide, 'Helvetica-Bold')}</b><br/>"
        f"<span class='small'>{_soft(f.detail, wide, size=7.6)}</span>"
        + (f"<br/><span class='small'>"
           f"{_soft(' · '.join(f.documents), wide, size=7.6)}</span>"
           if f.documents else "")
        + "</td></tr>"
        for f in visible_findings(report)
    ) or "<tr><td colspan='2' class='small'>No critical or warning-level item. " \
         "What is below the line is listed in the sections after this one.</td></tr>"
    return f"""<h2>Status</h2>
<div class="status status-{esc(note.banner)}"><b>{esc(note.headline)}</b>
{f"<br/><span class='small'>{_soft(note.bridge, wide)}</span>" if note.bridge else ""}<br/>
<span class="small">Engine verdict: {esc(note.verdict)} · customs risk
{note.risk_score}/100 · {note.critical} critical, {note.warning} warning,
{note.info} informational · {esc(note.method)}</span>
{f"<br/><span class='small'>{esc(note.caveat)}</span>" if note.caveat else ""}</div>
<table><tbody>{rows}</tbody></table>"""


def _risk(report: Report) -> str:
    r = report.risk
    rows = "".join(
        f"<tr><td class='mono'>{_soft(c.code, _col(30), 'Courier', 7.6)}</td>"
        f"<td class='num'>+{c.points}</td>"
        f"<td>{_soft(c.detail, _col(62))}</td></tr>"
        for c in r.contributions
    ) or "<tr><td colspan='3' class='small'>No rule awarded a point: nothing " \
         "classification-relevant disagreed.</td></tr>"
    return f"""<h2>Customs risk</h2>
<div class="risk risk-{esc(r.level)}"><b>{esc(r.level.upper())} — {r.score}/100</b>
<span class="small"> (band boundaries: medium at {r.medium_at}, high at
{r.high_at})</span><br/><span class="small">{esc(r.method)}</span></div>
<table><thead><tr><th style="width:30%">Rule</th><th style="width:8%">Points
</th><th>Why it fired</th></tr></thead><tbody>{rows}</tbody></table>"""


def _comparisons(report: Report) -> str:
    rows = []
    field_w, why_w, val_w = _col(17), _col(33), _col(39)
    for comp in report.comparisons:
        values = []
        for v in comp.values:
            shown = (_soft(v.raw_value, val_w, "Courier", 7.4)
                     if v.raw_value else "<i>not printed</i>")
            # ASCII arrow, not U+2192: the renderer's built-in Courier has no
            # glyph for it, and a silently blank character in the one column an
            # auditor cross-checks by eye is the worst place to lose a character.
            canonical = ("<br/>&nbsp;&nbsp;-&gt; "
                         + _soft(v.canonical, val_w, "Courier", 7.4)
                         if v.canonical and v.canonical != v.raw_value else "")
            outlier = " <b>[outlier]</b>" if v.is_outlier else ""
            flags = []
            if v.warnings:
                flags.append("; ".join(v.warnings))
            # Confidence 0 is what an absent field legitimately carries, so
            # flagging it as a low-confidence read would tell the reader the
            # model was unsure about a value it never claimed.
            if v.raw_value and v.confidence < 0.5:
                flags.append(f"low confidence {v.confidence:.2f}")
            if not v.raw_value and not v.expected_here:
                flags.append("not expected on this document type")
            note = ("<br/><span class='small'>"
                    + _soft(" · ".join(flags), val_w, size=7.6) + "</span>"
                    if flags else "")
            values.append(
                f"<span class='val'>{_soft(v.document_label, val_w, 'Courier', 7.4)}"
                f" p{v.page_index}: {shown}{canonical}{outlier}</span>{note}"
            )
        value_cell = "<br/>".join(values) if values else (
            "<span class='small'>no document stated a value</span>"
        )
        rows.append(
            f"<tr><td><b>{_soft(comp.label, field_w, 'Helvetica-Bold')}</b><br/>"
            f"<span class='small mono'>{_soft(comp.field + ' · ' + comp.kind, field_w, 'Courier', 7.6)}</span></td>"
            f"<td class='st st-{esc(comp.status)}'>"
            f"{esc(STATUS_LABEL.get(comp.status, comp.status))}</td>"
            f"<td class='why'>{_soft(comp.reason, why_w)}</td>"
            f"<td>{value_cell}</td></tr>"
        )
    return f"""<h2>Field comparisons</h2>
<table><thead><tr><th style="width:17%">Field</th><th style="width:11%">Verdict
</th><th style="width:33%">Why</th><th style="width:39%">As printed on each
document</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<p class="small">Each cell above names the document and the page the value was
read from. The region itself is highlighted on screen, in report.html and in the
web app, where the page image can be drawn on; this PDF carries the page number
and the quoted line instead, because the print renderer composes text rather than
overlaying the document image, and a box drawn at a guessed position would be
evidence invented.</p>"""


def _sanity(report: Report) -> str:
    if not report.sanity:
        return ""
    items = "".join(
        f"<li><b>{esc(f.level)}</b> <span class='mono'>{esc(f.code)}</span> — "
        f"{_soft(f.explanation, _col(94), size=8.6)}</li>"
        for f in report.sanity
    )
    return f"""<h2>Cross-field checks</h2>
<p class="small">Rules no single-field comparison can see. Advisory: a finding
here cannot change a verdict above.</p><ul>{items}</ul>"""


def _internal_checks(report: Report) -> str:
    """Arithmetic inside each document, including what was never checked.

    The not_run rows are printed on purpose. A printed report that shows only
    passing checks cannot be distinguished from one where the checks never
    happened, and the reader has no way to know which they are holding.
    """
    checks = report.internal_checks
    if not checks:
        return ""
    failed = [c for c in checks if c.status == "fail"]
    passed = [c for c in checks if c.status == "pass"]
    # Passes and fails stay one row each: each is a distinct claim about a
    # distinct document. not_run is the opposite -- the same rule, the same
    # reason, once per document that has no goods lines -- so it is grouped, and
    # the grouping is defined in app/sections.py so the HTML export lists the
    # same documents this table does.
    doc_w, code_w, detail_w = _col(24, 1.3), _col(16, 1.3), _col(51, 1.3)
    not_run_rows = "".join(
        f"<tr><td>{_soft(', '.join(documents), doc_w, size=7)}</td>"
        f"<td>{_soft(code.replace('_', ' '), code_w, size=7)}</td>"
        f"<td>not run</td>"
        f"<td class='small'>{_soft(reason, detail_w, size=7.6)}"
        f"{f' <i>({len(documents)} documents)</i>' if len(documents) > 1 else ''}</td></tr>"
        for code, reason, documents in not_run_groups(report)
    )
    rows = "".join(
        f"<tr><td>{_soft(c.document_label, doc_w, size=7)}</td>"
        f"<td>{_soft(c.code.replace('_', ' '), code_w, size=7)}</td>"
        f"<td>{esc(c.status)}</td><td class='small'>{_soft(c.detail, detail_w, size=7.6)}</td></tr>"
        for c in (failed + passed)
    ) + not_run_rows
    # Counts come from the shared helper, so "not run" counts checks rather than
    # the grouped rows this table now prints.
    passed_n, failed_n, skipped_n = check_counts(report)
    return f"""<h2>Arithmetic inside each document</h2>
<p class="small">Each document checked against itself: lines that multiply out,
lines that add up to the stated total, per-row weights that sum, net under gross.
{passed_n} passed, {failed_n} failed, {skipped_n} not run. A check that
did not run is not a check that passed; three of these need the goods lines, read
only when line-item extraction is enabled.</p>
<table class="dense"><thead><tr><th style="width:24%">Document</th>
<th style="width:16%">Check</th><th style="width:9%">Status</th>
<th style="width:51%">Detail</th></tr></thead>
<tbody>{rows}</tbody></table>"""


def _unverified(report: Report) -> str:
    """Fields removed because their quoted line is not on the page.

    Printed even though it is the unflattering half of the report: a PDF that
    shows only the surviving comparisons would let a printed copy claim more than
    the analysis supports.
    """
    items = report.unverified
    if not items:
        return ""
    # Six columns the renderer splits equally; a quoted line is text from inside
    # a shipment document and can contain a token of any length at all.
    w = _col(100 / 6)
    rows = "".join(
        f"<tr><td>{_soft(u.label, w)}</td><td>{_soft(u.document_label, w)}</td>"
        f"<td class='val'>{_soft(u.value or '', w, 'Courier', 7.4)}</td>"
        f"<td class='small'>{_soft(u.evidence_text, w, size=7.6)}</td>"
        f"<td class='small'>{_soft(u.reason, w, size=7.6)}</td>"
        f"<td class='small'>"
        + (f"<i>AI-generated, advisory:</i> {_soft(u.advisor_note, w, size=7.6)}"
           f" ({_soft(u.advisor_model, w, size=7.6)})"
           if u.advisor_note else "—")
        + "</td></tr>"
        for u in items
    )
    return f"""<h2>Unverified evidence</h2>
<p class="small">{len(items)} field(s) quoted a line that does not appear on the
document, so they were excluded from the comparisons above rather than trusted.
A value nobody printed cannot disagree with another document. Any advisor note
is a model's guess at the cause, labelled as such, and has no bearing on the
exclusion.</p>
<table><thead><tr><th>Field</th><th>Document</th><th>Claimed value</th>
<th>Quoted line</th><th>Why it was dropped</th><th>Advisor note</th></tr>
<tbody>{rows}</tbody></table>"""


def _drafts(report: Report) -> str:
    """The correction drafts, printed as they will be sent.

    Monospaced and unedited on purpose: a filed copy of a letter has to read like
    the letter, and a draft re-typeset into prose is a draft that can say
    something the findings did not.
    """
    if not report.correction_emails:
        return ""
    blocks = "".join(
        f"<div class='draft'><b>To {_soft(m.recipient_role, _col(96), 'Helvetica-Bold', 8.6)}</b>"
        f"<div class='dsub'>{_soft(m.subject, _col(96), 'Helvetica-Bold', 8.6)}</div>"
        f"<div class='small'>{_soft(m.document_label, _col(96), size=7.6)} &middot; "
        f"{m.critical} critical, {m.warning} warning</div>"
        f"<pre>{_soft_pre(m.body, _col(95))}</pre></div>"
        for m in report.correction_emails
    )
    return f"""<h2>Correction drafts</h2>
<p class="small">Written by rule from the critical and warning findings above; no
language model was asked, so no figure here is one nobody printed. Nothing has
been sent -- a role is named because a role is all a document type establishes,
and no contact address exists in this report.</p>{blocks}"""


def _corrections(report: Report) -> str:
    active = [c for d in report.documents for c in d.corrections if not c.reverted_at]
    reverted = [c for d in report.documents for c in d.corrections if c.reverted_at]
    if not active and not reverted:
        return ""
    w = _col(100 / 6)  # six columns the renderer splits equally
    rows = "".join(
        f"<tr><td>{_soft(c.field, w)}</td><td>{_soft(c.document_id, w)}</td>"
        f"<td class='val'>{_soft(c.original_value or '— absent —', w, 'Courier', 7.4)}</td>"
        f"<td class='val'>{_soft(c.corrected_value, w, 'Courier', 7.4)}</td>"
        f"<td>{_soft(c.actor, w)}</td><td class='small'>{_soft(c.at, w, size=7.6)}</td></tr>"
        for c in active
    )
    table = (f"<table><thead><tr><th>Field</th><th>Document</th><th>Model said"
             f"</th><th>Corrected to</th><th>By</th><th>When</th></tr>"
             f"<tbody>{rows}</tbody></table>") if rows else ""
    back = (f"<p class='small'>{len(reverted)} correction(s) were later reverted "
            "and are kept in the audit trail.</p>") if reverted else ""
    return f"""<h2>Human corrections</h2>
<p class="small">Applied over the model's extraction; the comparison above
already reflects them.</p>{table}{back}"""


def _line_items(report: Report) -> str:
    """The goods-line section, only when it was asked for and has something in it.

    Omitted entirely when disabled. A "Goods lines: not run" heading in a formal
    artifact would read as a check that was skipped, which is true but is not
    information the paper needs about a feature that defaults off.
    """
    li = report.line_items
    if not li.enabled or not li.matches:
        return ""
    docs = li.documents
    # Item, one column per document, status, detail -- the renderer splits them
    # equally, and the document columns are headed by file names.
    w = _col(100 / (3 + len(docs)))
    head = "".join(f"<th>{_soft(d, w, 'Helvetica-Bold', 8)}</th>" for d in docs)
    rows = "".join(
        "<tr>"
        f"<td class='val'>{_soft(m.label, w, 'Courier', 7.4)}</td>"
        + "".join(f"<td class='val'>{_soft(m.quantities.get(d) or '—', w, 'Courier', 7.4)}</td>"
                  for d in docs)
        + f"<td>{_soft(m.status.replace('_', ' '), w)}</td>"
        f"<td class='small'>{_soft(m.detail, w, size=7.6)}</td>"
        "</tr>"
        for m in li.matches
    )
    note = (f"<p class='small'>{esc(li.note)}</p>") if li.note else ""
    return f"""<h2>Goods lines</h2>
<p class="small">Line by line, not total by total: {li.matched} agree,
{li.conflicts} disagree, {li.uncheckable} printed on one document only. Matched in
Python on the item number, or on an identical description once package words are
removed; nothing is scored on similarity. {esc(li.source.replace('_', ' '))}.
Advisory — these lines do not feed the verdicts above.</p>
<table><thead><tr><th>Item</th>{head}<th>Status</th><th>What it means</th></tr>
<tbody>{rows}</tbody></table>{note}"""


def _model_prose(report: Report) -> str:
    """Narrative and draft email, each labelled as the model output it is."""
    blocks = []
    for label, part in (("Executive summary", report.narrative),
                        ("Draft discrepancy email", report.briefing)):
        if not (part.text and part.status in ("ok", "cached")):
            continue
        blocks.append(
            f"<h2>{esc(label)}</h2>"
            f"<div class='quote'>{_soft(part.text, _col(96))}</div>"
            f"<div class='small'>Written by {esc(part.model)} via "
            f"{esc(part.provider)} · {esc(part.source)}. Model prose: verify "
            "every figure against the documents.</div>"
        )
    return "".join(blocks)


def _documents(report: Report) -> str:
    file_w, type_w, model_w, src_w = (_col(19, 1.3), _col(16, 1.3),
                                      _col(20, 1.3), _col(16, 1.3))
    rows = "".join(
        f"<tr><td>{_soft(d.label, file_w, size=7)}</td>"
        f"<td class='small'>{_soft(d.detected_doc_type or d.declared_doc_type or '—', type_w, size=7)}</td>"
        f"<td>{esc(d.status)}</td>"
        f"<td class='small'>{_soft(d.provider or '—', model_w, size=7)}<br/>"
        f"{_soft(d.model or '—', model_w, size=7)}</td>"
        f"<td class='small'>{_soft(DOC_SOURCE_TEXT.get(d.source, d.source), src_w, size=7)}</td>"
        f"<td class='num'>{d.usage.total if d.usage else 0}</td>"
        f"<td class='num'>{d.timings_ms}</td>"
        f"<td class='num'>{d.attempts}</td></tr>"
        for d in report.documents
    )
    notes = "".join(
        f"<p class='small'><b>{esc(d.label)}</b>: {esc(n)}</p>"
        for d in report.documents for n in d.notes
    )
    return f"""<h2>Documents read</h2>
<table class="dense"><thead><tr><th style="width:19%">File</th>
<th style="width:16%">Type</th><th style="width:9%">Status</th>
<th style="width:20%">Model</th><th style="width:16%">Source</th>
<th style="width:6%">Tok</th><th style="width:6%">ms</th>
<th style="width:8%">Try</th></tr></thead>
<tbody>{rows}</tbody></table>{notes}"""


def _transparency(report: Report) -> str:
    """Every model step, in order, with where each answer came from.

    The same list the web panel and the standalone HTML show, computed by the
    same code (`app/transparency.py`), so the three views cannot disagree about
    which model did what or whether it ran now.
    """
    from .transparency import cost_line, powered_by, source_word

    steps, tokens, ms = transparency(report)
    step_w, prov_w, model_w, src_w = (_col(21, 1.3), _col(10, 1.3),
                                      _col(28, 1.3), _col(19, 1.3))
    rows = "".join(
        f"<tr><td>{_soft(s['step'], step_w, size=7)}</td>"
        f"<td class='small'>{_soft(s['provider'], prov_w, size=7)}</td>"
        f"<td class='small'>{_soft(s['model'], model_w, size=7)}</td>"
        f"<td class='small'>{_soft(source_word(s['source']), src_w, size=7)}</td>"
        f"<td class='num'>{s['tries']}</td>"
        f"<td class='num'>{s['usage'].total if s['usage'] else 0}</td>"
        f"<td class='num'>{s['latency_ms']}</td></tr>"
        for s in steps
    )
    if not rows:
        rows = '<tr><td colspan="7" class="small">No model step ran.</td></tr>'
    return (
        "<h2>Model steps</h2>"
        "<table class='dense'><thead><tr><th style='width:21%'>Step</th>"
        "<th style='width:10%'>Provider</th><th style='width:28%'>Model</th>"
        "<th style='width:19%'>Answer came from</th><th style='width:5%'>Try</th>"
        "<th style='width:7%'>Tok</th><th style='width:10%'>ms</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        f"<p class='small'>{esc(powered_by(report))}</p>"
        f"<p class='small'>{esc(cost_line(report, tokens, ms))}</p>"
    )


_SECTION_RENDERERS = {
    "status": _status,
    "risk": _risk,
    "comparisons": _comparisons,
    "unverified": _unverified,
    "internal_checks": _internal_checks,
    "sanity": _sanity,
    "line_items": _line_items,
    "drafts": _drafts,
    "corrections": _corrections,
    "model_prose": _model_prose,
    "transparency": _transparency,
    "documents": _documents,
}


def report_html(report: Report) -> str:
    """The printable HTML. Exposed because it is the thing tests can read."""
    body = _header(report) + "".join(
        _SECTION_RENDERERS[s.key](report) for s in SECTIONS
    )
    footer = (
        f"prompt {esc(report.run.prompt_version)} · normalizer "
        f"{esc(report.run.normalizer_version)} · comparator "
        f"{esc(report.run.comparator_version)} · the versions that produced this "
        "report, not the ones installed now · generated by Shipment Doc Reconciler"
    )
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>{CSS}</style></head><body>{body}
<p id="footer" style="margin-top:6mm;font-size:7pt;color:#666">{footer}</p>
</body></html>"""


def render_pdf(report: Report) -> bytes:
    """Render the report to PDF bytes. Raises RuntimeError if the renderer fails.

    Deliberately loud on failure: a silently empty or half-written PDF would be
    the worst possible artifact for a document whose whole job is evidence.
    """
    from xhtml2pdf import pisa

    buffer = io.BytesIO()
    document = io.BytesIO(report_html(report).encode("utf-8"))
    result = pisa.CreatePDF(document, dest=buffer)
    pdf = buffer.getvalue()
    if result.err or len(pdf) < 1024 or not pdf.startswith(b"%PDF"):
        raise RuntimeError(
            f"PDF rendering failed (err={result.err}, bytes={len(pdf)}); "
            "the report itself is unaffected"
        )
    return pdf
