"""The frozen canonical field set and per-document-type expectations.

Six fields, fixed keys. This module is the only place the field list lives.
"""

from __future__ import annotations

from typing import Literal

DocType = Literal["commercial_invoice", "packing_list", "bill_of_lading", "other"]
FieldKey = Literal[
    "quantity",
    "gross_weight",
    "container_number",
    "hs_code",
    "consignee_name",
    "invoice_value",
]
FieldKind = Literal["count", "weight", "container_number", "hs_code", "party_name", "money"]

FIELD_ORDER: tuple[FieldKey, ...] = (
    "quantity",
    "gross_weight",
    "container_number",
    "hs_code",
    "consignee_name",
    "invoice_value",
)

DOC_TYPES: tuple[DocType, ...] = (
    "commercial_invoice",
    "packing_list",
    "bill_of_lading",
    "other",
)

FIELD_KIND: dict[FieldKey, FieldKind] = {
    "quantity": "count",
    "gross_weight": "weight",
    "container_number": "container_number",
    "hs_code": "hs_code",
    "consignee_name": "party_name",
    "invoice_value": "money",
}

FIELD_LABEL: dict[FieldKey, str] = {
    "quantity": "Quantity",
    "gross_weight": "Gross weight",
    "container_number": "Container no.",
    "hs_code": "HS code",
    "consignee_name": "Consignee",
    "invoice_value": "Invoice value",
}

# Fields that SHOULD be present on each document type. A field missing from a
# document where it is not expected is normal and never rendered as a problem.
EXPECTED: dict[DocType, frozenset[FieldKey]] = {
    "commercial_invoice": frozenset({"quantity", "consignee_name", "hs_code", "invoice_value"}),
    "packing_list": frozenset({"quantity", "gross_weight", "consignee_name"}),
    "bill_of_lading": frozenset({"quantity", "gross_weight", "container_number", "consignee_name"}),
    "other": frozenset(),
}

# Human-facing description of where each field usually appears, used in the
# "absent" reason string so the user can tell expected-absent from worrying.
TYPICALLY_IN: dict[FieldKey, str] = {
    "quantity": "invoice, packing list, bill of lading",
    "gross_weight": "packing list, bill of lading",
    "container_number": "bill of lading",
    "hs_code": "invoice",
    "consignee_name": "all documents",
    "invoice_value": "invoice",
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
}
