"""Render the runtime architecture diagram for this project.

Graphviz is not installed on this machine and there is no network to add it, so
the picture is generated here: one structure table below is the single source,
emitted as SVG, then rasterised to PNG by the Chromium build the e2e suite
already uses. Re-run after an architectural change:

    .venv/Scripts/python.exe scripts/render_architecture.py

Nodes and edges are drawn from what the code does, not from folder names: the
routes, the guards, the pipeline stages and the storage layout are all read off
app/, web/ and fixtures/ and are regenerated only by editing this file.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "architecture-model"

W = 1640
H = 1310

INK = "#0f172a"
MUTED = "#475569"
LINE = "#94a3b8"
PAPER = "#ffffff"

TONES = {
    # fill, stroke
    "client": ("#f1f5f9", "#64748b"),
    "api": ("#eef4ff", "#3b6fb6"),
    "guard": ("#f8fafc", "#94a3b8"),
    "pipeline": ("#f1faf4", "#2f7d53"),
    "optional": ("#fbfffc", "#7fb497"),
    "model": ("#fdf7ee", "#a87234"),
    "store": ("#f7f4fc", "#6d5aa8"),
    "external": ("#fdf2f2", "#a34a4a"),
}

BANDS = [
    ("Browser", 104, 148, "client", 40, 1560),
    ("HTTP API — FastAPI", 272, 288, "api", 40, 1560),
    ("Reconciliation pipeline — one asyncio task per shipment", 580, 234, "pipeline", 40, 1560),
    ("Model access", 840, 182, "model", 40, 1560),
    ("Filesystem is the database", 1048, 190, "store", 40, 1110),
    ("Delivery", 1048, 190, "client", 1170, 406),
]

PROVIDERS = (1170, 840, 406, 190)

# kind: box | pill | cyl | note
NODES = [
    # --- browser -----------------------------------------------------------
    dict(id="reviewer", kind="box", x=64, y=146, w=210, h=94, tone="client",
         title="Reviewer",
         lines=["a person,", "in a browser"]),
    dict(id="ui", kind="box", x=298, y=146, w=300, h=94, tone="client",
         title="UI shell",
         lines=["web/index.html · app.js · app.css",
                "vanilla JS, no build step"]),
    dict(id="transport", kind="box", x=622, y=146, w=330, h=94, tone="client",
         title="Report transport",
         lines=["polls /report at 1 Hz · SSE /events",
                "HttpOnly session cookie"]),
    dict(id="poll_note", kind="note", x=976, y=146, w=600, h=94,
         title="Why poll a file instead of holding a socket",
         lines=["report.json is the only settled artifact, so a closed tab",
                "costs a poll and nothing else; the run is held server-side."]),

    # --- API ---------------------------------------------------------------
    dict(id="routes", kind="box", x=64, y=312, w=1512, h=76, tone="api",
         title="Routes — 22 endpoints; every data-bearing one needs the key",
         lines=["upload · report · retry · correct · corrections · raw · events (SSE) · export ZIP · "
                "report.pdf · page · fixtures · config · models — open: /api/health, /api/session"]),
    dict(id="auth", kind="box", x=64, y=404, w=240, h=100, tone="guard",
         title="Auth", lines=["X-API-Key / Bearer,", "or session cookie"]),
    dict(id="limit", kind="box", x=320, y=404, w=240, h=100, tone="guard",
         title="Rate limiter", lines=["sliding window,", "per client identity"]),
    dict(id="spend", kind="box", x=576, y=404, w=240, h=100, tone="guard",
         title="Spend gate", lines=["daily cap + projected", "reservations"]),
    dict(id="ledger", kind="cyl", x=832, y=404, w=240, h=100, tone="store",
         title="spend_log.json",
         lines=["one row per billed call,", "written after it answers"]),
    dict(id="config", kind="box", x=1088, y=404, w=240, h=100, tone="guard",
         title="Config + models", lines=["masked keys, token-gated;", "catalog + probe"]),

    # --- pipeline ----------------------------------------------------------
    dict(id="ingest", kind="pill", x=64, y=624, w=200, h=64, tone="pipeline",
         title="ingest", lines=["rasterise + clamp"]),
    dict(id="extract", kind="pill", x=278, y=624, w=200, h=64, tone="pipeline",
         title="extract", lines=["vision, one call/doc"]),
    dict(id="verify", kind="pill", x=492, y=624, w=200, h=64, tone="pipeline",
         title="verify", lines=["text layer vs model"]),
    dict(id="compare", kind="pill", x=706, y=624, w=200, h=64, tone="pipeline",
         title="compare", lines=["10 fields, normalised"]),
    dict(id="rules", kind="pill", x=920, y=624, w=200, h=64, tone="pipeline",
         title="rules", lines=["cross-field + maths"]),
    dict(id="score", kind="pill", x=1134, y=624, w=200, h=64, tone="pipeline",
         title="score", lines=["deterministic risk"]),
    dict(id="severity", kind="pill", x=1348, y=624, w=200, h=64, tone="pipeline",
         title="severity", lines=["status + report"]),
    dict(id="consensus", kind="pill", x=64, y=712, w=356, h=72, tone="optional",
         title="consensus — app/consensus.py",
         lines=["second provider reads every page; flags, never decides"]),
    dict(id="lineitems", kind="pill", x=434, y=712, w=356, h=72, tone="optional",
         title="line items — app/lineitems.py",
         lines=["second read for goods lines; cannot move a verdict"]),
    dict(id="adjudicate", kind="pill", x=804, y=712, w=356, h=72, tone="optional",
         title="adjudication — app/adjudicate.py",
         lines=["a model opinion on the 70–89 band, advisory only"]),
    dict(id="summary", kind="pill", x=1174, y=712, w=356, h=72, tone="optional",
         title="summary — app/summary.py",
         lines=["executive summary + draft email, cached by verdict"]),

    # --- model access ------------------------------------------------------
    dict(id="summary_client", kind="box", x=64, y=884, w=300, h=110, tone="model",
         title="SummaryClient",
         lines=["HTTPS → Nebius Token Factory", "text-only, one call per report"]),
    dict(id="discovery", kind="box", x=396, y=884, w=300, h=110, tone="model",
         title="Discovery + probe",
         lines=["reads /v1/models,", "checks a model can hold the schema"]),
    dict(id="extraction", kind="box", x=728, y=884, w=414, h=110, tone="model",
         title="ExtractionClient",
         lines=["NVIDIA primary, Nebius fallback",
                "retry ladder with backoff · usage recorded per attempt"]),

    # --- persistence -------------------------------------------------------
    dict(id="runs", kind="cyl", x=64, y=1088, w=340, h=120, tone="store",
         title="runs/<shipment_id>/",
         lines=["report.json · meta.json", "uploads/ · pages/ · raw/"]),
    dict(id="fixtures", kind="cyl", x=428, y=1088, w=330, h=120, tone="store",
         title="fixtures/responses/",
         lines=["committed captures, read first", "never written by a run"]),
    dict(id="livecache", kind="cyl", x=782, y=1088, w=330, h=120, tone="store",
         title=".live_cache/",
         lines=["machine captures, gitignored,", "pruned by LIVE_CACHE_TTL_HOURS"]),

    # --- providers + deployment -------------------------------------------
    dict(id="nvidia", kind="box", x=1170, y=884, w=406, h=64, tone="external",
         title="NVIDIA build.nvidia.com", lines=["vision, primary"]),
    dict(id="nebius", kind="box", x=1170, y=966, w=406, h=64, tone="external",
         title="Nebius Token Factory", lines=["vision fallback, summaries"]),
    dict(id="deploy", kind="note", x=1170, y=1088, w=406, h=150,
         title="One container, two configs",
         lines=["Dockerfile → Render blueprint",
                "intentionally open demo, $2/day cap, 24 h TTL",
                "or docker compose · CI gates: lint,",
                "pytest, chromium e2e, docker smoke"]),
]

# (points, label, style, label_at, rotate)
EDGES = [
    ([(274, 193), (298, 193)], "", "solid", None, 0),
    ([(598, 193), (622, 193)], "", "solid", None, 0),
    ([(787, 240), (787, 312)], "HTTPS", "solid", (793, 282), 0),
    ([(184, 388), (184, 404)], "", "solid", None, 0),
    ([(440, 388), (440, 404)], "", "solid", None, 0),
    ([(696, 388), (696, 404)], "", "solid", None, 0),
    ([(952, 388), (952, 404)], "", "solid", None, 0),
    ([(1208, 388), (1208, 404)], "", "solid", None, 0),
    ([(1576, 350), (1608, 350), (1608, 580), (1600, 580)], "dispatch task",
     "solid", (1600, 470), -90),
    ([(935, 812), (935, 884)], "vision call per document", "solid", (975, 832), 0),
    ([(214, 812), (214, 884)], "narrative", "dotted", (246, 832), 0),
    ([(1142, 939), (1170, 916)], "HTTPS, retried with backoff", "solid", None, 0),
    ([(1142, 950), (1170, 998)], "", "dashed", None, 0),
    ([(935, 994), (935, 1036), (947, 1036), (947, 1088)], "get / put, page-hash keyed",
     "dashed", (1085, 1030), 0),
    ([(40, 760), (26, 760), (26, 1100), (64, 1100)], "", "dashed", None, 0),
    ([(64, 340), (14, 340), (14, 1136), (64, 1136)], "", "dashed", None, 0),
]


def esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def text_block(lines: list[str], x: float, y: float, *, size: float = 12.6,
               fill: str = MUTED, weight: str = "400") -> str:
    spans = "".join(
        f'<tspan x="{x}" dy="{0 if i == 0 else size + 4}">{esc(line)}</tspan>'
        for i, line in enumerate(lines))
    return (f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" '
            f'font-weight="{weight}">{spans}</text>')


def node_svg(n: dict) -> str:
    x, y, w, h = n["x"], n["y"], n["w"], n["h"]
    fill, stroke = TONES.get(n.get("tone", "guard"), ("#f8fafc", LINE))
    dash = ""
    if n["kind"] == "note":
        fill, stroke, dash = "#fbfdff", "#cbd5e1", ' stroke-dasharray="5 4"'
    out = []
    if n["kind"] == "cyl":
        rx = 22
        out.append(f'<path d="M{x} {y + rx} A {w / 2} {rx} 0 0 1 {x + w} {y + rx} '
                   f'L {x + w} {y + h - rx} A {w / 2} {rx} 0 0 1 {x} {y + h - rx} Z" '
                   f'fill="{fill}" stroke="{stroke}" stroke-width="1.3"/>')
        out.append(f'<path d="M{x} {y + rx} A {w / 2} {rx} 0 0 0 {x + w} {y + rx}" '
                   f'fill="none" stroke="{stroke}" stroke-width="1.1" opacity="0.55"/>')
    else:
        rx = 10 if n["kind"] != "pill" else h / 2
        dashline = dash or (' stroke-dasharray="6 4"' if n.get("tone") == "optional" else "")
        out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
                   f'fill="{fill}" stroke="{stroke}" stroke-width="1.3"{dashline}/>')
    pad = 18
    if n["kind"] == "pill":
        out.append(f'<text x="{x + w / 2}" y="{y + 26}" font-size="14" '
                   f'font-weight="600" fill="{INK}" text-anchor="middle">{esc(n["title"])}</text>')
        out.append(f'<text x="{x + w / 2}" y="{y + 47}" font-size="11.8" fill="{MUTED}" '
                   f'text-anchor="middle">{esc(n["lines"][0])}</text>')
        return "".join(out)
    title_y = y + (30 if n["kind"] != "cyl" else 40)
    out.append(f'<text x="{x + pad}" y="{title_y}" font-size="15" font-weight="600" '
               f'fill="{INK}">{esc(n["title"])}</text>')
    out.append(text_block(n["lines"], x + pad, title_y + 22))
    return "".join(out)


def edge_svg(points, label, style, label_at, rotate) -> str:
    dash = {"solid": "", "dashed": ' stroke-dasharray="7 5"',
            "dotted": ' stroke-dasharray="2 4"'}[style]
    d = "M" + " L".join(f"{px} {py}" for px, py in points)
    out = [f'<path d="{d}" fill="none" stroke="#64748b" stroke-width="1.5"{dash} '
           f'marker-end="url(#arrow)"/>']
    if label and label_at:
        lx, ly = label_at
        anchor = "middle"
        transform = f' transform="rotate({rotate} {lx} {ly})"' if rotate else ""
        out.append(f'<text x="{lx}" y="{ly}" font-size="11.4" fill="{MUTED}" '
                   f'text-anchor="{anchor}"{transform}>{esc(label)}</text>')
    return "".join(out)


def build() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        sha = "unknown"
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="Segoe UI, system-ui, sans-serif">',
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#64748b"/></marker></defs>',
        f'<rect width="{W}" height="{H}" fill="{PAPER}"/>',
        f'<text x="40" y="46" font-size="27" font-weight="600" fill="{INK}">'
        'shipment-doc-reconciler — runtime architecture</text>',
        f'<text x="40" y="72" font-size="13.4" fill="{MUTED}">'
        'Current state, read off app/, web/ and fixtures/ at commit '
        f'{esc(sha)}. Solid = runtime call · dashed = data access · dotted = optional '
        'or off by default.</text>',
    ]
    for title, y, h, tone, bx, bw in BANDS:
        fill, stroke = TONES[tone]
        parts.append(f'<rect x="{bx}" y="{y}" width="{bw}" height="{h}" rx="14" '
                     f'fill="{fill}" fill-opacity="0.45" stroke="{stroke}" '
                     f'stroke-opacity="0.35" stroke-width="1"/>')
        parts.append(f'<text x="{bx + 24}" y="{y + 26}" font-size="12.4" font-weight="600" '
                     f'fill="{stroke}" letter-spacing="0.7">{esc(title.upper())}</text>')
    px, py, pw, ph = PROVIDERS
    fill, stroke = TONES["external"]
    parts.append(f'<rect x="{px}" y="{py}" width="{pw}" height="{ph}" rx="14" '
                 f'fill="{fill}" fill-opacity="0.45" stroke="{stroke}" '
                 f'stroke-opacity="0.35"/>')
    parts.append(f'<text x="{px + 20}" y="{py + 26}" font-size="12.4" font-weight="600" '
                 f'fill="{stroke}" letter-spacing="0.7">EXTERNAL PROVIDERS</text>')
    for e in EDGES:
        parts.append(edge_svg(*e))
    for n in NODES:
        parts.append(node_svg(n))
    parts.append(f'<text x="40" y="1270" font-size="11.8" fill="{MUTED}">'
                 'Left rail, outer to inner: the API reads report.json and serves pages/ and raw/ '
                 'for every request; the pipeline writes report.json as each document settles.</text>')
    parts.append(f'<text x="40" y="1290" font-size="11.8" fill="{MUTED}">'
                 'Retry, spend and cache detail: docs/ARCHITECTURE.md and '
                 'docs/architecture-model/architecture-summary.md.</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def rasterise(svg_path: Path, png_path: Path, height: int) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": W, "height": height},
                                device_scale_factor=2)
        page.goto(svg_path.as_uri())
        page.locator("svg").screenshot(path=str(png_path))
        browser.close()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    svg = build()
    svg_path = OUT / "architecture.svg"
    svg_path.write_text(svg, encoding="utf-8")
    print(f"wrote {svg_path.relative_to(ROOT)} ({len(svg)} bytes)")
    try:
        rasterise(svg_path, OUT / "architecture.png", H)
        print(f"wrote {(OUT / 'architecture.png').relative_to(ROOT)} "
              f"({(OUT / 'architecture.png').stat().st_size} bytes, 2x)")
    except Exception as exc:
        print(f"SVG written, PNG not rendered: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
