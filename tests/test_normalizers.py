"""Table-driven tests for the normalizers. Zero API calls.

Written before the implementations, per the build order. The number-format
table is the highest-value test in the project because trade documents are
genuinely international and a silent misparse here corrupts everything
downstream.
"""

from __future__ import annotations

import pytest

from app.normalize.container import normalize_container as norm_container
from app.normalize.hs_code import normalize_hs_code as norm_hs
from app.normalize.money import normalize_money as norm_money
from app.normalize.number import parse_number as norm_number
from app.normalize.party import normalize_party as norm_party
from app.normalize.quantity import normalize_quantity as norm_quantity
from app.normalize.weight import normalize_weight as norm_weight

# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_value,expect_ambiguous",
    [
        # both separators present -> rightmost is decimal
        ("12,450.00", 12450.00, False),
        ("12.450,00", 12450.00, False),
        ("1,234,567.89", 1234567.89, False),
        ("1.234.567,89", 1234567.89, False),
        # plain
        ("12450", 12450.0, False),
        ("12450.00", 12450.0, False),
        ("0.5", 0.5, False),
        # comma only, 3 trailing digits and short -> AMBIGUOUS, default thousands
        ("1,200", 1200.0, True),
        ("12,450", 12450.0, True),
        # comma only, non-3 trailing -> decimal
        ("12,45", 12.45, False),
        ("1,5", 1.5, False),
        # comma only, many digits -> not ambiguous, thousands
        ("1,234,567", 1234567.0, False),
        # space / apostrophe grouping
        ("12 450.00", 12450.0, False),
        ("12'450.00", 12450.0, False),
        ("12 450,00", 12450.0, False),
        ("1'234'567", 1234567.0, False),
    ],
)
def test_number_parsing(raw, expected_value, expect_ambiguous):
    res = norm_number(raw)
    assert res.value == pytest.approx(expected_value), f"{raw} -> {res.value}"
    assert res.ambiguous == expect_ambiguous, f"{raw} ambiguity flag wrong"


def test_number_rejects_garbage():
    for bad in ("", "abc", "N/A", "--", "1.2.3"):
        res = norm_number(bad)
        assert res.value is None, f"{bad!r} should not parse"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("l2,450", 12450.0),      # l -> 1
        ("O500", 500.0),          # O -> 0
        ("S,200", 5200.0),        # S -> 5
        ("8O0", 800.0),           # O -> 0
        ("Z450", 2450.0),         # Z -> 2
    ],
)
def test_ocr_repair_only_when_needed(raw, expected):
    res = norm_number(raw)
    assert res.value == pytest.approx(expected)
    assert "ocr_repair" in res.rules_applied


def test_ocr_repair_not_applied_to_clean_number():
    """A token that already parses must never be touched. 'B8' etc."""
    res = norm_number("12450")
    assert "ocr_repair" not in res.rules_applied
    res2 = norm_number("10")
    assert res2.value == 10.0 and "ocr_repair" not in res2.rules_applied


def test_ocr_repair_requires_unique_solution():
    """Exactly one substitution must yield a valid parse, else no repair."""
    res = norm_number("OO")  # 00 or ... ambiguous/invalid
    assert res.value is None


# ---------------------------------------------------------------------------
# Weight
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_kg",
    [
        ("12,450.00 KGS", 12450.0),
        ("12450 kg", 12450.0),
        ("12.450,00 KGS", 12450.0),
        ("12450 KGS", 12450.0),
        ("500 g", 0.5),
        ("12.5 MT", 12500.0),
        ("12450 LBS", 12450 * 0.45359237),
        ("1,200lb", 1200 * 0.45359237),
        ("3.5 TONNE", 3500.0),
        ("2 METRIC TON", 2000.0),
        ("12450 KILOGRAMS", 12450.0),
    ],
)
def test_weight_to_kg(raw, expected_kg):
    res = norm_weight(raw)
    assert res.value_kg == pytest.approx(expected_kg, rel=1e-9), f"{raw} -> {res.value_kg}"


def test_weight_bare_ton_is_ambiguous():
    res = norm_weight("12 TON")
    assert res.value_kg == pytest.approx(12000.0)
    assert res.warnings, "bare 'ton' must warn about metric vs short ton"
    assert any("ton" in w.lower() for w in res.warnings)


def test_weight_bare_t_warns():
    res = norm_weight("12T")
    assert res.value_kg == pytest.approx(12000.0)
    assert res.warnings


def test_weight_missing_unit_is_not_ok():
    res = norm_weight("12450")
    assert res.value_kg is None
    assert not res.ok


def test_weight_canonical_format():
    res = norm_weight("12450 KGS")
    assert res.canonical == "12450.000 kg"
    assert res.compare_key == "weight:12450.000"


# ---------------------------------------------------------------------------
# Quantity: count AND package kind, kept separate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,count,unit",
    [
        ("1,200 pieces", 1200, "piece"),
        ("100 CARTONS", 100, "carton"),
        ("100 cartons", 100, "carton"),
        ("25 pallets", 25, "pallet"),
        ("1.200 ST", 1200, "set"),
        ("500 PCS", 500, "piece"),
        ("12 BOXES", 12, "box"),
        ("40 CASES", 40, "case"),
        ("7", 7, None),
        ("12,450.00 KGS", None, None),  # weight, not a count
    ],
)
def test_quantity_count_and_unit(raw, count, unit):
    res = norm_quantity(raw)
    assert res.count == count, f"{raw} -> count={res.count}"
    assert res.unit == unit, f"{raw} -> unit={res.unit}"


def test_quantity_compare_key_includes_unit():
    a = norm_quantity("1,200 pieces")
    b = norm_quantity("100 cartons")
    assert a.compare_key != b.compare_key
    assert a.compare_key == "count:1200:piece"
    assert b.compare_key == "count:100:carton"


# ---------------------------------------------------------------------------
# Container number: ISO 6346 in full
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "CSQU3054383",       # ISO 6346 worked example from the standard
        "MSKU3067322",
        "MSCU1234566",
        "MSKU 306732 2",     # spaces tolerated
        "msku3067322",       # lowercase tolerated
    ],
)
def test_container_valid(raw):
    res = norm_container(raw)
    assert res.check_digit_valid is True, f"{raw} should be valid: {res.detail}"
    assert res.ok


@pytest.mark.parametrize(
    "raw",
    [
        "MSKU3067323",  # real-world-looking but check digit is 2, not 3
        "MSKU3067324",
        "ABCU5247889",
        "MSKU306732",    # too short
        "MSKU30673223",  # too long
        "1SKU3067322",   # owner must be letters
        "MSKU306732A",   # last must be digit
        "HELLO WORLD",   # garbage
        "",
    ],
)
def test_container_invalid(raw):
    res = norm_container(raw)
    assert res.check_digit_valid is False


def test_container_single_char_corruption_detected():
    """The demo case: one character differs and exactly one side fails checksum."""
    good = norm_container("MSKU3067322")
    bad = norm_container("MSKU3067328")  # check digit corrupted
    assert good.check_digit_valid is True
    assert bad.check_digit_valid is False
    assert bad.expected_check_digit == 2
    assert bad.actual_check_digit == 8


def test_container_canonical_strips_separators():
    res = norm_container("msku 306732 2")
    assert res.canonical == "MSKU3067322"
    assert res.compare_key == "container:MSKU3067322"


# ---------------------------------------------------------------------------
# HS code: compare on first 6 digits only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,digits6",
    [
        ("8517.62.00", "851762"),
        ("8517620000", "851762"),
        ("HS 8517.62.00 90", "851762"),
        ("6109.10.0012", "610910"),
        ("8517 62", "851762"),
    ],
)
def test_hs_code_first_six(raw, digits6):
    res = norm_hs(raw)
    assert res.digits6 == digits6
    assert res.compare_key == f"hs:{digits6}"


def test_hs_code_different_national_tail_is_same_compare_key():
    a = norm_hs("8517.62.00")
    b = norm_hs("8517.62.0090")
    assert a.compare_key == b.compare_key


def test_hs_code_too_short_is_not_ok():
    res = norm_hs("8517")
    assert res.ok is False


# ---------------------------------------------------------------------------
# Consignee / party name
# ---------------------------------------------------------------------------


def test_party_strips_legal_suffixes_repeatedly():
    a = norm_party("NIPPON ELECTRONICS CORPORATION LTD")
    b = norm_party("Nippon Electronics")
    assert a.compare_key == b.compare_key


def test_party_folds_accents_and_reordering_is_stable():
    a = norm_party("Müller Söhne GmbH")
    assert a.normalized == "MULLER SOHNE"
    # Pure reordering must not change the compare key (sorted token set).
    b = norm_party("Söhne Müller")
    assert a.compare_key == b.compare_key


def test_party_keeps_ampersand_and_hyphen():
    a = norm_party("Johnson & Johnson Inc")
    assert a.normalized == "JOHNSON & JOHNSON"
    h = norm_party("Smith-Jones Ltd")
    assert h.normalized == "SMITH-JONES"


def test_party_strips_trailing_ampersand():
    a = norm_party("Baker & Co")
    assert a.normalized == "BAKER"


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,amount,currency",
    [
        ("USD 8,420.00", 8420.00, "USD"),
        ("8,420.00 USD", 8420.00, "USD"),
        ("$8,420.00", 8420.00, "USD"),
        ("EUR 12.450,00", 12450.00, "EUR"),
        ("12.500,00 €", 12500.00, "EUR"),
        ("GBP 1,000.00", 1000.00, "GBP"),
        ("USD 8420", 8420.00, "USD"),
        ("TOTAL: USD 8,420.00", 8420.00, "USD"),
    ],
)
def test_money_parse(raw, amount, currency):
    res = norm_money(raw)
    assert res.amount == pytest.approx(amount), f"{raw} -> {res.amount}"
    assert res.currency == currency


def test_money_strips_incoterms_and_prose():
    res = norm_money("FOB SHANGHAI USD 8,420.00 SAY US DOLLARS EIGHT THOUSAND FOUR HUNDRED TWENTY ONLY")
    assert res.amount == pytest.approx(8420.00)
    assert res.currency == "USD"
    assert "strip_prose" in res.rules_applied


def test_money_compare_key_keeps_currency():
    """Different currency must never collapse to the same compare_key."""
    usd = norm_money("USD 100.00")
    eur = norm_money("EUR 100.00")
    assert usd.compare_key != eur.compare_key
    assert usd.compare_key == "money:USD:100.00"


def test_money_missing_currency_warns():
    res = norm_money("8,420.00")
    assert res.amount == pytest.approx(8420.0)
    assert res.currency is None
    assert res.warnings
