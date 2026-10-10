"""A planted-error test set: thirteen shipments whose ground truth is known.

The demo fixtures in `shipments.py` answer "does the app run". These answer a
sharper question -- "when something is wrong, does it say so, and when nothing is
wrong, does it keep quiet?" -- because here the right answer is written down
before the code runs. Each case declares what it contains and what the report is
expected to conclude, and `scripts/evaluate_planted.py` scores the run against
that declaration.

Three groups:

  clean    nothing wrong. A finding here is a false alarm.
  tricky   values that differ in *form* only -- pounds against kilos, a European
           decimal comma, national HS digits, a legal suffix, a counting basis.
           Every one of these was a real defect found during this project's
           development, so the group exists to stop it coming back.
  planted  exactly one error per shipment, of a different kind each time: a
           container that fails its own checksum against another document, a
           consignee who is a different company, a heading that is not the same
           product, two currencies, a weight gap, arithmetic that does not close,
           and a value no document prints.

Everything here is hand-authored, including the model responses. That is the
point: the bundle must run offline, cost nothing, and be reproducible, so the
score measures the engine rather than a model's mood. It says nothing about live
accuracy -- `docs/EVAL.md` carries that, from captured responses.

A document and its extraction are generated from the same dict, so they cannot
drift: the page prints `GROSS WEIGHT 12,450.00 KGS` and the response quotes that
line because it is the line. A field listed in `invented` is claimed by the
response and *not* printed on the page, which is the only way to make a
fabrication reproducible.
"""

from __future__ import annotations

from .templates import CSS

from app.fields import FIELD_ORDER

# The label printed beside each value. Uppercased on the page, kept as written
# here in the quote, because the evidence check folds case.
LABELS: dict[str, str] = {
    "quantity": "Total quantity",
    "gross_weight": "Total gross weight",
    "net_weight": "Total net weight",
    "container_number": "Container no.",
    "hs_code": "HS code",
    "consignee_name": "Consignee",
    "invoice_value": "Total amount",
    "incoterms": "Terms of delivery",
    "po_number": "PO no.",
    "country_of_origin": "Country of origin",
}

TITLE = {
    "commercial_invoice": "COMMERCIAL INVOICE",
    "packing_list": "PACKING LIST",
    "bill_of_lading": "BILL OF LADING",
    "certificate_of_origin": "CERTIFICATE OF ORIGIN",
}

# A 600 g gap on a 12,450 kg shipment. The engine's absolute band is 1 kg, so it
# reads as rounding -- which is the designed miss, documented in docs/PLANTED.md.
NEAR_MISS_KGS = "12,449.40 KGS"


def _doc(**spec) -> dict:
    """One document. `fields` prints, `invented` claims without printing."""
    return {
        "fields": dict(spec.get("fields", {})),
        "invented": dict(spec.get("invented", {})),
        "rows": list(spec.get("rows", [])),
        "total": spec.get("total"),
        "doc_no": spec.get("doc_no", "PLANTED-2026-0001"),
        "date": spec.get("date", "2026-04-02"),
    }


def render_page(doc_type: str, doc: dict) -> str:
    """The document as HTML: a header, one labelled line per printed field.

    Deliberately plainer than the demo fixtures. These pages exist to make a
    known value appear in a known place, and a layout with more going on would
    make it harder to see what a score means.
    """
    rows = "".join(
        f'<tr><td class="k">{LABELS[key]}</td><td>{value}</td></tr>'
        for key, value in doc["fields"].items()
    )
    goods = ""
    if doc["rows"]:
        body = "".join(
            f'<tr><td>{desc}</td><td class="num">{qty}</td><td>{unit}</td>'
            f'<td class="num">{price}</td><td class="num">{line_total}</td></tr>'
            for desc, qty, unit, price, line_total in doc["rows"]
        )
        goods = (
            '<table><thead><tr><th>Description</th><th>Qty</th><th>Unit</th>'
            '<th>Unit price</th><th>Line total</th></tr></thead>'
            f'<tbody>{body}</tbody></table>'
        )
    totals = (f'<table class="totals"><tr><td>TOTAL</td>'
              f'<td class="num">{doc["total"]}</td></tr></table>') if doc["total"] else ""
    body = (
        f'<table class="kv"><tr><td class="k">Document no.</td>'
        f'<td>{doc["doc_no"]}</td></tr>'
        f'<tr><td class="k">Date</td><td>{doc["date"]}</td></tr></table>'
        f'<table class="kv">{rows}</table>{goods}{totals}'
    )
    subtitle = f"{TITLE[doc_type]} — planted-error test set, synthetic data"
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'><style>{CSS}</style>"
            f"</head><body><h1>{TITLE[doc_type]}</h1><div class='sub'>{subtitle}</div>"
            f"{body}<div class='stamp'>Synthetic test data for the planted-error "
            f"evaluation. Not a real shipment, company, or transaction.</div>"
            f"</body></html>")


def extraction(doc: dict) -> dict:
    """The authored model response for one rendered page.

    Every value printed gets the line it was printed on as its quote, so a clean
    case can pass the evidence check for the right reason. `invented` values are
    quoted from a line this renderer never emits.
    """
    # Every contracted field appears: a missing key is not the same message as
    # `present: false`, and the schema asks for all ten.
    spec: dict = dict.fromkeys(FIELD_ORDER)
    for key, value in doc["fields"].items():
        spec[key] = (value, f"{LABELS[key]} {value}", 1, 0.96)
    for key, (value, quote) in doc["invented"].items():
        spec[key] = (value, quote, 1, 0.9)
    return spec


def line_items(doc: dict) -> list[dict] | None:
    """The goods rows, in the shape the second contract wants.

    Only the printed rows are transcribed. A document with no table returns None
    so the arithmetic rules report `not_run` for it rather than inventing a
    passing check.
    """
    if not doc["rows"]:
        return None
    return [
        {
            "description": desc, "sku": "", "quantity": qty, "unit": unit,
            "unit_price": price, "line_total": line_total,
            "gross_weight": "", "net_weight": "",
            "evidence_text": f"{desc} {qty} {unit} {price} {line_total}",
            "page_index": 1,
        }
        for desc, qty, unit, price, line_total in doc["rows"]
    ]


# ---------------------------------------------------------------------------
# Shared values. Anything a case overrides, it overrides in one place.
# ---------------------------------------------------------------------------

_HS = "8517.62.00"
_CONSIGNEE = "Nordwind Industrial Supply GmbH"
_GROSS = "12,450.00 KGS"
_NET = "11,600.00 KGS"
_QTY = "100 CARTONS"
_VALUE = "USD 8,420.00"
_ORIGIN = "China"
_INCOTERM = "CIF Hamburg (Incoterms 2020)"
_PO = "PO 4410-DE"
_CONTAINER = "MSKU3067322"
_CONTAINER_BAD = "MSKU3067328"          # same body, corrupted ISO 6346 check digit

_GOODS = [
    ("CNC aluminium housing, type H-40", "600", "pieces", "4.85", "2,910.00"),
    ("Stainless fastener set, M6", "500", "pieces", "11.02", "5,510.00"),
]
_GOODS_BAD_ROW = [
    ("CNC aluminium housing, type H-40", "600", "pieces", "4.85", "2,915.00"),
    ("Stainless fastener set, M6", "500", "pieces", "11.02", "5,510.00"),
]
_GOODS_BAD_SUM = [
    ("CNC aluminium housing, type H-40", "600", "pieces", "4.85", "2,910.00"),
    ("Stainless fastener set, M6", "500", "pieces", "11.02", "5,510.00"),
]


def _invoice(**over) -> dict:
    rows = over.pop("rows", _GOODS)
    total = over.pop("total", _VALUE)
    fields = {"quantity": _QTY, "gross_weight": _GROSS, "hs_code": _HS,
              "consignee_name": _CONSIGNEE, "invoice_value": _VALUE,
              "incoterms": _INCOTERM, "po_number": _PO,
              "country_of_origin": _ORIGIN}
    fields.update(over.pop("fields", {}))
    doc_no = over.pop("doc_no", "HW-2026-0912")
    return _doc(doc_type="commercial_invoice", fields=fields, rows=rows,
                total=total, doc_no=doc_no, **over)


def _packing(**over) -> dict:
    fields = {"quantity": _QTY, "gross_weight": _GROSS, "net_weight": _NET,
              "consignee_name": _CONSIGNEE, "country_of_origin": _ORIGIN}
    fields.update(over.pop("fields", {}))
    doc_no = over.pop("doc_no", "PL-2026-0912")
    return _doc(doc_type="packing_list", fields=fields, doc_no=doc_no, **over)


def _bol(**over) -> dict:
    fields = {"quantity": _QTY, "gross_weight": _GROSS, "net_weight": _NET,
              "container_number": _CONTAINER, "consignee_name": _CONSIGNEE}
    fields.update(over.pop("fields", {}))
    doc_no = over.pop("doc_no", "BOL-2026-0912")
    return _doc(doc_type="bill_of_lading", fields=fields, doc_no=doc_no, **over)


def _coo(**over) -> dict:
    fields = {"quantity": _QTY, "gross_weight": _GROSS, "net_weight": _NET,
              "hs_code": _HS, "consignee_name": _CONSIGNEE,
              "country_of_origin": _ORIGIN}
    fields.update(over.pop("fields", {}))
    doc_no = over.pop("doc_no", "CO-2026-0912")
    return _doc(doc_type="certificate_of_origin", fields=fields,
                doc_no=doc_no, **over)


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------

CASES: list[dict] = [
    # ---------------- clean: a finding here is a false alarm ----------------
    {
        "name": "clean_three_documents",
        "group": "clean",
        "category": "baseline",
        "docs": [("commercial_invoice", _invoice()), ("packing_list", _packing()),
                 ("bill_of_lading", _bol())],
        "expect": {"outcome": "ready"},
        "note": "The reference case: three documents, every field agreeing.",
    },
    {
        "name": "clean_four_documents",
        "group": "clean",
        "category": "baseline",
        "docs": [("commercial_invoice", _invoice()), ("packing_list", _packing()),
                 ("bill_of_lading", _bol()), ("certificate_of_origin", _coo())],
        "expect": {"outcome": "ready"},
        "note": "A certificate of origin adds a fourth reader, not a fourth claim.",
    },
    {
        "name": "clean_two_documents",
        "group": "clean",
        "category": "coverage",
        "docs": [("commercial_invoice", _invoice()), ("packing_list", _packing())],
        "expect": {"outcome": "ready"},
        "note": "No bill of lading, so no container number anywhere: an expected "
                "absence, and the only case where the goods lines are read from "
                "one document and cannot be cross-checked.",
    },

    # ------- tricky but clean: same shipment, different paperwork -------
    {
        "name": "tricky_pounds_and_kilos",
        "group": "tricky",
        "category": "units",
        "docs": [("commercial_invoice", _invoice()),
                 ("packing_list", _packing()),
                 ("bill_of_lading", _bol(fields={"gross_weight": "27,447.00 LBS",
                                                 "net_weight": "25,573.63 LBS"}))],
        "expect": {"outcome": "ready"},
        "note": "12,450 kg is 27,447.06 lb. The engine converts units; it never "
                "converts currency, and this is the difference.",
    },
    {
        "name": "tricky_decimal_comma",
        "group": "tricky",
        "category": "formatting",
        "docs": [("commercial_invoice", _invoice()),
                 ("packing_list", _packing(fields={"gross_weight": "12.450,00 kg"}))],
        "expect": {"outcome": "ready"},
        "note": "European grouping. A naive diff calls this a conflict; the "
                "normalizer reads the same number.",
    },
    {
        "name": "tricky_national_digits",
        "group": "tricky",
        "category": "classification",
        "docs": [("commercial_invoice", _invoice()),
                 ("certificate_of_origin", _coo(fields={"hs_code": "8517.62.0090"}))],
        "expect": {"outcome": "ready"},
        "note": "HS is harmonised to six digits; the tail is each country's own. "
                "Comparing the whole string would flag every shipment.",
    },
    {
        "name": "tricky_legal_suffix",
        "group": "tricky",
        "category": "party",
        "docs": [("commercial_invoice", _invoice()),
                 ("packing_list", _packing(fields={"consignee_name": "Nordwind Industrial Supply"}))],
        "expect": {"outcome": "ready"},
        "note": "The same buyer with the suffix dropped. Fuzzy on purpose, with "
                "the score printed so a reader can disagree with it.",
    },
    {
        "name": "tricky_counting_basis",
        "group": "tricky",
        "category": "counting basis",
        "docs": [("commercial_invoice", _invoice(fields={"quantity": "1,200 pieces"})),
                 ("packing_list", _packing(fields={"quantity": "100 CARTONS"}))],
        "expect": {"outcome": "quiet"},
        "note": "1,200 items in 100 cartons. A difference of basis, not of fact, "
                "so the severity table calls it informational and no supplier "
                "letter is written about it. The banner still says review, "
                "because the verdict floor holds whatever this level says -- "
                "'ready' and 'nothing to block' are different claims.",
    },
    {
        "name": "tricky_single_source",
        "group": "tricky",
        "category": "coverage",
        "docs": [("commercial_invoice", _invoice()),
                 ("packing_list", _packing(fields={"po_number": _PO}))],
        "expect": {"outcome": "ready"},
        "note": "The PO printed on both, the incoterms on one. Nothing to "
                "reconcile a lone statement against.",
    },

    # ---------------- planted: exactly one error each ----------------
    {
        "name": "planted_container",
        "group": "planted",
        "category": "container",
        "docs": [("commercial_invoice", _invoice()),
                 ("packing_list", _packing(fields={"container_number": _CONTAINER})),
                 ("bill_of_lading", _bol(fields={"container_number": _CONTAINER_BAD}))],
        "expect": {"outcome": "blocked", "level": "critical",
                   "code": "comparison:container_number:conflict"},
        "note": "One digit apart, and the check digit says which one is wrong.",
    },
    {
        "name": "planted_consignee",
        "group": "planted",
        "category": "party",
        "docs": [("commercial_invoice", _invoice()),
                 ("packing_list", _packing(fields={"consignee_name": "Baltic Trading Ltd"}))],
        "expect": {"outcome": "blocked", "level": "critical",
                   "code": "comparison:consignee_name:conflict"},
        "note": "Goods delivered to a company that never bought them.",
    },
    {
        "name": "planted_hs_heading",
        "group": "planted",
        "category": "classification",
        "docs": [("commercial_invoice", _invoice()),
                 ("certificate_of_origin", _coo(fields={"hs_code": "8471.30.00"}))],
        "expect": {"outcome": "blocked", "level": "critical",
                   "code": "comparison:hs_code:conflict"},
        "note": "Automatic data processing against telecommunications: a "
                "different product, a different duty.",
    },
    {
        "name": "planted_currency",
        "group": "planted",
        "category": "currency",
        "docs": [("commercial_invoice", _invoice()),
                 ("packing_list", _packing(fields={"invoice_value": "EUR 8,420.00"}))],
        "expect": {"outcome": "blocked", "level": "critical",
                   "code": "comparison:invoice_value:conflict:currency"},
        "note": "The same number in two currencies is not a rounding gap. The "
                "engine refuses to convert, so it refuses to compare.",
    },
    {
        "name": "planted_weight",
        "group": "planted",
        "category": "weight",
        "docs": [("commercial_invoice", _invoice()),
                 ("packing_list", _packing(fields={"gross_weight": "14,250.00 KGS"}))],
        "expect": {"outcome": "review", "level": "warning",
                   "code": "comparison:gross_weight:conflict"},
        "note": "1.8 tonnes on a 12-tonne shipment: a real discrepancy, not a "
                "classification problem.",
    },
    {
        "name": "planted_line_math",
        "group": "planted",
        "category": "arithmetic",
        "docs": [("commercial_invoice", _invoice(rows=_GOODS_BAD_ROW)),
                 ("packing_list", _packing())],
        "expect": {"outcome": "review", "level": "warning", "code": "math:fail",
                   "check": "line_total_math"},
        "note": "600 x 4.85 printed as 2,915.00. Every document agrees because "
                "every document copied the same invoice -- the hole "
                "cross-document comparison cannot see.",
    },
    {
        "name": "planted_line_sum",
        "group": "planted",
        "category": "arithmetic",
        "docs": [("commercial_invoice", _invoice(rows=_GOODS_BAD_SUM,
                                                 total="USD 9,420.00",
                                                 fields={"invoice_value": "USD 9,420.00"})),
                 ("packing_list", _packing())],
        "expect": {"outcome": "review", "level": "warning", "code": "math:fail",
                   "check": "line_total_sum"},
        "note": "The rows add to 8,420.00 and the total says 9,420.00.",
    },
    {
        "name": "planted_net_above_gross",
        "group": "planted",
        "category": "arithmetic",
        "docs": [("commercial_invoice", _invoice()),
                 ("packing_list", _packing(fields={"net_weight": "12,900.00 KGS"}))],
        "expect": {"outcome": "review", "level": "warning", "code": "math:fail",
                   "check": "net_above_gross_in_document"},
        "note": "Packaging cannot have negative mass. Needs no goods lines, so it "
                "runs on every shipment.",
    },
    {
        "name": "planted_invented_value",
        "group": "planted",
        "category": "invented evidence",
        "docs": [("commercial_invoice", _invoice()),
                 # A distinct document number, so this page is not byte-identical
                 # to the plain bill of lading: the response claims a PO the page
                 # does not print, and two authored answers for one set of page
                 # hashes would make the score depend on which the cache read first.
                 ("bill_of_lading", _bol(doc_no="BOL-2026-0913",
                                         invented={"po_number": ("PO 9999-NOPE",
                                                                 "PO NO. 9999-NOPE")}))],
        "expect": {"outcome": "review", "level": "warning", "code": "evidence:excluded",
                   "excluded": "po_number"},
        "note": "The bill of lading prints no PO number at all, and the response "
                "quotes one. Without the text layer this becomes a false conflict "
                "between two documents; with it, the value is withdrawn.",
    },

    # ---------------- the miss, designed and declared ----------------
    {
        "name": "miss_inside_tolerance",
        "group": "miss",
        "category": "weight",
        "docs": [("commercial_invoice", _invoice()),
                 ("packing_list", _packing(fields={"gross_weight": NEAR_MISS_KGS}))],
        "expect": {"outcome": "ready", "caught": False},
        "note": "600 g on 12,450 kg. The engine's absolute band is 1 kg, chosen "
                "because a re-weigh of a container rarely agrees to the gram, so "
                "this is reported as agreement. It is the cost of that band, "
                "written down rather than discovered by a customer.",
    },
]


def case_names() -> list[str]:
    return [c["name"] for c in CASES]
