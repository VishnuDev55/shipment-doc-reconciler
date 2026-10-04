"""The frozen canonical field set and per-document-type expectations.

Ten fields, fixed keys, one canonical order. This module is the only place the
field list lives: the JSON Schema, the prompt, the normalizer dispatch, the
comparator table, the UI rows and the authored fixtures are all driven from
here, so adding a field means editing this file and nothing else that matters.
"""

from __future__ import annotations

from typing import Literal

DocType = Literal["commercial_invoice", "packing_list", "bill_of_lading",
              "certificate_of_origin", "other"]
FieldKey = Literal[
    "quantity",
    "gross_weight",
    "net_weight",
    "container_number",
    "hs_code",
    "consignee_name",
    "invoice_value",
    "incoterms",
    "po_number",
    "country_of_origin",
]
FieldKind = Literal[
    "count",
    "weight",
    "container_number",
    "hs_code",
    "party_name",
    "money",
    "incoterm",
    "reference",
    "country",
]

FIELD_ORDER: tuple[FieldKey, ...] = (
    "quantity",
    "gross_weight",
    "net_weight",
    "container_number",
    "hs_code",
    "consignee_name",
    "invoice_value",
    "incoterms",
    "po_number",
    "country_of_origin",
)

DOC_TYPES: tuple[DocType, ...] = (
    "commercial_invoice",
    "packing_list",
    "bill_of_lading",
    "certificate_of_origin",
    "other",
)

FIELD_KIND: dict[FieldKey, FieldKind] = {
    "quantity": "count",
    "gross_weight": "weight",
    "net_weight": "weight",
    "container_number": "container_number",
    "hs_code": "hs_code",
    "consignee_name": "party_name",
    "invoice_value": "money",
    "incoterms": "incoterm",
    "po_number": "reference",
    "country_of_origin": "country",
}

FIELD_LABEL: dict[FieldKey, str] = {
    "quantity": "Quantity",
    "gross_weight": "Gross weight",
    "container_number": "Container no.",
    "hs_code": "HS code",
    "consignee_name": "Consignee",
    "invoice_value": "Invoice value",
    "net_weight": "Net weight",
    "incoterms": "Incoterms",
    "po_number": "PO number",
    "country_of_origin": "Country of origin",
}

# Fields that SHOULD be present on each document type. A field missing from a
# document where it is not expected is normal and never rendered as a problem.
EXPECTED: dict[DocType, frozenset[FieldKey]] = {
    "commercial_invoice": frozenset({
        "quantity", "consignee_name", "hs_code", "invoice_value", "incoterms",
        "po_number", "country_of_origin",
    }),
    "packing_list": frozenset({
        "quantity", "gross_weight", "net_weight", "consignee_name", "country_of_origin",
    }),
    "bill_of_lading": frozenset({
        "quantity", "gross_weight", "net_weight", "container_number", "consignee_name",
    }),
    # A certificate of origin exists to state two of our fields and nothing else:
    # it carries no value line on purpose, because duty is assessed from the
    # invoice, not the certificate.
    "certificate_of_origin": frozenset({
        "quantity", "gross_weight", "net_weight", "hs_code", "consignee_name",
        "country_of_origin",
    }),
    "other": frozenset(),
}

# Human-facing description of where each field usually appears, used in the
# "absent" reason string so the user can tell expected-absent from worrying.
TYPICALLY_IN: dict[FieldKey, str] = {
    "quantity": "invoice, packing list, bill of lading",
    "gross_weight": "packing list, bill of lading",
    "container_number": "bill of lading",
    "hs_code": "invoice, certificate of origin",
    "consignee_name": "all documents",
    "invoice_value": "invoice",
    "net_weight": "packing list, bill of lading",
    "incoterms": "invoice",
    "po_number": "invoice, purchase order references on any document",
    "country_of_origin": "invoice, packing list, certificate of origin",
}

# Surface-form hints sent to the model in the user prompt. These tell it what
# to look for and, critically, what NOT to grab (per-carton weight instead of
# total, net instead of gross).
SURFACE_HINTS: dict[FieldKey, str] = {
    "quantity": (
        "Look for QUANTITY, QTY, TOTAL QTY, NO. OF PACKAGES, CARTONS, PALLETS, "
        "PIECES. Return the TOTAL for the shipment, not a per-line figure. "
        "Include the package unit if printed next to it (e.g. '100 CARTONS')."
    ),
    "gross_weight": (
        "Look for GROSS WEIGHT, G.W., TOTAL GROSS WT, BRUTTO, GEWICHT. Return the "
        "TOTAL gross weight for the whole shipment, never a per-carton figure and "
        "never the net weight. Copy the unit exactly as printed (KGS, LBS, MT)."
    ),
    "container_number": (
        "Look for CONTAINER NO., CTNR, CONTAINER NUMBER, EQUIPMENT. It is normally "
        "4 uppercase letters followed by 7 digits. Copy it exactly, including any "
        "spaces or dots you can see."
    ),
    "hs_code": (
        "Look for HS CODE, H.S. CODE, HTS, TARIFF CODE, HS-NUMMER. Return the code "
        "for the goods. If several different codes appear, return the first one and "
        "quote the line it came from."
    ),
    "consignee_name": (
        "Look for CONSIGNEE, BUYER, SHIP TO, DELIVER TO, EMPFÄNGER. Return the "
        "consignee's name only, not the address. Copy punctuation and legal suffixes "
        "exactly as printed."
    ),
    "invoice_value": (
        "Look for TOTAL, TOTAL AMOUNT, GRAND TOTAL, TOTAL DUE, SAY ... ONLY. Return "
        "the final total invoice amount including the currency as printed "
        "(e.g. 'USD 8,420.00', '12.500,00 EUR')."
    ),
    "net_weight": (
        "Look for NET WEIGHT, NET WT, N.W., NETTO. Return the TOTAL net weight for "
        "the whole shipment, never a per-carton figure and never the gross weight. "
        "Copy the unit exactly as printed."
    ),
    "incoterms": (
        "Look for INCOTERMS, INCO TERMS, TERMS OF DELIVERY, and a three-letter code "
        "followed by a place (e.g. 'CIF HAMBURG', 'FCA SHANGHAI'). Return the code "
        "and the place exactly as printed."
    ),
    "po_number": (
        "Look for PO NO., PURCHASE ORDER, ORDER NO., CUSTOMER ORDER REF. Return the "
        "reference exactly as printed. A row labelled INVOICE NO. or INVOICE "
        "REFERENCE is the seller's own number, not a purchase order: if nothing "
        "purchase-order-like is printed, return present=false rather than "
        "reaching for the nearest reference."
    ),
    "country_of_origin": (
        "Look for COUNTRY OF ORIGIN, ORIGIN, MADE IN, PAYS D'ORIGINE. Return the "
        "country as printed (e.g. 'GERMANY', 'DE', 'Made in Germany'). A port of "
        "loading, a port of discharge or any other place name is NOT the origin: "
        "'Ningbo, China' under PORT OF LOADING does not make China the stated "
        "origin unless the document says so in an origin field."
    ),
}
