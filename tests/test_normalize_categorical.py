"""Tests for the categorical normalizers: Incoterms, references, origin.

Zero API calls. These three are the newest deterministic code in the project and
the least forgiving: a country quietly mapped to the wrong ISO code is a wrong
customs declaration, not a display glitch. So every case below is either "the
same fact written two ways must compare equal" or "a fact we cannot recognise
must say so instead of guessing".
"""

from __future__ import annotations

import pytest

from app.compare import compare_field
from app.models import ExtractedField
from app.normalize import normalize_field
from app.normalize.categorical import (
    normalize_country,
    normalize_incoterm,
    normalize_reference,
)

# ---------------------------------------------------------------------------
# Incoterms: the code is the term, the place is part of the promise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,key",
    [
        ("CIF Hamburg", "incoterm:CIF:HAMBURG"),
        ("CIF HAMBURG", "incoterm:CIF:HAMBURG"),
        ("cif hamburg", "incoterm:CIF:HAMBURG"),
        ("CIF Hamburg (Incoterms 2020)", "incoterm:CIF:HAMBURG"),
        ("CIF, Hamburg.", "incoterm:CIF:HAMBURG"),
        ("FCA Shanghai", "incoterm:FCA:SHANGHAI"),
        ("EXW Ningbo", "incoterm:EXW:NINGBO"),
        ("EXW  Ningbo ", "incoterm:EXW:NINGBO"),
        ("DDP Los Angeles", "incoterm:DDP:LOS ANGELES"),
        ("DAP St. Louis", "incoterm:DAP:ST. LOUIS"),
        ("FOB Ningbo (Incoterms 2020)", "incoterm:FOB:NINGBO"),
    ],
)
def test_incoterm_code_and_place_become_one_key(raw, key):
    assert normalize_incoterm(raw).compare_key == key


@pytest.mark.parametrize(
    "left,right",
    [
        ("CIF Hamburg", "CFR Hamburg"),
        ("CIF Hamburg", "CIF Rotterdam"),
        ("FCA Shanghai", "FAS Shanghai"),
        ("EXW Ningbo", "FOB Ningbo"),
        ("CIP Warsaw", "CPT Warsaw"),
    ],
)
def test_a_different_term_or_a_different_place_is_a_different_key(left, right):
    """Both halves matter: CIF and CFR split cost responsibility, and a different
    named place is a different promise even with the same code."""
    assert normalize_incoterm(left).compare_key != normalize_incoterm(right).compare_key


def test_incoterm_without_a_place_is_flagged_not_invented():
    res = normalize_incoterm("FOB")
    assert res.compare_key == "incoterm:FOB:"
    assert any("named place" in w for w in res.warnings)


def test_unrecognised_terms_text_is_kept_verbatim_and_warns():
    """Incoterms revisions and trade jargon we do not model must stay visible as
    what they are: unverified text, not a code we decided for them."""
    res = normalize_incoterm("C&F Hamburg")
    assert res.compare_key is not None
    assert res.compare_key.startswith("incoterm?:")
    assert any("no recognised Incoterms code" in w for w in res.warnings)


def test_retired_incoterm_codes_still_carry_their_place():
    """DAF/DES/DEQ were valid before 2010. The code-specific version of the place
    pattern silently dropped it from 'DAF someplace', so this is pinned."""
    res = normalize_incoterm("DAF Zonguldak")
    assert res.compare_key == "incoterm:DAF:ZONGULDAK"
    assert any("1990/2000 revision" in w for w in res.warnings)


# ---------------------------------------------------------------------------
# References: punctuation is noise, digits and letters are identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "PO 4410-DE",
        "po 4410-de",
        "4410DE",
        "P.O. 4410-DE",
        "P.O. 4410 DE",
        "Purchase Order No. 4410-DE",
        "PO No. (4410-DE)",
        "Ref: 4410-DE",
    ],
)
def test_the_same_order_written_any_of_these_ways_compares_equal(raw):
    assert normalize_reference(raw).compare_key == "ref:4410DE"


def test_one_letter_apart_is_a_different_order():
    """Deliberately not fuzzy: 90% similar here means two different orders, and a
    similarity band would report them as a near match."""
    a = normalize_reference("4471-B")
    b = normalize_reference("4471-C")
    assert a.compare_key == "ref:4471B"
    assert b.compare_key == "ref:4471C"
    assert a.compare_key != b.compare_key


def test_an_invoice_number_is_not_silently_a_purchase_order():
    """The prompt warns the model about this; the normalizer must not undo the
    warning by stripping the one token that distinguishes them."""
    assert normalize_reference("INV 4410").compare_key != \
        normalize_reference("PO 4410").compare_key


def test_a_thin_reference_is_warned_about():
    res = normalize_reference("PO 12")
    assert res.compare_key == "ref:12"
    assert any("fewer than 3 digits" in w for w in res.warnings)


def test_a_reference_with_nothing_left_returns_no_key():
    """Every token was a label, so there is no identity to compare. Producing
    'ref:' would let two unrelated empty reads agree."""
    res = normalize_reference("PO NO")
    assert res.compare_key is None
    assert res.warnings


# ---------------------------------------------------------------------------
# Origin: recognition is allowed, guessing is not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,code",
    [
        ("Germany", "DE"),
        ("GERMANY", "DE"),
        ("DE", "DE"),
        ("Made in Germany", "DE"),
        ("Country of origin: Germany", "DE"),
        ("China", "CN"),
        ("CHINA", "CN"),
        ("Made in P.R.C.", "CN"),
        ("P.R. China", "CN"),
        ("CN", "CN"),
        ("Netherlands", "NL"),
        ("Holland", "NL"),
        ("Great Britain", "GB"),
        ("England", "GB"),
        ("South Korea", "KR"),
        ("Korea", "KR"),
        ("Vietnam", "VN"),
        # The long-form naming and the short name carry the same ISO 3166-1 code,
        # so they must reconcile instead of showing up as two origins.
        ("Taiwan", "TW"),
        ("Made in Taiwan, Province of China", "TW"),
    ],
)
def test_origin_spellings_land_on_one_iso_code(raw, code):
    res = normalize_country(raw)
    assert res.compare_key == f"country:{code}"
    assert res.canonical == code


@pytest.mark.parametrize("raw", ["Ukraine", "Burkina Faso"])
def test_an_unrecognised_country_is_compared_verbatim_and_says_so(raw):
    """Nothing is inferred about origin. An unmatched name keeps its own text as
    the key, so two documents that both say it still agree, and one that does
    not is a visible difference rather than a silent mapping."""
    res = normalize_country(raw)
    assert res.compare_key is not None
    assert res.compare_key.startswith("country?:")
    assert any("not recognised" in w for w in res.warnings)
    assert res.ok


def test_destination_words_do_not_become_origin():
    """'Hamburg, Germany' is the discharge port on some documents. If it ever
    reaches this function it must stay recognisable as what was printed."""
    res = normalize_country("Hamburg, Germany")
    assert res.compare_key == "country:DE"
    assert any("matched" in w for w in res.warnings), "the match is recorded, not hidden"


# ---------------------------------------------------------------------------
# Wiring: the field set dispatches to these, and the comparators use them
# ---------------------------------------------------------------------------


def _field(value: str) -> ExtractedField:
    return ExtractedField(present=True, value=value, evidence_text=value,
                          page_index=1, confidence=0.95)


@pytest.mark.parametrize(
    "key,raw,expected_prefix",
    [
        ("incoterms", "CIF Hamburg (Incoterms 2020)", "incoterm:CIF:"),
        ("po_number", "P.O. 4410-DE", "ref:4410DE"),
        ("country_of_origin", "Made in P.R.C.", "country:CN"),
        ("net_weight", "11,600.00 KGS", "weight:11600.000"),
    ],
)
def test_normalize_field_routes_each_new_key_to_its_comparator_kind(
    key, raw, expected_prefix
):
    """FIELD_KIND -> normalize_field is the only wiring between the contract and
    these functions; a missing branch would fall through to 'unknown field kind'
    at run time rather than at import."""
    norm = normalize_field(key, _field(raw))
    assert norm.compare_key.startswith(expected_prefix), norm


def _obs(doc: str, key: str, value: str):
    from app.compare import FieldObservation

    extracted = _field(value)
    return FieldObservation(
        document_id=doc, document_label=doc, doc_type="commercial_invoice",
        extracted=extracted, normalized=normalize_field(key, extracted),
    )


def test_categorical_fields_conflict_only_on_a_real_difference():
    consistent = compare_field(
        "country_of_origin",
        [_obs("invoice", "country_of_origin", "Made in P.R.C."),
         _obs("packing_list", "country_of_origin", "CHINA")],
    )
    assert consistent.status == "consistent"

    conflict = compare_field(
        "country_of_origin",
        [_obs("invoice", "country_of_origin", "China"),
         _obs("packing_list", "country_of_origin", "Germany")],
    )
    assert conflict.status == "conflict"


def test_a_reference_never_lands_in_the_rounding_band():
    """Weights and money get tolerance because measurements round. Identifiers do
    not: two POs that are one character apart are two orders, and calling that
    'suspicious' would hide it in a different column."""
    result = compare_field(
        "po_number",
        [_obs("invoice", "po_number", "PO 4410-DE"),
         _obs("packing_list", "po_number", "PO 4410-DF")],
    )
    assert result.status == "conflict"


def test_incoterms_disagreement_is_a_conflict_not_a_near_match():
    result = compare_field(
        "incoterms",
        [_obs("invoice", "incoterms", "CIF Hamburg (Incoterms 2020)"),
         _obs("other", "incoterms", "FOB Hamburg")],
    )
    assert result.status == "conflict"
