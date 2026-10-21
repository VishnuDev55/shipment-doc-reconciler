"""Generate the harder live-test inputs, with their ground truth.

Each case is one document plus a truth.json saying what every one of the ten
fields should read (or that it is absent). The stress runner compares the live
extraction against this — not the other way round.

    python scripts/build_stress_inputs.py

Outputs land in runs/stress/ (gitignored); the script is the reproducible source,
and the results are what get written into docs/EVAL.md.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts._common import ROOT, html_to_pdf_bytes  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from fixtures.templates import invoice  # noqa: E402

OUT = ROOT / "runs" / "stress"

# The ten contracted fields; truth is a string value or None for absent.
FIELDS = ("quantity", "gross_weight", "net_weight", "container_number", "hs_code",
          "consignee_name", "invoice_value", "incoterms", "po_number",
          "country_of_origin")


def _pages(pdf: bytes, *, scale: float = 2.0) -> list[Image.Image]:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf)
    out = []
    for i in range(len(doc)):
        page = doc[i]
        bitmap = page.render(scale=scale)
        out.append(bitmap.to_pil().convert("RGB"))
    return out


def _save_png(img: Image.Image, name: str) -> None:
    target = OUT / name
    target.parent.mkdir(parents=True, exist_ok=True)
    img.save(target, "PNG")
    print(f"  wrote {target.relative_to(ROOT)}")


def _save_pdf(pages: list[Image.Image], name: str) -> None:
    target = OUT / name
    target.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(target, "PDF", save_all=True, append_images=pages[1:])
    print(f"  wrote {target.relative_to(ROOT)}")


def _write_truth(name: str, truth: dict[str, str | None], *, doc_type: str,
                 note: str) -> None:
    (OUT / f"{name}.truth.json").write_text(
        json.dumps({"doc_type": doc_type, "note": note, "fields": truth}, indent=2),
        encoding="utf-8",
    )


def _invoice_html(**kw) -> str:
    base = dict(
        doc_no="INV-STRESS-01", date="2026-10-14",
        shipper="Nordwind Industrial Supply", shipper_addr="Hafenstr. 12, 20457 Hamburg, DE",
        consignee="Baltic Trading Ltd", consignee_addr="14 Harbour Rd, Klaipeda, LT",
        po_no="PO 4410-DE",
        lines=[("Valve assembly 4in", "100", "CARTONS", "84.20", "8,420.00")],
        qty_total="100 CARTONS", weight_total="12,450.00 KGS",
        incoterm="CIF Hamburg", currency_word="USD EIGHT THOUSAND FOUR HUNDRED TWENTY",
        subtotal="8,420.00", freight="0.00", total="8,420.00",
        hs_code="8517.62.00", origin="China",
    )
    base.update(kw)
    return invoice(**base)


def case_phone_photo() -> None:
    """Rotated 90°, sheared, darkened, JPEG-crushed, with a stamp and a scrawl
    over the total amount."""
    name = "phone_photo"
    img = _pages(html_to_pdf_bytes(_invoice_html()))[0]
    img = img.rotate(90, expand=True, resample=Image.BICUBIC, fillcolor=(250, 250, 250))
    # Horizontal shear: (1, 0.15) skews the image sideways like a phone angle.
    w, h = img.size
    img = img.transform((int(w + 0.15 * h), h), Image.AFFINE, (1, 0.15, -0.15 * h / 2, 0, 1, 0))
    img = img.point(lambda p: int(p * 0.6))  # low light
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=38)
    img = Image.open(buf).convert("RGB")
    draw = ImageDraw.Draw(img)
    # A red "RECEIVED" stamp, rotated, over the totals area.
    stamp = Image.new("RGBA", (220, 60), (0, 0, 0, 0))
    ImageDraw.Draw(stamp).text((6, 6), "RECEIVED", fill=(200, 30, 20, 200))
    stamp = stamp.rotate(-18, expand=True)
    img.paste(stamp, (w // 2 - 80, h - 260), stamp)
    # A handwritten-style scribble over the total.
    draw.line([(w // 2, h - 200), (w // 2 + 120, h - 180), (w // 2 + 200, h - 210)],
              fill=(20, 20, 160), width=6)
    _save_png(img, f"{name}.png")
    _write_truth(name, {
        "quantity": "100 CARTONS", "gross_weight": "12,450.00 KGS",
        "net_weight": None, "container_number": None, "hs_code": "8517.62.00",
        "consignee_name": "Baltic Trading Ltd", "invoice_value": "8,420.00 USD",
        "incoterms": "CIF Hamburg", "po_number": "PO 4410-DE",
        "country_of_origin": "China",
    }, doc_type="commercial_invoice",
        note="rotated 90, sheared, dark, JPEG 38, stamp + scribble over the total")


def case_two_page() -> None:
    """A 2-page invoice whose totals are on page 2."""
    name = "two_page"
    html = """<html><head><style>
body { font-family: Helvetica; font-size: 10pt; }
table { border-collapse: collapse; width: 100%; margin-top: 4mm; }
td, th { border: 1px solid #999; padding: 2mm; }
</style></head><body>
<h2>COMMERCIAL INVOICE</h2>
<table><tr><td>Invoice number</td><td>INV-2P-01</td></tr>
<tr><td>Invoice date</td><td>2026-10-14</td></tr>
<tr><td>Consignee</td><td>Baltic Trading Ltd</td></tr>
<tr><td>Purchase order</td><td>PO 4410-DE</td></tr>
<tr><td>Terms of delivery</td><td>CIF Hamburg</td></tr>
<tr><td>Country of origin</td><td>China</td></tr></table>
<table><thead><tr><th>Description of goods</th><th>Qty</th><th>Unit</th>
<th>Unit price</th><th>Amount</th></tr></thead><tbody>
<tr><td>Valve assembly 4in</td><td>100</td><td>CARTONS</td><td>84.20</td><td>8,420.00</td></tr>
</tbody></table>
<div style="page-break-before: always">
<p><b>Page 2 — totals</b></p>
<table><tbody>
<tr><td>Total quantity</td><td>100 CARTONS</td></tr>
<tr><td>Total gross weight</td><td>12,450.00 KGS</td></tr>
<tr><td>HS code</td><td>8517.62.00</td></tr>
<tr><td>TOTAL AMOUNT</td><td>USD 8,420.00</td></tr>
</tbody></table></div></body></html>"""
    pages = _pages(html_to_pdf_bytes(html))
    _save_pdf(pages, f"{name}.pdf")
    _write_truth(name, {
        "quantity": "100 CARTONS", "gross_weight": "12,450.00 KGS",
        "net_weight": None, "container_number": None, "hs_code": "8517.62.00",
        "consignee_name": "Baltic Trading Ltd", "invoice_value": "8,420.00 USD",
        "incoterms": "CIF Hamburg", "po_number": "PO 4410-DE",
        "country_of_origin": "China",
    }, doc_type="commercial_invoice", note="2 pages; totals are on page 2")


def case_german() -> None:
    """A German invoice: decimal commas, German labels."""
    name = "german"
    html = """<html><body style="font-family: Helvetica; font-size: 10pt">
<h2>HANDELSRECHNUNG</h2>
<table><tr><td>Rechnungsnummer</td><td>RE-9911</td></tr>
<tr><td>Empf&auml;nger</td><td>Baltic Trading GmbH</td></tr>
<tr><td>Bestellnummer</td><td>PO 4410-DE</td></tr>
<tr><td>Lieferbedingung</td><td>CIF Hamburg</td></tr>
<tr><td>Ursprungsland</td><td>China</td></tr></table>
<table><tr><th>Warenbezeichnung</th><th>Menge</th><th>Einzelpreis</th><th>Betrag</th></tr>
<tr><td>Ventilbaugruppe 4 Zoll</td><td>100 KARTONS</td><td>84,20</td><td>8.420,00</td></tr></table>
<p>Gesamtmenge: 100 KARTONS</p>
<p>Bruttogewicht: 12.450,00 KGS</p>
<p>Warennummer: 8517.62.00</p>
<p>GESAMTBETRAG: 8.420,00 USD</p></body></html>"""
    _save_png(_pages(html_to_pdf_bytes(html))[0], f"{name}.png")
    _write_truth(name, {
        "quantity": "100 KARTONS", "gross_weight": "12.450,00 KGS",
        "net_weight": None, "container_number": None, "hs_code": "8517.62.00",
        "consignee_name": "Baltic Trading GmbH", "invoice_value": "8.420,00 USD",
        "incoterms": "CIF Hamburg", "po_number": "PO 4410-DE",
        "country_of_origin": "China",
    }, doc_type="commercial_invoice",
        note="German labels and decimal commas (12.450,00 / 8.420,00)")


def case_french() -> None:
    """A French invoice: French labels, decimal commas, same numbers."""
    name = "french"
    html = """<html><body style="font-family: Helvetica; font-size: 10pt">
<h2>FACTURE COMMERCIALE</h2>
<table><tr><td>Num&eacute;ro de facture</td><td>FR-9911</td></tr>
<tr><td>Destinataire</td><td>Baltic Trading SARL</td></tr>
<tr><td>N&deg; de commande</td><td>PO 4410-FR</td></tr>
<tr><td>Conditions de livraison</td><td>CIF Hamburg</td></tr>
<tr><td>Pays d'origine</td><td>China</td></tr></table>
<table><tr><th>D&eacute;signation</th><th>Quantit&eacute;</th><th>Prix unitaire</th><th>Montant</th></tr>
<tr><td>Vanne 4 pouces</td><td>100 CARTONS</td><td>84,20</td><td>8.420,00</td></tr></table>
<p>QUANTITE: 100 CARTONS</p>
<p>POIDS BRUT: 12.450,00 KGS</p>
<p>CODE SH: 8517.62.00</p>
<p>MONTANT TOTAL: 8.420,00 USD</p></body></html>"""
    _save_png(_pages(html_to_pdf_bytes(html))[0], f"{name}.png")
    _write_truth(name, {
        "quantity": "100 CARTONS", "gross_weight": "12.450,00 KGS",
        "net_weight": None, "container_number": None, "hs_code": "8517.62.00",
        "consignee_name": "Baltic Trading SARL", "invoice_value": "8.420,00 USD",
        "incoterms": "CIF Hamburg", "po_number": "PO 4410-FR",
        "country_of_origin": "China",
    }, doc_type="commercial_invoice",
        note="French labels and decimal commas (12.450,00 / 8.420,00)")


def case_absent() -> None:
    """An invoice that genuinely prints no PO number and no origin."""
    name = "absent"
    html = """<html><body style="font-family: Helvetica; font-size: 10pt">
<h2>COMMERCIAL INVOICE</h2>
<table><tr><td>Invoice number</td><td>INV-EMPTY-1</td></tr>
<tr><td>Consignee</td><td>Baltic Trading Ltd</td></tr></table>
<table><tr><th>Description</th><th>Qty</th><th>Amount</th></tr>
<tr><td>Valve assembly 4in</td><td>100 CARTONS</td><td>8,420.00</td></tr></table>
<p>Total gross weight: 12,450.00 KGS</p>
<p>HS code: 8517.62.00</p>
<p>TOTAL AMOUNT: USD 8,420.00</p></body></html>"""
    _save_png(_pages(html_to_pdf_bytes(html))[0], f"{name}.png")
    _write_truth(name, {
        "quantity": "100 CARTONS", "gross_weight": "12,450.00 KGS",
        "net_weight": None, "container_number": None, "hs_code": "8517.62.00",
        "consignee_name": "Baltic Trading Ltd", "invoice_value": "8,420.00 USD",
        "incoterms": None, "po_number": None, "country_of_origin": None,
    }, doc_type="commercial_invoice",
        note="PO number, incoterms and origin are genuinely absent from the page")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    case_phone_photo()
    case_two_page()
    case_german()
    case_french()
    case_absent()
    print(f"\nstress inputs -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
