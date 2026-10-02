"""Comparison-engine tests: thresholds, cardinality gate, checksum arbitration,
outlier attribution. Zero API calls.
"""

from __future__ import annotations


from app.compare import (
    PARTY_FUZZY_CONSISTENT,
    PARTY_SUSPICIOUS,
    WEIGHT_TOLERANCE_CONSISTENT_ABS_KG,
    WEIGHT_TOLERANCE_SUSPICIOUS_REL,
    FieldObservation,
    compare_field,
)
from app.fields import FIELD_KIND, FieldKey
from app.models import ExtractedField
from app.normalize import normalize_field


def obs(doc_id: str, label: str, doc_type: str, key: FieldKey, value: str | None,
        *, present: bool = True, confidence: float = 0.95) -> FieldObservation:
    extracted = ExtractedField(
        present=present and value is not None,
        value=value,
        evidence_text=f"{key}: {value}" if value else "",
        page_index=1,
        confidence=confidence,
    )
    return FieldObservation(
        document_id=doc_id,
        document_label=label,
        doc_type=doc_type,
        extracted=extracted,
        normalized=normalize_field(key, extracted),
    )


# ---------------------------------------------------------------------------
# Thresholds are frozen in exactly one place
# ---------------------------------------------------------------------------


def test_threshold_constants_are_what_the_spec_freeze():
    assert WEIGHT_TOLERANCE_SUSPICIOUS_REL == 0.02
    assert WEIGHT_TOLERANCE_CONSISTENT_ABS_KG == 1.0
    assert (PARTY_FUZZY_CONSISTENT, PARTY_SUSPICIOUS) == (90, 70)


# ---------------------------------------------------------------------------
# Weight tolerance bands
# ---------------------------------------------------------------------------


def test_weight_within_abs_tolerance_is_consistent():
    r = compare_field("gross_weight", [
        obs("a", "PL", "packing_list", "gross_weight", "12450 KGS"),
        obs("b", "BOL", "bill_of_lading", "gross_weight", "12450.5 KGS"),
    ])
    assert r.status == "consistent"


def test_weight_1_5_percent_is_suspicious_rounding():
    r = compare_field("gross_weight", [
        obs("a", "PL", "packing_list", "gross_weight", "10000 KGS"),
        obs("b", "BOL", "bill_of_lading", "gross_weight", "10150 KGS"),
    ])
    assert r.status == "suspicious"
    assert "%" in r.reason


def test_weight_over_2_percent_is_conflict():
    r = compare_field("gross_weight", [
        obs("a", "PL", "packing_list", "gross_weight", "12450 KGS"),
        obs("b", "BOL", "bill_of_lading", "gross_weight", "13200 KGS"),
    ])
    assert r.status == "conflict"
    assert "materiality" in r.reason


def test_weight_reason_interpolates_actual_numbers():
    r = compare_field("gross_weight", [
        obs("a", "PL", "packing_list", "gross_weight", "12450 KGS"),
        obs("b", "BOL", "bill_of_lading", "gross_weight", "13200 KGS"),
    ])
    assert "12,450.00" in r.reason and "13,200.00" in r.reason


# ---------------------------------------------------------------------------
# Quantity: the false-conflict trap
# ---------------------------------------------------------------------------


def test_different_package_basis_is_suspicious_not_conflict():
    """'1,200 pieces' vs '100 cartons' is normal on real documents."""
    r = compare_field("quantity", [
        obs("a", "INV", "commercial_invoice", "quantity", "1,200 pieces"),
        obs("b", "PL", "packing_list", "quantity", "100 cartons"),
    ])
    assert r.status == "suspicious"
    assert "package basis" in r.reason


def test_two_different_package_types_is_conflict_not_basis_difference():
    """Cartons vs pallets are both PACKAGING, so a count gap is real."""
    r = compare_field("quantity", [
        obs("a", "PL", "packing_list", "quantity", "100 cartons"),
        obs("b", "BOL", "bill_of_lading", "quantity", "20 pallets"),
    ])
    assert r.status == "conflict"


def test_same_count_generic_package_word_is_consistent():
    """A carton is a package: same count, same basis, cosmetic word difference."""
    r = compare_field("quantity", [
        obs("a", "PL", "packing_list", "quantity", "100 cartons"),
        obs("b", "BOL", "bill_of_lading", "quantity", "100 packages"),
    ])
    assert r.status == "consistent"


def test_same_count_but_one_doc_states_no_unit_is_suspicious():
    r = compare_field("quantity", [
        obs("a", "PL", "packing_list", "quantity", "100 cartons"),
        obs("b", "BOL", "bill_of_lading", "quantity", "100"),
    ])
    assert r.status == "suspicious"


def test_same_package_basis_different_count_is_conflict():
    r = compare_field("quantity", [
        obs("a", "PL", "packing_list", "quantity", "100 cartons"),
        obs("b", "BOL", "bill_of_lading", "quantity", "120 cartons"),
    ])
    assert r.status == "conflict"


def test_same_count_same_unit_is_consistent():
    r = compare_field("quantity", [
        obs("a", "PL", "packing_list", "quantity", "100 CARTONS"),
        obs("b", "BOL", "bill_of_lading", "quantity", "100 cartons"),
    ])
    assert r.status == "consistent"


# ---------------------------------------------------------------------------
# Container number: ISO 6346 arbitration (the flagship case)
# ---------------------------------------------------------------------------


def test_checksum_arbitrates_single_character_disagreement():
    r = compare_field("container_number", [
        obs("a", "BOL", "bill_of_lading", "container_number", "MSKU3067322"),
        obs("b", "PL", "packing_list", "container_number", "MSKU3067328"),
    ])
    assert r.status == "conflict"
    assert "ARBITRATES" in r.reason
    assert "MSKU3067328" in r.reason
    assert "more likely correct" in r.reason
    bad = next(v for v in r.values if v.canonical == "MSKU3067328")
    assert "check digit mismatch" in " ".join(bad.warnings)


def test_matching_valid_container_is_consistent():
    r = compare_field("container_number", [
        obs("a", "BOL", "bill_of_lading", "container_number", "CSQU3054383"),
        obs("b", "PL", "packing_list", "container_number", "CSQU 305438 3"),
    ])
    assert r.status == "consistent"
    assert "ISO 6346" in r.reason


def test_both_invalid_reports_cannot_arbitrate():
    r = compare_field("container_number", [
        obs("a", "BOL", "bill_of_lading", "container_number", "MSKU3067324"),
        obs("b", "PL", "packing_list", "container_number", "MSKU3067325"),
    ])
    assert r.status == "conflict"
    assert "cannot say which" in r.reason


# ---------------------------------------------------------------------------
# HS code: 6-digit harmonized comparison
# ---------------------------------------------------------------------------


def test_hs_code_different_national_tail_is_consistent():
    r = compare_field("hs_code", [
        obs("a", "INV", "commercial_invoice", "hs_code", "8517.62.00"),
        obs("b", "INV2", "commercial_invoice", "hs_code", "8517.62.0090"),
    ])
    assert r.status == "consistent"


def test_hs_code_different_subheading_is_conflict():
    r = compare_field("hs_code", [
        obs("a", "INV", "commercial_invoice", "hs_code", "8517.62.00"),
        obs("b", "INV2", "commercial_invoice", "hs_code", "8517.13.00"),
    ])
    assert r.status == "conflict"


# ---------------------------------------------------------------------------
# Consignee fuzzy thresholds
# ---------------------------------------------------------------------------


def test_consignee_legal_suffix_variation_is_consistent():
    r = compare_field("consignee_name", [
        obs("a", "INV", "commercial_invoice", "consignee_name", "Nippon Electronics Co., Ltd."),
        obs("b", "BOL", "bill_of_lading", "consignee_name", "NIPPON ELECTRONICS COMPANY LTD"),
        obs("c", "PL", "packing_list", "consignee_name", "Nippon Electronics"),
    ])
    assert r.status == "consistent"


def test_consignee_word_reorder_is_consistent():
    r = compare_field("consignee_name", [
        obs("a", "INV", "commercial_invoice", "consignee_name", "Electronics Nippon"),
        obs("b", "BOL", "bill_of_lading", "consignee_name", "Nippon Electronics"),
    ])
    assert r.status == "consistent"


def test_consignee_different_company_is_conflict():
    r = compare_field("consignee_name", [
        obs("a", "INV", "commercial_invoice", "consignee_name", "Nippon Electronics Ltd"),
        obs("b", "BOL", "bill_of_lading", "consignee_name", "Shanghai Steel Works Corp"),
    ])
    assert r.status == "conflict"


# ---------------------------------------------------------------------------
# Money: never FX-convert
# ---------------------------------------------------------------------------


def test_different_currencies_is_hard_conflict():
    r = compare_field("invoice_value", [
        obs("a", "INV", "commercial_invoice", "invoice_value", "USD 8,420.00"),
        obs("b", "INV2", "commercial_invoice", "invoice_value", "EUR 8,420.00"),
    ])
    assert r.status == "conflict"
    assert "cannot compare across currencies" in r.reason


def test_same_amount_same_currency_consistent():
    r = compare_field("invoice_value", [
        obs("a", "INV", "commercial_invoice", "invoice_value", "USD 8,420.00"),
        obs("b", "TAX", "other", "invoice_value", "US$ 8420"),
    ])
    assert r.status == "consistent"


# ---------------------------------------------------------------------------
# Cardinality gate
# ---------------------------------------------------------------------------


def test_single_source_never_conflicts():
    r = compare_field("invoice_value", [
        obs("a", "INV", "commercial_invoice", "invoice_value", "USD 8,420.00"),
        obs("b", "BOL", "bill_of_lading", "invoice_value", None, present=False),
    ])
    assert r.status == "single_source"
    assert "nothing to reconcile" in r.reason


def test_absent_field_expected_for_doc_type_is_quiet():
    """No HS code on a BOL is normal and must not read as a problem."""
    r = compare_field("hs_code", [
        obs("a", "BOL", "bill_of_lading", "hs_code", None, present=False),
        obs("b", "PL", "packing_list", "hs_code", None, present=False),
    ])
    assert r.status == "absent"
    assert "expected, not a problem" in r.reason


def test_absent_field_where_expected_warns():
    r = compare_field("invoice_value", [
        obs("a", "INV", "commercial_invoice", "invoice_value", None, present=False),
        obs("b", "INV2", "commercial_invoice", "invoice_value", None, present=False),
    ])
    assert r.status == "absent"
    assert "Worth checking" in r.reason


# ---------------------------------------------------------------------------
# Outlier attribution with 3+ documents
# ---------------------------------------------------------------------------


def test_minority_value_is_marked_outlier_with_three_documents():
    r = compare_field("gross_weight", [
        obs("a", "PL", "packing_list", "gross_weight", "12450 KGS"),
        obs("b", "BOL", "bill_of_lading", "gross_weight", "12450 KGS"),
        obs("c", "AWB", "other", "gross_weight", "13200 KGS"),
    ])
    assert r.status == "conflict"
    assert r.majority_compare_key == "weight:12450.000"
    outliers = [v.document_label for v in r.values if v.is_outlier]
    assert outliers == ["AWB"]


def test_no_outliers_marked_with_only_two_documents():
    r = compare_field("gross_weight", [
        obs("a", "PL", "packing_list", "gross_weight", "12450 KGS"),
        obs("b", "BOL", "bill_of_lading", "gross_weight", "13200 KGS"),
    ])
    assert not any(v.is_outlier for v in r.values)


# ---------------------------------------------------------------------------
# Confidence escalation
# ---------------------------------------------------------------------------


def test_low_confidence_upgrades_consistent_to_suspicious():
    r = compare_field("gross_weight", [
        obs("a", "PL", "packing_list", "gross_weight", "12450 KGS", confidence=0.9),
        obs("b", "BOL", "bill_of_lading", "gross_weight", "12450 KGS", confidence=0.3),
    ])
    assert r.status == "suspicious"
    assert "confidence" in r.reason


# ---------------------------------------------------------------------------
# compare_key / canonical separation
# ---------------------------------------------------------------------------


def test_comparator_uses_compare_key_not_display_string():
    """The classic bug: display says '12,450.000 kg' but comparison must not
    be a string match on that display form."""
    r = compare_field("gross_weight", [
        obs("a", "PL", "packing_list", "gross_weight", "12,450.00 KGS"),
        obs("b", "BOL", "bill_of_lading", "gross_weight", "12.450,00 kg"),
    ])
    assert r.values[0].canonical == "12450.000 kg" == r.values[1].canonical
    assert r.values[0].raw_value != r.values[1].raw_value
    assert r.values[0].compare_key == r.values[1].compare_key == "weight:12450.000"
    assert r.status == "consistent"


def test_every_field_kind_has_a_comparator():
    from app.compare import COMPARATORS

    assert set(COMPARATORS) == set(FIELD_KIND.values())
