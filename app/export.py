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

VERDICT_HEADLINE = {
    "clean": "All cross-checked fields agree.",
    "review": "No hard conflicts, but some fields need a human glance.",
    "conflicts": "Conflicts found. Reconcile before the goods move.",
    "running": "Extraction still in progress.",
    "empty": "No documents processed.",
}


def report_csv(report: Report) -> str:
    """Long format: one row per field per document. Pivots cleanly in a sheet."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "shipment_id", "field", "field_label", "kind", "field_status",
            "document", "document_type", "page", "raw_value", "normalized_value",
            "compare_key", "confidence", "outlier", "present", "warnings", "reason",
        ]
    )
    for comp in report.comparisons:
        for v in comp.values:
            writer.writerow(
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
    return buf.getvalue()


def _evidence_data_uri(run_dir: Path | None, image_path: str) -> str | None:
    if run_dir is None:
        return None
    path = run_dir / image_path
    if not path.is_file():
        return None
    b64 = base64.b64encode(path.read_bytes()).decode()
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
            if uri:
                pages_html += (
                    f'<figure><img src="{uri}" alt="{esc(doc.label)} page {ref.index}" loading="lazy">'
                    f'<figcaption>{esc(doc.label)} — page {ref.index}</figcaption></figure>'
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
        doc_rows.append(
            f'<details><summary><b>{esc(doc.label)}</b> '
            f'<span class="pill {doc.status}">{esc(doc.status)}</span> '
            f'<span class="muted">{esc(doc.detected_doc_type or doc.declared_doc_type or "?")} · '
            f'{len(doc.pages)} page(s) · {doc.attempts} attempt(s)</span></summary>'
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
    sanity_html = "".join(
        f'<div class="sanity {esc(f.level)}"><b>{esc(f.level)}</b>'
        f'<span>{esc(f.explanation)}</span></div>'
        for f in report.sanity
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
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
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
table.cmp th, table.cmp td {{ border:1px solid var(--line); padding:8px 10px; vertical-align:top; text-align:left; }}
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
table.ev {{ width:100%; border-collapse:collapse; margin:10px 0; font-size:12px; }}
table.ev td, table.ev th {{ border-bottom:1px solid var(--line); padding:4px 6px; text-align:left; }}
.pages {{ display:flex; flex-wrap:wrap; gap:10px; }}
.pages figure {{ margin:0; }} .pages img {{ max-height:330px; border:1px solid var(--line); }}
.pages figcaption {{ font-size:11px; color:var(--muted); }}
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
.briefing {{ margin:12px 0; padding:11px 13px; border:1px solid #d7dce2;
  border-left:4px solid #2f6fb5; background:#fbfcfe; font-size:13px; }}
.briefing pre {{ margin:6px 0 0; white-space:pre-wrap; font-family:inherit; font-size:13px; }}
table.prov {{ border-collapse:collapse; font-size:12px; margin-bottom:8px; }}
table.prov td {{ padding:4px 10px 4px 0; vertical-align:top; }}
</style></head><body>
<h1>Shipment reconciliation</h1>
<div class="sub">shipment <code>{esc(report.shipment_id)}</code> · generated {esc(report.updated_at)}</div>
{banner}
<div class="verdict {esc(s.verdict)}">{esc(VERDICT_HEADLINE.get(s.verdict, s.verdict))}
 &nbsp;·&nbsp; {s.conflict} conflict(s), {s.suspicious} suspicious, {s.consistent} consistent,
 {s.single_source} single-source, {s.absent} absent ·
 {s.documents_ok}/{s.documents_ok + s.documents_failed} documents extracted</div>
{risk_html}
{sanity_html}
{narrative_html}
{briefing_html}
<h2>Field comparison</h2>
<table class="cmp"><thead><tr><th>Field</th><th>Status</th><th>Why</th>
{''.join(f'<th>{esc(d.label)}</th>' for d in report.documents)}</tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<h2>Source documents and evidence</h2>
{''.join(doc_rows)}
<h2>Providers</h2>
<table class="prov"><tbody>
<tr><td><b>Document extraction (vision)</b></td>
<td><code>{esc(report.run.vision_provider or '?')}</code> · model
<code>{esc(report.run.model or '?')}</code> · structured output
<code>{esc(report.run.json_schema_mode)}</code></td></tr>
<tr><td><b>Executive summary (reasoning)</b></td>
<td><code>{esc(report.run.summary_provider or 'nebius')}</code> · model
<code>{esc(report.run.summary_model or 'not recorded')}</code>
· status <code>{esc(report.narrative.status)}</code></td></tr>
<tr><td><b>Model verified against host</b></td>
<td><code>{esc(report.run.model_selection or 'unverified')}</code>
{esc(' — ' + report.run.model_note if report.run.model_note else '')}</td></tr>
</tbody></table>
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
        # Imported here, not at the top of the module: app.pdf reads the label tables
        # out of this module, so a module-level import would be a cycle.
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
        zf.writestr(
            "README.txt",
            "Shipment Doc Reconciler export\n"
            "================================\n"
            f"shipment_id : {report.shipment_id}\n"
            f"state       : {report.state}\n"
            f"verdict     : {report.summary.verdict}\n"
            f"vision model: {report.run.model} ({report.run.vision_provider})\n"
            f"summary     : {report.run.summary_model} ({report.run.summary_provider})"
            f" -> {report.narrative.status}\n"
            f"demo_mode   : {report.run.demo_mode}\n\n"
            "report.html  standalone, self-contained view (opens anywhere)\n"
            + pdf_note +
            "report.json  full machine-readable report incl. evidence and versions\n"
            "report.csv   one row per field per document\n"
            "pages/       normalized page images used as evidence\n"
            "raw/         every raw model response, including failed attempts\n",
        )
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
