"""Three synthetic shipments, defined once.

The same dict drives BOTH the rendered document and the expected extraction
JSON, so a fixture image and its cached response can never drift apart. The
extraction values are the literal strings printed on the page — which is what
a correct model extraction would return.

These are authored, not model output. They exercise the pipeline; they do not
prove anything about the live model. See PROGRESS.md.
"""

from __future__ import annotations

from . import templates

SHIPPER = "Hanwei Precision Components Co., Ltd."
SHIPPER_ADDR = "88 Jianshe Road, Yinzhou District\n315040 Ningbo, Zhejiang, China"

CONSIGNEE = "Nordwind Industrial Supply GmbH"
CONSIGNEE_ADDR = "Hafenstrasse 12\n20457 Hamburg, Germany"

# MSKU3067322 is a real ISO 6346-valid number (check digit 2 verified against
# the mod-11 algorithm). MSKU3067328 is the same body with a corrupted check
# digit, so it fails its own checksum.
CONTAINER_OK = "MSKU3067322"
CONTAINER_BAD = "MSKU3067328"

SHIPMENTS: dict[str, dict] = {
    # ------------------------------------------------------------------
    # Everything agrees. Proves the check path.
    # ------------------------------------------------------------------
    "shipment_clean": {
        "invoice_no": "HW-2026-0912",
        "date": "2026-04-02",
        "consignee": CONSIGNEE,
        "consignee_addr": CONSIGNEE_ADDR,
        "shipper": SHIPPER,
        "shipper_addr": SHIPPER_ADDR,
        "quantity": "100 CARTONS",
        "gross_weight": "12,450.00 KGS",
        "container": CONTAINER_OK,
        "hs_code": "8517.62.00",
        "invoice_value": "USD 8,420.00",
        "docs": {
            "commercial_invoice": {
                "filename": "invoice_clean.png",
                "html": lambda s: templates.invoice(
                    doc_no=s["invoice_no"], date=s["date"], shipper=s["shipper"],
                    shipper_addr=s["shipper_addr"], consignee=s["consignee"],
                    consignee_addr=s["consignee_addr"], po_no="PO 4410-DE",
                    lines=[
                        ("CNC aluminium housing, type H-40", "600", "pieces", "4.85", "2,910.00"),
                        ("Stainless fastener set, M6", "600", "pieces", "9.18", "5,510.00"),
                    ],
                    qty_total="100 CARTONS",
                    weight_total="12,450.00 KGS",
                    incoterm="CIF Hamburg (Incoterms 2020)",
                    currency_word="US DOLLARS EIGHT THOUSAND FOUR HUNDRED TWENTY",
                    subtotal="8,420.00", freight="0.00", total="USD 8,420.00",
                    hs_code="8517.62.00",
                ),
                "extraction": lambda s: {
                    "quantity": ("100 CARTONS", "Total quantity 100 CARTONS", 1, 0.97),
                    "gross_weight": ("12,450.00 KGS", "Total gross weight 12,450.00 KGS", 1, 0.96),
                    "container_number": None,
                    "hs_code": ("8517.62.00", "HS code 8517.62.00", 1, 0.95),
                    "consignee_name": ("Nordwind Industrial Supply GmbH", "Consignee Nordwind Industrial Supply GmbH", 1, 0.98),
                    "invoice_value": ("USD 8,420.00", "TOTAL AMOUNT USD 8,420.00", 1, 0.97),
                },
            },
            "packing_list": {
                "filename": "packing_list_clean.png",
                "html": lambda s: templates.packing_list(
                    doc_no="PL-2026-0912", date=s["date"], consignee=s["consignee"],
                    consignee_addr=s["consignee_addr"], invoice_ref=s["invoice_no"],
                    lines=[
                        ("1-50", "CNC aluminium housings", "50 CTN", "6,225.00", "5,800.00", "18.40"),
                        ("51-100", "Stainless fastener sets", "50 CTN", "6,225.00", "5,800.00", "12.10"),
                    ],
                    total_packages="100 cartons",
                    total_gross="12,450.00 KGS", total_net="11,600.00 KGS",
                    total_volume="30.50 CBM", container=CONTAINER_OK,
                ),
                "extraction": lambda s: {
                    "quantity": ("100 cartons", "TOTAL NO. OF PACKAGES 100 cartons", 1, 0.97),
                    "gross_weight": ("12,450.00 KGS", "TOTAL GROSS WT 12,450.00 KGS", 1, 0.97),
                    "container_number": (CONTAINER_OK, "Container no. MSKU3067322", 1, 0.94),
                    "hs_code": None,
                    "consignee_name": ("Nordwind Industrial Supply GmbH", "Consignee Nordwind Industrial Supply GmbH", 1, 0.98),
                    "invoice_value": None,
                },
            },
            "bill_of_lading": {
                "filename": "bol_clean.png",
                "html": lambda s: templates.bol(
                    doc_no="HLCUHAM260417781", date="2026-04-04", shipper=s["shipper"],
                    shipper_addr=s["shipper_addr"], consignee=s["consignee"],
                    consignee_addr=s["consignee_addr"], vessel="MV HANWEI EXPRESS",
                    voyage="0421W", port_loading="Ningbo, China",
                    port_discharge="Hamburg, Germany", container=CONTAINER_OK,
                    seal="CN8892143", packages="100 CARTONS",
                    description="CNC ALUMINIUM HOUSINGS AND FASTENER SETS SAID TO CONTAIN",
                    gross_weight="12,450.00 KGS", freight_terms="FREIGHT PREPAID",
                ),
                "extraction": lambda s: {
                    "quantity": ("100 CARTONS", "No. of containers or packages 100 CARTONS", 1, 0.95),
                    "gross_weight": ("12,450.00 KGS", "Gross weight 12,450.00 KGS", 1, 0.96),
                    "container_number": (CONTAINER_OK, "Container no. MSKU3067322", 1, 0.96),
                    "hs_code": None,
                    "consignee_name": ("Nordwind Industrial Supply GmbH", "Consignee Nordwind Industrial Supply GmbH", 1, 0.97),
                    "invoice_value": None,
                },
            },
        },
    },

    # ------------------------------------------------------------------
    # Planted errors. Proves the cross path and checksum arbitration.
    #   - gross weight: BOL differs by >2% (and is the minority of three)
    #   - container: the packing list copy fails its own ISO 6346 checksum
    #   - consignee: legal-suffix variation that SHOULD resolve as consistent
    # ------------------------------------------------------------------
    "shipment_conflict": {
        "invoice_no": "HW-2026-0913",
        "date": "2026-04-02",
        "consignee": CONSIGNEE,
        "consignee_addr": CONSIGNEE_ADDR,
        "shipper": SHIPPER,
        "shipper_addr": SHIPPER_ADDR,
        "quantity": "100 CARTONS",
        "gross_weight": "13,200.00 KGS",
        "container": CONTAINER_OK,
        "hs_code": "8517.62.00",
        "invoice_value": "USD 8,420.00",
        "docs": {
            "commercial_invoice": {
                "filename": "invoice_conflict.png",
                "html": lambda s: templates.invoice(
                    doc_no=s["invoice_no"], date=s["date"], shipper=s["shipper"],
                    shipper_addr=s["shipper_addr"],
                    consignee="Nordwind Industrial Supply",  # suffix variation, on purpose
                    consignee_addr=s["consignee_addr"], po_no="PO 4410-DE",
                    lines=[
                        ("CNC aluminium housing, type H-40", "600", "pieces", "4.85", "2,910.00"),
                        ("Stainless fastener set, M6", "600", "pieces", "9.18", "5,510.00"),
                    ],
                    qty_total="100 CARTONS", weight_total="12,450.00 KGS",
                    incoterm="CIF Hamburg (Incoterms 2020)",
                    currency_word="US DOLLARS EIGHT THOUSAND FOUR HUNDRED TWENTY",
                    subtotal="8,420.00", freight="0.00", total="USD 8,420.00",
                    hs_code="8517.62.00",
                ),
                "extraction": lambda s: {
                    "quantity": ("100 CARTONS", "Total quantity 100 CARTONS", 1, 0.97),
                    "gross_weight": ("12,450.00 KGS", "Total gross weight 12,450.00 KGS", 1, 0.96),
                    "container_number": None,
                    "hs_code": ("8517.62.00", "HS code 8517.62.00", 1, 0.95),
                    "consignee_name": ("Nordwind Industrial Supply", "Consignee Nordwind Industrial Supply", 1, 0.98),
                    "invoice_value": ("USD 8,420.00", "TOTAL AMOUNT USD 8,420.00", 1, 0.97),
                },
            },
            "packing_list": {
                "filename": "packing_list_conflict.png",
                "html": lambda s: templates.packing_list(
                    doc_no="PL-2026-0913", date=s["date"], consignee=s["consignee"],
                    consignee_addr=s["consignee_addr"], invoice_ref=s["invoice_no"],
                    lines=[
                        ("1-50", "CNC aluminium housings", "50 CTN", "6,225.00", "5,800.00", "18.40"),
                        ("51-100", "Stainless fastener sets", "50 CTN", "6,225.00", "5,800.00", "12.10"),
                    ],
                    total_packages="100 cartons", total_gross="12,450.00 KGS",
                    total_net="11,600.00 KGS", total_volume="30.50 CBM",
                    container=CONTAINER_BAD,  # one digit misread, on purpose
                ),
                "extraction": lambda s: {
                    "quantity": ("100 cartons", "TOTAL NO. OF PACKAGES 100 cartons", 1, 0.97),
                    "gross_weight": ("12,450.00 KGS", "TOTAL GROSS WT 12,450.00 KGS", 1, 0.97),
                    "container_number": (CONTAINER_BAD, "Container no. MSKU3067328", 1, 0.91),
                    "hs_code": None,
                    "consignee_name": ("Nordwind Industrial Supply GmbH", "Consignee Nordwind Industrial Supply GmbH", 1, 0.98),
                    "invoice_value": None,
                },
            },
            "bill_of_lading": {
                "filename": "bol_conflict.png",
                "html": lambda s: templates.bol(
                    doc_no="HLCUHAM260417782", date="2026-04-04", shipper=s["shipper"],
                    shipper_addr=s["shipper_addr"], consignee=s["consignee"],
                    consignee_addr=s["consignee_addr"], vessel="MV HANWEI EXPRESS",
                    voyage="0421W", port_loading="Ningbo, China",
                    port_discharge="Hamburg, Germany", container=CONTAINER_OK,
                    seal="CN8892143", packages="100 CARTONS",
                    description="CNC ALUMINIUM HOUSINGS AND FASTENER SETS SAID TO CONTAIN",
                    gross_weight="13,200.00 KGS",  # the planted conflict
                    freight_terms="FREIGHT PREPAID",
                ),
                "extraction": lambda s: {
                    "quantity": ("100 CARTONS", "No. of containers or packages 100 CARTONS", 1, 0.95),
                    "gross_weight": ("13,200.00 KGS", "Gross weight 13,200.00 KGS", 1, 0.95),
                    "container_number": (CONTAINER_OK, "Container no. MSKU3067322", 1, 0.96),
                    "hs_code": None,
                    # Live-verified 2026-09-23: the consignee cell renders in mixed
                    # case (td.pval has no text-transform), so this is what is
                    # actually printed. An earlier version of this fixture claimed
                    # all-caps, which the document does not show.
                    "consignee_name": ("Nordwind Industrial Supply GmbH", "Consignee Nordwind Industrial Supply GmbH", 1, 0.97),
                    "invoice_value": None,
                },
            },
        },
    },

    # ------------------------------------------------------------------
    # International formatting. Proves the normalizer is doing real work.
    #   - European decimal comma: "12.450,00 kg"
    #   - LBS vs KGS: 27,447.00 LBS is the SAME weight, not a conflict
    #   - pieces vs cartons: different package basis, not a conflict
    #   - two-page document, with a value found on page 2
    # ------------------------------------------------------------------
    "shipment_messy": {
        "invoice_no": "HW-2026-0914",
        "date": "2026-04-02",
        "consignee": CONSIGNEE,
        "consignee_addr": CONSIGNEE_ADDR,
        "shipper": SHIPPER,
        "shipper_addr": SHIPPER_ADDR,
        "quantity": "1,200 pieces",
        "gross_weight": "12.450,00 kg",
        "container": CONTAINER_OK,
        "hs_code": "8517.62.0090",
        "invoice_value": "USD 8,420.00",
        "docs": {
            "commercial_invoice": {
                "filename": "invoice_messy.png",
                "html": lambda s: templates.invoice(
                    doc_no=s["invoice_no"], date=s["date"], shipper=s["shipper"],
                    shipper_addr=s["shipper_addr"], consignee=s["consignee"],
                    consignee_addr=s["consignee_addr"], po_no="PO 4410-DE",
                    lines=[
                        ("CNC aluminium housing, type H-40", "600", "pieces", "4.85", "2,910.00"),
                        ("Stainless fastener set, M6", "600", "pieces", "9.18", "5,510.00"),
                    ],
                    qty_total="1,200 pieces",
                    weight_total="12.450,00 kg",  # European decimal comma
                    incoterm="FOB Ningbo (Incoterms 2020)",
                    currency_word="US DOLLARS EIGHT THOUSAND FOUR HUNDRED TWENTY",
                    subtotal="8,420.00", freight="0.00",
                    total="FOB NINGBO USD 8,420.00",
                    hs_code="8517.62.0090",  # US national tail, differs from EU filing
                ),
                "extraction": lambda s: {
                    "quantity": ("1,200 pieces", "Total quantity 1,200 pieces", 1, 0.96),
                    "gross_weight": ("12.450,00 kg", "Total gross weight 12.450,00 kg", 1, 0.93),
                    "container_number": None,
                    "hs_code": ("8517.62.0090", "HS code 8517.62.0090", 1, 0.94),
                    "consignee_name": ("Nordwind Industrial Supply GmbH", "Consignee Nordwind Industrial Supply GmbH", 1, 0.98),
                    "invoice_value": ("FOB NINGBO USD 8,420.00", "TOTAL AMOUNT FOB NINGBO USD 8,420.00", 1, 0.9),
                },
            },
            "packing_list": {
                "filename": "packing_list_messy.png",
                "html": lambda s: templates.packing_list(
                    doc_no="PL-2026-0914", date=s["date"], consignee=s["consignee"],
                    consignee_addr=s["consignee_addr"], invoice_ref=s["invoice_no"],
                    # 20 rows x 5 CTN = 100 cartons, deliberately long enough to
                    # push the TOTAL rows onto page 2, so page_index has to be
                    # right for the evidence to be verifiable.
                    lines=[
                        (f"{i}-{i + 9}", "Mixed machined parts", "5 CTN",
                         "622.50", "580.00", "1.53")
                        for i in range(1, 201, 10)
                    ],
                    total_packages="100 cartons", total_gross="12,450.00 KGS",
                    total_net="11,600.00 KGS", total_volume="30.50 CBM",
                    container=CONTAINER_OK,
                ),
                "extraction": lambda s: {
                    "quantity": ("100 cartons", "TOTAL NO. OF PACKAGES 100 cartons", 2, 0.95),
                    "gross_weight": ("12,450.00 KGS", "TOTAL GROSS WT 12,450.00 KGS", 2, 0.96),
                    "container_number": (CONTAINER_OK, "Container no. MSKU3067322", 1, 0.95),
                    "hs_code": None,
                    "consignee_name": ("Nordwind Industrial Supply GmbH", "Consignee Nordwind Industrial Supply GmbH", 1, 0.97),
                    "invoice_value": None,
                },
            },
            "bill_of_lading": {
                "filename": "bol_messy.png",
                "html": lambda s: templates.bol(
                    doc_no="HLCUHAM260417783", date="2026-04-04", shipper=s["shipper"],
                    shipper_addr=s["shipper_addr"], consignee=s["consignee"],
                    consignee_addr=s["consignee_addr"], vessel="MV HANWEI EXPRESS",
                    voyage="0421W", port_loading="Ningbo, China",
                    port_discharge="Hamburg, Germany", container=CONTAINER_OK,
                    seal="CN8892143", packages="100 CARTONS",
                    description="CNC ALUMINIUM HOUSINGS AND FASTENER SETS SAID TO CONTAIN",
                    gross_weight="27,447.00 LBS",  # same mass, imperial unit
                    freight_terms="FREIGHT PREPAID",
                ),
                "extraction": lambda s: {
                    "quantity": ("100 CARTONS", "No. of containers or packages 100 CARTONS", 1, 0.95),
                    "gross_weight": ("27,447.00 LBS", "Gross weight 27,447.00 LBS", 1, 0.94),
                    "container_number": (CONTAINER_OK, "Container no. MSKU3067322", 1, 0.96),
                    "hs_code": None,
                    "consignee_name": ("Nordwind Industrial Supply GmbH", "Consignee Nordwind Industrial Supply GmbH", 1, 0.97),
                    "invoice_value": None,
                },
            },
        },
    },
}
