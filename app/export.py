"""Reconciliation export: ZIP with JSON + CSV + a standalone HTML report.

The HTML report embeds its CSS and its evidence images as data URIs so it
opens correctly from anywhere — emailed, opened from the zip without
extracting, or archived. No CDN, no network, no relative-path assumptions.
"""

from __future__ import annotations

import base64
import csv
import html
import io
import json
import zipfile
from pathlib import Path

from .config import COMPARATOR_VERSION, NORMALIZER_VERSION, PROMPT_VERSION
from .fields import FIELD_LABEL, FIELD_ORDER
from .models import Report
from .sections import (check_counts, not_run_groups, transparency,
                       visible_findings)
from .transparency import cost_line, powered_by, source_word

STATUS_LABEL = {
    "consistent": "Consistent",
    "suspicious": "Suspicious",
    "conflict": "Conflict",
    "single_source": "Single source",
    "absent": "Absent",
}

STATUS_ICON = {
    "consistent": "✓",
    "suspicious": "⚠",
    "conflict": "✗",
    "single_source": "•",
    "absent": "–",
}

# One sentence per verdict, shared word for word with the HEADLINE map in
# web/app.js -- tests/test_severity.py fails if the two drift, because the web
# app and the filed HTML describing the same shipment differently is a
# disagreement nobody would notice until they had both open.
#
# Descriptive, never imperative: the banner in app/severity.py is the only line
# in the report that tells a reader what to do, and it is the only one that has
# read every stage. A verdict that also issued an instruction would be a second
# answer beside the first.
VERDICT_HEADLINE = {
    "clean": "All cross-checked fields agree",
    "review": "No hard conflicts — a few fields deserve a glance",
    "conflicts": "Conflicts found in the field table",
    "running": "Extracting…",
    "empty": "Nothing to reconcile yet",
}


# A cell whose first character is one of these is a formula to a spreadsheet.
# The tab and CR cases matter as much as the obvious ones: Excel strips leading
# whitespace before deciding, so "\t=1+1" is a formula that looks like text.
_FORMULA_STARTS = ("=", "+", "@", "\t", "\r")


def _csv_cell(value: object) -> str:
    """A value a spreadsheet reads as text, not as a program.

    report.csv is built from document text, and a spreadsheet treats a cell
    beginning `=`, `+`, `-` or `@` as an expression. Opening the export is enough
    to run it -- no click, no macro prompt -- and the person opening it is the
    reviewer, who is exactly the account the payload wants.

    A leading apostrophe is the conventional way to say "this is text": Excel and
    Sheets hide it, and every other reader gets the original string with one
    harmless character in front. `-` is exempted when what follows is a plain
    number, because a negative amount is a common, honest value and prefixing all
    of them would deface every export to stop `-2+3`.
    """
    text = "" if value is None else str(value)
    if text[:1] in _FORMULA_STARTS:
        return "'" + text
    if text[:1] == "-":
        try:
            float(text.replace(",", ""))
        except ValueError:
            return "'" + text
    return text


def report_csv(report: Report) -> str:
    """Long format: one row per field per document. Pivots cleanly in a sheet."""
    buf = io.StringIO()
    writer = csv.writer(buf)

    def emit(cells: list[object]) -> None:
        writer.writerow([_csv_cell(c) for c in cells])

    writer.writerow(
        [
            "shipment_id", "field", "field_label", "kind", "field_status",
            "document", "document_type", "page", "raw_value", "normalized_value",
            "compare_key", "confidence", "outlier", "present", "warnings", "reason",
        ]
    )
    for comp in report.comparisons:
        for v in comp.values:
            emit(
                [
                    report.shipment_id,
                    comp.field,
                    comp.label,
                    comp.kind,
                    STATUS_LABEL.get(comp.status, comp.status),
                    v.document_label,
                    v.doc_type,
                    v.page_index,
                    v.raw_value or "",
                    v.canonical or "",
                    v.compare_key or "",
                    f"{v.confidence:.2f}",
                    "yes" if v.is_outlier else "",
                    "yes" if v.raw_value else "no",
                    " | ".join(v.warnings),
                    comp.reason,
                ]
            )
    # The excluded rows go in too. A CSV that only lists what survived would let
    # somebody pivot a shipment to "no problems" while a value nobody printed sat
    # quietly outside the file.
    for u in report.unverified:
        emit(
            [
                report.shipment_id, u.field, u.label, "", "excluded: evidence not found",
                u.document_label, "", u.page_index, u.value or "", "", "", "", "", "yes",
                "", u.reason,
            ]
        )
    return buf.getvalue()


def _slug(label: str) -> str:
    """A filename from a document label, without letting the label choose it."""
    keep = "".join(c if c.isalnum() or c in "-_." else "_" for c in label)
    return keep.strip("_.")[:60] or "document"


def _evidence_data_uri(run_dir: Path | None, image_path: str) -> str | None:
    if run_dir is None:
        return None
    path = run_dir / image_path
    # image_path is written by ingest (pages/<n>.jpg), so a run read from disk
    # cannot carry a traversal. Checked anyway: this embeds bytes into a
    # standalone HTML file that ships with no CSP, and the cost of anchoring the
    # join is one comparison.
    base = run_dir.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(base):
        return None
    if not resolved.is_file():
        return None
    b64 = base64.b64encode(resolved.read_bytes()).decode()
    return f"data:image/jpeg;base64,{b64}"


def standalone_html(report: Report, run_dir: Path | None = None) -> str:
    esc = html.escape
    e = _evidence_data_uri

    rows = []
    for comp in report.comparisons:
        cells = []
        for v in comp.values:
            cls = "val"
            if v.is_outlier:
                cls += " outlier"
            if not v.raw_value:
                cls += " missing"
            warn = f'<div class="warn">{esc("; ".join(v.warnings))}</div>' if v.warnings else ""
            conf = f'<span class="conf">{v.confidence:.2f}</span>' if v.raw_value else ""
            cells.append(
                f'<td class="{cls}"><div class="doc">{esc(v.document_label)}</div>'
                f'<div class="raw">{esc(v.raw_value or "not printed on this document")}</div>'
                f'<div class="can">{esc(v.canonical or "")}</div>{conf}{warn}</td>'
            )
        rows.append(
            f'<tr><th><div class="fname">{esc(comp.label)}</div>'
            f'<div class="fkey">{esc(comp.field)} · {esc(comp.kind)}</div></th>'
            f'<td class="status {comp.status}"><span>{STATUS_ICON.get(comp.status,"")}</span>'
            f'{esc(STATUS_LABEL.get(comp.status, comp.status))}</td>'
            f'<td class="reason">{esc(comp.reason)}</td>{"".join(cells)}</tr>'
        )

    doc_rows = []
    for doc in report.documents:
        pages_html = ""
        for ref in doc.pages:
            uri = e(run_dir, ref.image_path)
            if not uri:
                continue
            # The same rectangles the web app draws, from the same source: the PDF
            # page's own text layer. A page with no positioned lines gets no box --
            # and no implication that one was available.
            marks, placed = "", []
            for key, ver in (doc.verification or {}).items():
                box = ver.box
                if box is None or not ver.highlightable or ver.box_page != ref.index:
                    continue
                x0, y0, x1, y1 = box
                pad = 0.4                        # percentage points, to make a glyph-tight box visible
                marks += (
                    f'<span class="pgbox" style="left:{max(x0 * 100 - pad, 0):.2f}%;'
                    f'top:{max(y0 * 100 - pad, 0):.2f}%;'
                    f'width:{(x1 - x0) * 100 + 2 * pad:.2f}%;'
                    f'height:{(y1 - y0) * 100 + 2 * pad:.2f}%"></span>'
                )
                placed.append(FIELD_LABEL.get(key, key))
            caption = f'{esc(doc.label)} — page {ref.index}'
            if placed:
                caption += (f' · outlined: {esc(", ".join(sorted(placed)))}'
                            ' (positioned from this file’s own text layer)')
            pages_html += (
                f'<figure><span class="pgwrap"><img src="{uri}" alt="{esc(doc.label)} '
                f'page {ref.index}" loading="lazy">{marks}</span>'
                f'<figcaption>{caption}</figcaption></figure>'
            )
        evidence_rows = ""
        if doc.extraction:
            for key in FIELD_ORDER:
                f = doc.extraction.fields.get(key)
                if not f or not f.present:
                    continue
                evidence_rows += (
                    f'<tr><td>{esc(FIELD_LABEL[key])}</td>'
                    f'<td><code>{esc(f.value or "")}</code></td>'
                    f'<td>{esc(f.evidence_text)}</td><td>p{f.page_index}</td>'
                    f'<td>{f.confidence:.2f}</td></tr>'
                )
        notes_html = "".join(f'<div class="note">{esc(n)}</div>' for n in doc.notes)
        doc_rows.append(
            f'<details><summary><b>{esc(doc.label)}</b> '
            f'<span class="pill {doc.status}">{esc(doc.status)}</span> '
            f'<span class="muted">{esc(doc.detected_doc_type or doc.declared_doc_type or "?")} · '
            f'{len(doc.pages)} page(s) · {doc.attempts} attempt(s)</span></summary>'
            f'{notes_html}'
            f'<table class="ev"><thead><tr><th>Field</th><th>Verbatim value</th>'
            f'<th>Evidence line</th><th>Page</th><th>Conf.</th></tr></thead>'
            f'<tbody>{evidence_rows or "<tr><td colspan=5 class=muted>nothing extracted</td></tr>"}</tbody></table>'
            f'<div class="pages">{pages_html}</div></details>'
        )

    s = report.summary
    n = report.narrative
    if n.status in ("ok", "cached") and n.text:
        label = "AI-generated summary"
        note = (
            "Written by a language model from the verdicts below; it does not "
            "replace them."
            if n.source in ("generated", "cached")
            else "Hand-authored demo text, shown because DEMO_MODE has no "
                 "cached model narrative for these verdicts."
        )
        narrative_html = (
            '<div class="narrative"><div class="nhead">'
            f'<span class="ntag">{esc(label)}</span>'
            f'<span class="nsrc">{esc(n.provider)} · <code>{esc(n.model)}</code></span></div>'
            f'<p>{esc(n.text)}</p><div class="nfoot">{esc(note)}</div></div>'
        )
    else:
        narrative_html = ""

    risk = report.risk
    contributions_html = "".join(
        f'<li><b>+{c.points}</b> <code>{esc(c.code)}</code> — {esc(c.detail)}</li>'
        for c in risk.contributions
    )
    risk_html = (
        f'<div class="risk {esc(risk.level)}"><b>risk: {esc(risk.level)}'
        f' ({risk.score}/100)</b>'
        f'<span>{esc("; ".join(risk.reasons))}.</span>'
        f'<ul class="contrib">{contributions_html}</ul>'
        '<em>Scored by rule from the verdicts above — no model was asked. '
        f'{risk.score} points, band boundaries at {risk.medium_at} and '
        f'{risk.high_at}.</em></div>'
        if risk.reasons else ""
    )
    # The same grouped not-run disclosure the PDF prints, from the same helper in
    # app/sections.py: a check that did not run must be visible in both artifacts,
    # and neither may list a different set of documents than the other.
    if report.internal_checks:
        passed_n, failed_n, skipped_n = check_counts(report)
        groups = "".join(
            f'<li><code>{esc(code.replace("_", " "))}</code> — not run on '
            f'{esc(", ".join(docs))}: {esc(reason)}</li>'
            for code, reason, docs in not_run_groups(report))
        checks_html = (
            '<h2>Arithmetic inside each document</h2>'
            f'<p class="sub">{passed_n} passed, {failed_n} failed, {skipped_n} not '
            'run. A check that did not run is not a check that passed.</p>'
            + (f'<ul class="checks">{groups}</ul>' if groups else ''))
    else:
        checks_html = ""
    sanity_html = "".join(
        f'<div class="sanity {esc(f.level)}"><b>{esc(f.level)}</b>'
        f'<span>{esc(f.explanation)}</span></div>'
        for f in report.sanity
    )

    emails_html = "".join(
        '<div class="draft"><h3>To ' + esc(m.recipient_role) + '</h3>'
        '<p class="dsub">' + esc(m.subject) + '</p>'
        '<p class="dmeta">' + esc(m.document_label) + ' &middot; '
        + str(m.critical) + ' critical, ' + str(m.warning) + ' warning</p>'
        '<pre>' + esc(m.body) + '</pre></div>'
        for m in report.correction_emails
    )
    emails_block = (
        '<h2>Correction drafts</h2><p class="sub">Written by rule from the '
        'critical and warning findings above. No language model was asked, so no '
        'figure here is one nobody printed. Nothing is sent: a role is named '
        'because a role is all a document type establishes.</p>' + emails_html
        if emails_html else ""
    )

    status = report.status_report
    status_items = "".join(
        f'<li class="sev-{esc(f.severity)}"><b>{esc(f.severity)}</b> '
        f'{esc(f.label)} — {esc(f.detail)}'
        + (f' <em>{esc(" · ".join(f.documents))}</em>' if f.documents else "")
        + "</li>"
        for f in visible_findings(report)
    )
    status_html = (
        f'<div class="statusbar {esc(status.banner)}"><b>{esc(status.headline)}</b>'
        + (f'<span class="bridge">{esc(status.bridge)}</span>' if status.bridge else "")
        + f'<span>engine verdict {esc(status.verdict)} · customs risk '
        f'{status.risk_score}/100 · {status.critical} critical, {status.warning} '
        f'warning, {status.info} informational</span>'
        + (f'<span class="caveat">{esc(status.caveat)}</span>' if status.caveat else "")
        + (f'<ul class="findings">{status_items}</ul>' if status_items else "")
        + "</div>"
        if status.banner else ""
    )

    b = report.briefing
    briefing_html = (
        '<div class="briefing"><div class="nhead"><span class="ntag">Draft '
        'discrepancy email</span><span class="nsrc">'
        f'{esc(b.provider)} · <code>{esc(b.model)}</code> · {esc(b.source)}</span></div>'
        f'<pre>{esc(b.text)}</pre>'
        '<div class="nfoot">Written from the verdicts above. Verify every figure '
        'against the documents before sending.</div></div>'
        if b.status == "ok" and b.text else ""
    )

    src = getattr(report.run, "result_source", "authored")
    live_calls = getattr(report.run, "live_calls", 0)
    model = esc(report.run.model)
    mode = esc(report.run.json_schema_mode)
    if src == "live" and live_calls:
        banner = (
            f'<div class="banner live">Live — {live_calls} document(s) read by '
            f"<code>{model}</code> on this run ({mode} mode).</div>"
        )
    elif src == "live":
        # Every value came from a real model response, but from the cache, not
        # from a call made now. Saying "live" here would overstate it.
        banner = (
            f'<div class="banner replay">Model-backed, replayed from cache — every '
            f"value came from a real <code>{model}</code> response captured earlier; "
            "no API call was made for this run.</div>"
        )
    elif src == "authored":
        banner = (
            '<div class="banner">Demo cache — these extractions were hand-authored, '
            "not produced by a model.</div>"
        )
    elif src == "live+authored":
        banner = (
            '<div class="banner mixed">Mixed provenance — some values were read by '
            "the model, others came from the hand-authored fixture cache.</div>"
        )
    else:
        banner = ""
    steps, tokens, ms = transparency(report)
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shipment reconciliation {esc(report.shipment_id)}</title>
<style>
:root {{ --ok:#0a7d3c; --warn:#8a5a00; --fail:#b00020; --line:#d8d8d8; --muted:#666; }}
* {{ box-sizing:border-box; }}
body {{ font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  margin:0; padding:24px; color:#111; background:#fafafa; }}
h1 {{ font-size:20px; margin:0 0 4px; }} h2 {{ font-size:16px; margin:28px 0 8px; }}
.sub {{ color:var(--muted); font-size:12px; margin-bottom:16px; }}
.verdict {{ padding:10px 14px; border-radius:6px; font-weight:600; margin:12px 0 20px; }}
.verdict.clean {{ background:#e6f4ea; color:var(--ok); }}
.verdict.review {{ background:#fff4e0; color:var(--warn); }}
.verdict.conflicts {{ background:#fde8e8; color:var(--fail); }}
.verdict.running,.verdict.empty {{ background:#eef1f4; color:var(--muted); }}
.banner {{ background:#fff4e0; border:1px solid #e8c98a; padding:8px 12px; border-radius:6px;
  font-size:12px; margin-bottom:16px; }}
.banner.live {{ background:#e6f4ea; border-color:#b7e0c3; color:#0a7d3c; }}
.banner.mixed {{ background:#eef4fb; border-color:#b8d4ee; color:#1a4d7a; }}
.banner.replay {{ background:#eef4fb; border-color:#b8d4ee; color:#1a4d7a; }}
table.cmp {{ width:100%; border-collapse:collapse; background:#fff; }}
table.cmp th, table.cmp td {{ border:1px solid var(--line); padding:8px 10px; vertical-align:top; text-align:left; overflow-wrap:anywhere; }}
/* The same long ids the PDF has to wrap: a model name is one unbreakable
   token to CSS unless told otherwise, and the PDF's fix must not leave the
   HTML export with the overflow it was fixed for. */
table.prov td, table.ev td {{ overflow-wrap:anywhere; }}
table.cmp thead th {{ background:#f0f0f0; font-size:11px; text-transform:uppercase; letter-spacing:.4px; }}
table.cmp tbody th {{ background:#fcfcfc; min-width:130px; }}
.fname {{ font-weight:600; }} .fkey {{ font-size:10px; color:var(--muted); font-family:ui-monospace,monospace; }}
td.status {{ white-space:nowrap; font-weight:600; width:130px; }}
td.status span {{ display:inline-block; min-width:34px; margin-right:4px; }}
td.status.consistent {{ color:var(--ok); }} td.status.suspicious {{ color:var(--warn); }}
td.status.conflict {{ color:var(--fail); }} td.status.single_source,td.status.absent {{ color:var(--muted); }}
td.reason {{ font-size:12px; max-width:330px; }}
td.val {{ min-width:150px; font-size:12px; }}
td.val .doc {{ font-size:10px; color:var(--muted); text-transform:uppercase; letter-spacing:.3px; }}
td.val .raw {{ font-family:ui-monospace,SFMono-Regular,monospace; }}
td.val .can {{ color:#0366d6; }}
td.val.missing {{ color:var(--muted); font-style:italic; }}
td.val.outlier {{ background:#fde8e8; }}
.warn {{ color:var(--warn); font-size:11px; margin-top:2px; }}
.conf {{ font-size:10px; color:var(--muted); }}
details {{ background:#fff; border:1px solid var(--line); border-radius:6px; padding:8px 12px; margin-bottom:8px; }}
summary {{ cursor:pointer; }}
.pill {{ font-size:10px; padding:1px 7px; border-radius:10px; background:#e6f4ea; color:var(--ok); }}
.pill.failed {{ background:#fde8e8; color:var(--fail); }}
.pill.partial,.pill.running {{ background:#fff4e0; color:var(--warn); }}
.pill.cached {{ background:#eef1f4; color:var(--muted); }}
.muted {{ color:var(--muted); font-size:12px; }}
.note {{ margin:8px 0; padding:7px 10px; border-left:3px solid #8896a5; background:#f4f6f8; font-size:12px; }}
table.ev {{ width:100%; border-collapse:collapse; margin:10px 0; font-size:12px; }}
table.ev td, table.ev th {{ border-bottom:1px solid var(--line); padding:4px 6px; text-align:left; }}
.pages {{ display:flex; flex-wrap:wrap; gap:10px; }}
.pages figure {{ margin:0; }} .pages img {{ max-height:330px; border:1px solid var(--line); }}
.pages figcaption {{ font-size:11px; color:var(--muted); }}
.pgwrap {{ position:relative; display:inline-block; overflow:hidden; }}
.draft {{ border:1px solid #cfd5dd; border-left:4px solid #0e6a63;
  border-radius:6px; padding:10px 12px; margin:10px 0; }}
.draft h3 {{ margin:0; font-size:13px; }}
.draft pre {{ margin:8px 0 0; font-family:inherit; font-size:12px;
  white-space:pre-wrap; background:#f7f9fb; padding:9px 11px; border-radius:4px; }}
.dsub {{ margin:3px 0 0; font-weight:600; font-size:12px; }}
.dmeta {{ margin:2px 0 0; font-size:11px; color:var(--muted); }}
.pgbox {{ position:absolute; border:2px solid #0e6a63;
  background:rgba(14,106,99,.14); pointer-events:none; border-radius:3px; }}
footer {{ margin-top:26px; padding-top:12px; border-top:1px solid var(--line);
  font-size:11px; color:var(--muted); }}
code {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11px; }}
.narrative {{ background:#f7f9fc; border:1px solid #cfe0f5; border-left:4px solid #2f6fb5;
  border-radius:6px; padding:10px 14px; margin:0 0 18px; }}
.nhead {{ display:flex; gap:10px; align-items:baseline; flex-wrap:wrap; margin-bottom:6px; }}
.ntag {{ font-size:11px; font-weight:700; color:#1a4d7a; text-transform:uppercase;
  letter-spacing:.4px; }}
.nsrc {{ font-size:10px; color:var(--muted); }}
.narrative p {{ margin:0; font-size:14px; line-height:1.5; }}
.nfoot {{ margin-top:6px; font-size:10px; color:var(--muted); font-style:italic; }}
.risk {{ display:flex; gap:10px; flex-wrap:wrap; align-items:baseline; margin:10px 0;
  padding:9px 11px; font-size:12px; border-left:4px solid #8896a5; background:#f2f4f7; }}
.risk b {{ text-transform:uppercase; font-size:10px; letter-spacing:.6px; }}
.risk em {{ flex-basis:100%; font-size:10px; color:var(--muted); }}
.contrib {{ flex-basis:100%; margin:4px 0 0; padding-left:18px; font-size:11px; }}
.contrib li {{ margin:2px 0; }}
.contrib b {{ color:#1a4d7a; font-variant-numeric:tabular-nums; }}
.contrib code {{ font-size:10px; }}
.risk.high {{ border-left-color:#b3261e; background:#fbeceb; }}
.risk.medium {{ border-left-color:#8a5a00; background:#fbf3e2; }}
.risk.low {{ border-left-color:#1a7a43; background:#eaf5ee; }}
.sanity {{ display:flex; gap:10px; align-items:baseline; margin:5px 0; padding:8px 11px;
  font-size:12px; border-left:3px solid #8896a5; background:#f4f6f8; }}
.sanity b {{ text-transform:uppercase; font-size:10px; letter-spacing:.6px; }}
.sanity.warn {{ border-left-color:#8a5a00; background:#fbf3e2; }}
.sanity.risk {{ border-left-color:#b3261e; background:#fbeceb; }}
.sanity.info {{ border-left-color:#5a6672; background:#f2f4f7; }}
.statusbar {{ margin:14px 0 10px; padding:12px 14px; border:1px solid #cfd5dd;
  border-left:5px solid #5a6672; background:#f7f9fb; }}
.statusbar b {{ display:block; font-size:16px; margin-bottom:3px; }}
.statusbar span {{ display:block; font-size:12px; color:#43505f; }}
.statusbar .caveat {{ margin-top:6px; font-style:italic; }}
.statusbar.blocked {{ border-left-color:#b3261e; background:#fbeceb; }}
.statusbar.blocked b {{ color:#8f1d17; }}
.statusbar.review {{ border-left-color:#b7791f; background:#fdf5e6; }}
.statusbar.review b {{ color:#8a5a00; }}
.statusbar.ready {{ border-left-color:#1e7a4a; background:#eaf6ee; }}
.statusbar.ready b {{ color:#14714a; }}
ul.findings {{ margin:8px 0 0; padding-left:16px; font-size:13px; }}
ul.findings li {{ margin-bottom:4px; }}
.sev-critical b {{ color:#b3261e; text-transform:uppercase; font-size:11px; }}
.sev-warning b {{ color:#8a5a00; text-transform:uppercase; font-size:11px; }}
.briefing {{ margin:12px 0; padding:11px 13px; border:1px solid #d7dce2;
  border-left:4px solid #2f6fb5; background:#fbfcfe; font-size:13px; }}
.briefing pre {{ margin:6px 0 0; white-space:pre-wrap; font-family:inherit; font-size:13px; }}
table.prov {{ border-collapse:collapse; font-size:12px; margin-bottom:8px; }}
table.prov th {{ text-align:left; font-size:10px; text-transform:uppercase;
  letter-spacing:.4px; color:var(--muted); padding:2px 10px 4px 0;
  border-bottom:1px solid var(--line); }}
table.prov td {{ padding:4px 10px 4px 0; vertical-align:top; }}
</style></head><body>
<h1>Shipment reconciliation</h1>
<div class="sub">shipment <code>{esc(report.shipment_id)}</code> · generated {esc(report.updated_at)}</div>
{banner}
{status_html}
<div class="verdict {esc(s.verdict)}">{esc(VERDICT_HEADLINE.get(s.verdict, s.verdict))}
 &nbsp;·&nbsp; {s.conflict} conflict(s), {s.suspicious} suspicious, {s.consistent} consistent,
 {s.single_source} single-source, {s.absent} absent ·
 {s.documents_ok}/{s.documents_ok + s.documents_failed} documents extracted</div>
{risk_html}
{emails_block}
{checks_html}
{sanity_html}
{narrative_html}
{briefing_html}
<h2>Field comparison</h2>
<table class="cmp"><thead><tr><th>Field</th><th>Status</th><th>Why</th>
{''.join(f'<th>{esc(d.label)}</th>' for d in report.documents)}</tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<h2>Source documents and evidence</h2>
{''.join(doc_rows)}
<h2>Model steps</h2>
<table class="prov"><thead><tr><th>Step</th><th>Provider</th><th>Model</th>
<th>Answer came from</th><th>Try</th><th>Tok</th><th>ms</th></tr></thead>
<tbody>{''.join(
    f'<tr><td>{esc(s["step"])}</td>'
    f'<td><code>{esc(s["provider"])}</code></td>'
    f'<td><code>{esc(s["model"])}</code></td>'
    f'<td>{esc(source_word(s["source"]))}</td>'
    f'<td>{s["tries"]}</td>'
    f'<td>{s["usage"].total if s["usage"] else 0}</td>'
    f'<td>{s["latency_ms"]}</td></tr>'
    for s in steps)}</tbody></table>
<p class="sub">{esc(powered_by(report))}<br>{esc(cost_line(report, tokens, ms))}</p>
<footer>prompt {esc(report.run.prompt_version or PROMPT_VERSION)} ·
normalizer {esc(report.run.normalizer_version or NORMALIZER_VERSION)} ·
comparator {esc(report.run.comparator_version or COMPARATOR_VERSION)}<br>
Values are transcribed verbatim by the model and normalized in deterministic Python;
the model performs no unit conversion or arithmetic. Container numbers are validated
against the ISO 6346 check digit.</footer>
</body></html>"""


def build_zip(report: Report, run_dir: Path | None) -> bytes:
    """The run directory IS the artifact; this just compresses it."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("report.json", report.model_dump_json(indent=2))
        zf.writestr("report.csv", report_csv(report))
        zf.writestr("report.html", standalone_html(report, run_dir))
        # One text file per draft, so the archive carries the letters rather than
        # only a description of them. Written by rule from the findings, which is
        # what makes it safe to include: nothing here was composed after the fact.
        for mail in report.correction_emails:
            rule = "=" * len(mail.subject)
            zf.writestr(f"corrections/{_slug(mail.document_label)}.txt",
                        "\n".join([mail.subject, rule, "", mail.body, ""]))
        # Imported here, not at the top of the module: app.pdf reads the label
        # tables out of this module, so a module-level import would be a cycle.
        from .pdf import render_pdf

        try:
            pdf = render_pdf(report)
            zf.writestr("report.pdf", pdf)
            pdf_note = "report.pdf   the same report laid out for print and filing\n"
        except Exception as exc:  # noqa: BLE001
            # A missing PDF is disclosed in the manifest rather than left as an absent
            # file the reader assumes the archive never promised.
            pdf = None
            pdf_note = ("(report.pdf could not be rendered here: "
                        f"{type(exc).__name__})\n")
        # A list of lines, joined -- not one long concatenation with conditionals
        # inside it. The previous shape parsed as `(everything + excluded) if
        # unverified else ""`, so a report with no excluded fields got a README.txt
        # that started at the blank line below the header: the manifest describing
        # an export disappeared from the export, on the majority of runs.
        manifest = [
            "Shipment Doc Reconciler export",
            "================================",
            f"shipment_id : {report.shipment_id}",
            f"state       : {report.state}",
            f"verdict     : {report.summary.verdict}",
            f"status      : {report.status_report.banner}"
            f" ({report.status_report.critical} critical, "
            f"{report.status_report.warning} warning, "
            f"{report.status_report.info} informational)",
            f"vision model: {report.run.model} ({report.run.vision_provider})",
            f"summary     : {report.run.summary_model} "
            f"({report.run.summary_provider}) -> {report.narrative.status}",
            f"demo_mode   : {report.run.demo_mode}",
            f"drafts      : {len(report.correction_emails)} written by rule, "
            "critical and warning findings only",
        ]
        if report.unverified:
            manifest.append(f"excluded    : {len(report.unverified)} field(s) quoted "
                            "a line that is not printed on the document")
        if report.internal_checks:
            manifest.append(
                f"arithmetic  : "
                f"{sum(1 for c in report.internal_checks if c.status == 'fail')} "
                f"failing, "
                f"{sum(1 for c in report.internal_checks if c.status == 'not_run')} "
                f"not run of {len(report.internal_checks)} internal check(s)")
        manifest.append("")
        manifest.append("report.html  standalone, self-contained view "
                        "(opens anywhere)")
        manifest.append(pdf_note.rstrip("\n"))
        manifest.append("report.json  full machine-readable report incl. evidence "
                        "and versions")
        manifest.append("report.csv   one row per field per document")
        if report.correction_emails:
            manifest.append(f"corrections/ {len(report.correction_emails)} draft(s) "
                            "to whoever owes an answer, written by rule")
        manifest.append("pages/       normalized page images used as evidence")
        manifest.append("raw/         every raw model response, including failed "
                        "attempts")
        zf.writestr("README.txt", "\n".join(manifest) + "\n")
        if run_dir and run_dir.is_dir():
            for sub in ("pages", "raw"):
                base = run_dir / sub
                if base.is_dir():
                    for f in sorted(base.rglob("*")):
                        if f.is_file():
                            zf.write(f, arcname=str(f.relative_to(run_dir)))
    return buf.getvalue()


def write_zip(report: Report, run_dir: Path | None, out: Path) -> Path:
    out.write_bytes(build_zip(report, run_dir))
    return out


def fixture_manifest(path: Path) -> dict:
    """Read fixtures/index.json for the demo buttons. Returns {} if absent."""
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
