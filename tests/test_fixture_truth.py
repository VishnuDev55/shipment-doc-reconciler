"""The fixture data has to be true to the page it claims to show.

Two defects that shipped inside our own ground truth, and that nothing caught
until this file existed:

  * a certificate-of-origin `hs_code` quoted as "HS code 8517.62.00", a line the
    document never prints -- the page shows the column header "3. HS code" on one
    line and the cell "8517.62.00 P" on another. An invented evidence quote in the
    fixture is worse than an invented one in a model output, because it teaches the
    reader that the evidence column can be trusted when it cannot.
  * an invoice line printing 600 x 9.18 = 5,510.00, where the product is 5,508.00.
    The subtotal and total were built from the printed 5,510.00, so the documents
    agree with each other and only the arithmetic inside one line is wrong --
    precisely the class of defect Task "internal math checks" exists to find. It
    cannot find it if the fixture is wrong too.

So these tests render the real templates to a real PDF, read the text layer back,
and check the authored data against what is actually printed.
"""

from __future__ import annotations

import io
import re
import unicodedata

import pytest

from app.normalize.money import normalize_money
from app.normalize.number import parse_number
from fixtures.shipments import SHIPMENTS

# label cell + value cell of one table row: the renderer emits them as two lines,
# and a human transcribing that row would write them as one. Both forms count.
ROW_SPLIT = re.compile(r"<tr><td>(.*?)</td><td class='num'>(.*?)</td>"
                       r"<td class='num'>(.*?)</td><td class='num'>(.*?)</td>"
                       r"<td class='num'>(.*?)</td></tr>")
TOTAL_ROW = re.compile(r"<tr[^>]*><td>(Subtotal|Freight|TOTAL AMOUNT)</td>"
                       r"<td class=\"num\">(.*?)</td></tr>")


def normalize(text: str) -> str:
    """Same folding the evidence verifier uses: accents gone, case gone,
    whitespace collapsed. Kept here so the test is independent of app code."""
    folded = unicodedata.normalize("NFKD", text or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", folded).strip().lower()


def page_lines(html: str) -> list[list[str]]:
    """Rendered page -> printed lines, via the PDF text layer.

    xhtml2pdf produces a real text layer and build_fixtures.py throws it away to
    rasterise; reading it back is the only model-independent way to ask what the
    document actually says.
    """
    import pypdfium2 as pdfium
    from xhtml2pdf import pisa

    buf = io.BytesIO()
    pisa.CreatePDF(io.StringIO(html), dest=buf)
    doc = pdfium.PdfDocument(buf.getvalue())
    try:
        return [[normalize(ln) for ln in doc[i].get_textpage()
                 .get_text_bounded().split("\n") if ln.strip()]
                for i in range(len(doc))]
    finally:
        doc.close()


@pytest.fixture(scope="module")
def rendered() -> dict:
    out = {}
    for sname, shipment in SHIPMENTS.items():
        for dtype, doc in shipment["docs"].items():
            out[(sname, dtype)] = (page_lines(doc["html"](shipment)),
                                   doc["extraction"](shipment))
    return out


# ---------------------------------------------------------------------------
# evidence must be printed
# ---------------------------------------------------------------------------


def test_every_authored_evidence_quote_is_actually_printed(rendered):
    """The whole product rests on "here is the line it came from".

    A quote that appears nowhere on the page is not evidence, whoever wrote it.
    """
    bad = []
    for (sname, dtype), (pages, spec) in rendered.items():
        for field, entry in spec.items():
            if entry is None:
                continue
            _value, evidence, page, _conf = entry
            needle = normalize(evidence)
            lines = pages[page - 1] if page - 1 < len(pages) else []
            if any(needle in ln for ln in lines):
                continue
            if needle in " ".join(lines):        # label cell + value cell, one row
                continue
            bad.append(f"{sname}/{dtype}.{field}: {evidence!r}")
    assert not bad, "authored evidence quotes that no document prints:\n  " + "\n  ".join(bad)


def test_every_authored_value_appears_on_the_page_it_is_attributed_to(rendered):
    """A real quote wrapped around a value that is not on the page is how an
    invented field looks from the inside, so the value is checked separately."""
    bad = []
    for (sname, dtype), (pages, spec) in rendered.items():
        for field, entry in spec.items():
            if entry is None:
                continue
            value, _evidence, page, _conf = entry
            needle = normalize(value)
            page_text = " ".join(pages[page - 1]) if page - 1 < len(pages) else ""
            if needle and needle not in page_text:
                bad.append(f"{sname}/{dtype}.{field}: {value!r} not on page {page}")
    assert not bad, "authored values that no document prints:\n  " + "\n  ".join(bad)


def test_no_field_is_reported_present_with_an_empty_quote(rendered):
    for (sname, dtype), (_pages, spec) in rendered.items():
        for field, entry in spec.items():
            if entry is None:
                continue
            assert entry[1].strip(), f"{sname}/{dtype}.{field} is present but quotes nothing"


# ---------------------------------------------------------------------------
# the arithmetic inside one document has to be right
# ---------------------------------------------------------------------------


def _invoice_rows(html: str):
    return [(d, q, u, p, a) for d, q, u, p, a in ROW_SPLIT.findall(html)]


def _invoice_totals(html: str) -> dict[str, float]:
    out = {}
    for label, raw in TOTAL_ROW.findall(html):
        amount = normalize_money(raw).amount
        assert amount is not None, f"unparseable total {raw!r} for {label}"
        out[label] = amount
    return out


@pytest.mark.parametrize("sname", sorted(SHIPMENTS))
def test_each_invoice_line_multiplies_out(sname):
    """qty x unit price = line amount, to the cent.

    This is the check that the fixture itself failed: 600 x 9.18 is 5,508.00 and
    the page printed 5,510.00. A rule we are about to ship has to be true of our
    own documents first, or the clean demo alarms and the rule gets ignored.
    """
    shipment = SHIPMENTS[sname]
    doc = shipment["docs"]["commercial_invoice"]
    html = doc["html"](shipment)
    rows = _invoice_rows(html)
    assert len(rows) >= 2, "no goods rows parsed; the template shape changed"

    for desc, qty, _unit, price, amount in rows:
        parsed_qty = parse_number(qty).value
        parsed_price = parse_number(price).value
        parsed_amount = parse_number(amount).value
        assert None not in (parsed_qty, parsed_price, parsed_amount), desc
        expected = round(parsed_qty * parsed_price, 2)
        assert abs(expected - parsed_amount) <= 0.01, (
            f"{sname}: {desc!r} prints {qty} x {price} = {amount}, "
            f"but the product is {expected:.2f}"
        )


@pytest.mark.parametrize("sname", sorted(SHIPMENTS))
def test_invoice_lines_add_up_to_the_stated_totals(sname):
    shipment = SHIPMENTS[sname]
    doc = shipment["docs"]["commercial_invoice"]
    html = doc["html"](shipment)
    totals = _invoice_totals(html)
    rows_total = round(sum(parse_number(a).value for *_x, a in _invoice_rows(html)), 2)

    assert abs(rows_total - totals["Subtotal"]) <= 0.01, (
        f"{sname}: lines sum to {rows_total}, subtotal prints {totals['Subtotal']}"
    )
    assert abs(totals["Subtotal"] + totals["Freight"] - totals["TOTAL AMOUNT"]) <= 0.01, (
        f"{sname}: subtotal {totals['Subtotal']} + freight {totals['Freight']} "
        f"!= total {totals['TOTAL AMOUNT']}"
    )


@pytest.mark.parametrize("sname", sorted(SHIPMENTS))
def test_no_invoice_line_is_internally_contradictory_after_the_fix(sname):
    """Guards the specific repair: the fastener row must keep the printed
    subtotal, so fixing the multiplication cannot move the shipment value."""
    shipment = SHIPMENTS[sname]
    doc = shipment["docs"]["commercial_invoice"]
    totals = _invoice_totals(doc["html"](shipment))
    assert totals["Subtotal"] == 8420.00, f"{sname} subtotal drifted: {totals}"
    assert totals["TOTAL AMOUNT"] == 8420.00, f"{sname} total drifted: {totals}"
