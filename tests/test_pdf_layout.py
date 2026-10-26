"""The PDF is a filing artifact, so its geometry is part of what it claims.

A table whose text overprints the next column is unreadable in exactly the place
an auditor is cross-checking, and a table that is narrower than its siblings on
the same page looks like a rendering accident. These tests read the *rendered*
PDF -- not the HTML that produced it -- and assert the properties a reader can
see: nothing overprints, every table ends inside the margin and shares its
siblings' width, no word is cut at the page edge, the long model id is present
in full somewhere in the text, ordinary words are never broken mid-word, every
per-document entry starts on its own line, and a correction draft keeps the
line structure of the letter it will be sent as.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pdfplumber
import pytest

from app.config import Settings
from app.orchestrator import Reconciler
from app.pdf import render_pdf

REPO = Path(__file__).resolve().parents[1]
MARGIN = 13 * 72 / 25.4  # 13mm, matching @page margin in app/pdf.py
LONG_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
BREAK_MARKER = "\ufffe"


def settings() -> Settings:
    return Settings(_env_file=None, demo_mode=True, nebius_api_key="",
                    nvidia_api_key="", runs_dir=REPO / "runs" / "pdf_layout",
                    cache_dir=REPO / "fixtures" / "responses")


async def _build(name: str):
    rec = Reconciler(settings())
    index = json.loads((REPO / "fixtures" / "index.json").read_text(encoding="utf-8"))
    uploads = []
    for doc in index["shipments"][name]["documents"]:
        for fname in doc["files"]:
            path = REPO / "fixtures" / "images" / fname
            uploads.append((path.name, doc["doc_type"], path.read_bytes()))
    shipment_id, _ = await rec.create_shipment(uploads)
    await rec.wait(shipment_id, timeout=300)
    return rec.get(shipment_id)


@pytest.fixture(scope="module")
def samples() -> dict:
    import asyncio

    return {name: asyncio.run(_build(name))
            for name in ("shipment_clean", "shipment_conflict", "shipment_messy")}


def _long_token_report(report):
    """A report whose identifiers are longer than any comfortable column.

    Uses the real messy report and replaces the fields a live run makes long:
    model ids, file names, a document type, and token/latency numbers big enough
    to widen a numeric column. Everything else stays a real report.
    """
    synthetic = report.model_copy(deep=True)
    long_model = "nvidia/some-vendor-with-a-very-long-name/omni-30b-a3b-reasoning-2026-10"
    synthetic.run.model = long_model
    for i, doc in enumerate(synthetic.documents, start=1):
        doc.model = long_model
        doc.label = f"extremely_long_file_name_for_an_imported_document_{i}_scanned_at_300dpi.png"
        doc.filename = doc.label
        doc.timings_ms = 123_456 + i
        if doc.usage:
            doc.usage = type(doc.usage)(prompt=1_234_567, completion=98_765,
                                        total=1_333_332)
    return synthetic, long_model


def _text(pdf_bytes: bytes) -> str:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def _flat(text: str) -> str:
    return "".join(text.split()).replace(BREAK_MARKER, "")


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------


_RENDERED: list[tuple[str, bytes]] = []


def _panels(samples):
    """(label, report, pdf bytes) for every report shape the tests care about."""
    out = [(name, rep, render_pdf(rep)) for name, rep in samples.items()]
    synthetic, _ = _long_token_report(samples["shipment_messy"])
    out.append(("long-tokens", synthetic, render_pdf(synthetic)))
    _RENDERED[:] = [(label, pdf_bytes) for label, _, pdf_bytes in out]
    return out


@pytest.fixture(autouse=True)
def _page_artifacts_on_failure(request):
    """Rasterise the rendered pages only when a test failed.

    Committed? No: tests/artifacts/ is gitignored and CI uploads it on failure.
    A layout bug is much faster to diagnose from the page than from the
    assertion, and writing nothing on green keeps the tree clean.
    """
    yield
    report = getattr(request.node, "rep_call", None)
    if not (report and report.failed) or not _RENDERED:
        return
    import pypdfium2 as pdfium

    out = REPO / "tests" / "artifacts" / "pdf"
    out.mkdir(parents=True, exist_ok=True)
    for label, pdf_bytes in _RENDERED:
        document = pdfium.PdfDocument(pdf_bytes)
        for index in range(len(document)):
            document[index].render(scale=2).to_pil().save(
                out / f"{request.node.name}-{label}-p{index + 1}.png")


def test_no_two_words_overprint_each_other(samples):
    """Break caught: an unbreakable model id forces its column wide and draws
    over the next cell -- the reported Model steps and Documents read defect."""
    for label, _rep, pdf_bytes in _panels(samples):
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for number, page in enumerate(pdf.pages, start=1):
                words = page.extract_words()
                for i, first in enumerate(words):
                    for second in words[i + 1:]:
                        overlap_x = min(first["x1"], second["x1"]) - max(first["x0"], second["x0"])
                        overlap_y = min(first["bottom"], second["bottom"]) - max(first["top"], second["top"])
                        # >1pt in both axes: adjacent words touch, they do not sit on each other.
                        assert not (overlap_x > 1 and overlap_y > 1), (
                            f"{label} page {number}: {first['text']!r} overlaps "
                            f"{second['text']!r} at y={first['top']:.0f}")


def test_every_table_ends_inside_the_page_margin(samples):
    """Break caught: a table that starts inside the margin but is laid out wider
    than the content box, so its right border runs into the margin."""
    for label, _rep, pdf_bytes in _panels(samples):
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for number, page in enumerate(pdf.pages, start=1):
                right_limit = page.width - MARGIN
                for table in page.find_tables():
                    x0, _, x1, _ = table.bbox
                    assert x0 >= MARGIN - 2, f"{label} p{number}: table starts at {x0:.1f}"
                    assert x1 <= right_limit + 2, (
                        f"{label} p{number}: table ends at {x1:.1f}, margin at {right_limit:.1f}")


def test_tables_on_a_page_share_one_width(samples):
    """Break caught: the Documents read table's column percentages summed to 91,
    so it was visibly narrower than the Model steps table above it -- and the
    header background, laid out to the page width, left a stray block beside it."""
    for label, _rep, pdf_bytes in _panels(samples):
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for number, page in enumerate(pdf.pages, start=1):
                widths = [(round(t.bbox[0], 1), round(t.bbox[2], 1))
                          for t in page.find_tables() if t.bbox[2] - t.bbox[0] > 200]
                if not widths:
                    continue
                lefts = {w[0] for w in widths}
                rights = {w[1] for w in widths}
                assert max(lefts) - min(lefts) <= 1, f"{label} p{number}: left edges {lefts}"
                assert max(rights) - min(rights) <= 1, (
                    f"{label} p{number}: tables differ in width: {widths}")


def test_no_word_is_cut_at_the_page_edge(samples):
    """Break caught: a long token drawn past the right edge, so the printed sheet
    loses characters the text layer still contains."""
    for label, _rep, pdf_bytes in _panels(samples):
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for number, page in enumerate(pdf.pages, start=1):
                limit = page.width - MARGIN
                over = [(w["text"], round(w["x1"], 1)) for w in page.extract_words()
                        if w["x1"] > limit + 2]
                assert not over, f"{label} p{number}: words past the margin {over[:5]}"


def _tables(pdf_bytes: bytes):
    """(page number, rows) for every table pdfplumber can reconstruct.

    Cell text, not the flat stream: a multi-line cell and its neighbour share a
    baseline in the text layer, so a flat string interleaves the two columns and
    a perfectly readable cell looks like a corrupted id. The drawn grid is what
    separates them.
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            for rows in page.extract_tables():
                yield number, [[(cell or "").strip() for cell in row] for row in rows]


def _norm(cell: str) -> str:
    return "".join(cell.split())


def test_the_full_model_name_stays_readable_and_stays_in_its_cell(samples):
    """Break caught: wrapping or clipping that loses the end of the id, or a
    model id that bleeds into the neighbouring cell's text -- both make the one
    string a reader uses to check what ran unusable."""
    checked = 0
    for label, _rep, pdf_bytes in _panels(samples):
        for number, rows in _tables(pdf_bytes):
            if not rows or "MODEL" not in [c.upper() for c in rows[0]]:
                continue
            header = [c.upper() for c in rows[0]]
            model_at = header.index("MODEL")
            source_at = next((i for i, h in enumerate(header) if "ANSWER" in h or "SOURCE" in h), None)
            for row in rows[1:]:
                cell = _norm(row[model_at])
                if not cell:
                    continue
                checked += 1
                # Provider and model may share the cell; the model id must be in
                # it, whole, and nowhere near truncated.
                assert LONG_MODEL in cell or "/" in cell, (
                    f"{label} p{number}: model cell {row[model_at]!r} is not an id")
                if LONG_MODEL in cell:
                    assert LONG_MODEL in cell
                if source_at is not None:
                    assert "nvidia" not in row[source_at].lower(), (
                        f"{label} p{number}: the model bled into the source cell: "
                        f"{row[source_at]!r}")
    assert checked, "no model cells were found to check"

    # The synthetic report's own long id must survive every wrap.
    synthetic, long_model = _long_token_report(samples["shipment_messy"])
    text_cells = ["".join("".join(row) for row in rows)
                  for _, rows in _tables(render_pdf(synthetic))]
    assert any(long_model in _norm(cells) for cells in text_cells), (
        "the synthetic long model id is not readable in full in any cell")
    for doc in synthetic.documents:
        assert any(_norm(doc.label) in _norm(cells) for cells in text_cells), (
            f"the long file name {doc.label} is not readable in full")


# ---------------------------------------------------------------------------
# the not_run rows
# ---------------------------------------------------------------------------


def _not_run_instances(report):
    return [(c.document_label, c.code) for c in report.internal_checks
            if c.status == "not_run"]


def _arithmetic_table_text(pdf_bytes: bytes) -> str:
    """The text of the page that carries the arithmetic table."""
    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if "Arithmetic inside each document" in text or "not run of" in text:
                pages.append(text)
    return "\n".join(pages)


def _arithmetic_rows(pdf_bytes: bytes):
    """Rows of the arithmetic table, found by its own headers."""
    for _, rows in _tables(pdf_bytes):
        header = [c.upper() for c in rows[0]] if rows else []
        if "CHECK" in header and "STATUS" in header and "DOCUMENT" in header:
            return rows
    return []


def test_not_run_checks_are_grouped_one_row_per_rule(samples):
    """Break caught: twelve near-identical rows, each repeating the same sentence,
    pushing a two-line fact across a page and a half."""
    report = samples["shipment_messy"]
    instances = _not_run_instances(report)
    assert len(instances) > 8, "this fixture is meant to exercise the grouping"
    groups = {(c.code, c.detail) for c in report.internal_checks if c.status == "not_run"}
    assert len(groups) < len(instances), "nothing to group if every rule ran once"

    rows = _arithmetic_rows(render_pdf(report))
    status_at = [c.upper() for c in rows[0]].index("STATUS")
    not_run_rows = [r for r in rows[1:] if r[status_at].strip().lower() == "not run"]
    assert len(not_run_rows) == len(groups), (
        f"{len(groups)} distinct not-run groups, {len(not_run_rows)} rows printed")
    assert len(not_run_rows) < len(instances), "the rows were not grouped at all"


def test_the_grouping_drops_no_document(samples):
    """Break caught: a grouped row that lists some documents and silently omits
    the rest, which would turn 'not checked' into 'checked' for the ones missing."""
    report = samples["shipment_messy"]
    instances = _not_run_instances(report)
    by_code: dict[str, set[str]] = {}
    for document, code in instances:
        by_code.setdefault(code, set()).add(document)

    text = _arithmetic_table_text(render_pdf(report))
    flat = _flat(text)
    for code, documents in by_code.items():
        for document in documents:
            assert document in flat, (
                f"{code}: {document} was not listed in the grouped not-run row")


def test_the_summary_counts_still_match_the_grouping(samples):
    """Break caught: the table is grouped but the sentence above it still counts
    rows, or stops counting not_run at all -- either way the numbers lie."""
    report = samples["shipment_messy"]
    instances = _not_run_instances(report)
    passed = sum(1 for c in report.internal_checks if c.status == "pass")
    failed = sum(1 for c in report.internal_checks if c.status == "fail")

    text = " ".join(_arithmetic_table_text(render_pdf(report)).split())
    match = re.search(r"(\d+) passed, (\d+) failed, (\d+) not run", text)
    assert match, "the arithmetic summary line is missing its counts"
    assert int(match.group(1)) == passed
    assert int(match.group(2)) == failed
    assert int(match.group(3)) == len(instances), (
        "the 'not run' count must count every check that did not run, grouped or not")


# ---------------------------------------------------------------------------
# wrapping: whole words, one entry per line
# ---------------------------------------------------------------------------


def _vocabulary(report) -> set[str]:
    """Every alphabetic run in the report's own HTML, lowercased.

    The printed page may only contain words the report contains: a token too
    wide for its column is broken at a separator ('/', '-', '_', '.'), which
    leaves alphabetic runs intact, so any alphabetic run on the page that is
    *not* in this vocabulary is an ordinary word broken mid-word.
    """
    from app.pdf import report_html

    return {w for w in re.findall(r"[a-z]+", report_html(report).lower())
            if len(w) >= 5}


def test_no_ordinary_word_is_broken_mid_word(samples):
    """Break caught: character-boundary wrapping broke ordinary words --
    'Quantit/y', 'Nordwind Industr/ial', 'king_list' -- in the columns an
    auditor reads word by word."""
    for label, report, pdf_bytes in _panels(samples):
        vocab = _vocabulary(report)
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for number, page in enumerate(pdf.pages, start=1):
                for word in page.extract_words():
                    text = word["text"].replace(BREAK_MARKER, "")
                    for piece in re.findall(r"[A-Za-z]+", text):
                        if len(piece) < 5:
                            continue
                        assert piece.lower() in vocab, (
                            f"{label} p{number}: {piece!r} (from {text!r}) is not "
                            "a word of the report -- it was broken mid-word")


def test_every_value_entry_starts_its_own_line(samples):
    """Break caught: the per-document entries in 'As printed on each document'
    ran together ('100 cartonspacking_list_conflict.png p1:') because the
    renderer dropped the <br/> between them. Every entry must begin a printed
    line with its document label, and no entry may be missing."""
    report = samples["shipment_messy"]
    by_field = {c.field: c for c in report.comparisons}
    labels = [d.label for d in report.documents]
    checked = 0
    for number, rows in _tables(render_pdf(report)):
        header = [c.upper() for c in rows[0]] if rows else []
        if "VERDICT" not in header or "WHY" not in header:
            continue
        value_at = len(header) - 1
        for row in rows[1:]:
            flat_field = _norm(row[0]).lower()
            comp = next((c for f, c in by_field.items() if f in flat_field), None)
            if comp is None:
                continue
            cell = row[value_at] or ""
            checked += 1
            flat_cell = _norm(cell).lower()
            for value in comp.values:
                assert _norm(value.document_label).lower() in flat_cell, (
                    f"p{number} {comp.field}: entry for "
                    f"{value.document_label} is missing from the cell")
            for line in cell.split("\n"):
                stripped = line.strip()
                for lab in labels:
                    if lab in stripped:
                        assert stripped.startswith(lab), (
                            f"p{number} {comp.field}: {lab!r} appears mid-line, "
                            f"entries ran together: {stripped!r}")
    assert checked >= 5, f"only {checked} comparison rows were found to check"


def test_draft_body_keeps_its_line_structure(samples):
    """Break caught: the correction draft printed as one run-together blob --
    the letter lost every newline, and a filed copy that does not read like the
    letter cannot be checked against it. Each body line must start a printed
    line, in order."""
    report = samples["shipment_conflict"]
    assert report.correction_emails, "this fixture is meant to carry drafts"
    body_lines = [ln.strip() for ln in report.correction_emails[0].body.split("\n")
                  if ln.strip()]
    assert len(body_lines) >= 3

    rendered: list[str] = []
    with pdfplumber.open(io.BytesIO(render_pdf(report))) as pdf:
        started = False
        for page in pdf.pages:
            text = page.extract_text() or ""
            started = started or "Correction drafts" in text
            if started:
                rendered.extend(" ".join(ln.split()) for ln in text.splitlines())

    last = -1
    for line in body_lines:
        head = " ".join(line.split()[:4])
        starts = [i for i, r in enumerate(rendered) if r.startswith(head)]
        assert starts, f"draft line starts no printed line: {line!r}"
        index = next((i for i in starts if i > last), None)
        assert index is not None, (
            f"draft line out of order or run together with the previous one: {line!r}")
        last = index
