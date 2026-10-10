"""Does the quoted line exist on the page, and does the value?

The interesting failure this guards against is not the obvious one. A fabricated
value is easy to catch and easy to talk about. The failure that actually happened
in this project's own captures is a model reading a real string off a real page
and filing it under the wrong field -- a packing list's invoice reference
reported as the PO number, a port of loading ("Ningbo, China") reported as the
country of origin. Both gates pass on those, because the text really is there.

So this file asserts two things in opposite directions: that a string which is
not on the page gets pulled out of the comparison, and that the check never
claims to catch what it cannot see.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models import ExtractedField, ExtractionResponse
from app.textlayer import PageText
from app.verify import normalize_for_match, squash, verify_document, verify_field

REPO = Path(__file__).resolve().parents[1]

PAGE = PageText(index=1, source="fixture_sidecar", lines=(
    "COMMERCIAL INVOICE",
    "Invoice number INV-2026-0417",
    "Consignee Nordwind Industrial Supply GmbH",
    "Purchase order PO 4410-DE",
    "Total gross weight 12,450.00 KGS",
    "HS code 8517.62.00",
    "TOTAL AMOUNT USD 8,420.00",
))


def field(value: str, quote: str, *, page: int = 1, present: bool = True) -> ExtractedField:
    return ExtractedField(present=present, value=value if present else None,
                          evidence_text=quote, page_index=page, confidence=0.95)


# ---------------------------------------------------------------------------
# what counts as real
# ---------------------------------------------------------------------------


def test_a_quote_copied_from_one_line_verifies():
    v = verify_field(field("PO 4410-DE", "Purchase order PO 4410-DE"), [PAGE])
    assert v.state == "verified"
    assert v.tier.startswith("line")


def test_case_and_spacing_differences_are_not_invention():
    v = verify_field(field("PO 4410-DE", "purchase   ORDER po 4410-de"), [PAGE])
    assert v.state == "verified"


def test_a_quote_spanning_two_cells_of_one_row_verifies():
    """The text layer splits a table row's label cell from its value cell.

    Three of our own bill-of-lading quotes are of this shape, and an earlier
    version of this check called them invented.
    """
    page = PageText(index=1, source="fixture_sidecar",
                    lines=("Number and kind of packages", "100 CARTONS"))
    v = verify_field(field("100 CARTONS", "No. of containers or packages 100 CARTONS"),
                     [page])
    assert v.state == "verified"
    assert v.tier.split(":")[0] in {"row", "spread", "fuzzy"}


def test_a_quote_joining_a_column_header_to_a_cell_verifies():
    """Exactly what NVIDIA produced for a certificate of origin's HS code:
    '3. HS CODE 8517.62', where the page prints the header and the cell on
    separate lines. Rejecting it would discard a correct extraction."""
    page = PageText(index=1, source="fixture_sidecar", lines=(
        "3. HS code 4. Origin criterion", "8517.62.00 P"))
    v = verify_field(field("8517.62.00", "3. HS CODE 8517.62.00"), [page])
    assert v.state == "verified"
    assert v.tier.startswith("spread")


def test_a_value_reformatted_across_number_conventions_still_verifies():
    """The model was told to copy verbatim and did not. That is a formatting
    miss, not a fabrication, and the value gate compares digits for that reason."""
    v = verify_field(field("12.450,00 kg", "Total gross weight 12.450,00 kg"), [PAGE])
    assert v.state == "verified"


def test_a_quote_from_another_page_verifies_and_says_which():
    second = PageText(index=2, source="fixture_sidecar", lines=("Net weight 11,600.00 KGS",))
    v = verify_field(field("11,600.00 KGS", "Net weight 11,600.00 KGS", page=1),
                     [PAGE, second])
    assert v.state == "verified"
    assert v.matched_page == 2
    assert "page 2" in v.detail


# ---------------------------------------------------------------------------
# what does not
# ---------------------------------------------------------------------------


def test_a_value_nowhere_on_the_page_is_unverified():
    v = verify_field(field("PO 9999-ZZ", "Purchase order PO 9999-ZZ"), [PAGE])
    assert v.state == "unverified"
    assert v.tier == "quote_absent"


def test_a_real_quote_wrapping_an_invented_value_is_unverified():
    """The two-gate case: quote the invoice-number line, report a number that is
    not on the page. A quote-only check would pass this."""
    v = verify_field(field("USD 99,999.00", "TOTAL AMOUNT USD 8,420.00"), [PAGE])
    assert v.state == "unverified"
    assert v.tier == "value_absent"


def test_no_text_source_is_not_the_same_as_a_failed_check():
    """A phone photo has no text layer. Calling its fields unverified would drop
    every field of every scanned shipment and report a clean empty table."""
    v = verify_field(field("PO 4410-DE", "Purchase order PO 4410-DE"), [])
    assert v.state == "not_checkable"
    assert v.excluded is False


def test_absent_fields_are_not_checked():
    v = verify_field(field("", "", present=False), [PAGE])
    assert v.state == "absent"
    assert v.excluded is False


def test_only_unverified_excludes():
    """The one property the rest of the pipeline depends on.

    Exclusion is what makes a bad quote harmless, so exactly one of the four
    states may trigger it -- and `not_checkable` most emphatically may not.
    """
    good = verify_field(field("PO 4410-DE", "Purchase order PO 4410-DE"), [PAGE])
    bad = verify_field(field("USD 99,999.00", "TOTAL AMOUNT USD 8,420.00"), [PAGE])
    unknown = verify_field(field("PO 4410-DE", "Purchase order PO 4410-DE"), [])
    missing = verify_field(field("", "", present=False), [PAGE])

    assert (good.state, good.excluded) == ("verified", False)
    assert (bad.state, bad.excluded) == ("unverified", True)
    assert (unknown.state, unknown.excluded) == ("not_checkable", False)
    assert (missing.state, missing.excluded) == ("absent", False)


def test_a_relocated_but_real_value_is_NOT_caught():
    """The documented ceiling of this check, pinned so it cannot be forgotten.

    The packing list prints its invoice reference 'HW-2026-0913' and the model
    reported that string as the PO number. Every word of the quote is on the
    page and so is the value, because the model did not hallucinate a string --
    it answered the wrong question with a real one. All six fields our Nebius
    captures 'invented' are this shape. Page text cannot see a misattribution,
    and no threshold change makes it able to.
    """
    page = PageText(index=1, source="fixture_sidecar",
                    lines=("Invoice reference HW-2026-0913", "100 CARTONS"))
    v = verify_field(field("HW-2026-0913", "Invoice reference HW-2026-0913"), [page])
    assert v.state == "verified", "the check must not claim powers it does not have"


# ---------------------------------------------------------------------------
# whole documents
# ---------------------------------------------------------------------------


def _response(**fields: ExtractedField) -> ExtractionResponse:
    from app.fields import FIELD_ORDER

    blank = ExtractedField(present=False, value=None, evidence_text="",
                           page_index=1, confidence=0.0)
    return ExtractionResponse(
        doc_type="commercial_invoice", doc_type_reason="t",
        fields={k: fields.get(k, blank) for k in FIELD_ORDER},
    )


def test_verify_document_covers_every_field_and_names_the_bad_one():
    resp = _response(
        po_number=field("PO 4410-DE", "Purchase order PO 4410-DE"),
        invoice_value=field("USD 99,999.00", "TOTAL AMOUNT USD 8,420.00"),
    )
    out = verify_document(resp, [PAGE])
    assert set(out) == set(resp.fields)
    assert out["po_number"].state == "verified"
    assert out["invoice_value"].state == "unverified"
    assert out["quantity"].state == "absent"


def test_the_threshold_is_respected():
    near = field("Nordwind Industrial Supply Gmb", "Consignee Nordwind Industrial Supply GmbX")
    assert verify_field(near, [PAGE], threshold=90).state == "verified"
    assert verify_field(near, [PAGE], threshold=100).state == "unverified"


# ---------------------------------------------------------------------------
# against the real fixture set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stem", [
    "invoice_clean", "invoice_conflict", "invoice_messy",
    "packing_list_clean", "packing_list_conflict", "packing_list_messy",
    "bol_clean", "bol_conflict", "bol_messy",
    "coo_clean", "coo_conflict", "coo_messy",
])
def test_every_live_capture_quote_is_actually_on_its_page(stem):
    """Not a synthetic check: every genuine model response we hold, verified
    against the text of the document it was read from.

    This is the acceptance criterion for the feature, run over real output rather
    than a hand-made case. If a future capture fabricates a string, this fails.
    """
    from app.models import ExtractionResponse as ER
    from app.textlayer import load_sidecar

    found = False
    for provider in ("nvidia", "nebius"):
        path = REPO / "fixtures" / "live_responses" / provider / f"{stem}.json"
        if not path.is_file():
            continue
        found = True
        resp = ER.model_validate(json.loads(path.read_text())["response"])
        pages = load_sidecar(REPO / "fixtures" / "text" / f"{stem}.json")
        assert pages, f"no text sidecar for {stem}; run scripts/build_fixtures.py"
        bad = {k: v for k, v in verify_document(resp, pages).items()
               if v.state == "unverified"}
        assert not bad, (
            f"{provider}/{stem} quotes lines that are not printed: "
            + "; ".join(f"{k}={v.tier}" for k, v in sorted(bad.items()))
        )
    assert found, f"no live capture for {stem} on either provider"


def test_squash_treats_every_grouping_convention_as_the_same_number():
    assert squash("12,450.00") == squash("12.450,00") == squash("12 450,00") == squash("12'450.00")
    assert squash("USD 8,420.00") in squash("TOTAL AMOUNT USD 8,420.00")


def test_normalize_for_match_keeps_punctuation_that_carries_meaning():
    assert normalize_for_match("P.O. Box 9") == "p.o. box 9"
    assert normalize_for_match("Écran LED") == "ecran led"
