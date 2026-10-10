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

from .export import STATUS_LABEL, VERDICT_HEADLINE
from .models import Report

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
    rows = "".join(
        f"<tr><td class='sev sev-{esc(f.severity)}'>{esc(f.severity)}</td>"
        f"<td><b>{esc(f.label)}</b><br/><span class='small'>{esc(f.detail)}</span>"
        + (f"<br/><span class='small'>{esc(' · '.join(f.documents))}</span>"
           if f.documents else "")
        + "</td></tr>"
        for f in note.findings if f.severity != "info"
    ) or "<tr><td colspan='2' class='small'>No critical or warning-level item. " \
         "What is below the line is listed in the sections after this one.</td></tr>"
    return f"""<h2>Status</h2>
<div class="status status-{esc(note.banner)}"><b>{esc(note.headline)}</b><br/>
<span class="small">Engine verdict: {esc(note.verdict)} · customs risk
{note.risk_score}/100 · {note.critical} critical, {note.warning} warning,
{note.info} informational · {esc(note.method)}</span>
{f"<br/><span class='small'>{esc(note.caveat)}</span>" if note.caveat else ""}</div>
<table><tbody>{rows}</tbody></table>"""


def _risk(report: Report) -> str:
    r = report.risk
    rows = "".join(
        f"<tr><td class='mono'>{esc(c.code)}</td><td class='num'>+{c.points}</td>"
        f"<td>{esc(c.detail)}</td></tr>"
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
    for comp in report.comparisons:
        values = []
        for v in comp.values:
            shown = esc(v.raw_value) if v.raw_value else "<i>not printed</i>"
            # ASCII arrow, not U+2192: the renderer's built-in Courier has no
            # glyph for it, and a silently blank character in the one column an
            # auditor cross-checks by eye is the worst place to lose a character.
            canonical = (f"<br/>&nbsp;&nbsp;-&gt; {esc(v.canonical)}"
                         if v.canonical and v.canonical != v.raw_value else "")
            outlier = " <b>[outlier]</b>" if v.is_outlier else ""
            flags = []
            if v.warnings:
                flags.append("; ".join(esc(w) for w in v.warnings))
            # Confidence 0 is what an absent field legitimately carries, so
            # flagging it as a low-confidence read would tell the reader the
            # model was unsure about a value it never claimed.
            if v.raw_value and v.confidence < 0.5:
                flags.append(f"low confidence {v.confidence:.2f}")
            if not v.raw_value and not v.expected_here:
                flags.append("not expected on this document type")
            note = ("<br/><span class='small'>" + " · ".join(flags) + "</span>"
                    if flags else "")
            values.append(
                f"<span class='val'>{esc(v.document_label)} p{v.page_index}: "
                f"{shown}{canonical}{outlier}</span>{note}"
            )
        value_cell = "<br/>".join(values) if values else (
            "<span class='small'>no document stated a value</span>"
        )
        rows.append(
            f"<tr><td><b>{esc(comp.label)}</b><br/>"
            f"<span class='small mono'>{esc(comp.field)} · {esc(comp.kind)}</span></td>"
            f"<td class='st st-{esc(comp.status)}'>"
            f"{esc(STATUS_LABEL.get(comp.status, comp.status))}</td>"
            f"<td class='why'>{esc(comp.reason)}</td>"
            f"<td>{value_cell}</td></tr>"
        )
    return f"""<h2>Field comparisons</h2>
<table><thead><tr><th style="width:17%">Field</th><th style="width:11%">Verdict
</th><th style="width:33%">Why</th><th style="width:39%">As printed on each
document</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"""


def _sanity(report: Report) -> str:
    if not report.sanity:
        return ""
    items = "".join(
        f"<li><b>{esc(f.level)}</b> <span class='mono'>{esc(f.code)}</span> — "
        f"{esc(f.explanation)}</li>"
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
    skipped = [c for c in checks if c.status == "not_run"]
    passed = [c for c in checks if c.status == "pass"]
    rows = "".join(
        f"<tr><td>{esc(c.document_label)}</td><td>{esc(c.code.replace('_', ' '))}</td>"
        f"<td>{esc(c.status)}</td><td class='small'>{esc(c.detail)}</td></tr>"
        for c in (failed + skipped + passed)
    )
    return f"""<h2>Arithmetic inside each document</h2>
<p class="small">Each document checked against itself: lines that multiply out,
lines that add up to the stated total, per-row weights that sum, net under gross.
{len(passed)} passed, {len(failed)} failed, {len(skipped)} not run. A check that
did not run is not a check that passed; three of these need the goods lines, read
only when line-item extraction is enabled.</p>
<table><thead><tr><th>Document</th><th>Check</th><th>Status</th>
<th>Detail</th></tr><tbody>{rows}</tbody></table>"""


def _unverified(report: Report) -> str:
    """Fields removed because their quoted line is not on the page.

    Printed even though it is the unflattering half of the report: a PDF that
    shows only the surviving comparisons would let a printed copy claim more than
    the analysis supports.
    """
    items = report.unverified
    if not items:
        return ""
    rows = "".join(
        f"<tr><td>{esc(u.label)}</td><td>{esc(u.document_label)}</td>"
        f"<td class='val'>{esc(u.value or '')}</td>"
        f"<td class='small'>{esc(u.evidence_text)}</td>"
        f"<td class='small'>{esc(u.reason)}</td></tr>"
        for u in items
    )
    return f"""<h2>Unverified evidence</h2>
<p class="small">{len(items)} field(s) quoted a line that does not appear on the
document, so they were excluded from the comparisons above rather than trusted.
A value nobody printed cannot disagree with another document.</p>
<table><thead><tr><th>Field</th><th>Document</th><th>Claimed value</th>
<th>Quoted line</th><th>Why it was dropped</th></tr><tbody>{rows}</tbody></table>"""


def _corrections(report: Report) -> str:
    active = [c for d in report.documents for c in d.corrections if not c.reverted_at]
    reverted = [c for d in report.documents for c in d.corrections if c.reverted_at]
    if not active and not reverted:
        return ""
    rows = "".join(
        f"<tr><td>{esc(c.field)}</td><td>{esc(c.document_id)}</td>"
        f"<td class='val'>{esc(c.original_value or '— absent —')}</td>"
        f"<td class='val'>{esc(c.corrected_value)}</td>"
        f"<td>{esc(c.actor)}</td><td class='small'>{esc(c.at)}</td></tr>"
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
    head = "".join(f"<th>{esc(d)}</th>" for d in docs)
    rows = "".join(
        "<tr>"
        f"<td class='val'>{esc(m.label)}</td>"
        + "".join(f"<td class='val'>{esc(m.quantities.get(d) or '—')}</td>" for d in docs)
        + f"<td>{esc(m.status.replace('_', ' '))}</td>"
        f"<td class='small'>{esc(m.detail)}</td>"
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
            f"<div class='quote'>{esc(part.text)}</div>"
            f"<div class='small'>Written by {esc(part.model)} via "
            f"{esc(part.provider)} · {esc(part.source)}. Model prose: verify "
            "every figure against the documents.</div>"
        )
    return "".join(blocks)


def _documents(report: Report) -> str:
    rows = "".join(
        f"<tr><td>{esc(d.label)}</td>"
        f"<td class='small'>{esc(d.detected_doc_type or d.declared_doc_type or '—')}</td>"
        f"<td>{esc(d.status)}</td>"
        f"<td class='small'>{esc(d.provider or '—')}<br/>{esc(d.model or '—')}</td>"
        f"<td class='small'>{DOC_SOURCE_TEXT.get(d.source, esc(d.source))}</td>"
        f"<td class='num'>{d.usage.total if d.usage else 0}</td>"
        f"<td class='num'>{d.timings_ms}</td>"
        f"<td class='num'>{d.attempts}</td></tr>"
        for d in report.documents
    )
    return f"""<h2>Documents read</h2>
<table><thead><tr><th style="width:22%">File</th><th style="width:12%">Type</th>
<th style="width:8%">Status</th><th style="width:18%">Model</th>
<th style="width:14%">Source</th><th style="width:6%">Tok</th>
<th style="width:6%">ms</th><th style="width:5%">Try</th></tr></thead>
<tbody>{rows}</tbody></table>"""


def report_html(report: Report) -> str:
    """The printable HTML. Exposed because it is the thing tests can read."""
    body = "".join([
        _header(report), _status(report), _risk(report), _comparisons(report),
        _unverified(report),
        _internal_checks(report), _sanity(report),
        _line_items(report), _corrections(report), _model_prose(report),
        _documents(report),
    ])
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
