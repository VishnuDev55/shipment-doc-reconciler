"""Synthetic trade documents, as HTML.

Rendered to images by scripts/build_fixtures.py. All names, numbers and
addresses are fictional. Three shipments, each with a commercial invoice, a
packing list and a bill of lading.

    shipment_clean     everything agrees            -> proves the check path
    shipment_conflict  planted errors               -> proves the cross path
    shipment_messy     international formatting     -> proves the normalizer
"""

from __future__ import annotations

CSS = """
@page { size: A4; margin: 14mm 12mm; }
body { font-family: Helvetica, Arial, sans-serif; font-size: 9.5pt; color: #111; }
h1 { font-size: 17pt; margin: 0 0 2mm 0; letter-spacing: 1px; }
.sub { color: #444; font-size: 8.5pt; margin-bottom: 5mm; }
table.party { width: 100%; border: 1px solid #999; margin: 0 0 3mm 0; border-collapse: separate; }
table.party td { border: none; padding: 2mm 3mm; }
td.plbl { width: 30mm; font-size: 7pt; color: #666; text-transform: uppercase; letter-spacing: .5px; vertical-align: top; }
td.pval { font-size: 9pt; }
table { width: 100%; border-collapse: collapse; margin: 3mm 0; }
th, td { border: 1px solid #aaa; padding: 1.6mm 2mm; font-size: 8.5pt; text-align: left; }
th { background: #eee; text-transform: uppercase; font-size: 7.5pt; letter-spacing: .5px; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.totals { width: 62%; margin-left: auto; }
.totals td { border: none; border-top: 1px solid #bbb; padding: 1.2mm 2mm; }
.totals tr.grand td { border-top: 2px solid #111; font-weight: bold; font-size: 10pt; }
.kv { width: auto; }
.kv td { border: none; padding: 1mm 4mm 1mm 0; font-size: 9pt; }
.kv td.k { color: #555; text-transform: uppercase; font-size: 7.5pt; width: 42mm; }
.stamp { margin-top: 6mm; font-size: 8pt; color: #555; border-top: 1px dashed #999; padding-top: 2mm; }
"""


def _page(title: str, subtitle: str, body: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>{CSS}</style></head>
<body><h1>{title}</h1><div class="sub">{subtitle}</div>{body}
<div class="stamp">This document is synthetic test data generated for a hackathon demo.
It does not represent a real shipment, company, or transaction.</div></body></html>"""


CONSIGNEE_BLOCK = """<table class="party"><tr><td class="plbl">Consignee</td>
<td class="pval"><b>{consignee}</b><br>{consignee_addr}</td></tr></table>"""

SHIPPER_BLOCK = """<table class="party"><tr><td class="plbl">Shipper / Exporter</td>
<td class="pval"><b>{shipper}</b><br>{shipper_addr}</td></tr></table>"""


def invoice(
    *,
    doc_no: str,
    date: str,
    shipper: str,
    shipper_addr: str,
    consignee: str,
    consignee_addr: str,
    po_no: str,
    lines: list[tuple[str, str, str, str, str]],
    qty_total: str,
    weight_total: str,
    incoterm: str,
    currency_word: str,
    subtotal: str,
    freight: str,
    total: str,
    hs_code: str,
    origin: str,
) -> str:
    rows = "".join(
        f"<tr><td>{d}</td><td class='num'>{q}</td><td class='num'>{u}</td>"
        f"<td class='num'>{p}</td><td class='num'>{a}</td></tr>"
        for d, q, u, p, a in lines
    )
    body = (
        SHIPPER_BLOCK.format(shipper=shipper, shipper_addr=shipper_addr)
        + CONSIGNEE_BLOCK.format(consignee=consignee, consignee_addr=consignee_addr)
        + f"""<table class="kv">
<tr><td class="k">Invoice number</td><td>{doc_no}</td></tr>
<tr><td class="k">Invoice date</td><td>{date}</td></tr>
<tr><td class="k">Purchase order</td><td>{po_no}</td></tr>
<tr><td class="k">Terms of delivery</td><td>{incoterm}</td></tr>
<tr><td class="k">Country of origin</td><td>{origin}</td></tr>
</table>
<table><thead><tr><th>Description of goods</th><th>Qty</th><th>Unit</th>
<th>Unit price</th><th>Amount</th></tr></thead><tbody>{rows}</tbody></table>
<table class="totals"><tbody>
<tr><td>Total quantity</td><td class="num">{qty_total}</td></tr>
<tr><td>Total gross weight</td><td class="num">{weight_total}</td></tr>
<tr><td>HS code</td><td class="num">{hs_code}</td></tr>
<tr><td>Subtotal</td><td class="num">{subtotal}</td></tr>
<tr><td>Freight</td><td class="num">{freight}</td></tr>
<tr class="grand"><td>TOTAL AMOUNT</td><td class="num">{total}</td></tr>
</tbody></table>
<div style="margin-top:4mm;font-size:8.5pt">SAY: {currency_word} ONLY</div>"""
    )
    return _page("COMMERCIAL INVOICE", "Invoice document — synthetic sample", body)


def packing_list(
    *,
    doc_no: str,
    date: str,
    consignee: str,
    consignee_addr: str,
    invoice_ref: str,
    lines: list[tuple[str, str, str, str, str, str]],
    total_packages: str,
    total_gross: str,
    total_net: str,
    total_volume: str,
    origin: str,
    container: str | None = None,
) -> str:
    rows = "".join(
        f"<tr><td class='num'>{m}</td><td>{d}</td><td class='num'>{q}</td>"
        f"<td class='num'>{gw}</td><td class='num'>{nw}</td><td class='num'>{v}</td></tr>"
        for m, d, q, gw, nw, v in lines
    )
    cont_row = (
        f'<tr><td class="k">Container no.</td><td>{container}</td></tr>' if container else ""
    )
    body = (
        CONSIGNEE_BLOCK.format(consignee=consignee, consignee_addr=consignee_addr)
        + f"""<table class="kv">
<tr><td class="k">Packing list no.</td><td>{doc_no}</td></tr>
<tr><td class="k">Date</td><td>{date}</td></tr>
<tr><td class="k">Invoice reference</td><td>{invoice_ref}</td></tr>
<tr><td class="k">Country of origin</td><td>{origin}</td></tr>
{cont_row}
</table>
<table><thead><tr><th>Mark / No.</th><th>Description</th><th>Packages</th>
<th>Gross wt</th><th>Net wt</th><th>Volume</th></tr></thead><tbody>{rows}</tbody></table>
<table class="totals"><tbody>
<tr><td>TOTAL NO. OF PACKAGES</td><td class="num">{total_packages}</td></tr>
<tr><td>TOTAL GROSS WT</td><td class="num">{total_gross}</td></tr>
<tr><td>TOTAL NET WT</td><td class="num">{total_net}</td></tr>
<tr><td>TOTAL MEASUREMENT</td><td class="num">{total_volume}</td></tr>
</tbody></table>"""
    )
    return _page("PACKING LIST", "Packing specification — synthetic sample", body)


def bol(
    *,
    doc_no: str,
    date: str,
    shipper: str,
    shipper_addr: str,
    consignee: str,
    consignee_addr: str,
    vessel: str,
    voyage: str,
    port_loading: str,
    port_discharge: str,
    container: str,
    seal: str,
    packages: str,
    description: str,
    gross_weight: str,
    net_weight: str,
    freight_terms: str,
) -> str:
    body = (
        SHIPPER_BLOCK.format(shipper=shipper, shipper_addr=shipper_addr)
        + CONSIGNEE_BLOCK.format(consignee=consignee, consignee_addr=consignee_addr)
        + f"""<table class="kv">
<tr><td class="k">Bill of lading no.</td><td>{doc_no}</td></tr>
<tr><td class="k">Date of issue</td><td>{date}</td></tr>
<tr><td class="k">Vessel / voyage</td><td>{vessel} / {voyage}</td></tr>
<tr><td class="k">Port of loading</td><td>{port_loading}</td></tr>
<tr><td class="k">Port of discharge</td><td>{port_discharge}</td></tr>
</table>
<table class="kv">
<tr><td class="k">Container no.</td><td>{container}</td></tr>
<tr><td class="k">Seal no.</td><td>{seal}</td></tr>
<tr><td class="k">No. of containers or packages</td><td>{packages}</td></tr>
<tr><td class="k">Description of packages and goods</td><td>{description}</td></tr>
<tr><td class="k">Gross weight</td><td>{gross_weight}</td></tr>
<tr><td class="k">Net weight</td><td>{net_weight}</td></tr>
<tr><td class="k">Freight &amp; charges</td><td>{freight_terms}</td></tr>
</table>"""
    )
    return _page("BILL OF LADING", "Sea waybill — synthetic sample", body)
